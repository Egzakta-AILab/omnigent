import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from omnigent.db.db_models import current_workspace_id
from omnigent.entities.conversation import (
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
    NewConversationItem,
)
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
from omnigent.studio import fleet, lease_turns, rpc
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
    assert client.get(f"/v1/sessions/{source.id}", headers=headers).status_code == 403
    assert client.get(path, headers=headers).status_code == 403
    assert client.post(path, headers=headers, json={}).status_code == 403
    policy = client.post(f"/v1/sessions/{source.id}/policies/evaluate", headers=headers, json={})
    assert policy.status_code == 403
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
    conversations = SqlAlchemyConversationStore(db_uri)
    agent = SqlAlchemyAgentStore(db_uri).create(uuid4().hex, "test-native", "bundle").id
    deliveries = []

    def deliver(sid, body):
        rnd = len(deliveries) + 1
        deliveries.append(body["data"]["stable_id"])
        assert "org dev test-int" in body["data"]["content"][0]["text"]
        assert "Verify the change" in body["data"]["content"][0]["text"]
        response_id = str(rnd)
        result = {"status": "check_failed", "round": rnd, "lease_id": value.idempotency_key.hex}
        receipt = {
            "status": "check_failed",
            "commit_sha": "c" * 40,
            "base_sha": "b" * 40,
            "output_digest": "a" * 64,
        }
        items = [
            ("message", MessageData(role="user", content=body["data"]["content"])),
            (
                "function_call",
                FunctionCallData(
                    agent="native",
                    name="shell",
                    arguments=json.dumps({"command": lease_turns.COMMAND}),
                    call_id=response_id,
                ),
            ),
            (
                "function_call_output",
                FunctionCallOutputData(call_id=response_id, output=json.dumps(receipt)),
            ),
            (
                "message",
                MessageData(
                    role="assistant",
                    agent="native",
                    content=[
                        {
                            "type": "output_text",
                            "text": lease_turns.FENCE + "\n" + json.dumps(result) + "\n```",
                        }
                    ],
                ),
            ),
        ]
        conversations.append(
            sid,
            [
                NewConversationItem(type=kind, response_id=response_id, data=data)
                for kind, data in items
            ],
        )

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
        conversations.create_conversation(
            conversation_id=sid, agent_id=agent, workspace=str(tmp_path)
        )
        deliver(sid, (await request.json())["initial_input"])
        sessions[sid] = "active"
        return {"id": sid}

    @runner.get("/v1/sessions/{sid}")
    def read(sid: str):
        if sid not in sessions:
            raise HTTPException(404)
        return {"id": sid}

    @runner.post("/v1/sessions/{sid}/events")
    async def stop(sid: str, request: Request):
        body = await request.json()
        if body["type"] == "message":
            deliver(sid, body)
            return {"queued": True}
        assert body == {"type": "stop_session"}
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
        with pytest.raises(HTTPException, match="Native rounds"):
            store.update_lease(
                "human",
                rpc.FinishRound(idempotency_key=value.idempotency_key, round=1, passed=True),
            )
        import asyncio

        for _ in range(6):
            await asyncio.gather(
                *(fleet.reconcile_turns(req, store, conversations) for _ in range(2))
            )
        lease = store.inventory("human", rpc.Page())["leases"][0]
        assert (lease["state"], lease["fix_round"], lease["last_result"]["source"]) == (
            "escalated",
            3,
            "native",
        )
        assert len(set(deliveries)) == len(deliveries) == 3
        cards = store.inbox("human", rpc.Page())
        assert len(cards) == 1 and str(value.idempotency_key) in cards[0]["evidence"][0]
    else:
        now[0] += LEASE_SECONDS
    await fleet.reap(req, store)
    assert list(sessions.values()) == ["archived"]
    assert store.expired_sessions() == []


@pytest.mark.parametrize(
    "counts", [(1, 2, 1, 3), (False, 2, 1, 3), (1, 4, 1, 3), (1, 2, 2, 3), None]
)
def test_fleet_capacity_is_bounded_and_provider_owned(monkeypatch, counts):
    calls = []
    payload = (
        dict(
            zip(
                ("bot_review_held", "other_held", "bot_review_limit", "other_limit"),
                counts,
                strict=True,
            )
        )
        if counts is not None
        else None
    )

    def resolve(request, actor, profile):
        calls.append((actor, profile))
        return SimpleNamespace(fleet_capacity=lambda: payload), {}

    monkeypatch.setattr(fleet, "provider", resolve)
    assert fleet.fleet_capacity(None, None) is None
    actual = fleet.fleet_capacity(None, "configured-bot")
    assert calls == [("configured-bot", "bot-review")]
    assert actual == (
        payload if counts and type(counts[0]) is int and counts == (1, 2, 1, 3) else None
    )
