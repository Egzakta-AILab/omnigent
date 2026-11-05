"""Workspace settings route and host-frame integration with local fixtures."""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest_asyncio

from omnigent.host.connect import HostProcess
from omnigent.host.frames import (
    HostFsRequestFrame,
    HostFsWriteFrame,
    HostHelloFrame,
    decode_host_frame,
)
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
    frames: list[HostFsRequestFrame | HostFsWriteFrame]

    @property
    def project_path(self) -> str:
        return f"/v1/projects/{self.project_id}/studio"

    @property
    def session_path(self) -> str:
        return f"/v1/sessions/{self.source_id}/studio"


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
    frames: list[HostFsRequestFrame | HostFsWriteFrame] = []

    def dispatch(conn: HostConnection, data: str) -> None:
        assert conn is connection
        frame = decode_host_frame(data)
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
    initial = await harness.client.get(project, params={"workspace": "/forged"})
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
