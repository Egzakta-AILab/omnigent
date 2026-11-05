"""Workspace settings route and host-frame integration with local fixtures."""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
import pytest_asyncio

from omnigent.host.connect import HostProcess
from omnigent.host.frames import (
    HostFsRequestFrame,
    HostFsWriteFrame,
    HostHelloFrame,
    HostStatFrame,
    decode_host_frame,
)
from omnigent.runner import github_resource
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, UnifiedAuthProvider
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

ALICE = "alice@example.test"
BOB = "bob@example.test"


@dataclass
class WorkspaceHarness:
    client: httpx.AsyncClient
    project_id: str
    source_id: str
    workspace: Path
    registry: HostRegistry
    connection: HostConnection
    frames: list[HostFsRequestFrame | HostFsWriteFrame | HostStatFrame]

    @property
    def project_path(self) -> str:
        return f"/v1/projects/{self.project_id}/studio"

    @property
    def session_path(self) -> str:
        return f"/v1/sessions/{self.source_id}/studio"

    @property
    def selection(self) -> dict[str, str]:
        return {"host_id": self.connection.host_id, "workspace": str(self.workspace)}


@pytest_asyncio.fixture
async def workspace_harness(
    runtime_init: None, db_uri: str, tmp_path: Path, monkeypatch
) -> AsyncIterator[WorkspaceHarness]:
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "0")
    root = tmp_path / "workspace"
    root.mkdir()
    host_id = uuid.uuid4().hex
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(host_id, "Workspace host", ALICE)
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    projects = SqlAlchemyProjectStore(db_uri)
    agents = SqlAlchemyAgentStore(db_uri)
    agent = agents.create(uuid.uuid4().hex, "Source agent", "source-bundle")
    source = conversations.create_conversation(
        agent_id=agent.id, host_id=host_id, workspace=str(root)
    )
    permissions.grant(ALICE, source.id, LEVEL_OWNER)
    permissions.grant(BOB, source.id, LEVEL_EDIT)
    project = projects.create(
        uuid.uuid4().hex, "Workspace project", ALICE, {"host_id": host_id, "workspace": str(root)}
    )
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=agents,
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conversations,
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        project_store=projects,
        auth_provider=UnifiedAuthProvider(source="header"),
        permission_store=permissions,
        host_store=hosts,
    )
    registry = app.state.host_registry
    connection = registry.register(
        host_id,
        Mock(),
        HostHelloFrame(version="0.1.0", frame_protocol_version=1, name="Workspace host"),
        owner=ALICE,
    )
    # The server emits its real wire frames. Deliver them in-process to the
    # host handlers and complete the same futures as the WebSocket receiver.
    host = object.__new__(HostProcess)
    frames: list[HostFsRequestFrame | HostFsWriteFrame | HostStatFrame] = []

    def dispatch(conn: HostConnection, data: str) -> None:
        assert conn is connection
        frame = decode_host_frame(data)
        if isinstance(frame, HostStatFrame):
            result = host._handle_stat(frame)
            frames.append(frame)
            conn.pending_stats.pop(result.request_id).set_result(asdict(result))
            return
        if isinstance(frame, HostFsRequestFrame):
            result = host._handle_fs_request(frame)
        else:
            assert isinstance(frame, HostFsWriteFrame)
            result = host._handle_fs_write(frame)
        frames.append(frame)
        conn.pending_fs_requests.pop(result.request_id).set_result(asdict(result))

    monkeypatch.setattr(registry, "send_text", dispatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Forwarded-Email": ALICE},
    ) as client:
        yield WorkspaceHarness(client, project.id, source.id, root, registry, connection, frames)


async def test_profile_editor_shared_across_project_and_session(
    workspace_harness: WorkspaceHarness,
):
    harness = workspace_harness
    project = f"{harness.project_path}/review-profiles/editor"
    session = f"{harness.session_path}/review-profiles/editor"
    partial = await harness.client.get(project, params={"workspace": "/forged"})
    assert partial.status_code == 400
    initial = await harness.client.get(project)
    assert initial.status_code == 200, initial.text
    assert initial.json()["profiles"] == []
    assert initial.headers["Cache-Control"] == "no-store"
    body = {
        "revision": initial.json()["revision"],
        "profile": {
            "id": "backend",
            "label": "Backend QA",
            "instructions": "Check service boundaries.",
            "archived": False,
            "defaults": {"harness": "codex-native", "model": "review-model"},
        },
    }
    saved = await harness.client.put(session, json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["profiles"][0]["instructions"] == body["profile"]["instructions"]
    assert saved.json()["profiles"][0]["managed"] is True
    assert (await harness.client.get(project)).json() == saved.json()

    writes = [frame for frame in harness.frames if isinstance(frame, HostFsWriteFrame)]
    assert len(writes) == 1
    assert writes[0].op == "studio_review_profile_editor"
    assert writes[0].workspace == str(harness.workspace)
    assert writes[0].session_id == harness.source_id
    assert writes[0].params["revision"] == "missing"

    stale = await harness.client.put(
        project, json={**body, "profile": {**body["profile"], "label": "Stale"}}
    )
    assert stale.status_code == 409, stale.text
    assert (await harness.client.get(session)).json() == saved.json()


async def test_workspace_management_rejects_relative_path(workspace_harness: WorkspaceHarness):
    harness = workspace_harness
    moved = await harness.client.patch(
        f"/v1/projects/{harness.project_id}",
        json={"config": {"host_id": harness.connection.host_id, "workspace": "."}},
    )
    assert moved.status_code == 200, moved.text
    response = await harness.client.get(f"{harness.project_path}/repositories?depth=0")
    assert response.status_code == 400, response.text
    assert "absolute workspace path" in response.text


async def test_workspace_edit_rejects_a_moved_project(workspace_harness: WorkspaceHarness):
    harness = workspace_harness
    path = f"{harness.project_path}/repositories"
    initial = await harness.client.get(path)
    assert initial.status_code == 200, initial.text
    other = harness.workspace.parent / "other"
    other.mkdir()
    moved = await harness.client.patch(
        f"/v1/projects/{harness.project_id}",
        json={"config": {"host_id": harness.connection.host_id, "workspace": str(other)}},
    )
    assert moved.status_code == 200, moved.text
    stale = await harness.client.put(
        path, json={"revision": initial.json()["revision"], "depth": 2, "repositories": []}
    )
    assert stale.status_code == 409, stale.text
    assert len(harness.frames) == 1
    assert not list(other.iterdir())


async def test_workspace_management_requires_owner_and_connected_host(
    workspace_harness: WorkspaceHarness,
):
    harness = workspace_harness
    for base in (harness.project_path, harness.session_path):
        path = f"{base}/review-profiles/editor"
        denied = await harness.client.get(path, headers={"X-Forwarded-Email": BOB})
        assert denied.status_code == 404, denied.text
    foreign = await harness.client.post(
        "/v1/projects",
        headers={"X-Forwarded-Email": BOB},
        json={
            "name": "Foreign host",
            "config": {"host_id": harness.connection.host_id, "workspace": str(harness.workspace)},
        },
    )
    assert foreign.status_code == 200, foreign.text
    denied_host = await harness.client.get(
        f"/v1/projects/{foreign.json()['id']}/studio/repositories",
        headers={"X-Forwarded-Email": BOB},
    )
    assert denied_host.status_code == 403, denied_host.text
    harness.registry.deregister(harness.connection.host_id)
    offline = await harness.client.get(f"{harness.session_path}/repositories")
    assert offline.status_code == 503, offline.text
    assert harness.frames == []


async def test_repository_scan_and_promotion_use_workspace_host(
    workspace_harness: WorkspaceHarness,
):
    harness = workspace_harness
    checkout = harness.workspace / "nested" / "service"
    checkout.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet"], cwd=checkout, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/team/service.git"],
        cwd=checkout,
        check=True,
    )
    path = f"{harness.project_path}/repositories"
    shallow = await harness.client.get(path, params={"depth": 1})
    assert shallow.status_code == 200, shallow.text
    assert shallow.json()["repositories"] == []
    scanned = await harness.client.get(path, params={"depth": 2})
    assert scanned.status_code == 200, scanned.text
    [repo] = scanned.json()["repositories"]
    assert repo["path"] == "nested/service"
    assert repo["role"] == "reference"
    assert repo["origin"] == "github.com/team/service"
    saved = await harness.client.put(
        path,
        json={
            "revision": scanned.json()["revision"],
            "depth": 2,
            "repositories": [{"path": repo["path"], "role": "active", "origin": repo["origin"]}],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["repositories"][0]["role"] == "active"
    assert (
        await harness.client.get(f"{harness.session_path}/repositories")
    ).json() == saved.json()


async def test_project_can_preview_files_and_profiles_without_saved_config(
    workspace_harness: WorkspaceHarness,
) -> None:
    harness = workspace_harness
    project = await harness.client.post("/v1/projects", json={"name": "New project"})
    assert project.status_code == 200, project.text
    project_path = f"/v1/projects/{project.json()['id']}/studio"
    profiles = await harness.client.get(
        f"{project_path}/review-profiles/editor", params=harness.selection
    )
    assert profiles.status_code == 200, profiles.text
    saved = await harness.client.put(
        f"{project_path}/review-profiles/editor",
        params=harness.selection,
        json={
            "revision": profiles.json()["revision"],
            "profile": {
                "id": "backend",
                "label": "Backend",
                "instructions": "Check service boundaries.",
                "archived": False,
                "defaults": {},
            },
        },
    )
    assert saved.status_code == 200, saved.text
    catalog = await harness.client.get(f"{project_path}/review-profiles", params=harness.selection)
    assert catalog.status_code == 200, catalog.text
    assert [entry["id"] for entry in catalog.json()["profiles"]] == ["backend"]

    source = harness.workspace / "app.py"
    source.write_text("x" * (64 * 1024 - 1) + "€", encoding="utf-8")
    params = {**harness.selection, "path": source.name}
    preview = await harness.client.get(f"{project_path}/files", params=params)
    assert preview.status_code == 200, preview.text
    assert preview.json() == {
        "path": source.name,
        "content": "x" * (64 * 1024 - 1),
        "truncated": True,
    }
    assert preview.headers["Cache-Control"] == "no-store"

    source.write_bytes(b"\x00\xff")
    binary = await harness.client.get(f"{project_path}/files", params=params)
    assert binary.json() == {"path": source.name, "content": None, "truncated": False}
    outside = harness.workspace.parent / "outside"
    outside.mkdir()
    (outside / "secret").write_text("not for preview")
    (harness.workspace / "link").symlink_to(outside, target_is_directory=True)
    escaped = await harness.client.get(
        f"{project_path}/files", params={**params, "path": "link/secret"}
    )
    assert escaped.status_code == 404, escaped.text
    traversed = await harness.client.get(
        f"{project_path}/files", params={**params, "path": "../outside/secret"}
    )
    assert traversed.status_code == 400, traversed.text

    before = len(harness.frames)
    forbidden = await harness.client.get(
        f"{project_path}/files", params=params, headers={"X-Forwarded-Email": BOB}
    )
    assert forbidden.status_code == 404, forbidden.text
    foreign = await harness.client.post(
        "/v1/projects", json={"name": "Bob"}, headers={"X-Forwarded-Email": BOB}
    )
    forbidden_host = await harness.client.get(
        f"/v1/projects/{foreign.json()['id']}/studio/files",
        params=params,
        headers={"X-Forwarded-Email": BOB},
    )
    assert forbidden_host.status_code == 403, forbidden_host.text
    assert len(harness.frames) == before


async def test_project_pull_requests_bind_to_the_selected_nested_checkout(
    workspace_harness: WorkspaceHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = workspace_harness
    checkout = harness.workspace / "nested" / "service"
    checkout.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet"], cwd=checkout, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/team/service.git"],
        cwd=checkout,
        check=True,
    )
    (harness.workspace / "repos.yaml").write_text("version: 1\nrepos: {}\n", encoding="utf-8")
    base, head = "a" * 40, "b" * 40
    current_head = head
    url = "https://github.com/team/service/pull/12"
    calls: list[list[str]] = []

    def fake_gh(argv: list[str], *, cwd: str, token: str | None = None) -> tuple[int, str, str]:
        assert Path(cwd) == checkout.resolve()
        calls.append(argv)
        if argv[:2] == ["pr", "list"]:
            assert "--repo=github.com/team/service" in argv
            return 0, json.dumps([{"number": 12, "title": "Add service", "url": url}]), ""
        if argv[:2] == ["pr", "diff"]:
            assert argv == ["pr", "diff", "12", "-R", "github.com/team/service"]
            return 0, "diff --git a/service.py b/service.py\n", ""
        assert argv[:3] == ["api", "--hostname", "github.com"]
        if argv[3].endswith("/files?per_page=40"):
            return 0, json.dumps([{"filename": "service.py", "status": "added"}]), ""
        assert argv[3] == "repos/team/service/pulls/12"
        return (
            0,
            json.dumps(
                {
                    "number": 12,
                    "title": "Add service",
                    "state": "open",
                    "base": {"ref": "main", "sha": base},
                    "head": {"ref": "feature", "sha": current_head},
                    "changed_files": 2,
                }
            ),
            "",
        )

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    monkeypatch.setattr(github_resource, "_pr_token", lambda root, reference: None)
    path = f"{harness.project_path}/pull-requests"
    params = {**harness.selection, "repository": "nested/service"}
    listed = await harness.client.get(path, params=params)
    assert listed.status_code == 200, listed.text
    assert [row["url"] for row in listed.json()["pull_requests"]] == [url]
    preview = await harness.client.get(f"{path}/preview", params={**params, "pr_url": url})
    assert preview.status_code == 200, preview.text
    assert preview.json()["canonical_workspace"] == str(harness.workspace.resolve())
    assert preview.json()["pr"]["head_sha"] == head
    assert preview.json()["files"][0]["path"] == "service.py"
    assert preview.json()["files_truncated"] is True
    assert preview.json()["review_available"] is False

    scanned = await harness.client.get(
        f"{harness.project_path}/repositories", params={**harness.selection, "depth": 2}
    )
    assert scanned.status_code == 200, scanned.text
    promoted = await harness.client.put(
        f"{harness.project_path}/repositories",
        params=harness.selection,
        json={
            "revision": scanned.json()["revision"],
            "depth": 2,
            "repositories": [
                {"path": "nested/service", "role": "active", "origin": "github.com/team/service"}
            ],
        },
    )
    assert promoted.status_code == 200, promoted.text
    preview = await harness.client.get(f"{path}/preview", params={**params, "pr_url": url})
    assert preview.json()["review_available"] is True

    patch_params = {**params, "pr_url": url, "head_sha": head, "base_sha": base}
    patch = await harness.client.get(f"{path}/patch", params=patch_params)
    assert patch.status_code == 200, patch.text
    assert patch.json() == {"patch": "diff --git a/service.py b/service.py\n", "truncated": False}
    current_head = "c" * 40
    stale = await harness.client.get(f"{path}/patch", params=patch_params)
    assert stale.status_code == 400, stale.text
    assert "pull request changed" in stale.text

    before = len(calls)
    unrelated = await harness.client.get(
        f"{path}/preview", params={**params, "pr_url": "https://github.com/team/other/pull/12"}
    )
    assert unrelated.status_code == 400, unrelated.text
    subprocess.run(
        ["git", "remote", "set-url", "origin", "https://github.com/team/other.git"],
        cwd=checkout,
        check=True,
    )
    moved = await harness.client.get(path, params=params)
    assert moved.status_code == 409, moved.text
    assert len(calls) == before
