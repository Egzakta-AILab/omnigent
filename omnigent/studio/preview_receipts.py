"""Read immutable checkout evidence from the trusted preview workflow."""

import hashlib
import io
import json
import os
import re
import zipfile
from threading import BoundedSemaphore
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

import httpx

from omnigent.onboarding.sandboxes.base import SandboxGoneError

_LOOKUPS = BoundedSemaphore(4)


def verified_preview(
    client: httpx.Client,
    repository: str,
    number: int,
    head: str,
    identity: dict[str, str],
    allowed_host: str,
    deadline: float,
) -> dict[str, Any] | None:
    if not re.fullmatch(r"[\w-][\w.-]*/[\w-][\w.-]*", repository) or not re.fullmatch(
        "[0-9a-f]{40}", head
    ):
        return None
    root = f"/repos/{repository}"

    def get(path: str, **params: Any) -> Any:
        if monotonic() >= deadline:
            raise TimeoutError("Preview lookup timed out")
        r = client.get(root + path, params=params)
        r.raise_for_status()
        return r.json()

    branch = get("")["default_branch"]
    name = f"studio-preview-{number}-{head}"
    artifacts = get("/actions/artifacts", name=name, per_page=20)["artifacts"]
    for artifact in sorted(artifacts, key=lambda item: int(item["id"]), reverse=True):
        if artifact["name"] != name or artifact["expired"]:
            continue
        run = get(f"/actions/runs/{int(artifact['workflow_run']['id'])}")
        if not (
            run["event"] == "workflow_dispatch"
            and run["conclusion"] == "success"
            and run["head_branch"] == branch
            and run["path"] == ".github/workflows/coder-pr-workspace.yml"
            and run["head_repository"]["full_name"].casefold() == repository.casefold()
        ):
            continue
        response = client.get(root + f"/actions/artifacts/{int(artifact['id'])}/zip")
        if response.status_code != 302:
            return None
        url = response.headers["Location"]
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.username
            or parts.password
            or not parts.hostname
            or parts.port not in (None, 443)
            or not parts.hostname.endswith((".blob.core.windows.net", ".githubusercontent.com"))
        ):
            return None
        # Never forward the GitHub bearer on the signed download redirect.
        with (
            httpx.Client(trust_env=False, timeout=5) as download,
            download.stream("GET", url) as stream,
        ):
            stream.raise_for_status()
            data = bytearray()
            for part in stream.iter_bytes(8192):
                data.extend(part)
                if len(data) > 65536:
                    return None
        if artifact.get("digest") != "sha256:" + hashlib.sha256(data).hexdigest():
            return None
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = archive.infolist()
            if len(files) != 1 or files[0].filename != "receipt.json" or files[0].file_size > 8192:
                return None
            receipt = json.loads(archive.read(files[0]))
        if not isinstance(receipt, dict):
            return None
        if any(
            receipt.get(k) != v
            for k, v in {
                **identity,
                "repository": repository,
                "pr_number": number,
                "requested_sha": head,
                "checked_out_sha": head,
            }.items()
        ):
            continue
        target = urlsplit(receipt.get("url", ""))
        if (
            target.scheme != "https"
            or target.username
            or target.password
            or target.hostname != allowed_host
            or target.path not in ("", "/")
            or target.query
            or target.fragment
            or not target.port
            or not 32100 <= target.port <= 32199
        ):
            return None
        return {
            k: receipt[k]
            for k in (
                "repository",
                "pr_number",
                "requested_sha",
                "checked_out_sha",
                "url",
                "workspace_id",
                "build_id",
                "template_version",
            )
        }
    return None


def read_previews(launcher: Any, pull_requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    token, host = (
        os.getenv("OMNIGENT_STUDIO_GITHUB_READ_TOKEN"),
        os.getenv("OMNIGENT_STUDIO_PREVIEW_HOST"),
    )
    repo = os.getenv("OMNIGENT_STUDIO_PREVIEW_REPOSITORY")
    if not token or not host or not repo or not hasattr(launcher, "preview_identity"):
        return []
    if not _LOOKUPS.acquire(blocking=False):
        return []
    try:
        return _read(launcher, pull_requests, token, host, repo)
    finally:
        _LOOKUPS.release()


def _read(
    launcher: Any, pull_requests: list[dict[str, Any]], token: str, host: str, repo: str
) -> list[dict[str, Any]]:
    previews = []
    deadline = monotonic() + 5
    seen = set()
    with httpx.Client(
        base_url="https://api.github.com",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
        trust_env=False,
    ) as client:
        for pr in pull_requests:
            if monotonic() >= deadline:
                break
            key = (pr["repository"], pr["number"], pr["head_sha"])
            if key in seen or key[0].casefold() != repo.casefold():
                continue
            seen.add(key)
            try:
                first = launcher.preview_identity(key[1], key[2])
                if not first:
                    continue
                receipt = verified_preview(client, *key, first, host, deadline)
                if receipt and launcher.preview_identity(key[1], key[2]) == first:
                    previews.append(receipt)
            except (
                httpx.HTTPError,
                TimeoutError,
                KeyError,
                ValueError,
                RuntimeError,
                SandboxGoneError,
                TypeError,
                zipfile.BadZipFile,
            ):
                continue
    return previews
