"""Serve operator-owned encrypted-volume profiles through a private Unix socket."""

import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import jwt
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field


class Admission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    workspace_id: int
    mode: Literal["subscription", "api", "on_prem"]
    harness: Literal["codex-native", "claude-native"]
    session_id: str
    reference: str | None = Field(default=None, max_length=512)
    lease_id: str | None = None
    max_cents: int | None = Field(default=None, ge=1, le=300)
    pull_request: str | None = None


def protected_file(path: Path) -> str:
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_mode & 0o077
    ):
        raise ValueError("Private credential file required")
    return path.read_text()


def https(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError("A trusted HTTPS origin is required")
    return value.rstrip("/")


def write_private(path: Path, content: str) -> None:
    with open(path, "x", encoding="utf-8", opener=lambda f, m: os.open(f, m, 0o600)) as target:
        target.write(content)


def cleanup_credentials(root: Path) -> None:
    if not root.is_dir() or root.is_symlink() or root.stat().st_mode & 0o077:
        return
    for marker in root.glob(".studio-expiry-*"):
        tag = marker.name.removeprefix(".studio-expiry-")
        if not re.fullmatch("[0-9a-f]{32}", tag):
            continue
        try:
            if int(protected_file(marker)) > time.time():
                continue
            directory = root / ("api-" + tag)
            if directory.is_symlink():
                continue
            for filename in ("auth.json", "config.toml", "settings.json"):
                (directory / filename).unlink(missing_ok=True)
            # Preserve any session state written by the native harness.
            with suppress(OSError):
                directory.rmdir()
            marker.unlink()
        except (OSError, ValueError):
            continue


def create_credential_agent(
    manifest: Path,
    root: Path,
    client_factory: Callable[[], httpx.Client] = lambda: httpx.Client(
        timeout=1.25, trust_env=False
    ),
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async def sweep():
            while True:
                await asyncio.to_thread(cleanup_credentials, root)
                await asyncio.sleep(30)

        task = asyncio.create_task(sweep())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.post("/v1/admit")
    def admit(value: Admission) -> dict[str, Any]:
        try:
            org = json.loads(protected_file(manifest))["workspaces"][str(value.workspace_id)]
            if (
                not root.is_absolute()
                or not root.is_dir()
                or root.is_symlink()
                or root.stat().st_mode & 0o077
            ):
                raise ValueError("Private seat volume required")
            if value.mode == "subscription":
                digest = hashlib.sha256((value.reference or "").encode()).hexdigest()
                spec = org["subscriptions"][digest]
                if spec["harness"] != value.harness:
                    raise ValueError("Invalid harness")
                with client_factory() as client:
                    r = client.get(
                        https(spec["quota_url"]),
                        headers={
                            "Authorization": "Bearer "
                            + protected_file(Path(spec["quota_token_file"])).strip()
                        },
                    )
                    if r.status_code == 429:
                        raise HTTPException(
                            429,
                            "Seat cooling down",
                            headers={"Retry-After": r.headers.get("Retry-After", "60")},
                        )
                    r.raise_for_status()
                    quota = r.json()
                    if quota.get("available") is False:
                        raise HTTPException(429, "Seat cooling down")
                    if quota.get("available") is not True:
                        raise ValueError("Quota evidence unavailable")
                return {"profile": spec["profile"]}
            if value.mode == "on_prem":
                spec = org["on_prem"][value.harness]
                if spec["provider"] not in ("vllm", "sparkle"):
                    raise ValueError("Unknown on-prem inference profile")
                return {"profile": spec["profile"], "provider": spec["provider"]}
            if not value.lease_id or not value.pull_request or value.max_cents is None:
                raise ValueError("An allocated PR budget is required")
            api = org["api"][value.harness]
            # An issuer with a prepaid, non-overdraft balance is required. A
            # post-charge usage limit (including a gateway max_budget) is insufficient.
            gateway = https(api["origin"])
            issuer = https(api["grant_url"])
            public_key = protected_file(Path(api["attestation_public_key_file"]))
            tag = UUID(value.lease_id).hex
            handle = "api-" + tag
            directory = root / handle
            now = datetime.now(UTC)
            month_end = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=UTC)
            deadline = min(int(now.timestamp()) + 1200, int(month_end.timestamp()))
            model = api["model"]
            bound = {
                "workspace_id": value.workspace_id,
                "lease_id": value.lease_id,
                "pull_request": value.pull_request,
                "session_id": value.session_id,
                "harness": value.harness,
                "model": model,
                "origin": gateway,
                "hard_limit_cents": value.max_cents,
                "no_overdraft": True,
                "billing": "prepaid",
            }
            with client_factory() as client:
                response = client.post(
                    issuer,
                    headers={
                        "Authorization": "Bearer "
                        + protected_file(Path(api["issuer_key_file"])).strip()
                    },
                    json={**bound, "expires_by": deadline},
                )
                response.raise_for_status()
                issued = response.json()
            token = issued["credential"]
            if not isinstance(token, str) or not token:
                raise ValueError("No isolated credential issued")
            expected = {**bound, "credential_sha256": hashlib.sha256(token.encode()).hexdigest()}
            claims = jwt.decode(
                issued["attestation"],
                public_key,
                algorithms=["EdDSA"],
                audience="omnigent-studio-seat",
                issuer=issuer,
                options={"require": ["exp", "iat", "nbf"]},
            )
            if claims["exp"] > deadline or any(
                type(claims.get(k)) is not type(v) or claims[k] != v for k, v in expected.items()
            ):
                raise ValueError("Credential has no verified prepaid ceiling")
            write_private(root / (".studio-expiry-" + tag), str(int(claims["exp"])))
            directory.mkdir(mode=0o700, exist_ok=False)
            if value.harness == "claude-native":
                write_private(
                    directory / "settings.json",
                    json.dumps(
                        {
                            "model": model,
                            "env": {"ANTHROPIC_BASE_URL": gateway, "ANTHROPIC_AUTH_TOKEN": token},
                        }
                    ),
                )
            else:
                write_private(directory / "auth.json", json.dumps({"OPENAI_API_KEY": token}))
                write_private(
                    directory / "config.toml",
                    "model = "
                    + json.dumps(model)
                    + "\nopenai_base_url = "
                    + json.dumps(gateway + "/v1")
                    + "\n",
                )
            return {
                "profile": handle,
                "lease_id": value.lease_id,
                "hard_limit_cents": value.max_cents,
                "isolated_credential": True,
            }
        except (KeyError, TypeError, ValueError, OSError, httpx.HTTPError, jwt.PyJWTError):
            raise HTTPException(503, "Studio credential admission unavailable") from None

    return app


def main() -> None:
    import uvicorn

    socket = Path(os.environ["OMNIGENT_STUDIO_SEAT_SOCKET"])
    if not socket.is_absolute() or socket.parent.stat().st_mode & 0o077:
        raise ValueError("Use a private Unix socket directory")
    os.umask(0o077)
    app = create_credential_agent(
        Path(os.environ["OMNIGENT_STUDIO_SEAT_MANIFEST"]),
        Path(os.environ["OMNIGENT_STUDIO_SEAT_ROOT"]),
    )
    uvicorn.run(app, uds=str(socket), access_log=False)


if __name__ == "__main__":
    main()
