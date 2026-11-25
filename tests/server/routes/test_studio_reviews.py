"""Review HTTP contracts with real persistence and a controlled PR resource boundary."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request

from omnigent.db.utils import builtin_agent_id
from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.host.frames import HostHelloFrame
from omnigent.native.native_coding_agents import CLAUDE_NATIVE_AGENT_NAME, CODEX_NATIVE_AGENT_NAME
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, LEVEL_READ, UnifiedAuthProvider
from omnigent.server.routes.studio_reviews import create_studio_reviews_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.stores.session_pull_request_store import PullRequestReport, SessionPullRequestStore
from omnigent.stores.studio_review_store import StudioReviewStore
from omnigent.studio.reviews.models import ReviewTarget

ALICE = "alice@example.test"
BOB = "bob@example.test"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


@dataclass
class ReviewHarness:
    store: StudioReviewStore
    conversations: SqlAlchemyConversationStore
    agents: SqlAlchemyAgentStore
    permissions: SqlAlchemyPermissionStore
    source_id: str
    agent_id: str
    target: ReviewTarget
    app: FastAPI | None = None
    capture_error: HTTPException | None = None
    captured: list[tuple[str, str | None]] = field(default_factory=list)
    read_patch: AsyncMock = field(default_factory=AsyncMock)
    create_reviewer: AsyncMock = field(default_factory=AsyncMock)
    send_review: AsyncMock = field(default_factory=AsyncMock)
    project_store: SqlAlchemyProjectStore | None = None


@pytest.fixture
def review_harness(
    runtime_init: None, db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> ReviewHarness:
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "0")
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    agents = SqlAlchemyAgentStore(db_uri)
    source_agent = agents.create(uuid.uuid4().hex, "Source agent", "source-bundle")
    reviewer = agents.create(uuid.uuid4().hex, "Reviewer", "reviewer-bundle")
    source = conversations.create_conversation(
        agent_id=source_agent.id,
        host_id=uuid.uuid4().hex,
        workspace="/workspace/project",
    )
    permissions.grant(ALICE, source.id, LEVEL_OWNER)
    store = StudioReviewStore(db_uri)
    assert source.host_id is not None and source.workspace is not None
    target = ReviewTarget(
        source_session_id=source.id,
        host_id=source.host_id,
        workspace=source.workspace,
        pr_url="https://github.com/example/project/pull/42",
        repository="example/project",
        pr_number=42,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )
    harness = ReviewHarness(
        store, conversations, agents, permissions, source.id, reviewer.id, target
    )
    harness.project_store = SqlAlchemyProjectStore(db_uri)

    async def capture_context(
        _request: Request, source_session_id: str, pr_url: str | None
    ) -> ReviewTarget:
        harness.captured.append((source_session_id, pr_url))
        if harness.capture_error is not None:
            raise harness.capture_error
        return harness.target

    auth = UnifiedAuthProvider(source="header")
    router = create_studio_reviews_router(
        store,
        conversations,
        agents,
        capture_context=capture_context,
        read_patch=harness.read_patch,
        create_reviewer=harness.create_reviewer,
        send_review=harness.send_review,
        auth_provider=auth,
        permission_store=permissions,
        project_store=harness.project_store,
    )
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    harness.app = create_app(
        agent_store=agents,
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conversations,
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        auth_provider=auth,
        permission_store=permissions,
        extra_routers=[(router, "/v1", ["studio reviews"])],
    )
    return harness


@pytest_asyncio.fixture
async def review_client(review_harness: ReviewHarness) -> AsyncIterator[httpx.AsyncClient]:
    assert review_harness.app is not None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=review_harness.app), base_url="http://test"
    ) as client:
        yield client


def _body(context: ReviewHarness, **overrides: Any) -> dict[str, Any]:
    return {
        "expected_base_sha": BASE_SHA,
        "expected_head_sha": HEAD_SHA,
        "agent_id": context.agent_id,
        "harness": "codex-native",
        "skill": "review",
        **overrides,
    }


async def _capture(
    client: httpx.AsyncClient,
    context: ReviewHarness,
    *,
    user: str = ALICE,
    key: str = "capture-one",
    **overrides: Any,
) -> httpx.Response:
    return await client.post(
        f"/v1/sessions/{context.source_id}/studio/reviews",
        headers={"X-Forwarded-Email": user, "Idempotency-Key": key},
        json=_body(context, **overrides),
    )


async def test_capture_freezes_context_and_reads_private_state(
    review_harness: ReviewHarness, review_client: httpx.AsyncClient
) -> None:
    response = await _capture(review_client, review_harness)
    assert response.status_code == 201
    result = response.json()
    review, operation = result["review"], result["operation"]
    assert result["created"] is True
    assert review["target"] == review_harness.target.model_dump(mode="json")
    assert review["profile"]["agent_id"] == review_harness.agent_id
    assert review["profile"]["agent_version"] == "1"
    assert review["state"] == "prepared"
    assert review["dispatch_status"] == "not_requested"
    assert review["reviewer_session_id"] is None
    assert operation["action"] == "review.capture"
    assert operation["state"] == "succeeded"
    headers = {"X-Forwarded-Email": ALICE}
    fetched = await review_client.get(f"/v1/studio/reviews/{review['id']}", headers=headers)
    assert fetched.json() == review
    listed = await review_client.get(
        f"/v1/sessions/{review_harness.source_id}/studio/reviews", headers=headers
    )
    assert listed.json()["data"] == [review]
    op = await review_client.get(
        f"/v1/studio/reviews/{review['id']}/operations/{operation['id']}", headers=headers
    )
    assert op.json() == operation
    events = await review_client.get(f"/v1/studio/reviews/{review['id']}/events", headers=headers)
    assert len(events.json()["data"]) == 1
    sequence = events.json()["data"][0]["sequence"]
    after = await review_client.get(
        f"/v1/studio/reviews/{review['id']}/events?after_sequence={sequence}", headers=headers
    )
    assert after.json()["data"] == []


async def test_exact_replay_keeps_capture_when_remote_and_agent_change(
    review_harness: ReviewHarness, review_client: httpx.AsyncClient
) -> None:
    first = (await _capture(review_client, review_harness)).json()
    review_harness.capture_error = HTTPException(status_code=503, detail="Unavailable")
    review_harness.agents.update(review_harness.agent_id, "new-bundle")
    replay = await _capture(review_client, review_harness)
    assert replay.status_code == 200
    assert replay.json() == {**first, "created": False}
    assert len(review_harness.captured) == 1
    assert replay.json()["review"]["profile"]["agent_version"] == "1"


async def test_conflicting_idempotency_intent_does_not_refetch(
    review_harness: ReviewHarness, review_client: httpx.AsyncClient
) -> None:
    assert (await _capture(review_client, review_harness)).status_code == 201
    changed = await _capture(review_client, review_harness, skill="another-review")
    assert changed.status_code == 409
    assert len(review_harness.captured) == 1


@pytest.mark.parametrize("level", [LEVEL_READ, LEVEL_EDIT])
async def test_shared_source_access_does_not_grant_review_access(
    review_harness: ReviewHarness, review_client: httpx.AsyncClient, level: int
) -> None:
    review_harness.permissions.grant(BOB, review_harness.source_id, level)
    result = (await _capture(review_client, review_harness)).json()
    review_id, operation_id = result["review"]["id"], result["operation"]["id"]
    assert (await _capture(review_client, review_harness, user=BOB)).status_code == 404
    for path in (
        f"/v1/sessions/{review_harness.source_id}/studio/reviews",
        f"/v1/sessions/{review_harness.source_id}/studio/review-profiles",
        f"/v1/studio/reviews/{review_id}",
        f"/v1/studio/reviews/{review_id}/operations/{operation_id}",
        f"/v1/studio/reviews/{review_id}/events",
        f"/v1/studio/reviews/{review_id}/report",
    ):
        assert (
            await review_client.get(path, headers={"X-Forwarded-Email": BOB})
        ).status_code == 404
    response = await review_client.post(
        f"/v1/studio/reviews/{review_id}/start",
        headers={"X-Forwarded-Email": BOB, "Idempotency-Key": "start"},
        json={},
    )
    assert response.status_code == 404
    assert len(review_harness.captured) == 1


async def test_admin_cannot_capture_another_owners_source(
    review_harness: ReviewHarness, review_client: httpx.AsyncClient
) -> None:
    review_harness.permissions.ensure_user(BOB, is_admin=True)
    assert (await _capture(review_client, review_harness, user=BOB)).status_code == 404
    assert review_harness.captured == []


async def test_replay_rechecks_source_ownership(
    review_harness: ReviewHarness, review_client: httpx.AsyncClient
) -> None:
    result = (await _capture(review_client, review_harness)).json()
    review_harness.permissions.grant(ALICE, review_harness.source_id, LEVEL_READ)
    assert (await _capture(review_client, review_harness)).status_code == 404
    assert len(review_harness.captured) == 1
    # Historical review evidence stays private to its initiating owner.
    response = await review_client.get(
        f"/v1/studio/reviews/{result['review']['id']}", headers={"X-Forwarded-Email": ALICE}
    )
    assert response.status_code == 200
    assert (
        await review_client.post(
            f"/v1/studio/reviews/{result['review']['id']}/start",
            headers={"X-Forwarded-Email": ALICE, "Idempotency-Key": "start"},
            json={},
        )
    ).status_code == 404


@pytest.mark.parametrize("field", ["base_sha", "head_sha"])
async def test_changed_revision_creates_no_pass(
    review_harness: ReviewHarness, review_client: httpx.AsyncClient, field: str
) -> None:
    review_harness.target = review_harness.target.model_copy(update={field: "c" * 40})
    assert (await _capture(review_client, review_harness)).status_code == 409
    assert (
        review_harness.store.find_operation(
            owner_user_id=ALICE, action="review.capture", idempotency_key="capture-one"
        )
        is None
    )


async def test_unavailable_capture_can_retry_without_phantom_execution(
    review_harness: ReviewHarness, review_client: httpx.AsyncClient
) -> None:
    review_harness.capture_error = HTTPException(status_code=503, detail="Host offline")
    assert (await _capture(review_client, review_harness)).status_code == 503
    assert (
        review_harness.store.find_operation(
            owner_user_id=ALICE, action="review.capture", idempotency_key="capture-one"
        )
        is None
    )
    review_harness.capture_error = None
    response = await _capture(review_client, review_harness)
    assert response.status_code == 201
    assert response.json()["review"]["dispatch_status"] == "not_requested"


async def test_capture_rejects_context_for_another_session(
    review_harness: ReviewHarness, review_client: httpx.AsyncClient
) -> None:
    review_harness.target = review_harness.target.model_copy(
        update={"source_session_id": uuid.uuid4().hex}
    )
    assert (await _capture(review_client, review_harness)).status_code == 503


async def test_reviewer_agent_must_be_visible(
    review_harness: ReviewHarness, review_client: httpx.AsyncClient
) -> None:
    hidden = review_harness.agents.create_user_agent(
        uuid.uuid4().hex, "Private agent", "private-bundle", BOB
    )
    assert (await _capture(review_client, review_harness, agent_id=hidden.id)).status_code == 404
    assert review_harness.captured == []


async def test_fresh_store_recovers_same_review(
    review_harness: ReviewHarness, review_client: httpx.AsyncClient, db_uri: str
) -> None:
    original = (await _capture(review_client, review_harness)).json()["review"]
    recovered = StudioReviewStore(db_uri).get(original["id"], owner_user_id=ALICE)
    assert recovered is not None
    assert recovered.model_dump(mode="json") == original


@pytest.mark.parametrize(
    "change",
    [
        {"owner_user_id": BOB},
        {"host_id": "forged-host"},
        {"workspace": "/other"},
        {"expected_head_sha": "short"},
        {"agent_id": "not-a-uuid"},
        {"harness": "other-native"},
        {"skill": "   "},
        {"skill": None},
        {"profile_id": "backend"},
        {"profile_digest": "a" * 64},
        {"profile_id": "../backend", "profile_digest": "a" * 64},
        {"profile_id": "backend", "profile_digest": "bad"},
        {"pr_url": "https://user:secret@github.com/example/project/pull/42"},
        {"pr_url": "https://github.com/example/project/pull/42?token=secret"},
    ],
)
async def test_invalid_or_forged_capture_fields_are_rejected(
    review_harness: ReviewHarness, review_client: httpx.AsyncClient, change: dict[str, Any]
) -> None:
    assert (await _capture(review_client, review_harness, **change)).status_code == 422
    assert review_harness.captured == []


async def test_missing_idempotency_key_and_login_are_rejected(
    review_harness: ReviewHarness, review_client: httpx.AsyncClient
) -> None:
    path = f"/v1/sessions/{review_harness.source_id}/studio/reviews"
    no_key = await review_client.post(
        path, headers={"X-Forwarded-Email": ALICE}, json=_body(review_harness)
    )
    assert no_key.status_code == 422
    no_login = await review_client.post(
        path, headers={"Idempotency-Key": "no-login"}, json=_body(review_harness)
    )
    assert no_login.status_code == 401
    assert review_harness.captured == []


async def test_model_metadata_uses_existing_validation(
    review_harness: ReviewHarness, review_client: httpx.AsyncClient
) -> None:
    response = await _capture(review_client, review_harness, model="--shell-shaped")
    assert response.status_code == 400
    assert review_harness.captured == []


async def test_legacy_capture_fingerprint_still_replays(
    review_harness: ReviewHarness, review_client: httpx.AsyncClient
) -> None:
    prior_request = {
        "action": "review.capture",
        "source_session_id": review_harness.source_id,
        **_body(review_harness),
        "model": None,
        "reasoning_effort": None,
        "pr_url": None,
    }
    prior_digest = hashlib.sha256(
        json.dumps(prior_request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    response = await _capture(review_client, review_harness)
    assert response.status_code == 201
    assert response.json()["operation"]["request_hash"] == prior_digest


async def test_role_catalog_and_capture_use_bound_owner_workspace(
    review_harness: ReviewHarness,
    review_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = review_harness.app
    assert app is not None
    target = review_harness.target
    host_store = HostStore(db_uri)
    app.state.host_store = host_store
    host_store.upsert_on_connect(
        target.host_id, "Review host", ALICE, configured_harnesses={"codex-native": True}
    )
    connection = app.state.host_registry.register(
        target.host_id,
        Mock(),
        HostHelloFrame(version="0.1.0", frame_protocol_version=1, name="Review host"),
        owner=ALICE,
    )
    role = {"id": "backend", "label": "Backend review", "description": "", "digest": "1" * 64}
    calls = []
    digest = role["digest"]

    async def read_from_host(**kwargs):
        assert kwargs["host_conn"] is connection
        assert kwargs["op"] == "studio_review_profiles"
        assert kwargs["session_id"] == target.source_session_id
        assert kwargs["workspace"] == target.workspace
        calls.append(kwargs["params"])
        if kwargs["params"]:
            assert kwargs["params"] == {"profile_id": role["id"]}
            return {**role, "digest": digest, "instructions": "Check service boundaries."}
        return {"profiles": [role]}

    monkeypatch.setattr(
        "omnigent.server.routes.studio_reviews.read_workspace_from_host", read_from_host
    )
    headers = {"X-Forwarded-Email": ALICE}
    path = f"/v1/sessions/{target.source_session_id}/studio/review-profiles"
    catalog = await review_client.get(path, headers=headers, params={"workspace": "/other"})
    assert catalog.status_code == 200, catalog.text
    assert catalog.json() == {"profiles": [role]}
    assert catalog.headers["Cache-Control"] == "no-store"

    reviewer = review_harness.agents.create(
        builtin_agent_id(CODEX_NATIVE_AGENT_NAME), CODEX_NATIVE_AGENT_NAME, "reviewer-bundle"
    )
    choices = {
        "agent_id": reviewer.id,
        "profile_id": role["id"],
        "profile_digest": role["digest"],
        "skill": None,
        "model": "review-model",
        "reasoning_effort": "high",
    }
    stale = await _capture(
        review_client, review_harness, **{**choices, "profile_digest": "2" * 64}
    )
    assert stale.status_code == 409, stale.text
    captured = await _capture(review_client, review_harness, **choices)
    assert captured.status_code == 201, captured.text
    review = captured.json()["review"]
    assert review["profile"]["role"] == role
    assert review["profile"]["skill"] is None
    assert review["profile"]["model"] == "review-model"
    assert review["profile"]["reasoning_effort"] == "high"
    assert calls == [{}, {"profile_id": "backend"}, {"profile_id": "backend"}]

    digest = "3" * 64
    replay = await _capture(review_client, review_harness, **choices)
    assert replay.status_code == 200
    assert replay.json()["review"] == review
    start = await review_client.post(
        f"/v1/studio/reviews/{review['id']}/start",
        headers={**headers, "Idempotency-Key": "start-role"},
        json={},
    )
    assert start.status_code == 409, start.text
    assert "profile changed" in start.json()["detail"]
    assert calls[-1] == {"profile_id": "backend"}
    review_harness.read_patch.assert_not_awaited()
    review_harness.create_reviewer.assert_not_awaited()


async def test_source_identifier_is_canonicalized(
    review_harness: ReviewHarness, review_client: httpx.AsyncClient
) -> None:
    dashed = str(uuid.UUID(review_harness.source_id))
    response = await review_client.post(
        f"/v1/sessions/{dashed}/studio/reviews",
        headers={"X-Forwarded-Email": ALICE, "Idempotency-Key": "capture-one"},
        json=_body(review_harness),
    )
    assert response.status_code == 201
    assert response.json()["review"]["target"]["source_session_id"] == review_harness.source_id


async def test_coordinator_approval_checks_revision_and_dispatches_only_selected_reviewers(
    review_harness: ReviewHarness,
    review_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = review_harness
    target = harness.target
    assert harness.app is not None and harness.project_store is not None
    project = harness.project_store.create(uuid.uuid4().hex, "Review project", ALICE, {})
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(
        target.host_id,
        "Review host",
        ALICE,
        configured_harnesses={"codex-native": True, "claude-native": True},
    )
    harness.app.state.host_store = hosts
    harness.app.state.host_registry.register(
        target.host_id,
        Mock(),
        HostHelloFrame(version="0.1.0", frame_protocol_version=1, name="Review host"),
        owner=ALICE,
    )
    monkeypatch.setattr(
        "omnigent.server.routes._studio_project_workspace.validate_workspace",
        AsyncMock(return_value=target.workspace),
    )
    codex, claude = [
        harness.agents.create(builtin_agent_id(name), name, "reviewer-bundle")
        for name in (CODEX_NATIVE_AGENT_NAME, CLAUDE_NATIVE_AGENT_NAME)
    ]
    provider_head = HEAD_SHA
    isolated = False
    local_head_available = False
    patch = (
        "diff --git a/fixtures.txt b/fixtures.txt\n--- /dev/null\n+++ b/fixtures.txt\n"
        "@@ -0,0 +1,2000 @@\n"
        + ("+" + "x" * 98 + "\n")
        * 2000
        + "diff --git a/check.py b/check.py\n--- a/check.py\n+++ b/check.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    profile_digest = "3" * 64

    async def host_read(*, op, params, **_):
        if op == "studio_review_profiles":
            profile = params["profile_id"]
            return {
                "id": profile,
                "label": profile.title(),
                "description": "",
                "digest": profile_digest,
                "instructions": f"Inspect the {profile} scope.",
            }
        if op == "studio_project_pull_request":
            return {
                "pr": {
                    "url": target.pr_url,
                    "number": target.pr_number,
                    "base_sha": BASE_SHA,
                    "head_sha": provider_head,
                }
            }
        if op == "studio_project_pull_request_local_head":
            return {"available": local_head_available}
        assert op == "studio_project_pull_request_patch"
        return {
            "object": "session.github.pr_diff",
            "base_sha": BASE_SHA,
            "head_sha": provider_head,
            "patch": patch,
        }

    monkeypatch.setattr(
        "omnigent.server.routes.studio_reviews.read_workspace_from_host", host_read
    )

    async def create_reviewer(_request, owner, review, session_id):
        session = harness.conversations.create_conversation(
            conversation_id=session_id,
            agent_id=review.profile.agent_id,
            host_id=target.host_id,
            workspace=f"/review-worktrees/{session_id}" if isolated else target.workspace,
            git_branch=f"studio-review/{session_id}" if isolated else None,
            project_id=project.id,
            runner_id=uuid.uuid4().hex,
        )
        harness.permissions.grant(owner, session.id, LEVEL_OWNER)
        return SimpleNamespace(
            id=session.id,
            runner_id=session.runner_id,
            agent_id=session.agent_id,
            host_id=session.host_id,
            workspace=session.workspace,
            harness=review.profile.harness,
        )

    harness.create_reviewer.side_effect = create_reviewer
    harness.send_review.return_value = {"queued": True, "pending_id": "accepted"}

    def selection(profile, agent, native_harness):
        return _body(
            harness,
            host_id=target.host_id,
            workspace=target.workspace,
            repository=".",
            pr_url=target.pr_url,
            agent_id=agent.id,
            harness=native_harness,
            skill=None,
            profile_id=profile,
            profile_digest=profile_digest,
        )

    headers = {"X-Forwarded-Email": ALICE, "Idempotency-Key": "coordinated-run"}
    captured = await review_client.post(
        f"/v1/projects/{project.id}/studio/review-runs",
        headers=headers,
        json={
            "coordinator": selection("coordinator", codex, "codex-native"),
            "reviewers": [
                selection("backend", codex, "codex-native"),
                selection("security", claude, "claude-native"),
                selection("frontend", codex, "codex-native"),
            ],
            "required_reviewer_indices": [0],
        },
    )
    assert captured.is_success, captured.text
    run = captured.json()
    path = f"/v1/studio/review-runs/{run['id']}"
    saved = await review_client.get(f"{path}/patch", headers=headers)
    assert saved.is_success and saved.json()["patch"] == patch
    started = await review_client.post(f"{path}/start", headers=headers, json={})
    assert started.is_success and started.json()["errors"] == {}, started.text
    coordinator = started.json()["run"]["coordinator"]
    assert harness.send_review.await_count == 1
    planning_prompt = harness.send_review.await_args.args[2].data["content"][0]["text"]
    assert "linked Linear issue" in planning_prompt
    assert "select the applicable specialists" in planning_prompt
    assert "untrusted_patch" not in planning_prompt and "fixtures.txt" not in planning_prompt
    assert all(item["reviewer_session_id"] is None for item in started.json()["run"]["reviewers"])

    def append_reply(session_id, fence, data):
        harness.conversations.append(
            session_id,
            [
                NewConversationItem(
                    type="message",
                    response_id=uuid.uuid4().hex,
                    data=MessageData(
                        role="assistant",
                        agent="scripted-native-reviewer",
                        content=[
                            {"type": "output_text", "text": f"```{fence}\n{json.dumps(data)}\n```"}
                        ],
                    ),
                )
            ],
        )

    decisions = [
        {
            "review_id": member["id"],
            "selected": member["profile"]["role"]["id"] != "frontend",
            "reason": "No frontend changes"
            if member["profile"]["role"]["id"] == "frontend"
            else "Backend scope",
            **(
                {
                    "assignment": {
                        "objective": "Investigate the "
                        f"{member['profile']['role']['id']} boundary.",
                        "acceptance_criterion": "Reject unauthorized requests.",
                        "expected_evidence": "Cite the guard and its regression test.",
                    }
                }
                if member["profile"]["role"]["id"] != "frontend"
                else {"assignment": None}
            ),
        }
        for member in run["reviewers"]
    ]
    revision = {"base_sha": BASE_SHA, "head_sha": HEAD_SHA}
    append_reply(
        coordinator["reviewer_session_id"],
        "studio-review-plan-v2",
        {"coordinator_id": coordinator["id"], **revision, "decisions": decisions},
    )
    plan = (await review_client.get(f"{path}/report", headers=headers)).json()["proposal"]
    assert plan["state"] == "ready", plan
    assert plan["plan"]["decisions"] == decisions

    provider_head = "c" * 40
    approval = {"proposal_digest": plan["digest"]}
    stale = await review_client.post(f"{path}/start", headers=headers, json=approval)
    assert stale.status_code == 409
    assert (await review_client.get(path, headers=headers)).json()["plan"] is None
    assert harness.send_review.await_count == 1

    provider_head = HEAD_SHA
    approved = await review_client.post(f"{path}/start", headers=headers, json=approval)
    assert approved.is_success and approved.json()["errors"] == {}, approved.text
    members = approved.json()["run"]["reviewers"]
    selected = [member for member in members if member["reviewer_session_id"] is not None]
    assert {member["profile"]["role"]["id"] for member in selected} == {"backend", "security"}
    assert {member["profile"]["harness"] for member in selected} == {
        "codex-native",
        "claude-native",
    }
    assert approved.json()["run"]["plan"]["decisions"] == decisions
    assert harness.send_review.await_count == 3
    for call in harness.send_review.await_args_list[1:]:
        reviewer_prompt = call.args[2].data["content"][0]["text"]
        assert "linked Linear issue" in reviewer_prompt
        assert "Trace changed code through callers" in reviewer_prompt
        assert "existing isolated environment" in reviewer_prompt
        assert "untrusted_patch" not in reviewer_prompt and "fixtures.txt" not in reviewer_prompt
        member = next(
            member for member in selected if member["reviewer_session_id"] == call.args[1]
        )
        role = member["profile"]["role"]["id"]
        other_role = "security" if role == "backend" else "backend"
        assert f"Investigate the {role} boundary." in reviewer_prompt
        assert f"Investigate the {other_role} boundary." not in reviewer_prompt
        assert "Cite the guard and its regression test." in reviewer_prompt
    assert (await review_client.post(f"{path}/start", headers=headers, json=approval)).is_success
    assert harness.send_review.await_count == 3

    finding = {
        "title": "Unexpected behavior",
        "detail": "The changed line no longer checks the documented precondition.",
        "severity": "high",
        "path": "check.py",
        "side": "head",
        "line": 1,
    }
    for member in selected:
        append_reply(
            member["reviewer_session_id"],
            "studio-review-v1",
            {
                "review_id": member["id"],
                **revision,
                "summary": "Checked the change",
                "findings": [finding],
            },
        )
    report = (await review_client.get(f"{path}/report", headers=headers)).json()
    assert [row["report"]["state"] for row in report["reviewers"]] == ["ready", "ready"]
    assert len(report["findings"]) == 1
    assert set(report["findings"][0]["review_ids"]) == {member["id"] for member in selected}

    drafts = f"{path}/discussion/inline-drafts/from-report"
    for created in (1, 0):
        prepared = await review_client.post(drafts, headers=headers, json={})
        assert prepared.is_success, prepared.text
        assert prepared.json() == {"state": "prepared", "eligible": 1, "created": created}
    discussion = (await review_client.get(f"{path}/discussion", headers=headers)).json()
    (draft,) = discussion["inline_drafts"]
    assert draft["publication_state"] == "draft"
    assert draft["anchor"] == {
        "kind": "diff",
        "path": "check.py",
        "side": "head",
        "start": 1,
        "end": 1,
    }

    # The task reviewer can retry after fetching a missing head without creating
    # a second run or losing the verdict for the prior revision.
    isolated = True
    harness.project_store.update(project.id, user_id=ALICE, config={"source_key": "linear:w:i"})
    provider_head = "c" * 40
    member = selection("backend", codex, "codex-native")
    member["expected_head_sha"] = provider_head
    capture_path = f"/v1/projects/{project.id}/studio/review-runs"
    first = await review_client.post(
        capture_path,
        headers={**headers, "Idempotency-Key": "task-new-head"},
        json={"reviewers": [member]},
    )
    assert first.is_success, first.text
    task_path = f"/v1/studio/review-runs/{first.json()['id']}"
    blocked = await review_client.post(f"{task_path}/start", headers=headers, json={})
    assert "missing locally" in next(iter(blocked.json()["errors"].values()))
    assert blocked.json()["run"]["reviewers"][0]["dispatch_status"] == "not_requested"
    assert harness.send_review.await_count == 3
    local_head_available = True
    replay = await review_client.post(
        capture_path,
        headers={**headers, "Idempotency-Key": "task-retry"},
        json={"reviewers": [member]},
    )
    assert replay.json()["id"] == first.json()["id"]
    task_started = await review_client.post(f"{task_path}/start", headers=headers, json={})
    assert task_started.is_success and task_started.json()["errors"] == {}
    assert harness.send_review.await_count == 4
    reviewer = task_started.json()["run"]["reviewers"][0]
    append_reply(
        reviewer["reviewer_session_id"],
        "studio-review-v1",
        {
            "review_id": reviewer["id"],
            "base_sha": BASE_SHA,
            "head_sha": provider_head,
            "summary": "The fix restored the precondition.",
            "findings": [],
        },
    )
    fixed = (await review_client.get(f"{task_path}/report", headers=headers)).json()
    assert fixed["reviewers"][0]["report"]["state"] == "ready"
    assert fixed["findings"] == []
    assert (await review_client.get(f"{path}/report", headers=headers)).json()["findings"]


@pytest.mark.parametrize("synced", [True, False], ids=["associated", "not-yet-synced"])
async def test_production_app_captures_through_associated_pr_resource(
    review_harness: ReviewHarness,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synced: bool,
) -> None:
    """Exercise app -> sessions -> strict capture -> runner, without an agent process."""
    target = review_harness.target
    associations = SessionPullRequestStore(db_uri)
    associations.apply_report(
        review_harness.source_id,
        [
            PullRequestReport(
                url=target.pr_url,
                provider="github",
                host="github.com",
                repository=target.repository,
                number=target.pr_number,
                relationship="attached",
                first_seen_at=100,
                last_seen_at=100,
            )
        ],
        complete=synced,
    )
    calls: list[tuple[str, dict[str, Any] | None]] = []

    class RunnerClient:
        async def get(
            self, path: str, *, params: dict[str, Any] | None, timeout: float
        ) -> httpx.Response:
            calls.append((path, params))
            return httpx.Response(
                200,
                json={
                    "available": True,
                    "authenticated": True,
                    "repo": {"name_with_owner": target.repository},
                    "pr": {
                        "url": target.pr_url,
                        "number": target.pr_number,
                        "base_sha": target.base_sha,
                        "head_sha": target.head_sha,
                    },
                },
            )

    async def get_runner(session_id: str, *, conversation: Any) -> RunnerClient:
        assert session_id == review_harness.source_id
        assert conversation.host_id == target.host_id
        assert conversation.workspace == target.workspace
        return RunnerClient()

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.routes_resources._get_runner_client_for_resource_access",
        get_runner,
    )
    artifacts = LocalArtifactStore(str(tmp_path / "production-artifacts"))
    app = create_app(
        agent_store=review_harness.agents,
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=review_harness.conversations,
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "production-cache"),
        auth_provider=UnifiedAuthProvider(source="header"),
        permission_store=review_harness.permissions,
        studio_review_store=review_harness.store,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await _capture(client, review_harness, pr_url=target.pr_url)

    assert review_harness.captured == []  # The fixture's injected callback was not used.
    if synced:
        assert response.status_code == 201
        assert response.json()["review"]["target"] == target.model_dump(mode="json")
        assert calls == [
            (
                f"/v1/sessions/{review_harness.source_id}/resources/github",
                {"pr_url": target.pr_url, "server_tracked": True},
            )
        ]
    else:
        assert response.status_code == 503
        assert calls == []
        assert (
            review_harness.store.find_operation(
                owner_user_id=ALICE, action="review.capture", idempotency_key="capture-one"
            )
            is None
        )


@pytest.mark.parametrize("key", ["with space", "\tcontrol", "k" * 129])
async def test_invalid_idempotency_key_is_rejected_before_capture(
    review_harness: ReviewHarness, review_client: httpx.AsyncClient, key: str
) -> None:
    assert (await _capture(review_client, review_harness, key=key)).status_code == 422
    assert review_harness.captured == []


async def test_event_sequence_is_bounded_to_storage_integer(
    review_harness: ReviewHarness, review_client: httpx.AsyncClient
) -> None:
    result = (await _capture(review_client, review_harness)).json()
    response = await review_client.get(
        f"/v1/studio/reviews/{result['review']['id']}/events?after_sequence={2**63}",
        headers={"X-Forwarded-Email": ALICE},
    )
    assert response.status_code == 422
