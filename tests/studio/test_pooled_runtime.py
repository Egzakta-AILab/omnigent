from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from omnigent.db.db_models import current_workspace_id
from omnigent.host.frames import (
    CAP_STUDIO_SEAT,
    HostHelloFrame,
    HostLaunchRunnerFrame,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_OWNER, UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.sandbox_profile_store import SandboxProfileStore
from omnigent.stores.studio_tasks import LEASE_SECONDS, StudioTaskStore
from omnigent.studio import fleet, rpc
from omnigent.studio.credential_agent import write_private


def test_persisted_pool_rejects_shell_and_native_launch_without_provider(db_uri, tmp_path):
    permissions = SqlAlchemyPermissionStore(db_uri)
    actor = "worker@example.test"
    permissions.ensure_user(actor)
    agents = SqlAlchemyAgentStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    source = conversations.create_conversation(
        agent_id=agents.create(uuid4().hex, "agent", "bundle").id, workspace=str(tmp_path)
    )
    permissions.grant(actor, source.id, LEVEL_OWNER)
    SandboxProfileStore(conversations).bind_session(
        source.id,
        {
            "provider": "egzakta.coder",
            "id": "free-agent",
            "revision": "r1",
            "workspace_id": current_workspace_id(),
            "name": "Free agent",
            "description": "Pooled",
            "user_id": actor,
        },
    )
    artifacts = LocalArtifactStore(str(tmp_path / "objects"))
    app = create_app(
        agent_store=agents,
        conversation_store=conversations,
        file_store=SqlAlchemyFileStore(db_uri),
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        permission_store=permissions,
        host_store=HostStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
    )
    headers = {"X-Forwarded-Email": actor}
    path = f"/v1/sessions/{source.id}/resources/terminals"
    client = TestClient(app)
    assert client.get(path, headers=headers).status_code == 403
    assert client.post(path, headers=headers, json={}).status_code == 403
    for suffix in ("", "?read_only=true"):
        with (
            pytest.raises(WebSocketDisconnect) as error,
            client.websocket_connect(
                path + "/" + uuid4().hex + "/attach" + suffix, headers=headers
            ),
        ):
            pytest.fail("pooled terminal unexpectedly connected")
        assert error.value.code == 1008
    registry = app.state.host_registry
    hello = HostHelloFrame(
        name="pooled", version="current", frame_protocol_version=1, capabilities=[CAP_STUDIO_SEAT]
    )
    conn = SimpleNamespace(host_id=uuid4().hex, hello=hello)
    frame = HostLaunchRunnerFrame(
        request_id="launch",
        session_id=source.id,
        binding_token=uuid4().hex,
        workspace="/repo",
        harness="codex-native",
    )
    with pytest.raises(ValueError, match="Pooled dispatch"):
        registry.studio_profile_for_launch(conn, frame)


@pytest.mark.parametrize("termination", ["round", "expiry"])
async def test_reaper_controls_dispatched_runner(db_uri, tmp_path, monkeypatch, termination):
    now = [10.0]
    store = StudioTaskStore(db_uri, clock=lambda: now[0])
    task = store.task_for_issue(
        "human", rpc.TaskKey(linear_workspace=uuid4(), linear_issue_id=uuid4())
    )
    tid = UUID(task["task_id"])
    store.change_task("human", rpc.Acceptance(task_id=tid, acceptance_check="Verify the change"))
    value = rpc.LeaseClaim(idempotency_key=uuid4(), task_id=tid, harness="pi-native")
    runner = FastAPI()
    sessions = {}
    busy = True
    token_file = tmp_path / "bot"
    write_private(token_file, uuid4().hex)
    monkeypatch.setenv("OMNIGENT_STUDIO_DISPATCH_ACCOUNT", "bot")
    monkeypatch.setenv("OMNIGENT_STUDIO_DISPATCH_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(fleet, "provider", lambda *args: (None, {"revision": "r1"}))

    @runner.get("/v1/me")
    def me(request: Request):
        assert request.headers["Authorization"] == "Bearer " + token_file.read_text()
        return {"user_id": "bot", "is_admin": True}

    @runner.post("/v1/sessions")
    async def start(request: Request):
        if busy:
            raise HTTPException(429)
        sid = fleet.agent_session_id("bot")
        assert sid and fleet.agent_session_id("bot") is None
        assert (await request.json())["sandbox_profile_id"] == "free-agent"
        sessions[sid] = "active"
        return {"id": sid}

    @runner.get("/v1/sessions/{sid}")
    def read(sid: str):
        if sid not in sessions:
            raise HTTPException(404)
        return {"id": sid}

    @runner.post("/v1/sessions/{sid}/events")
    async def stop(sid: str, request: Request):
        assert (await request.json()) == {"type": "stop_session"}
        sessions[sid] = "stopped"
        return {"status": "stopped"}

    @runner.patch("/v1/sessions/{sid}")
    async def archive(sid: str, request: Request):
        assert sessions[sid] == "stopped" and (await request.json()) == {"archived": True}
        sessions[sid] = "archived"
        return {"archived": True}

    req = Request(
        {
            "type": "http",
            "scheme": "http",
            "server": ("test", 80),
            "path": "/",
            "root_path": "",
            "headers": [],
            "app": runner,
        }
    )
    with pytest.raises(HTTPException) as queued:
        await fleet.dispatch(req, store, "human", value)
    assert queued.value.status_code == 429
    busy = False
    assert "session_id" not in await fleet.dispatch(req, store, "human", value)
    await fleet.dispatch(req, store, "human", value)
    assert len(sessions) == 1
    if termination == "round":
        for rnd in (1, 2, 3):
            result = store.update_lease(
                "human",
                rpc.FinishRound(idempotency_key=value.idempotency_key, round=rnd, passed=False),
            )
        assert result["state"] == "escalated"
        store.update_lease(
            "human", rpc.FinishRound(idempotency_key=value.idempotency_key, round=3, passed=False)
        )
        cards = store.inbox("human", rpc.Page())
        assert len(cards) == 1 and str(value.idempotency_key) in cards[0]["evidence"][0]
    else:
        now[0] += LEASE_SECONDS
    await fleet.reap(req, store)
    assert list(sessions.values()) == ["archived"]
    assert store.expired_sessions() == []
