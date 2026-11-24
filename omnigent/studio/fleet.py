"""Fleet operations bound to an authenticated account and provider-owned workspace."""

import asyncio
import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException, Request

from omnigent.db.db_models import current_workspace_id
from omnigent.db.utils import builtin_agent_id
from omnigent.native.native_coding_agents import native_coding_agent_for_harness
from omnigent.onboarding.sandboxes.types import SandboxError
from omnigent.server.managed_hosts import build_owner_launcher
from omnigent.server.provider_sandbox_profiles import select_provider_profile
from omnigent.stores.host_store import host_is_live
from omnigent.stores.studio_tasks import StudioTaskStore
from omnigent.studio import rpc
from omnigent.studio.pooled_hosts import PROVIDER
from omnigent.studio.preview_receipts import read_previews

_lease_creation: ContextVar[tuple[int, str, str] | None] = ContextVar(
    "studio_lease_creation", default=None
)


def agent_session_id(owner: str | None) -> str | None:
    binding = _lease_creation.get()
    if binding and binding[:2] == (current_workspace_id(), owner):
        _lease_creation.set(None)
        return binding[2]
    return None


def unavailable(reason: str) -> HTTPException:
    return HTTPException(503, {"status": "env_blocked", "reason": reason})


def provider(request: Request, actor: str, profile: str, sandbox_id: str | None = None):
    deployment = getattr(request.app.state, "sandbox_config", None)
    catalog = getattr(request.app.state, "sandbox_profiles", None)
    config = deployment.for_provider(PROVIDER) if deployment else None
    if not config or not catalog:
        raise unavailable("Coder provider is unavailable")
    snapshot = catalog.resolve(profile, provider=PROVIDER, user_id=actor)
    launcher = build_owner_launcher(config, deployment, actor)
    select_provider_profile(launcher, snapshot, sandbox_id=sandbox_id)
    return launcher, snapshot


def control(request: Request, actor: str, method: str, params: rpc.WorkspaceRef) -> dict[str, Any]:
    store = request.app.state.host_store
    host = store.get_host(params.host_id.hex) if store else None
    if (
        not host
        or host.user_id != actor
        or host.sandbox_provider != PROVIDER
        or not host.sandbox_id
    ):
        raise HTTPException(404, "Managed workspace not found")
    catalog = request.app.state.sandbox_profiles
    snapshot = catalog.get_host_snapshot(host.host_id) if catalog else None
    if not snapshot or (snapshot["id"] == "bot-review" and method == "stopWorkspace"):
        raise HTTPException(409, "This workspace cannot be stopped or resumed by this action")
    launcher, _ = provider(request, actor, snapshot["id"], host.sandbox_id)
    if method == "stopWorkspace":
        launcher.stop(host.sandbox_id)
    else:
        launcher.resume(host.sandbox_id)
    return {"host_id": host.host_id, "running": launcher.is_running(host.sandbox_id)}


def allocate(
    request: Request, tasks: StudioTaskStore, actor: str, params: rpc.Preview
) -> dict[str, Any]:
    tasks.require_task_pr(actor, params)
    if (
        os.getenv("OMNIGENT_STUDIO_PREVIEW_REPOSITORY", "").casefold()
        != params.repository.casefold()
    ):
        raise unavailable("Repository has no configured preview template")
    launcher, snapshot = provider(request, actor, "pr-preview")
    allocate_preview = getattr(launcher, "allocate_preview", None)
    if not callable(allocate_preview):
        raise unavailable("The provider cannot allocate pinned previews")
    sandbox_id = allocate_preview(params.number, params.head_sha)
    return {
        "workspace_id": sandbox_id,
        "template_version": snapshot["revision"],
        "requested_sha": params.head_sha,
    }


def fleet_capacity(request: Request, principal: str | None) -> dict[str, int] | None:
    if not principal:
        return None
    try:
        launcher, _ = provider(request, principal, "bot-review")
        read = getattr(launcher, "fleet_capacity", None)
        if not callable(read):
            return None
        result = read()
    except (
        HTTPException,
        SandboxError,
        OSError,
        sqlite3.Error,
        RuntimeError,
        ValueError,
        httpx.HTTPError,
    ):
        return None
    expected = {"bot_review_held", "other_held", "bot_review_limit", "other_limit"}
    if (
        not isinstance(result, dict)
        or result.keys() != expected
        or any(type(v) is not int for v in result.values())
    ):
        return None
    if (
        result["bot_review_limit"] != 1
        or result["other_limit"] != 3
        or not 0 <= result["bot_review_held"] <= 1
        or not 0 <= result["other_held"] <= 3
    ):
        return None
    return result


async def inventory(
    request: Request, tasks: StudioTaskStore, actor: str, page: rpc.FleetPage, can_admin: bool
) -> dict[str, Any]:
    result = await asyncio.to_thread(tasks.inventory, actor, page, can_admin)
    principal = os.getenv("OMNIGENT_STUDIO_DISPATCH_ACCOUNT")
    result["capacity"] = await asyncio.to_thread(fleet_capacity, request, principal)
    hosts = getattr(request.app.state, "host_store", None)
    result["hosts"] = [
        {
            "host_id": h.host_id,
            "name": h.name,
            "status": "online" if host_is_live(h) else "offline",
            "configured_harnesses": h.configured_harnesses or {},
            "managed": h.sandbox_provider == PROVIDER,
        }
        for h in (await asyncio.to_thread(hosts.list_hosts, actor) if hosts else [])
    ]
    result["previews"] = []
    if page.task_id and principal:
        prs = await asyncio.to_thread(tasks.task_prs, page.task_id)
        if prs:
            try:

                def previews():
                    launcher, _ = provider(request, principal, "pr-preview")
                    return read_previews(launcher, prs)

                result["previews"] = await asyncio.wait_for(asyncio.to_thread(previews), timeout=6)
            except (HTTPException, TimeoutError):
                pass
    return result


@asynccontextmanager
async def bot_client(request: Request) -> AsyncIterator[tuple[str, httpx.AsyncClient]]:
    # The pooled runtime has its own non-interactive operator identity. Its
    # session stays private to that owner; the requester sees only the lease.
    bot = os.environ.get("OMNIGENT_STUDIO_DISPATCH_ACCOUNT")
    key_file = os.environ.get("OMNIGENT_STUDIO_DISPATCH_TOKEN_FILE")
    if not bot or not key_file:
        raise unavailable("Studio dispatch identity is unavailable")
    path = Path(key_file)
    if not path.is_absolute() or not path.is_file() or path.stat().st_mode & 0o077:
        raise unavailable("Studio dispatch identity is unavailable")
    token = path.read_text().strip()
    if not token:
        raise unavailable("Studio dispatch identity is unavailable")
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=str(request.base_url),
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        me = await client.get("/v1/me")
        identity = me.json() if me.status_code == 200 else {}
        if identity.get("user_id") != bot or identity.get("is_admin") is not True:
            raise unavailable("Studio dispatch identity could not be verified")
        yield bot, client


async def reap(request: Request, tasks: StudioTaskStore, only: str | None = None) -> None:
    expired = await asyncio.to_thread(tasks.expired_sessions, "", only)
    if not expired:
        return
    async with bot_client(request) as (_, client):
        while expired:
            for key, session_id in expired:
                path = "/v1/sessions/" + session_id
                try:
                    current = await client.get(path)
                    if not current.is_success:
                        # Ambiguous creation or an unavailable runner is retried next sweep.
                        continue
                    stop = await client.post(path + "/events", json={"type": "stop_session"})
                    if not stop.is_success:
                        continue
                    archive = await client.patch(path, json={"archived": True})
                    if archive.is_success:
                        await asyncio.to_thread(tasks.mark_stopped, key, session_id)
                except httpx.TransportError:
                    continue
            if only or len(expired) < 100:
                break
            expired = await asyncio.to_thread(tasks.expired_sessions, expired[-1][0])


async def dispatch(
    request: Request, tasks: StudioTaskStore, actor: str, value: rpc.LeaseClaim
) -> dict[str, Any]:
    native = native_coding_agent_for_harness(value.harness)
    if native is None:
        raise HTTPException(422, "Unsupported native harness")
    async with bot_client(request) as (bot, client):
        _, snapshot = await asyncio.to_thread(provider, request, bot, "free-agent")
        result = await asyncio.to_thread(tasks.begin_agent, actor, value)
        if result.pop("launch"):
            acceptance = result.pop("acceptance_check")
            session_id = result.pop("session_id")
            marker = _lease_creation.set((current_workspace_id(), bot, session_id))
            try:
                response = await client.post(
                    "/v1/sessions",
                    json={
                        "agent_id": builtin_agent_id(native.agent_name),
                        "host_type": "managed",
                        "sandbox_provider": PROVIDER,
                        "sandbox_profile_id": "free-agent",
                        "sandbox_profile_revision": snapshot["revision"],
                        "initial_input": {
                            "type": "message",
                            "data": {
                                "role": "user",
                                "content": [{"type": "input_text", "text": acceptance}],
                            },
                        },
                    },
                )
                if (
                    response.status_code == 429
                    and (await client.get("/v1/sessions/" + session_id)).status_code == 404
                ):
                    await asyncio.to_thread(
                        tasks.complete_agent, actor, value.idempotency_key, False, queued=True
                    )
                    raise HTTPException(429, {"status": "queued_for_capacity"})
                response.raise_for_status()
                if response.json().get("id") != session_id:
                    raise ValueError("Unconfirmed free-agent session identity")
            except (httpx.HTTPError, ValueError):
                await asyncio.to_thread(tasks.complete_agent, actor, value.idempotency_key, False)
                raise unavailable("Free-agent launch outcome is unknown") from None
            finally:
                _lease_creation.reset(marker)
            await asyncio.to_thread(tasks.complete_agent, actor, value.idempotency_key, True)
        return result
