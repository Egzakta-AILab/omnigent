"""Runner dispatch through authenticated routes and real ownership/sharing stores."""

import hashlib
import json
from uuid import uuid4

import pytest

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER, RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.runner.tool_dispatch import execute_tool
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ
from omnigent.server.feature_flags import FeatureFlags
from omnigent.stores.conversation_store import (
    CREATED_BY_SESSION_LABEL_KEY,
    PROJECT_LABEL_KEY,
    SCHEDULED_TASK_ORIGIN_LABEL_KEY,
    SIDE_CHAT_SOURCE_LABEL_KEY,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.integration.test_managed_startup_inputs import auth_app as auth_app
from tests.server.integration.test_managed_startup_inputs import (
    startup_permissions as startup_permissions,
)
from tests.server.integration.test_sessions_attribution import _bind_runner
from tests.server.integration.test_sessions_harness_override import _stub_late_runner_bind
from tests.server.integration.test_sessions_permissions import _create_session_as
from tests.server.integration.test_sessions_subagent_context import (
    two_replica_stack as two_replica_stack,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def orchestration_environment(monkeypatch, request):
    features = getattr(request, "param", None)
    if features is None:
        monkeypatch.delenv("OMNIGENT_FEATURES", raising=False)
    else:
        monkeypatch.setenv("OMNIGENT_FEATURES", features)
    return features


@pytest.fixture
async def peers(auth_client, auth_app, db_uri, monkeypatch):
    source = await _create_session_as(auth_client, "", "alice")
    target = await _create_session_as(auth_client, "", "alice")
    headers = {"X-Forwarded-Email": "alice", **_bind_runner(db_uri, source["id"])}
    auth_client.headers.update(headers)
    captured = _stub_late_runner_bind(monkeypatch)
    return source, target, captured


async def _send(client, source, target, message="Please inspect the PR"):
    return json.loads(
        await execute_tool(
            tool_name="sys_session_message",
            arguments=json.dumps({"session_id": target["id"], "message": message}),
            server_client=client,
            conversation_id=source["id"],
        )
    )


@pytest.mark.parametrize("policy_denied", [False, True])
@pytest.mark.parametrize(
    "orchestration_environment",
    [None, "", "usage_page", "-session_orchestration"],
    indirect=True,
)
async def test_private_sender_can_message_shared_receiver(
    auth_client, peers, db_uri, monkeypatch, policy_denied, orchestration_environment
):
    from omnigent.server.routes.sessions import routes_events

    source, target, captured = peers
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")
    permissions.grant("bob", target["id"], LEVEL_READ)
    if policy_denied:

        async def deny_task(*args, **kwargs):
            return {"verdict": "deny", "reason": "Task needs approval"}

        monkeypatch.setattr(routes_events, "_evaluate_input_policy", deny_task)
    result = await _send(auth_client, source, target)
    if orchestration_environment == "-session_orchestration":
        assert "Session orchestration is disabled" in result["detail"]
        assert not captured.get("posts")
        return
    if policy_denied:
        assert "Task needs approval" in result["error"]
        assert result["denied"] is True and result["queued"] is False
        assert not captured.get("posts")
        return
    assert result.get("queued") is True, result
    assert "Please inspect the PR" in json.dumps(captured)
    items = await auth_client.get(f"/v1/sessions/{target['id']}/items")
    assert f"[Message from session {source['id']}]" in items.text
    assert '"created_by":"alice"' in items.text
    store = SqlAlchemyConversationStore(db_uri)
    assert store.get_conversation(target["id"]).parent_conversation_id is None
    assert store.get_conversation(source["id"]).parent_conversation_id is None


async def test_runner_is_trusted_for_other_owned_sessions_it_hosts(auth_client, peers, db_uri):
    source, target, _ = peers
    sibling = await _create_session_as(auth_client, "", "alice")
    store = SqlAlchemyConversationStore(db_uri)
    store.set_runner_id(sibling["id"], store.get_conversation(source["id"]).runner_id)
    assert (await _send(auth_client, sibling, target)).get("queued") is True
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")
    permissions.grant("bob", sibling["id"], LEVEL_EDIT)
    assert (
        "shared with edit access cannot" in (await _send(auth_client, sibling, target))["detail"]
    )


@pytest.mark.parametrize("batch", [False, True])
async def test_peer_message_tracks_runner_startup(auth_client, peers, monkeypatch, batch):
    from omnigent.server.routes import sessions
    from omnigent.server.routes._sessions import orchestration

    source, target, _ = peers
    started = []

    async def observe_startup(*args, **kwargs):
        started.append(orchestration._session_is_starting(target["id"]))

    monkeypatch.setattr(sessions, "_ensure_runner_relay_ready", observe_startup)
    event = {"type": "session_message", "data": {"message": "Start"}}
    response = await auth_client.post(
        f"/v1/sessions/{target['id']}/events",
        headers={ORCHESTRATION_SOURCE_HEADER: source["id"]},
        json=[event] if batch else event,
    )
    assert response.status_code == 202, response.text
    assert started == [True]
    assert not orchestration._dispatch_in_flight.get(target["id"])


@pytest.mark.parametrize(
    "condition",
    [
        "flag_off",
        "shared_edit",
        "public_edit",
        "other_owner",
        "admin",
        "missing_binding",
        "wrong_binding",
        "self",
        "blank",
        "oversize",
        "missing_source",
    ],
)
async def test_refusal_before_delivery(condition, auth_client, auth_app, peers, db_uri):
    source, target, captured = peers
    permissions = SqlAlchemyPermissionStore(db_uri)
    if condition == "flag_off":
        auth_app.state.feature_flags = FeatureFlags()
    if condition in {"shared_edit", "public_edit"}:
        user = "__public__" if condition == "public_edit" else "bob"
        permissions.ensure_user(user)
        permissions.grant(user, source["id"], LEVEL_EDIT)
    if condition in {"other_owner", "admin"}:
        other = await _create_session_as(auth_client, "", "bob")
        permissions.grant("alice", other["id"], LEVEL_EDIT)
        target = other
        if condition == "admin":
            permissions.set_admin("alice", True)
    if condition == "missing_binding":
        del auth_client.headers[RUNNER_TUNNEL_TOKEN_HEADER]
    if condition == "wrong_binding":
        auth_client.headers[RUNNER_TUNNEL_TOKEN_HEADER] = "wrong"
    if condition == "self":
        target = source
    captured.clear()
    response = await auth_client.post(
        f"/v1/sessions/{target['id']}/events",
        headers={}
        if condition == "missing_source"
        else {ORCHESTRATION_SOURCE_HEADER: source["id"]},
        json={
            "type": "session_message",
            "data": {
                "message": {
                    "blank": " ",
                    "oversize": "a" * 8001,
                }.get(condition, "DO_NOT_DELIVER")
            },
        },
    )
    assert response.status_code in {400, 403}, response.text
    assert not captured.get("posts")


@pytest.mark.parametrize("manager_kind", ["root", "child", "side_chat"])
async def test_view_only_sharing_keeps_new_controls(auth_client, peers, db_uri, manager_kind):
    source, target, captured = peers
    manager = source
    if manager_kind != "root":
        response = await auth_client.post(
            f"/v1/sessions/{source['id']}/fork" if manager_kind == "side_chat" else "/v1/sessions",
            json={"side_chat": True}
            if manager_kind == "side_chat"
            else {"agent_id": source["agent_id"], "parent_session_id": source["id"]},
        )
        assert response.status_code == 201, response.text
        manager = response.json()
        if manager_kind == "side_chat":
            _bind_runner(db_uri, manager["id"])
    permissions = SqlAlchemyPermissionStore(db_uri)
    for viewer in ("bob", "__public__"):
        permissions.ensure_user(viewer)
        permissions.grant(viewer, source["id"], LEVEL_READ)
        if manager_kind == "side_chat":
            permissions.grant(viewer, manager["id"], LEVEL_READ)
    captured.clear()
    assert (await _send(auth_client, manager, target)).get("queued") is True
    assert "Please inspect the PR" in json.dumps(captured)
    # Upgrading any viewer to edit restores the refusal.
    permissions.grant("bob", source["id"], LEVEL_EDIT)
    captured.clear()
    assert (
        "shared with edit access cannot" in (await _send(auth_client, manager, target))["detail"]
    )
    assert not captured.get("posts")


async def test_editor_beyond_first_grant_page_blocks_controls(auth_client, peers, db_uri):
    source, target, captured = peers
    permissions = SqlAlchemyPermissionStore(db_uri)
    for index in range(100):
        permissions.ensure_user(f"viewer-{index:03d}")
        permissions.grant(f"viewer-{index:03d}", source["id"], LEVEL_READ)
    assert (await _send(auth_client, source, target)).get("queued") is True
    permissions.ensure_user("zed")
    permissions.grant("zed", source["id"], LEVEL_EDIT)
    _, cursor = permissions.list_for_session(source["id"])
    assert cursor is not None
    captured.clear()
    assert "shared with edit access cannot" in (await _send(auth_client, source, target))["detail"]
    assert not captured.get("posts")


async def test_sharing_during_receiver_startup_blocks_delivery(
    auth_client, peers, db_uri, monkeypatch
):
    from omnigent.server.routes import sessions

    source, target, captured = peers
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")

    async def share_while_starting(*args, **kwargs):
        permissions.grant("bob", source["id"], LEVEL_EDIT)

    monkeypatch.setattr(sessions, "_ensure_runner_relay_ready", share_while_starting)
    result = await _send(auth_client, source, target)
    assert "error" in result, result
    assert not any("/events" in path for path, _ in captured.get("posts", []))
    items = await auth_client.get(f"/v1/sessions/{target['id']}/items")
    assert "Please inspect" not in items.text


async def test_child_inherits_source_sharing(auth_client, peers, db_uri):
    source, target, captured = peers
    response = await auth_client.post(
        "/v1/sessions",
        json={
            "agent_id": source["agent_id"],
            "parent_session_id": source["id"],
        },
    )
    assert response.status_code == 201, response.text
    child = response.json()
    assert (await _send(auth_client, child, target)).get("queued") is True
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")
    permissions.grant("bob", source["id"], LEVEL_EDIT)
    captured.clear()
    assert "shared with edit access cannot" in (await _send(auth_client, child, target))["detail"]
    assert not captured.get("posts")


@pytest.mark.parametrize("descendant", ["side_chat", "nested_side_chat", "child"])
@pytest.mark.parametrize("share", ["source", "side_chat"])
async def test_side_chat_inherits_sharing_for_new_controls(
    auth_client, peers, db_uri, descendant, share
):
    source, target, captured = peers
    response = await auth_client.post(
        f"/v1/sessions/{source['id']}/fork", json={"side_chat": True}
    )
    assert response.status_code == 201, response.text
    side = response.json()
    _bind_runner(db_uri, side["id"])
    manager = side
    if descendant != "side_chat":
        response = await auth_client.post(
            f"/v1/sessions/{side['id']}/fork"
            if descendant == "nested_side_chat"
            else "/v1/sessions",
            json={"side_chat": True}
            if descendant == "nested_side_chat"
            else {"agent_id": side["agent_id"], "parent_session_id": side["id"]},
        )
        assert response.status_code == 201, response.text
        manager = response.json()
        if descendant == "nested_side_chat":
            _bind_runner(db_uri, manager["id"])
    assert (await _send(auth_client, manager, target)).get("queued") is True
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")
    shared_id = source["id"] if share == "source" else side["id"]
    permissions.grant("bob", shared_id, LEVEL_EDIT)
    store = SqlAlchemyConversationStore(db_uri)
    before_sessions = {s.id for s in store.list_conversations().data}
    before_grants = permissions.list_for_user("alice")
    captured.clear()
    assert (
        "shared with edit access cannot" in (await _send(auth_client, manager, target))["detail"]
    )
    for placement in ("same_host", "managed"):
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps({"agent_id": side["agent_id"], "placement": placement}),
                server_client=auth_client,
                conversation_id=manager["id"],
            )
        )
        assert "shared with edit access cannot" in result["detail"], result
    assert not captured.get("posts")
    assert {s.id for s in store.list_conversations().data} == before_sessions
    assert permissions.list_for_user("alice") == before_grants
    # Sharing restricts new controls, not existing owner reads or incoming work.
    history = await auth_client.get(f"/v1/sessions/{manager['id']}/items")
    assert history.status_code == 200, history.text
    _bind_runner(db_uri, target["id"])
    assert (await _send(auth_client, target, manager)).get("queued") is True
    permissions.revoke("bob", shared_id)
    assert (await _send(auth_client, manager, target)).get("queued") is True


@pytest.mark.parametrize("source_link", ["missing", "cycle"])
async def test_side_chat_requires_resolvable_source(auth_client, peers, db_uri, source_link):
    source, target, captured = peers
    response = await auth_client.post(
        f"/v1/sessions/{source['id']}/fork", json={"side_chat": True}
    )
    assert response.status_code == 201, response.text
    side = response.json()
    _bind_runner(db_uri, side["id"])
    store = SqlAlchemyConversationStore(db_uri)
    store.set_labels(
        side["id"],
        {SIDE_CHAT_SOURCE_LABEL_KEY: side["id"] if source_link == "cycle" else uuid4().hex},
    )
    captured.clear()
    assert (
        "Session is not owned or available" in (await _send(auth_client, side, target))["detail"]
    )
    assert not captured.get("posts")


@pytest.mark.parametrize("execution_target", ["connected_host", "managed_sandbox"])
@pytest.mark.parametrize("full_fork", [False, True])
async def test_scheduled_origin_allows_new_controls_children_and_reads(
    auth_client, auth_app, peers, db_uri, monkeypatch, execution_target, full_fork
):
    from omnigent.server.routes.sessions import routes_core
    from omnigent.server.scheduled.fire import FireDeps, build_on_fire
    from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
    from tests._helpers.polling import poll_until_async

    source, target, captured = peers
    conversations = SqlAlchemyConversationStore(db_uri)
    schedules = SqlAlchemyScheduledTaskStore(db_uri)
    task = schedules.create(
        scheduled_task_id=uuid4().hex,
        name="Scheduled worker",
        prompt="Ordinary scheduled work",
        rrule="FREQ=HOURLY",
        user_id="alice",
        agent_id=source["agent_id"],
        timezone="UTC",
        execution_target=execution_target,
        host_id=uuid4().hex,
        workspace="/tmp",
    )
    labels_at_launch = []

    async def launch(session, task):
        labels_at_launch.append(session.labels)

    on_fire = build_on_fire(
        FireDeps(
            scheduled_task_store=schedules,
            agent_store=auth_app.state.agent_store,
            conversation_store=conversations,
            permission_store=SqlAlchemyPermissionStore(db_uri),
            host_store=auth_app.state.host_store,
            host_registry=auth_app.state.host_registry,
            sandbox_config=auth_app.state.sandbox_config,
        ),
        launch_dispatch=launch,
    )
    assert await on_fire(0, task.id)
    await poll_until_async(lambda: schedules.get(task.id).last_run_conversation_id, timeout_s=5)
    scheduled_id = schedules.get(task.id).last_run_conversation_id
    assert labels_at_launch[0][SCHEDULED_TASK_ORIGIN_LABEL_KEY] == task.id
    schedules.delete(task.id)
    assert schedules.list_runs(task.id)[0] == []

    if full_fork:
        response = await auth_client.post(f"/v1/sessions/{scheduled_id}/fork", json={})
        assert response.status_code == 201, response.text
        scheduled_id = response.json()["id"]
        assert (
            conversations.get_conversation(scheduled_id).labels[SCHEDULED_TASK_ORIGIN_LABEL_KEY]
            == task.id
        )

    # Managed creation succeeds now; record its launch instead of provisioning.
    launched_by = []

    async def launch(**kwargs):
        launched_by.append(kwargs["owner"])

    monkeypatch.setattr(routes_core, "_run_managed_launch", launch)
    scheduled = {"id": scheduled_id}
    auth_client.headers.update(_bind_runner(db_uri, scheduled_id))
    for depth in range(3):
        captured.clear()
        # A scheduled run, its forks and children message the owner's sessions
        # like any private session they own.
        result = await _send(auth_client, scheduled, target)
        assert result.get("queued") is True, result
        assert "Please inspect the PR" in json.dumps(captured)
        for placement in ("same_host", "managed"):
            result = json.loads(
                await execute_tool(
                    tool_name="sys_session_create",
                    arguments=json.dumps({"agent_id": source["agent_id"], "placement": placement}),
                    server_client=auth_client,
                    conversation_id=scheduled["id"],
                )
            )
            if placement == "managed":
                assert result.get("conversation_id"), result
            else:
                # Same-host placement needs a live host this test does not
                # register; it must not fail on the scheduled origin.
                assert result.get("error") != "access_denied", result
        history = json.loads(
            await execute_tool(
                tool_name="sys_session_get_history",
                arguments=json.dumps({"conversation_id": target["id"]}),
                server_client=auth_client,
                conversation_id=scheduled["id"],
            )
        )
        assert "error" not in history, history
        # Ordinary child creation retains its existing authorization and parentage.
        if depth < 2:
            response = await auth_client.post(
                "/v1/sessions",
                json={"agent_id": source["agent_id"], "parent_session_id": scheduled["id"]},
            )
            assert response.status_code == 201, response.text
            scheduled = response.json()

    await poll_until_async(lambda: len(launched_by) == 3, timeout_s=5)
    assert launched_by == ["alice"] * 3
    for labels in (
        {SCHEDULED_TASK_ORIGIN_LABEL_KEY: ""},
        {SCHEDULED_TASK_ORIGIN_LABEL_KEY: "other"},
    ):
        response = await auth_client.patch(f"/v1/sessions/{scheduled_id}", json={"labels": labels})
        assert response.status_code == 400, response.text
        response = await auth_client.post(
            "/v1/sessions", json={"agent_id": source["agent_id"], "labels": labels}
        )
        assert response.status_code == 400, response.text
    response = await auth_client.patch(f"/v1/sessions/{scheduled_id}", json={"labels": {}})
    assert response.status_code == 200, response.text
    assert (
        conversations.get_conversation(scheduled_id).labels[SCHEDULED_TASK_ORIGIN_LABEL_KEY]
        == task.id
    )


@pytest.mark.parametrize("placement", ["same_host", "managed"])
@pytest.mark.parametrize(
    "condition,orchestration_environment",
    [("flag_off", "-session_orchestration"), ("shared", None), ("binding", None)],
    indirect=["orchestration_environment"],
)
async def test_creation_refuses_before_launch(
    auth_client, auth_app, peers, db_uri, placement, condition
):
    from tests.server.integration.test_sessions_permissions import _register_online_host

    source, _, captured = peers
    host_id = uuid4().hex
    _register_online_host(auth_app, host_id, "alice")
    SqlAlchemyConversationStore(db_uri).set_host_id(source["id"], host_id, workspace="/tmp")
    assert auth_app.state.sandbox_config.managed_launch_supported
    if condition == "shared":
        permissions = SqlAlchemyPermissionStore(db_uri)
        permissions.ensure_user("bob")
        permissions.grant("bob", source["id"], LEVEL_EDIT)
    elif condition == "binding":
        auth_client.headers[RUNNER_TUNNEL_TOKEN_HEADER] = "wrong"
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    before_sessions = {s.id for s in store.list_conversations().data}
    before_grants = permissions.list_for_user("alice")
    result = json.loads(
        await execute_tool(
            tool_name="sys_session_create",
            arguments=json.dumps({"agent_id": source["agent_id"], "placement": placement}),
            server_client=auth_client,
            conversation_id=source["id"],
        )
    )
    expected = {
        "flag_off": "Session orchestration is disabled",
        "shared": "shared with edit access cannot",
        "binding": "requires the sender's bound runner",
    }[condition]
    assert expected in result["detail"], result
    if condition == "flag_off":
        response = await auth_client.post(
            "/v1/sessions",
            headers={ORCHESTRATION_SOURCE_HEADER: source["id"]},
            json={
                "agent_id": source["agent_id"],
                "host_type": "managed" if placement == "managed" else "external",
            },
        )
        assert response.status_code == 403 and expected in response.text
    assert not captured.get("posts")
    assert auth_app.state.host_registry.get(host_id).outbound_queue.empty()
    assert {s.id for s in store.list_conversations().data} == before_sessions
    assert permissions.list_for_user("alice") == before_grants


async def test_same_host_still_requires_host_ownership(auth_client, auth_app, peers, db_uri):
    from tests.server.integration.test_sessions_permissions import _register_online_host

    source, _, captured = peers
    host_id = "a" * 32
    _register_online_host(auth_app, host_id, "bob")
    SqlAlchemyConversationStore(db_uri).set_host_id(source["id"], host_id, workspace="/tmp")
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    before_sessions = {s.id for s in store.list_conversations().data}
    before_grants = permissions.list_for_user("alice")
    result = json.loads(
        await execute_tool(
            tool_name="sys_session_create",
            arguments=json.dumps({"agent_id": source["agent_id"], "placement": "same_host"}),
            server_client=auth_client,
            conversation_id=source["id"],
        )
    )
    assert "not your host" in result["detail"], result
    assert not captured.get("posts")
    assert auth_app.state.host_registry.get(host_id).outbound_queue.empty()
    assert {s.id for s in store.list_conversations().data} == before_sessions
    assert permissions.list_for_user("alice") == before_grants


@pytest.mark.parametrize(
    "scenario", ["unfiled", "moved", "foreign", "legacy", "stale-agent", "project"]
)
async def test_studio_reviewer_uses_private_session_and_original_runner(
    auth_client, auth_app, peers, db_uri, monkeypatch, scenario, tmp_path
):
    from base64 import b64decode
    from unittest.mock import AsyncMock

    from fastapi import APIRouter, Request

    from omnigent.db.utils import builtin_agent_id
    from omnigent.entities.conversation import MessageData, NewConversationItem
    from omnigent.errors import ErrorCode, OmnigentError
    from omnigent.harness_plugins import native_provider_for_key
    from omnigent.host.frames import (
        HostFsRequestFrame,
        HostFsWriteFrame,
        HostLaunchRunnerFrame,
        HostStatFrame,
        decode_host_frame,
    )
    from omnigent.native.native_coding_agents import CODEX_NATIVE_AGENT_NAME
    from omnigent.runtime import _globals
    from omnigent.server.app import _build_native_bundle, _ensure_builtin_agent
    from omnigent.server.auth import LEVEL_OWNER, RESERVED_USER_PUBLIC, UnifiedAuthProvider
    from omnigent.server.routes.sessions import routes_core, routes_events
    from omnigent.server.routes.studio_reviews import create_studio_reviews_router
    from omnigent.server.schemas import SessionEventInput
    from omnigent.server.sharing_settings import DefaultPublicSessions
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
    from omnigent.stores.studio_review_store import StudioReviewStore
    from omnigent.studio.reviews.models import ReviewPass, ReviewProfile, ReviewTarget
    from tests.server.integration.test_sessions_permissions import _register_online_host

    source, _, _ = peers
    agents = SqlAlchemyAgentStore(db_uri)
    provider = native_provider_for_key("codex")
    assert provider is not None and _globals._artifact_store is not None
    _ensure_builtin_agent(
        agents,
        _globals._artifact_store,
        _globals._agent_cache,
        name=CODEX_NATIVE_AGENT_NAME,
        bundle_bytes=_build_native_bundle(provider),
    )
    reviewer = agents.get(builtin_agent_id(CODEX_NATIVE_AGENT_NAME))
    assert reviewer is not None
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    projects = SqlAlchemyProjectStore(db_uri)
    permissions.grant(RESERVED_USER_PUBLIC, source["id"], LEVEL_READ)
    expected_project_id = None
    expected_project_name = None
    if scenario in {"moved", "foreign", "stale-agent"}:
        project = projects.create(
            uuid4().hex, "Original", "bob" if scenario == "foreign" else "alice"
        )
        assert conversations.set_conversation_project(source["id"], project.id)
    if scenario == "legacy":
        expected_project_name = "Legacy"
        conversations.set_labels(source["id"], {PROJECT_LABEL_KEY: expected_project_name})
    if scenario == "project":
        project = projects.create(uuid4().hex, "Direct review", "alice")
        expected_project_id = project.id
        expected_project_name = project.name
    host_id = uuid4().hex
    _register_online_host(auth_app, host_id, "alice")
    auth_app.state.host_store.upsert_on_connect(
        host_id, "Review host", "alice", configured_harnesses={"codex-native": True}
    )
    workspace = "/review-workspace"
    conversations.set_host_id(source["id"], host_id, workspace=workspace)
    auth_app.state.default_public_sessions = lambda: DefaultPublicSessions.ALL
    launches = []
    inline_writes = []
    pr_url = "https://github.com/example/project/pull/42"
    role_digest = "1" * 64
    current_head = "b" * 40
    base_sha = "a" * 40

    patch = "diff --git a/hello b/hello\n--- a/hello\n+++ b/hello\n@@ -1 +1 @@\n-before\n+after\n"
    captured_patch = patch

    def answer_host(connection, payload):
        frame = decode_host_frame(payload)
        if isinstance(frame, HostStatFrame):
            connection.pending_stats[frame.request_id].set_result(
                {"status": "ok", "exists": True, "type": "directory", "canonical_path": frame.path}
            )
        elif isinstance(frame, HostLaunchRunnerFrame):
            launches.append(frame)
            connection.pending_launches[frame.request_id].set_result({"status": "launched"})
        elif isinstance(frame, HostFsWriteFrame) and scenario == "project":
            assert frame.session_id == expected_project_id
            assert frame.workspace == workspace
            assert frame.op == "studio_project_pull_request_publish_inline"
            inline_writes.append(frame.params)
            if len(inline_writes) == 1:
                result = {
                    "status": "ok",
                    "payload": {
                        "thread": {"comments": {"data": [{"url": f"{pr_url}#discussion_r1"}]}}
                    },
                }
            else:
                result = {
                    "status": "error",
                    "error_status": 503,
                    "error": "Provider delivery could not be confirmed",
                }
            connection.pending_fs_requests[frame.request_id].set_result(result)
        elif isinstance(frame, HostFsRequestFrame) and scenario == "project":
            assert frame.session_id == expected_project_id
            assert frame.workspace == workspace
            if frame.op == "studio_project_pull_request":
                assert frame.params["repository"] == "nested/project"
                assert frame.params["pr_url"] == pr_url
                assert frame.params["require_active"] is True
                result = {
                    "pr": {
                        "url": pr_url,
                        "number": 42,
                        "base_sha": base_sha,
                        "head_sha": current_head,
                    }
                }
            elif frame.op == "studio_project_pull_request_patch":
                assert frame.params["base_sha"] == base_sha
                assert frame.params["head_sha"] == current_head
                assert frame.params["capture"] is True
                result = {
                    "object": "session.github.pr_diff",
                    "patch": patch,
                    "base_sha": base_sha,
                    "head_sha": current_head,
                }
            else:
                assert frame.op == "studio_review_profiles"
                assert frame.params == {"profile_id": "backend"}
                result = {
                    "id": "backend",
                    "label": "Backend",
                    "digest": role_digest,
                    "instructions": "Check service boundaries.",
                }
            connection.pending_fs_requests[frame.request_id].set_result(
                {"status": "ok", "payload": result}
            )
        else:
            pytest.fail(f"Unexpected host frame: {frame}")

    monkeypatch.setattr(auth_app.state.host_registry, "send_text", answer_host)
    review = ReviewPass(
        id=uuid4().hex,
        owner_user_id="alice",
        target=ReviewTarget(
            source_session_id=source["id"] if scenario != "project" else None,
            source_project_id=expected_project_id if scenario == "project" else None,
            repository_path="nested/project" if scenario == "project" else None,
            host_id=host_id,
            workspace=workspace,
            pr_url=pr_url,
            repository="example/project",
            pr_number=42,
            base_sha=base_sha,
            head_sha=current_head,
        ),
        profile=ReviewProfile(
            agent_id=reviewer.id,
            agent_version=str(reviewer.version + int(scenario == "stale-agent")),
            harness="codex-native",
            skill="review",
            model="review-model",
            reasoning_effort="high",
        ),
        state="prepared",
        dispatch_status="not_requested",
        reviewer_session_id=None,
        created_at=1,
        updated_at=1,
        version=1,
    )
    if scenario == "moved":
        project = projects.create(
            uuid4().hex,
            "Current",
            "alice",
            config={
                "agent_id": source["agent_id"],
                "workspace": "/project-default",
                "git": {"base": "main"},
            },
        )
        assert conversations.set_conversation_project(source["id"], project.id)
        expected_project_id = project.id
        expected_project_name = project.name
    request = Request(
        {"type": "http", "app": auth_app, "headers": [(b"x-forwarded-email", b"alice")]}
    )
    auth_provider = UnifiedAuthProvider(source="header", local_single_user=False)
    create_callbacks: list[routes_core.ReviewCreate] = []
    routes_core.register_core_routes(
        APIRouter(),
        conversation_store=conversations,
        agent_store=agents,
        agent_cache=_globals._agent_cache,
        auth_provider=auth_provider,
        permission_store=permissions,
        project_store=projects,
        register_review_create=create_callbacks.append,
    )
    create_reviewer = create_callbacks[0]
    if scenario == "stale-agent":
        with pytest.raises(OmnigentError) as stale:
            await create_reviewer(request, "alice", review, review.id)
        assert stale.value.code == ErrorCode.CONFLICT
        assert conversations.get_conversation(review.id) is None
        assert not launches
        return

    announcements = []
    announce_session = routes_core._announce_session_added

    def observe_announcement(user_id, session_id):
        announced = conversations.get_conversation(session_id)
        assert announced is not None
        announcements.append((user_id, session_id, announced.project_id))
        announce_session(user_id, session_id)

    monkeypatch.setattr(routes_core, "_announce_session_added", observe_announcement)
    if scenario == "project":
        artifact_store = LocalArtifactStore(str(tmp_path / "review-evidence"))
        send_review = AsyncMock(return_value={"queued": True, "pending_id": "review-input"})
        legacy_capture, legacy_patch = AsyncMock(), AsyncMock()
        # Keep the optional SPA catch-all after the injected API routes.
        mounts = [route for route in auth_app.router.routes if route.name == "web-ui"]
        for mount in mounts:
            auth_app.router.routes.remove(mount)
        auth_app.include_router(
            create_studio_reviews_router(
                StudioReviewStore(db_uri),
                conversations,
                agents,
                capture_context=legacy_capture,
                read_patch=legacy_patch,
                create_reviewer=create_reviewer,
                send_review=send_review,
                auth_provider=auth_provider,
                permission_store=permissions,
                project_store=projects,
                artifact_store=artifact_store,
            ),
            prefix="/v1",
        )
        auth_app.router.routes.extend(mounts)
        path = f"/v1/projects/{expected_project_id}/studio/reviews"
        before = {conv.id for conv in conversations.list_conversations().data}
        history = await auth_client.get(path)
        assert history.status_code == 200, history.text
        assert history.json()["data"] == []
        reviewer_input = {
            "host_id": host_id,
            "workspace": workspace,
            "repository": "nested/project",
            "pr_url": pr_url,
            "expected_base_sha": base_sha,
            "expected_head_sha": current_head,
            "agent_id": reviewer.id,
            "harness": "codex-native",
            "profile_id": "backend",
            "profile_digest": role_digest,
            "model": "review-model",
            "reasoning_effort": "high",
        }
        capture = await auth_client.post(
            path, headers={"Idempotency-Key": "project-capture"}, json=reviewer_input
        )
        assert capture.status_code == 201, capture.text
        captured = capture.json()["review"]
        assert captured["target"] == review.target.model_dump()
        assert {conv.id for conv in conversations.list_conversations().data} == before
        assert not launches
        assert (await auth_client.get(path)).json()["data"] == [captured]
        start_path = f"/v1/studio/reviews/{captured['id']}/start"
        current_head = "c" * 40
        stale = await auth_client.post(
            start_path, headers={"Idempotency-Key": "project-start"}, json={}
        )
        assert stale.status_code == 409, stale.text
        assert {conv.id for conv in conversations.list_conversations().data} == before
        current_head = "b" * 40
        started = await auth_client.post(
            start_path, headers={"Idempotency-Key": "project-start"}, json={}
        )
        assert started.status_code == 200, started.text
        started_review = started.json()
        assert started_review["dispatch_status"] == "running", started.text
        review_id = captured["id"]
        assert started_review["reviewer_session_id"] == review_id
        replay = await auth_client.post(
            start_path, headers={"Idempotency-Key": "project-start"}, json={}
        )
        assert replay.json() == started_review
        assert {conv.id for conv in conversations.list_conversations().data} == before | {
            review_id
        }
        assert [frame.session_id for frame in launches] == [review_id]
        assert announcements == [("alice", review_id, expected_project_id)]
        send_review.assert_awaited_once()
        legacy_capture.assert_not_awaited()
        legacy_patch.assert_not_awaited()
        conv = conversations.get_conversation(review_id)
        assert conv is not None
        assert (conv.host_id, conv.workspace, conv.project_id, conv.parent_conversation_id) == (
            host_id,
            workspace,
            expected_project_id,
            None,
        )
        assert conv.model_override == "review-model" and conv.reasoning_effort == "high"
        assert permissions.get(RESERVED_USER_PUBLIC, review_id) is None
        reviewer_path = f"/v1/sessions/{review_id}/studio/review"
        assert (await auth_client.get(reviewer_path)).json() == started_review
        for private_path in (path, reviewer_path):
            private = await auth_client.get(private_path, headers={"X-Forwarded-Email": "bob"})
            assert private.status_code == 404, private.text
        runs_path = path.replace("/reviews", "/review-runs")
        headers = {"Idempotency-Key": "project-review-run"}
        roster = {
            "reviewers": [
                reviewer_input,
                {**reviewer_input, "model": "second-review-model"},
            ]
        }
        run_capture = await auth_client.post(runs_path, headers=headers, json=roster)
        assert run_capture.status_code == 200, run_capture.text
        run = run_capture.json()
        members = {member["id"] for member in run["reviewers"]}
        assert len(members) == 2
        assert run["target"] == captured["target"]
        assert all(member["run_id"] == run["id"] for member in run["reviewers"])
        assert {conv.id for conv in conversations.list_conversations().data} == before | {
            review_id
        }
        assert (await auth_client.post(runs_path, headers=headers, json=roster)).json() == run
        mismatched = {**reviewer_input, "expected_head_sha": "c" * 40}
        invalid = await auth_client.post(
            runs_path, headers=headers, json={"reviewers": [reviewer_input, mismatched]}
        )
        assert invalid.status_code == 422, invalid.text
        assert (await auth_client.get(runs_path, params={"pr_url": pr_url})).json() == {
            "entries": [run],
            "next_cursor": None,
        }

        run_path = f"/v1/studio/review-runs/{run['id']}"
        frozen = await auth_client.get(f"{run_path}/patch")
        assert frozen.status_code == 200, frozen.text
        assert frozen.json()["patch"] == captured_patch
        assert frozen.json()["sha256"] == run["patch_sha256"]
        cited = await auth_client.get(
            f"{run_path}/patch", params={"path": "hello", "side": "head", "line": 1}
        )
        assert cited.json()["source_line"] == "after"
        invalid_citation = await auth_client.get(
            f"{run_path}/patch", params={"path": "hello", "side": "base", "line": 200}
        )
        assert invalid_citation.json()["source_line"] is None
        forbidden_patch = await auth_client.get(
            f"{run_path}/patch", headers={"X-Forwarded-Email": "bob"}
        )
        assert forbidden_patch.status_code == 404
        current_head = "c" * 40
        stale_run = await auth_client.post(f"{run_path}/start", json={})
        assert stale_run.status_code == 200, stale_run.text
        assert set(stale_run.json()["errors"]) == members
        assert all(
            member["launch_error"]
            for member in (await auth_client.get(run_path)).json()["reviewers"]
        )
        assert len(launches) == 1
        current_head = "b" * 40
        patch = captured_patch.replace("+after", "+different")
        drift = await auth_client.post(f"{run_path}/start", json={})
        assert drift.status_code == 200, drift.text
        assert len(drift.json()["errors"]) == len(members)
        assert len(launches) == 1
        patch = captured_patch
        started_run = await auth_client.post(f"{run_path}/start", json={})
        assert started_run.status_code == 200, started_run.text
        assert started_run.json()["errors"] == {}
        assert all(
            member["launch_error"] is None for member in started_run.json()["run"]["reviewers"]
        )
        assert {
            member["reviewer_session_id"] for member in started_run.json()["run"]["reviewers"]
        } == members
        assert {conv.id for conv in conversations.list_conversations().data} == before | {
            review_id
        } | members
        assert send_review.await_count == 3
        await auth_client.post(f"{run_path}/start", json={})
        assert send_review.await_count == 3
        consolidation = await auth_client.get(f"{run_path}/report")
        assert consolidation.status_code == 200, consolidation.text
        assert {entry["review_id"] for entry in consolidation.json()["reviewers"]} == members
        assert consolidation.json()["findings"] == []
        finding = {
            "title": "Validate the input",
            "detail": "The new branch accepts invalid values.",
            "severity": "high",
            "path": "hello",
            "side": "head",
            "line": 1,
        }

        def save_assistant_message(member_id: str, text: str) -> None:
            conversations.append(
                member_id,
                [
                    NewConversationItem(
                        type="message",
                        response_id=uuid4().hex,
                        data=MessageData(
                            role="assistant",
                            content=[{"type": "output_text", "text": text}],
                            agent=reviewer.id,
                        ),
                    )
                ],
            )
            conversations.set_session_live_status(member_id, "idle")

        for member_id in members:
            envelope = json.dumps(
                {
                    "review_id": member_id,
                    "base_sha": base_sha,
                    "head_sha": current_head,
                    "summary": "Independent reviewer report",
                    "findings": [finding, {**finding, "line": 200}],
                }
            )
            save_assistant_message(member_id, "```studio-review-v1\n" + envelope + "\n```")
        ready = await auth_client.get(f"{run_path}/report")
        assert ready.status_code == 200, ready.text
        assert len(ready.json()["findings"]) == 1
        assert set(ready.json()["findings"][0]["review_ids"]) == members
        assert all(
            entry["report"]["citation_validation"] == "checked"
            and entry["report"]["unverified_findings"][0]["line"] == 200
            for entry in ready.json()["reviewers"]
        )
        assert "finding_key" not in ready.json()["findings"][0]
        keyed_report = await auth_client.get(
            f"{run_path}/report", params={"include_finding_keys": True}
        )
        finding_key = keyed_report.json()["findings"][0]["finding_key"]
        reproduction_path = f"{run_path}/findings/{finding_key}/reproductions"
        assert (await auth_client.get(reproduction_path)).json() == []
        side = {
            "archive_sha256": "e" * 64,
            "archive_bytes": 10_240,
            "state": "exited",
            "exit_code": 0,
            "oom_killed": False,
            "elapsed_ms": 250,
            "output": "x" * 8192,
            "output_sha256": "f" * 64,
            "output_bytes": 8192,
            "output_truncated": False,
        }
        receipt = {
            "schema_version": "omnigent.studio.reproduction.v1",
            "observed_at": "2026-11-05T00:00:00Z",
            "runtime": "docker-no-network",
            "image_id": "sha256:" + "1" * 64,
            "command": ["python", "check.py"],
            "timeout_seconds": 60,
            "source": {
                "run_id": run["id"],
                "pr_url": pr_url,
                "patch_sha256": run["patch_sha256"],
                "base_sha": base_sha,
                "head_sha": current_head,
            },
            "base": {**side, "commit": base_sha},
            "head": {**side, "commit": current_head, "exit_code": 1},
        }
        artifacts_path = f"{run_path}/findings/{finding_key}/evidence"
        artifact_params = {"patch_sha256": run["patch_sha256"]}
        png = b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lX8AAAAASUVORK5CYII="
        )
        upload_png = {"content": png, "headers": {"Content-Type": "image/png"}}
        assert (await auth_client.get(artifacts_path)).json() == []
        assert (
            await auth_client.post(artifacts_path, params={"patch_sha256": "0" * 64}, **upload_png)
        ).status_code == 409
        assert (
            await auth_client.post(
                artifacts_path,
                params=artifact_params,
                content=b"<svg/>",
                headers={"Content-Type": "image/png"},
            )
        ).status_code == 422
        assert (
            await auth_client.post(
                artifacts_path,
                params=artifact_params,
                content=b" " * (64 * 1024 + 1),
                headers={"Content-Type": "application/json"},
            )
        ).status_code == 413
        png_upload = await auth_client.post(artifacts_path, params=artifact_params, **upload_png)
        assert png_upload.status_code == 200, png_upload.text
        screenshot = png_upload.json()
        assert screenshot["source"] == receipt["source"]
        assert screenshot["provenance"] == "uploaded"
        assert screenshot["byte_size"] == len(png)
        assert (
            await auth_client.post(artifacts_path, params=artifact_params, **upload_png)
        ).json() == screenshot
        screenshot_url = f"{artifacts_path}/{screenshot['digest']}"
        downloaded = await auth_client.get(screenshot_url)
        assert downloaded.content == png
        assert downloaded.headers["content-type"] == "image/png"
        assert downloaded.headers["cache-control"] == "no-store"
        assert downloaded.headers["x-content-type-options"] == "nosniff"
        with monkeypatch.context() as corrupt:
            corrupt.setattr(artifact_store, "get", lambda _key: png[:-1] + b"!")
            assert (await auth_client.get(screenshot_url)).status_code == 409
        metrics = {
            "method": "Median of three runs",
            "metrics": [{"label": "Latency", "unit": "ms", "baseline": 10, "observed": 20}],
        }
        repeated_metrics = {**metrics, "metrics": metrics["metrics"] * 2}
        assert (
            await auth_client.post(artifacts_path, params=artifact_params, json=repeated_metrics)
        ).status_code == 422
        measurement_upload = await auth_client.post(
            artifacts_path, params=artifact_params, json=metrics
        )
        assert measurement_upload.status_code == 200, measurement_upload.text
        measurement = measurement_upload.json()
        assert measurement["measurements"] == metrics
        assert {entry["digest"] for entry in (await auth_client.get(artifacts_path)).json()} == {
            measurement["digest"],
            screenshot["digest"],
        }
        stale_receipt = {
            **receipt,
            "source": {**receipt["source"], "patch_sha256": "0" * 64},
        }
        assert (await auth_client.post(reproduction_path, json=stale_receipt)).status_code == 409
        unknown_finding = f"{run_path}/findings/{'0' * 64}/reproductions"
        assert (await auth_client.post(unknown_finding, json=receipt)).status_code == 404
        too_large = await auth_client.post(
            reproduction_path,
            content=b" " * (192 * 1024 + 1),
            headers={"Content-Type": "application/json"},
        )
        assert too_large.status_code == 413
        attached = await auth_client.post(reproduction_path, json=receipt)
        assert attached.status_code == 200, attached.text
        evidence = attached.json()
        assert evidence["provenance"] == "uploaded" and evidence["receipt"] == receipt
        assert (await auth_client.post(reproduction_path, json=receipt)).json() == evidence
        assert (await auth_client.get(reproduction_path)).json() == [evidence]
        foreign_headers = {"X-Forwarded-Email": "bob"}
        assert (
            await auth_client.get(reproduction_path, headers=foreign_headers)
        ).status_code == 404
        assert (
            await auth_client.post(reproduction_path, json=receipt, headers=foreign_headers)
        ).status_code == 404
        review_store = StudioReviewStore(db_uri)
        for member_id in members:
            member = review_store.get(member_id, owner_user_id="alice")
            assert member is not None
            snapshot = review_store.get_report_snapshot(member)
            assert snapshot is not None
            assert set(json.loads(snapshot)) == {"state", "summary", "findings", "message_id"}
        save_assistant_message(next(iter(members)), "A later response from the reviewer.")
        assert (await auth_client.get(f"{run_path}/report")).json() == ready.json()
        assert (await auth_client.get(reproduction_path)).json() == [evidence]
        discussion_path = f"{run_path}/discussion"
        collaborators_path = f"{discussion_path}/collaborators"
        assert (await auth_client.get(discussion_path, headers=foreign_headers)).status_code == 404
        permissions.ensure_user("bob")
        grant = await auth_client.post(collaborators_path, json={"user_id": "bob"})
        assert grant.status_code == 204, grant.text
        shared = await auth_client.get(discussion_path, headers=foreign_headers)
        assert shared.status_code == 200, shared.text
        view = shared.json()
        assert view["collaborators"] is None and view["can_comment"] is True
        assert view["can_attach_evidence"] is False
        assert view["review_workspace_host_id"] is None
        owner_view = (await auth_client.get(discussion_path)).json()
        assert owner_view["can_attach_evidence"] is True
        assert owner_view["review_workspace_host_id"] == host_id
        assert (view["base_sha"], view["head_sha"], view["patch"]) == (
            base_sha,
            current_head,
            captured_patch,
        )
        assert view["findings"][0]["key"] == finding_key
        assert {"workspace", "host_id", "reviewer_session_id"}.isdisjoint(view)
        assert (await auth_client.get(run_path, headers=foreign_headers)).status_code == 404
        assert (await auth_client.get(screenshot_url, headers=foreign_headers)).content == png
        assert (
            await auth_client.post(
                artifacts_path, params=artifact_params, json=metrics, headers=foreign_headers
            )
        ).status_code == 404
        shared_evidence = (
            await auth_client.get(reproduction_path, headers=foreign_headers)
        ).json()
        assert shared_evidence[0]["receipt"]["head"] == evidence["receipt"]["head"]
        assert {"command", "source"}.isdisjoint(shared_evidence[0]["receipt"])
        assert (
            await auth_client.post(reproduction_path, json=receipt, headers=foreign_headers)
        ).status_code == 404
        checked_patch = "diff --git a/hello b/hello\n--- a/hello\n+++ b/hello\n"
        checked_patch += "@@ -1 +1 @@\n-after\n+fixed\n"
        fix = {
            **{key: value for key, value in receipt.items() if key not in {"base", "head"}},
            "schema_version": "omnigent.studio.fix-verification.v1",
            "proposed_patch_sha256": hashlib.sha256(checked_patch.encode()).hexdigest(),
            "included_paths": ["hello"],
            "before": receipt["head"],
            "after": {**side, "commit": "f" * 40},
            "outcome": "fail_to_pass",
            "patch": checked_patch,
        }
        for forged in (
            {**fix, "patch": checked_patch.replace("fixed", "changed")},
            {**fix, "outcome": "pass_to_pass"},
        ):
            assert (await auth_client.post(reproduction_path, json=forged)).status_code == 422
        wrong_revision = {**fix, "source": {**fix["source"], "patch_sha256": "0" * 64}}
        assert (await auth_client.post(reproduction_path, json=wrong_revision)).status_code == 409
        fix_attachment = await auth_client.post(reproduction_path, json=fix)
        assert fix_attachment.status_code == 200, fix_attachment.text
        assert fix_attachment.json()["receipt"] == fix
        assert (
            await auth_client.post(reproduction_path, json=fix)
        ).json() == fix_attachment.json()
        shared_checks = (await auth_client.get(reproduction_path, headers=foreign_headers)).json()
        shared_fix = next(item["receipt"] for item in shared_checks if "patch" in item["receipt"])
        assert shared_fix["patch"] == checked_patch and shared_fix["outcome"] == "fail_to_pass"
        assert {"command", "source"}.isdisjoint(shared_fix)
        message = {
            "id": uuid4().hex,
            "anchor": {"kind": "finding", "finding_key": finding_key},
            "body": "Can we reproduce this with an empty input?",
        }
        comment_path = f"{discussion_path}/comments"
        oversized = await auth_client.post(
            comment_path,
            content=b" " * (20 * 1024 + 1),
            headers={**foreign_headers, "Content-Type": "application/json"},
        )
        assert oversized.status_code == 413
        comment = await auth_client.post(comment_path, headers=foreign_headers, json=message)
        assert comment.status_code == 200, comment.text
        assert comment.json()["author"] == "bob"
        replay = await auth_client.post(comment_path, headers=foreign_headers, json=message)
        assert replay.json() == comment.json()
        conflict = await auth_client.post(
            comment_path, headers=foreign_headers, json={**message, "body": "Another message"}
        )
        assert conflict.status_code == 409
        line_message = {
            **message,
            "id": uuid4().hex,
            "anchor": {"kind": "diff", "path": "hello", "side": "head", "start": 1, "end": 1},
        }
        assert (await auth_client.post(comment_path, json=line_message)).status_code == 200
        invalid_anchor = {**line_message["anchor"], "end": 2}
        invalid_range = await auth_client.post(
            comment_path, json={**line_message, "id": uuid4().hex, "anchor": invalid_anchor}
        )
        assert invalid_range.status_code == 422
        assert {
            item["author"] for item in (await auth_client.get(discussion_path)).json()["comments"]
        } == {
            "alice",
            "bob",
        }
        cannot_share = await auth_client.post(
            collaborators_path, headers=foreign_headers, json={"user_id": "eve"}
        )
        assert cannot_share.status_code == 404
        assert (
            await auth_client.delete(collaborators_path, params={"user_id": "bob"})
        ).status_code == 204
        assert (await auth_client.get(screenshot_url, headers=foreign_headers)).status_code == 404
        assert (await auth_client.get(artifacts_path, headers=foreign_headers)).status_code == 404
        assert (await auth_client.get(discussion_path, headers=foreign_headers)).status_code == 404
        assert (
            await auth_client.get(reproduction_path, headers=foreign_headers)
        ).status_code == 404
        assert (
            await auth_client.post(comment_path, headers=foreign_headers, json=message)
        ).status_code == 404

        private_run = await auth_client.get(run_path, headers={"X-Forwarded-Email": "bob"})
        assert private_run.status_code == 404, private_run.text

        planned = await auth_client.post(
            runs_path,
            headers={"Idempotency-Key": "project-review-plan"},
            json={
                **roster,
                "coordinator": {**reviewer_input, "model": "planning-model"},
                "required_reviewer_indices": [0],
            },
        )
        assert planned.status_code == 200, planned.text
        planned_run = planned.json()
        planning_id = planned_run["coordinator"]["id"]
        planned_members = {entry["run_role"]: entry for entry in planned_run["reviewers"]}
        required, optional = planned_members["required"], planned_members["candidate"]
        plan_path = f"/v1/studio/review-runs/{planned_run['id']}"

        async def start_directly(member_id: str) -> int:
            started = await auth_client.post(
                f"/v1/studio/reviews/{member_id}/start",
                headers={"Idempotency-Key": f"manual-start-{member_id}"},
                json={},
            )
            return started.status_code

        assert await start_directly(required["id"]) == 409
        planning = await auth_client.post(f"{plan_path}/start", json={})
        assert planning.status_code == 200, planning.text
        assert planning.json()["errors"] == {}
        assert [frame.session_id for frame in launches[-1:]] == [planning_id]
        assert send_review.await_count == 4
        assert (await auth_client.post(f"{plan_path}/start", json={})).status_code == 200
        assert send_review.await_count == 4
        coordinator = conversations.get_conversation(planning_id)
        assert coordinator is not None and coordinator.project_id == expected_project_id
        proposal = {
            "coordinator_id": planning_id,
            "base_sha": base_sha,
            "head_sha": current_head,
            "decisions": [
                {"review_id": required["id"], "selected": False, "reason": "No coverage needed"},
                {"review_id": optional["id"], "selected": True, "reason": "Covers the change"},
            ],
        }

        def save_proposal() -> None:
            save_assistant_message(
                planning_id, "```studio-review-plan-v1\n" + json.dumps(proposal) + "\n```"
            )

        save_proposal()
        rejected = (await auth_client.get(f"{plan_path}/report")).json()
        assert rejected["proposal"]["state"] == "incomplete"
        assert rejected["proposal"]["error"] == "The plan must select every required reviewer"
        assert rejected["reviewers"] == []
        proposal["decisions"][0]["selected"] = True
        proposal["decisions"][1]["selected"] = False
        save_proposal()
        proposed = (await auth_client.get(f"{plan_path}/report")).json()["proposal"]
        assert proposed["state"] == "ready" and proposed["plan"] == proposal
        stale_approval = await auth_client.post(
            f"{plan_path}/start", json={"proposal_digest": "0" * 64}
        )
        assert stale_approval.status_code == 409
        current_head = "c" * 40
        stale_revision = await auth_client.post(
            f"{plan_path}/start", json={"proposal_digest": proposed["digest"]}
        )
        assert stale_revision.status_code == 409
        assert (await auth_client.get(plan_path)).json()["plan"] is None
        current_head = "b" * 40
        assert send_review.await_count == 4
        approval = await auth_client.post(
            f"{plan_path}/start", json={"proposal_digest": proposed["digest"]}
        )
        assert approval.status_code == 200, approval.text
        assert approval.json()["errors"] == {}
        assert approval.json()["run"]["plan"] == proposal
        assert [frame.session_id for frame in launches[-2:]] == [planning_id, required["id"]]
        assert await start_directly(optional["id"]) == 409
        assert conversations.get_conversation(optional["id"]) is None
        assert (
            await auth_client.post(
                f"{plan_path}/start", json={"proposal_digest": proposed["digest"]}
            )
        ).status_code == 200
        assert send_review.await_count == 5
        save_assistant_message(planning_id, "A later coordinator message.")
        assert (await auth_client.get(plan_path)).json()["plan"] == proposal
        report = (await auth_client.get(f"{plan_path}/report")).json()
        assert [entry["review_id"] for entry in report["reviewers"]] == [required["id"]]
        assert report["proposal"] is None

        # Bob may use the shared diff but his question runs under his own account.
        assert (
            await auth_client.post(collaborators_path, json={"user_id": "bob"})
        ).status_code == 204
        bob_host = uuid4().hex
        _register_online_host(auth_app, bob_host, "bob")
        auth_app.state.host_store.upsert_on_connect(
            bob_host, "Bob host", "bob", configured_harnesses={"codex-native": True}
        )
        questions_path = f"{discussion_path}/questions"
        question = {
            "id": uuid4().hex,
            "anchor": {"kind": "finding", "finding_key": finding_key},
            "body": "Which path accepts the invalid input?",
            "reviewer_key": view["reports"][0]["key"],
        }
        forbidden_host = await auth_client.post(
            questions_path,
            headers=foreign_headers,
            json={**question, "host_id": host_id, "workspace": workspace},
        )
        assert forbidden_host.status_code in (403, 404), forbidden_host.text
        assert send_review.await_count == 5
        request_body = {**question, "host_id": bob_host, "workspace": "/bob-checkout"}
        asked = await auth_client.post(questions_path, headers=foreign_headers, json=request_body)
        assert asked.status_code == 200, asked.text
        private_question = asked.json()
        assert private_question["status"] == "accepted", asked.text
        assert send_review.await_count == 6
        private_session_id = private_question["session_id"]
        private_session = conversations.get_conversation(private_session_id)
        assert private_session is not None
        assert (
            private_session.host_id,
            private_session.workspace,
            private_session.project_id,
        ) == (bob_host, "/bob-checkout", None)
        assert permissions.get("bob", private_session_id).level == LEVEL_OWNER
        assert permissions.get(RESERVED_USER_PUBLIC, private_session_id) is None
        assert (
            await auth_client.post(questions_path, headers=foreign_headers, json=request_body)
        ).json() == private_question
        assert send_review.await_count == 6
        private_path = f"{questions_path}/{question['id']}"
        assert (await auth_client.get(private_path)).status_code == 404
        assert (await auth_client.get(discussion_path)).json()["questions"] == []
        assert (
            await auth_client.post(f"{private_path}/share", headers=foreign_headers, json={})
        ).status_code == 409
        prompt = send_review.await_args.args[2].data["content"][0]["text"]
        assert base_sha in prompt and current_head in prompt and "Which path accepts" in prompt
        assert '"line": 1, "text": "after"' in prompt
        citations = [{"path": "hello", "side": "head", "line": 1, "quote": "after"}]
        response_body = {
            "question_id": question["id"],
            "base_sha": base_sha,
            "head_sha": current_head,
            "answer": "The changed condition at hello:1 admits the invalid case.",
            "citations": citations,
        }
        reply = "```studio-review-answer-v2\n" + json.dumps(response_body) + "\n```"

        def question_echo(text: str, session_id: str = private_session_id) -> None:
            conversations.append(
                session_id,
                [
                    NewConversationItem(
                        type="message",
                        response_id=uuid4().hex,
                        data=MessageData(
                            role="user", content=[{"type": "input_text", "text": text}]
                        ),
                    )
                ],
            )

        question_echo(prompt)
        save_assistant_message(private_session_id, reply)
        question_echo("Unrelated input after the captured question")
        different_answer = {**response_body, "answer": "The latest reply should not be shared."}
        save_assistant_message(
            private_session_id,
            "```studio-review-answer-v2\n" + json.dumps(different_answer) + "\n```",
        )
        observed = await auth_client.get(private_path, headers=foreign_headers)
        assert observed.json()["answer"] == response_body["answer"], observed.text
        assert observed.json()["citations"] == citations
        assert (await auth_client.get(discussion_path)).json()["agent_answers"] == []
        published = await auth_client.post(
            f"{private_path}/share", headers=foreign_headers, json={}
        )
        assert published.status_code == 200, published.text
        shared_answer = (await auth_client.get(discussion_path)).json()["agent_answers"][0]
        assert shared_answer["answer"] == response_body["answer"]
        assert shared_answer["citations"] == citations
        assert shared_answer["requested_by"] == "bob"
        assert "session_id" not in shared_answer
        assert (
            await auth_client.delete(collaborators_path, params={"user_id": "bob"})
        ).status_code == 204
        assert (await auth_client.get(private_path, headers=foreign_headers)).status_code == 404
        send_review.side_effect = RuntimeError("The input acknowledgement was lost")
        owner_question = {**question, "id": uuid4().hex}
        uncertain = await auth_client.post(questions_path, json=owner_question)
        assert uncertain.status_code == 200 and uncertain.json()["status"] == "unknown"
        owner_session = conversations.get_conversation(uncertain.json()["session_id"])
        assert owner_session is not None and owner_session.project_id == expected_project_id
        assert (
            await auth_client.post(questions_path, json=owner_question)
        ).json() == uncertain.json()
        assert send_review.await_count == 7
        send_review.side_effect = None
        proposal_request = {**owner_question, "id": uuid4().hex, "action": "propose_fix"}
        invalid_anchor = {
            **proposal_request,
            "anchor": {"kind": "diff", "path": "hello", "side": "head", "start": 1, "end": 1},
        }
        assert (await auth_client.post(questions_path, json=invalid_anchor)).status_code == 422
        proposed = await auth_client.post(questions_path, json=proposal_request)
        assert proposed.status_code == 200 and proposed.json()["status"] == "accepted"
        fix_session = proposed.json()["session_id"]
        proposal_prompt = send_review.await_args.args[2].data["content"][0]["text"]
        assert "studio-fix-proposal-v1" in proposal_prompt
        assert proposed.json()["action"] == "propose_fix"
        question_echo(proposal_prompt, fix_session)
        proposal_response = {
            **response_body,
            "question_id": proposal_request["id"],
            "answer": "Reject invalid input.",
            "proposed_patch": checked_patch,
        }
        save_assistant_message(
            fix_session, "```studio-fix-proposal-v1\n" + json.dumps(proposal_response) + "\n```"
        )
        fix_path = f"{questions_path}/{proposal_request['id']}"
        saved_proposal = (await auth_client.get(fix_path)).json()
        assert saved_proposal["proposed_patch"] == checked_patch
        assert saved_proposal["answer_state"] == "ready"
        assert (await auth_client.post(f"{fix_path}/share", json={})).status_code == 409
        assert (await auth_client.get(fix_path, headers=foreign_headers)).status_code == 404
        fix_digest = fix_attachment.json()["digest"]
        rereview_path = f"{reproduction_path}/{fix_digest}/re-review"
        assert (
            await auth_client.post(rereview_path, json={}, headers=foreign_headers)
        ).status_code == 404
        current_head = "c" * 40
        assert (await auth_client.post(rereview_path, json={})).status_code == 409
        assert send_review.await_count == 8
        current_head = "b" * 40
        rereviewed = await auth_client.post(rereview_path, json={})
        assert rereviewed.status_code == 200, rereviewed.text
        candidate = rereviewed.json()["run"]
        assert rereviewed.json()["errors"] == {}
        assert candidate["target"]["base_sha"] == current_head
        assert candidate["target"]["head_sha"] == fix["after"]["commit"]
        assert candidate["target"]["candidate"]["run_id"] == run["id"]
        assert candidate["target"]["candidate"]["attachment_digest"] == fix_digest
        candidate_members = candidate["reviewers"]
        assert len(candidate_members) == len(run["reviewers"])
        for delivery in send_review.await_args_list[-len(candidate_members) :]:
            candidate_prompt = delivery.args[2].data["content"][0]["text"]
            assert "This is a candidate re-review" in candidate_prompt
            assert "+fixed" in candidate_prompt and "-before" not in candidate_prompt
        assert (await auth_client.post(rereview_path, json={})).json()["run"]["id"] == candidate[
            "id"
        ]
        assert send_review.await_count == 8 + len(candidate_members)
        evidence = (await auth_client.get(reproduction_path)).json()
        assert (
            next(item for item in evidence if item["digest"] == fix_digest)["candidate_run_id"]
            == candidate["id"]
        )
        candidate_path = f"/v1/studio/review-runs/{candidate['id']}"
        assert (await auth_client.get(f"{candidate_path}/patch")).json()["patch"] == checked_patch
        candidate_view = (await auth_client.get(f"{candidate_path}/discussion")).json()
        assert candidate_view["candidate"]["finding_key"] == finding_key
        drafts_path = f"{discussion_path}/inline-drafts"
        owner_draft = {"id": uuid4().hex, "anchor": owner_question["anchor"], "body": "Draft."}
        assert (
            await auth_client.post(f"{candidate_path}/discussion/inline-drafts", json=owner_draft)
        ).status_code == 409
        assert (
            await auth_client.post(collaborators_path, json={"user_id": "bob"})
        ).status_code == 204
        created_draft = await auth_client.post(drafts_path, json=owner_draft)
        assert created_draft.status_code == 200, created_draft.text
        initial_draft = created_draft.json()
        assert initial_draft["anchor"] == {
            "kind": "diff",
            "path": "hello",
            "side": "head",
            "start": 1,
            "end": 1,
        }
        assert (await auth_client.post(drafts_path, json=owner_draft)).json() == initial_draft
        assert (await auth_client.get(discussion_path, headers=foreign_headers)).json()[
            "inline_drafts"
        ] == []
        bob_draft = {
            **owner_draft,
            "id": uuid4().hex,
            "anchor": {
                "kind": "diff",
                "path": "hello",
                "side": "base",
                "start": 1,
                "end": 1,
            },
        }
        assert (
            await auth_client.post(drafts_path, json=bob_draft, headers=foreign_headers)
        ).status_code == 200
        bad_anchor = {**bob_draft, "anchor": {**bob_draft["anchor"], "start": 2, "end": 2}}
        assert (
            await auth_client.post(drafts_path, json=bad_anchor, headers=foreign_headers)
        ).status_code == 422
        assert (await auth_client.get(discussion_path)).json()["inline_drafts"] == [initial_draft]
        draft_path = f"{drafts_path}/{initial_draft['id']}"
        update = {"version": 1, "body": "Suggest keeping the validation at this line."}
        assert (
            await auth_client.put(draft_path, json=update, headers=foreign_headers)
        ).status_code == 404
        edited = await auth_client.put(draft_path, json=update)
        assert edited.status_code == 200 and edited.json()["version"] == 2
        assert (await auth_client.put(draft_path, json=update)).json() == edited.json()
        assert (
            await auth_client.put(draft_path, json={**update, "body": "A stale overwrite."})
        ).status_code == 409
        for _ in range(2):
            assert (
                await auth_client.post(f"{draft_path}/discard", json={"version": 2})
            ).status_code == 204
        assert (await auth_client.get(discussion_path)).json()["inline_drafts"] == []
        assert (await auth_client.post(drafts_path, json=owner_draft)).status_code == 409
        left_draft = {**bob_draft, "id": uuid4().hex, "body": "Comment on the removed line."}
        right_draft = {**owner_draft, "id": uuid4().hex, "body": "Comment on the added line."}
        for draft in (left_draft, right_draft):
            assert (await auth_client.post(drafts_path, json=draft)).status_code == 200
        publication = f"{discussion_path}/inline-publication"
        selection = {
            "selected": [{"id": draft["id"], "version": 1} for draft in (left_draft, right_draft)]
        }
        assert (
            await auth_client.post(
                f"{publication}/preview", json=selection, headers=foreign_headers
            )
        ).status_code == 403
        preview = await auth_client.post(f"{publication}/preview", json=selection)
        assert preview.status_code == 200, preview.text
        preview = preview.json()
        assert preview["head_sha"] == current_head
        assert [draft["body"] for draft in preview["drafts"]] == [
            left_draft["body"],
            right_draft["body"],
        ]
        assert inline_writes == []
        confirm = {**selection, "confirm": True, "digest": preview["digest"]}
        assert (
            await auth_client.post(f"{publication}/confirm", json={**confirm, "confirm": 1})
        ).status_code == 422
        assert (
            await auth_client.post(f"{publication}/confirm", json={**confirm, "digest": "0" * 64})
        ).status_code == 409
        captured_head = current_head
        current_head = "c" * 40
        assert (await auth_client.post(f"{publication}/confirm", json=confirm)).status_code == 409
        current_head = captured_head
        assert inline_writes == []
        posted = await auth_client.post(f"{publication}/confirm", json=confirm)
        assert posted.status_code == 200, posted.text
        posted_drafts = posted.json()["drafts"]
        assert [draft["publication_state"] for draft in posted_drafts] == ["posted", "unknown"]
        assert posted_drafts[0]["provider_url"] == f"{pr_url}#discussion_r1"
        assert posted_drafts[1]["provider_url"] is None
        assert inline_writes == [
            {
                "repository": "nested/project",
                "pr_url": pr_url,
                "anchor": draft["anchor"],
                "body": draft["body"],
                "base_sha": base_sha,
                "head_sha": current_head,
            }
            for draft in preview["drafts"]
        ]
        assert (
            await auth_client.post(f"{publication}/confirm", json=confirm)
        ).json() == posted.json()
        assert len(inline_writes) == 2
        assert (
            await auth_client.put(f"{drafts_path}/{left_draft['id']}", json=update)
        ).status_code == 409
        return

    created = await create_reviewer(request, "alice", review, review.id)
    assert created.id == review.id
    assert created.harness == review.profile.harness
    assert created.runner_id is not None
    assert created.project_id == expected_project_id
    conv = conversations.get_conversation(review.id)
    assert conv is not None
    assert (conv.host_id, conv.workspace, conv.parent_conversation_id, conv.project_id) == (
        host_id,
        workspace,
        None,
        expected_project_id,
    )
    assert CREATED_BY_SESSION_LABEL_KEY not in conv.labels
    assert conv.git_branch is None
    assert conv.model_override == review.profile.model
    assert conv.reasoning_effort == review.profile.reasoning_effort
    owner = permissions.get("alice", conv.id)
    assert owner is not None and owner.level == LEVEL_OWNER
    assert permissions.get(RESERVED_USER_PUBLIC, conv.id) is None
    assert [frame.session_id for frame in launches] == [conv.id]
    assert announcements == [("alice", conv.id, expected_project_id)]
    if expected_project_name is not None:
        project_sessions = await auth_client.get(
            "/v1/sessions", params={"project": expected_project_name, "visibility": "mine"}
        )
        assert project_sessions.status_code == 200, project_sessions.text
        assert {row["id"] for row in project_sessions.json()["data"]} == {source["id"], conv.id}

    send_callbacks: list[routes_events.ReviewSend] = []
    routes_events.register_events_routes(
        APIRouter(),
        conversation_store=conversations,
        agent_store=agents,
        auth_provider=auth_provider,
        permission_store=permissions,
        register_review_send=send_callbacks.append,
    )
    wait_for_runner = AsyncMock(return_value=None)
    monkeypatch.setattr(routes_events, "_get_runner_client", AsyncMock(return_value=None))
    monkeypatch.setattr(routes_events, "_wait_for_runner_client", wait_for_runner)
    event = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "Review this diff."}]},
    )
    with pytest.raises(OmnigentError) as unavailable:
        await send_callbacks[0](request, conv.id, event, created.runner_id)
    assert unavailable.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert wait_for_runner.await_args.kwargs["runner_id"] == created.runner_id
    assert [frame.session_id for frame in launches] == [conv.id]


@pytest.mark.parametrize(
    "child_manager,move_before_recheck,policy_denied,source_viewers",
    [
        (False, False, False, False),
        (True, False, False, False),
        (False, True, False, False),
        (True, True, False, False),
        (False, False, True, False),
        (False, False, False, True),
        (True, False, False, True),
    ],
)
async def test_same_host_creation_uses_current_source_and_stays_private(
    auth_client,
    auth_app,
    peers,
    db_uri,
    monkeypatch,
    move_before_recheck,
    child_manager,
    policy_denied,
    source_viewers,
):
    from omnigent.host.frames import HostLaunchRunnerFrame, HostStatFrame, decode_host_frame
    from omnigent.server.routes.sessions import routes_core, routes_events
    from omnigent.server.sharing_settings import DefaultPublicSessions
    from tests.server.integration.test_sessions_permissions import _register_online_host

    source, _, captured = peers
    hosts = [uuid4().hex for _ in range(3)]
    for host_id, owner in zip(hosts, ("alice", "alice", "bob"), strict=True):
        _register_online_host(auth_app, host_id, owner)
        auth_app.state.host_store.upsert_on_connect(host_id, host_id, owner)
    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(source["id"], hosts[0], workspace="/original")
    if source_viewers:
        viewers = SqlAlchemyPermissionStore(db_uri)
        for viewer in ("bob", "__public__"):
            viewers.ensure_user(viewer)
            viewers.grant(viewer, source["id"], LEVEL_READ)
    manager = source
    if child_manager:
        response = await auth_client.post(
            "/v1/sessions",
            json={"agent_id": source["agent_id"], "parent_session_id": source["id"]},
        )
        assert response.status_code == 201, response.text
        manager = response.json()
        assert store.get_conversation(manager["id"]).host_id is None
    auth_app.state.default_public_sessions = lambda: DefaultPublicSessions.ALL
    original_check = routes_core.require_orchestration_source
    checks = 0

    def check_source(*args, **kwargs):
        nonlocal checks
        checks += 1
        if checks == 2 and move_before_recheck:
            store.set_host_id(source["id"], hosts[1], workspace="/moved")
        return original_check(*args, **kwargs)

    monkeypatch.setattr(routes_core, "require_orchestration_source", check_source)
    frames = []

    def answer_host(connection, payload):
        frame = decode_host_frame(payload)
        frames.append((connection.host_id, frame))
        if isinstance(frame, HostStatFrame):
            connection.pending_stats[frame.request_id].set_result(
                {"status": "ok", "exists": True, "type": "directory", "canonical_path": frame.path}
            )
        elif isinstance(frame, HostLaunchRunnerFrame):
            connection.pending_launches[frame.request_id].set_result({"status": "launched"})
        else:
            pytest.fail(f"Unexpected host frame: {frame}")

    monkeypatch.setattr(auth_app.state.host_registry, "send_text", answer_host)
    if policy_denied:

        async def deny_task(*args, **kwargs):
            return {"verdict": "deny", "reason": "Task needs approval"}

        monkeypatch.setattr(routes_events, "_evaluate_input_policy", deny_task)
        before = {s.id for s in store.list_conversations().data}
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps(
                    {
                        "agent_id": source["agent_id"],
                        "placement": "same_host",
                        "message": "Start",
                    }
                ),
                server_client=auth_client,
                conversation_id=manager["id"],
            )
        )
        assert result["status"] == "created_initial_message_failed", result
        assert "Task needs approval" in result["error"]
        assert result["initial_message"]["denied"] is True
        assert all(path == "/v1/sessions" for path, _ in captured.get("posts", []))
        created_id = result["conversation_id"]
        assert {s.id for s in store.list_conversations().data} - before == {created_id}
    else:
        response = await auth_client.post(
            "/v1/sessions/orchestrated",
            headers={ORCHESTRATION_SOURCE_HEADER: manager["id"]},
            json={"agent_id": source["agent_id"], "host_id": hosts[2], "workspace": "/requested"},
        )
        assert response.status_code == 201, response.text
        created_id = response.json()["id"]
    created = store.get_conversation(created_id)
    expected_host = hosts[1] if move_before_recheck else hosts[0]
    expected_workspace = "/moved" if move_before_recheck else "/original"
    assert checks == 2
    assert created.parent_conversation_id is None
    assert created.labels[CREATED_BY_SESSION_LABEL_KEY] == manager["id"]
    assert (created.host_id, created.workspace) == (expected_host, expected_workspace)
    assert frames and all(host == expected_host for host, _ in frames)
    launches = [frame for _, frame in frames if isinstance(frame, HostLaunchRunnerFrame)]
    assert len(launches) == 1 and launches[0].workspace == expected_workspace
    grants, _ = SqlAlchemyPermissionStore(db_uri).list_for_session(created.id)
    assert [grant.user_id for grant in grants] == ["alice"]

    assert CREATED_BY_SESSION_LABEL_KEY not in store.get_conversation(manager["id"]).labels
    # Discovery and label-write rules are independent of the routing variants above.
    if child_manager or move_before_recheck or policy_denied or source_viewers:
        return

    info = json.loads(
        await execute_tool(
            tool_name="sys_session_get_info",
            arguments=json.dumps({"session_id": created_id}),
            server_client=auth_client,
            conversation_id=manager["id"],
        )
    )
    assert info["created_by_session_id"] == manager["id"]
    listing = json.loads(
        await execute_tool(
            tool_name="sys_session_list",
            arguments="{}",
            server_client=auth_client,
            conversation_id=manager["id"],
        )
    )
    worker = next(row for row in listing["sessions"] if row["session_id"] == created_id)
    assert worker["created_by_session_id"] == manager["id"]
    assert worker["parent_session_id"] is None
    for value in ("", source["id"]):
        labels = {CREATED_BY_SESSION_LABEL_KEY: value}
        response = await auth_client.patch(f"/v1/sessions/{created_id}", json={"labels": labels})
        assert response.status_code == 400, response.text
        response = await auth_client.post(
            "/v1/sessions", json={"agent_id": source["agent_id"], "labels": labels}
        )
        assert response.status_code == 400, response.text
    response = await auth_client.patch(f"/v1/sessions/{created_id}", json={"labels": {}})
    assert response.status_code == 200, response.text
    assert (
        SqlAlchemyConversationStore(db_uri)
        .get_conversation(created_id)
        .labels[CREATED_BY_SESSION_LABEL_KEY]
        == manager["id"]
    )


@pytest.mark.parametrize("shape", ["parent", "multipart", "initial_items", "skill"])
async def test_independent_creation_rejects_child_or_bundle_shape(auth_client, peers, shape):
    from tests._helpers.session import post_session_bundle

    source, _, _ = peers
    headers = {ORCHESTRATION_SOURCE_HEADER: source["id"]}
    if shape == "parent":
        response = await auth_client.post(
            "/v1/sessions/orchestrated",
            headers=headers,
            json={"agent_id": source["agent_id"], "parent_session_id": source["id"]},
        )
        expected = "Orchestration creates independent sessions"
    elif shape in ("initial_items", "skill"):
        first = {
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "Unattributed task"}],
            },
        }
        payload = (
            {"initial_items": [first]}
            if shape == "initial_items"
            else {
                "initial_input": {
                    "type": "slash_command",
                    "data": {"kind": "skill", "name": "work"},
                }
            }
        )
        response = await auth_client.post(
            "/v1/sessions/orchestrated",
            headers=headers,
            json={"agent_id": source["agent_id"], "host_type": "managed", **payload},
        )
        expected = "Use initial_input" if shape == "initial_items" else "first task must be text"
    else:
        response = await post_session_bundle(
            auth_client.post, "/v1/sessions", b"unused", metadata={}, headers=headers
        )
        expected = "Independent sessions require agent_id"
    assert response.status_code == 400, response.text
    assert expected in response.text


@pytest.mark.parametrize("provider", ["modal", "daytona", "embedding"])
async def test_managed_creation_uses_default_provider_and_saves_private_first_task(
    auth_client, auth_app, peers, db_uri, monkeypatch, provider
):
    import asyncio
    from dataclasses import replace

    from omnigent.server.managed_hosts import parse_sandbox_config
    from omnigent.server.routes.sessions import routes_core
    from omnigent.server.sharing_settings import DefaultPublicSessions

    source, _, _ = peers
    deployment = parse_sandbox_config(
        {
            "server_url": "https://managed-test.example.com",
            "providers": [
                {"provider": "lakebox"},
                {"provider": "modal" if provider == "embedding" else provider},
            ],
        }
    )
    assert deployment is not None
    if provider == "embedding":
        deployment = replace(deployment, configs=(replace(deployment.default, provider=provider),))
    auth_app.state.sandbox_config = deployment
    auth_app.state.default_public_sessions = lambda: DefaultPublicSessions.ALL
    launched = asyncio.Event()
    release = asyncio.Event()
    captured = {}

    async def launch(**kwargs):
        captured.update(kwargs)
        launched.set()
        await release.wait()

    monkeypatch.setattr(routes_core, "_run_managed_launch", launch)
    try:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps(
                    {
                        "agent_id": source["agent_id"],
                        "placement": "managed",
                        "message": "First task",
                    }
                ),
                server_client=auth_client,
                conversation_id=source["id"],
            )
        )
        assert "error" not in result, result
        await asyncio.wait_for(launched.wait(), timeout=2)
        session_id = result["conversation_id"]
        assert captured["provider"] is None
        assert captured["sandbox_config"].for_provider(None).provider == provider
        assert captured["owner"] == "alice"
        store = SqlAlchemyConversationStore(db_uri)
        assert store.get_conversation(session_id).parent_conversation_id is None
        assert (
            store.get_conversation(session_id).labels[CREATED_BY_SESSION_LABEL_KEY] == source["id"]
        )
        assert "First task" in str(store.list_pending_inputs(session_id))
        assert f"[Message from session {source['id']}]" in str(
            store.list_pending_inputs(session_id)
        )
        grants, _ = SqlAlchemyPermissionStore(db_uri).list_for_session(session_id)
        assert [g.user_id for g in grants] == ["alice"]
    finally:
        release.set()
        await auth_app.state.startup_inputs.shutdown()


@pytest.mark.parametrize("configured", [False, True])
async def test_managed_creation_requires_launchable_provider(
    auth_client, auth_app, peers, monkeypatch, configured
):
    from dataclasses import replace

    from omnigent.server.routes.sessions import routes_core

    source, _, _ = peers
    deployment = auth_app.state.sandbox_config
    auth_app.state.sandbox_config = (
        replace(deployment, configs=(replace(deployment.default, managed_launch_supported=False),))
        if configured
        else None
    )

    async def unexpected_creation(*args, **kwargs):
        pytest.fail("Unavailable managed creation must not create a session")

    monkeypatch.setattr(routes_core, "_create_session_from_existing_agent", unexpected_creation)
    result = json.loads(
        await execute_tool(
            tool_name="sys_session_create",
            arguments=json.dumps({"agent_id": source["agent_id"], "placement": "managed"}),
            server_client=auth_client,
            conversation_id=source["id"],
        )
    )
    assert "Managed sandboxes are unavailable" in result["detail"]


async def test_orchestrated_endpoint_requires_source(auth_client, peers, monkeypatch):
    from omnigent.server.routes.sessions import routes_core

    source, _, _ = peers

    async def unexpected_creation(*args, **kwargs):
        pytest.fail("Missing source must not create a session")

    monkeypatch.setattr(routes_core, "_create_session_from_existing_agent", unexpected_creation)
    response = await auth_client.post(
        "/v1/sessions/orchestrated", json={"agent_id": source["agent_id"]}
    )
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("share_before_admission", [False, True])
async def test_startup_queue_admission_and_replay(
    auth_client, auth_app, peers, db_uri, monkeypatch, share_before_admission
):
    from omnigent.server.startup_inputs import STARTUP_LABEL

    source, target, captured = peers
    store = SqlAlchemyConversationStore(db_uri)
    store.set_labels(target["id"], {STARTUP_LABEL: "pending"})
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")
    startup = auth_app.state.startup_inputs
    accepts = startup.accepts

    async def check_startup(*args):
        result = await accepts(*args)
        assert result is True
        if share_before_admission:
            permissions.grant("bob", source["id"], LEVEL_EDIT)
        return result

    monkeypatch.setattr(startup, "accepts", check_startup)
    result = await _send(auth_client, source, target)
    pending = store.list_pending_inputs(target["id"])
    assert not captured.get("posts")
    if share_before_admission:
        assert "shared with edit access cannot" in result["detail"]
        assert pending == []
    else:
        assert result["queued"] is True
        assert len(pending) == 1
        permissions.grant("bob", source["id"], LEVEL_EDIT)
        await startup._deliver(auth_app, target["id"], pending[0])
        await startup._deliver(auth_app, target["id"], pending[0])
        delivered = [body for path, body in captured.get("posts", []) if path.endswith("/events")]
        assert len(delivered) == 1
        assert f"[Message from session {source['id']}]" in json.dumps(delivered)
        assert store.list_pending_inputs(target["id"]) == []
        assert (
            "shared with edit access cannot"
            in (await _send(auth_client, source, target))["detail"]
        )


@pytest.mark.parametrize("two_replica_stack", ["authenticated"], indirect=True)
@pytest.mark.parametrize("target_kind", ["root", "child", "side_chat"])
@pytest.mark.parametrize("legacy_host", [False, True])
async def test_peer_message_readdresses_across_replicas(
    two_replica_stack, db_uri, monkeypatch, target_kind, legacy_host
):
    import httpx

    from omnigent.cli_auth import OMNIGENT_SLICE_KEY_HEADER, open_server_client
    from omnigent.runner._entry import _RunnerDatabricksAuth
    from omnigent.server.routes import sessions
    from omnigent.server.routes._sessions.helpers import _get_runner_client_impl
    from tests.server.integration.test_sessions_permissions import _register_online_host

    stack = two_replica_stack
    source = await _create_session_as(stack.keyless_client, "", "alice")
    target = await _create_session_as(stack.tunnel_client, "", "alice")
    source_headers = {"X-Forwarded-Email": "alice", **_bind_runner(db_uri, source["id"])}
    _bind_runner(db_uri, target["id"])
    host_id = uuid4().hex
    _register_online_host(stack.tunnel_replica, host_id, "alice")
    stack.conv_store.set_host_id(target["id"], host_id, workspace="/worker")
    captured = _stub_late_runner_bind(monkeypatch)
    capture_client = sessions._get_runner_client

    async def resolve_runner(session_id, router, **kwargs):
        if router is stack.tunnel_replica.state.runner_router:
            return await capture_client(session_id, router, **kwargs)
        return await _get_runner_client_impl(session_id, router, **kwargs)

    monkeypatch.setattr(sessions, "_get_runner_client", resolve_runner)
    if target_kind == "child":
        response = await stack.tunnel_client.post(
            "/v1/sessions",
            headers={"X-Forwarded-Email": "alice"},
            json={"agent_id": target["agent_id"], "parent_session_id": target["id"]},
        )
        assert response.status_code == 201, response.text
        target = response.json()
    elif target_kind == "side_chat":
        side = await _create_session_as(stack.tunnel_client, "", "alice")
        _bind_runner(db_uri, side["id"])
        stack.conv_store.set_labels(
            side["id"], {"omnigent.side_chat": "1", "omnigent.side_chat.source_id": target["id"]}
        )
        target = side
    captured.clear()
    monkeypatch.setenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", "1")
    monkeypatch.setenv("OMNIGENT_RUNNER_SLICE_KEY", "sender-host")
    received = []
    transports = [
        httpx.ASGITransport(app=app) for app in (stack.keyless_replica, stack.tunnel_replica)
    ]

    async def ingress(request):
        key = request.headers.get(OMNIGENT_SLICE_KEY_HEADER)
        on_target = key is None if legacy_host else key == host_id
        response = await transports[int(on_target)].handle_async_request(request)
        await response.aread()
        received.append((request.method, request.url.path, key, response.status_code))
        return response

    async with open_server_client(
        "http://sharded",
        headers=source_headers,
        auth=_RunnerDatabricksAuth(lambda: "test-bearer", "http://sharded"),
        transport=httpx.MockTransport(ingress),
    ) as client:
        result = await _send(client, source, target)
        assert client.headers[OMNIGENT_SLICE_KEY_HEADER] == "sender-host"
    assert result.get("queued") is True, result
    posts = [(key, status) for method, path, key, status in received if method == "POST"]
    assert posts == (
        [("sender-host", 400), (host_id, 400), (None, 202)]
        if legacy_host
        else [("sender-host", 400), (host_id, 202)]
    )
    delivered = [body for path, body in captured.get("posts", []) if path.endswith("/events")]
    assert len(delivered) == 1
    assert f"[Message from session {source['id']}]" in json.dumps(delivered)


@pytest.mark.parametrize("two_replica_stack", ["managed"], indirect=True)
async def test_managed_create_is_addressed_before_provisioning(
    two_replica_stack, db_uri, monkeypatch
):
    import asyncio

    import httpx

    from omnigent.cli_auth import OMNIGENT_SLICE_KEY_HEADER, open_server_client
    from omnigent.runner._entry import _RunnerDatabricksAuth
    from omnigent.server.routes.sessions import routes_core

    stack = two_replica_stack
    source = await _create_session_as(stack.keyless_client, "", "alice")
    headers = {"X-Forwarded-Email": "alice", **_bind_runner(db_uri, source["id"])}
    monkeypatch.setenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", "1")
    monkeypatch.setenv("OMNIGENT_RUNNER_SLICE_KEY", "sender-host")
    launched = asyncio.Event()
    captured = {}

    async def launch(**kwargs):
        captured.update(kwargs)
        launched.set()

    monkeypatch.setattr(routes_core, "_run_managed_launch", launch)
    transports = [
        httpx.ASGITransport(app=app) for app in (stack.keyless_replica, stack.tunnel_replica)
    ]
    reserved = []

    async def ingress(request):
        key = request.headers.get(OMNIGENT_SLICE_KEY_HEADER)
        if request.method == "POST":
            body = json.loads(request.content)
            assert key == body["host_id"] and key != "sender-host"
            reserved.append(key)
        response = await transports[int(key != "sender-host")].handle_async_request(request)
        await response.aread()
        return response

    async with open_server_client(
        "http://sharded",
        headers=headers,
        auth=_RunnerDatabricksAuth(lambda: "test-bearer", "http://sharded"),
        transport=httpx.MockTransport(ingress),
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps(
                    {
                        "agent_id": source["agent_id"],
                        "placement": "managed",
                        "message": "First task",
                    }
                ),
                server_client=client,
                conversation_id=source["id"],
            )
        )
    assert "error" not in result, result
    await asyncio.wait_for(launched.wait(), timeout=2)
    assert reserved == [captured["host_id"]]
    assert captured["host_registry"] is stack.tunnel_replica.state.host_registry
    worker = stack.conv_store.get_conversation(result["conversation_id"])
    assert worker.labels["omnigent.sandbox.requested_host_id"] == reserved[0]
    pending = stack.conv_store.list_pending_inputs(worker.id)
    assert len(pending) == 1
    assert (
        pending[0].data.content[0]["text"] == f"[Message from session {source['id']}]\nFirst task"
    )
    await stack.tunnel_replica.state.startup_inputs.shutdown()


@pytest.mark.parametrize("two_replica_stack", ["authenticated"], indirect=True)
@pytest.mark.parametrize("child_manager", [False, True])
@pytest.mark.parametrize("routing", ["host", "keyless", "wrong_replica", "disconnect", "offline"])
async def test_same_host_creation_across_replicas(
    two_replica_stack, db_uri, monkeypatch, child_manager, routing
):
    import httpx

    from omnigent.cli_auth import OMNIGENT_SLICE_KEY_HEADER, open_server_client
    from omnigent.host.frames import HostLaunchRunnerFrame, HostStatFrame, decode_host_frame
    from omnigent.runner._entry import _RunnerDatabricksAuth
    from omnigent.server.routes import _host_launch
    from tests.server.integration.test_sessions_permissions import _register_online_host

    stack = two_replica_stack
    source = await _create_session_as(stack.tunnel_client, "", "alice")
    headers = {"X-Forwarded-Email": "alice", **_bind_runner(db_uri, source["id"])}
    host_id = uuid4().hex
    _register_online_host(stack.tunnel_replica, host_id, "alice")
    stack.conv_store.set_host_id(source["id"], host_id, workspace="/worker")
    manager = source
    if child_manager:
        response = await stack.tunnel_client.post(
            "/v1/sessions",
            headers=headers,
            json={"agent_id": source["agent_id"], "parent_session_id": source["id"]},
        )
        assert response.status_code == 201, response.text
        manager = response.json()
        assert stack.conv_store.get_conversation(manager["id"]).host_id is None
    before = {s.id for s in stack.conv_store.list_conversations().data}
    launches = []

    def answer_host(connection, payload):
        frame = decode_host_frame(payload)
        if isinstance(frame, HostStatFrame):
            connection.pending_stats[frame.request_id].set_result(
                {"status": "ok", "exists": True, "type": "directory", "canonical_path": frame.path}
            )
            if routing in {"disconnect", "offline"}:
                # Workspace validation succeeded; the tunnel vanishes before launch.
                monkeypatch.setattr(
                    stack.tunnel_replica.state.host_registry, "get", lambda _: None
                )
        elif isinstance(frame, HostLaunchRunnerFrame):
            launches.append(frame)
            connection.pending_launches[frame.request_id].set_result({"status": "launched"})
        else:
            pytest.fail(f"Unexpected host frame: {frame}")

    monkeypatch.setattr(stack.tunnel_replica.state.host_registry, "send_text", answer_host)
    monkeypatch.setattr(_host_launch, "_host_sharded", routing != "offline")
    monkeypatch.setenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", "0" if routing == "keyless" else "1")
    monkeypatch.setenv("OMNIGENT_RUNNER_SLICE_KEY", host_id)
    transports = [
        httpx.ASGITransport(app=app) for app in (stack.keyless_replica, stack.tunnel_replica)
    ]
    posts = []

    async def ingress(request):
        key = request.headers.get(OMNIGENT_SLICE_KEY_HEADER)
        on_target = key is None if routing in {"keyless", "wrong_replica"} else key == host_id
        response = await transports[int(on_target)].handle_async_request(request)
        await response.aread()
        if request.method == "POST":
            posts.append((key, response.status_code))
        return response

    async with open_server_client(
        "http://sharded",
        headers=headers,
        auth=_RunnerDatabricksAuth(lambda: "test-bearer", "http://sharded"),
        transport=httpx.MockTransport(ingress),
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps(
                    {
                        "agent_id": source["agent_id"],
                        "placement": "same_host",
                        "title": "Review worker",
                    }
                ),
                server_client=client,
                conversation_id=manager["id"],
            )
        )
        if routing in {"disconnect", "offline"}:
            listing = json.loads(
                await execute_tool(
                    tool_name="sys_session_list",
                    arguments="{}",
                    server_client=client,
                    conversation_id=manager["id"],
                )
            )
            workers = [s for s in listing["sessions"] if s["title"] == result["title"]]
            assert len(workers) == 1
            recovered = json.loads(
                await execute_tool(
                    tool_name="sys_session_get_info",
                    arguments=json.dumps({"session_id": workers[0]["session_id"]}),
                    server_client=client,
                    conversation_id=manager["id"],
                )
            )
            assert recovered["agent_id"] == result["agent_id"]
            assert recovered["host_id"] == host_id
    created = {s.id for s in stack.conv_store.list_conversations().data} - before
    assert len(posts) == 1
    if routing in {"wrong_replica", "disconnect", "offline"}:
        assert "do not automatically retry" in result["error"], result
        assert posts == [(host_id, 409 if routing == "offline" else 400)]
        assert len(created) == (0 if routing == "wrong_replica" else 1)
        assert launches == []
    else:
        assert "error" not in result, result
        assert posts == [(None if routing == "keyless" else host_id, 201)]
        assert created == {result["conversation_id"]}
        worker = stack.conv_store.get_conversation(result["conversation_id"])
        assert worker.parent_conversation_id is None
        assert worker.host_id == host_id and worker.workspace == "/worker"
        assert len(launches) == 1 and launches[0].session_id == worker.id
