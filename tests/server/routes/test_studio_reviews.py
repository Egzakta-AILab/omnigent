"""Review HTTP contracts with real persistence and a controlled PR resource boundary."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request

from omnigent.db.utils import builtin_agent_id
from omnigent.host.frames import HostHelloFrame
from omnigent.native.native_coding_agents import CODEX_NATIVE_AGENT_NAME
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
        send_review=AsyncMock(),
        auth_provider=auth,
        permission_store=permissions,
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
