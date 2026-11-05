"""Private durable review state, idempotency and dispatch races on migrated stores."""

from __future__ import annotations

import hashlib
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from omnigent.db.db_models import workspace_scope
from omnigent.db.utils import get_or_create_engine
from omnigent.stores.studio_review_store import (
    ReviewConflictError,
    ReviewNotFoundError,
    ReviewTransitionError,
    StudioReviewStore,
)
from omnigent.studio.reviews.models import CaptureResult, ReviewProfile, ReviewTarget

OWNER = "owner@example.test"
OTHER = "other@example.test"
SESSION = "4e92b5a0c0ee6db3f874f9c4a3f855a5"
HOST = "5d29bee4350489d66feafecfebd94a97"
AGENT = "6d29bee4350489d66feafecfebd94a97"


@pytest.fixture
def store(db_uri: str) -> StudioReviewStore:
    return StudioReviewStore(db_uri)


def _target(**changes: object) -> ReviewTarget:
    return ReviewTarget.model_validate(
        {
            "source_session_id": SESSION,
            "host_id": HOST,
            "workspace": "/workspace/repository",
            "pr_url": "https://github.com/example/project/pull/3",
            "repository": "example/project",
            "pr_number": 3,
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            **changes,
        }
    )


def _profile(**changes: object) -> ReviewProfile:
    return ReviewProfile.model_validate(
        {
            "agent_id": AGENT,
            "agent_version": "1.0",
            "harness": "codex-native",
            "skill": "review",
            "model": None,
            "reasoning_effort": None,
            **changes,
        }
    )


def _capture(
    store: StudioReviewStore,
    *,
    key: str = "capture",
    owner: str | None = OWNER,
    target: ReviewTarget | None = None,
    profile: ReviewProfile | None = None,
    digest: str | None = None,
) -> CaptureResult:
    return store.capture(
        owner_user_id=owner,
        idempotency_key=key,
        request_hash=digest or hashlib.sha256(key.encode()).hexdigest(),
        target=target or _target(),
        profile=profile or _profile(),
    )


def test_capture_is_frozen_replayable_and_not_execution(store: StudioReviewStore) -> None:
    first = _capture(store)
    again = _capture(store)
    assert first.created and not again.created
    assert first.review == again.review
    assert first.operation == again.operation
    assert first.review.state == "prepared"
    assert first.review.dispatch_status == "not_requested"
    assert first.review.reviewer_session_id is None
    assert first.operation.action == "review.capture"
    assert first.operation.state == "succeeded"
    assert [e.kind for e in store.list_events(first.review.id, owner_user_id=OWNER).entries] == [
        "review.captured"
    ]
    with pytest.raises(ValidationError, match="frozen"):
        first.review.target.workspace = "/changed"
    with pytest.raises(ValidationError, match="frozen"):
        first.review.profile.skill = "changed"


def test_conflicting_key_does_not_modify_capture(store: StudioReviewStore) -> None:
    first = _capture(store)
    with pytest.raises(ReviewConflictError):
        _capture(store, digest="c" * 64, target=_target(head_sha="d" * 40))
    assert store.get(first.review.id, owner_user_id=OWNER) == first.review


@pytest.mark.parametrize("owner", [None, "", "local", "__local__"])
def test_single_user_identity_never_collides_with_named_users(
    store: StudioReviewStore, owner: str | None
) -> None:
    original = _capture(store, owner=owner)
    other = _capture(store, owner=OTHER)
    assert original.review.id != other.review.id
    assert store.get(original.review.id, owner_user_id=OTHER) is None
    assert store.get(other.review.id, owner_user_id=owner) is None
    assert (
        store.find_operation(
            owner_user_id=owner, action="review.capture", idempotency_key="capture"
        )
        == original.operation
    )


def test_owner_workspace_and_source_session_scope(store: StudioReviewStore) -> None:
    with workspace_scope(10):
        first = _capture(store)
    with workspace_scope(11):
        second = _capture(store)
        assert second.review.id != first.review.id
        assert store.get(first.review.id, owner_user_id=OWNER) is None
        assert (
            store.get_operation(first.review.id, first.operation.id, owner_user_id=OWNER) is None
        )
        with pytest.raises(ReviewNotFoundError):
            store.list_events(first.review.id, owner_user_id=OWNER)
    with workspace_scope(10):
        assert store.get(first.review.id, owner_user_id=OWNER) == first.review
        assert (
            store.list_page(owner_user_id=OTHER, source_session_id=SESSION, limit=10).entries == []
        )
        assert (
            store.list_page(
                owner_user_id=OWNER, source_session_id=uuid.uuid4().hex, limit=10
            ).entries
            == []
        )


def test_capture_survives_reopened_store(store: StudioReviewStore, db_uri: str) -> None:
    captured = _capture(store)
    reopened = StudioReviewStore(db_uri)
    assert reopened.get(captured.review.id, owner_user_id=OWNER) == captured.review
    assert (
        reopened.get_operation(captured.review.id, captured.operation.id, owner_user_id=OWNER)
        == captured.operation
    )


def test_concurrent_capture_returns_one_pass_and_receipt(store: StudioReviewStore) -> None:
    barrier = threading.Barrier(2)

    def capture() -> CaptureResult:
        barrier.wait(timeout=5)
        return _capture(store)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: capture(), range(2)))
    assert outcomes[0].review.id == outcomes[1].review.id
    assert sorted(result.created for result in outcomes) == [False, True]
    assert (
        len(store.list_page(owner_user_id=OWNER, source_session_id=SESSION, limit=10).entries) == 1
    )


def test_keyset_pages_preserve_tied_timestamp_order(
    store: StudioReviewStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omnigent.stores.studio_review_store.time.time_ns", lambda: 123_000_000)
    created = [_capture(store, key=f"capture-{number}").review for number in range(5)]
    first = store.list_page(owner_user_id=OWNER, source_session_id=SESSION, limit=2)
    second = store.list_page(
        owner_user_id=OWNER, source_session_id=SESSION, limit=2, after_id=first.next_cursor
    )
    third = store.list_page(
        owner_user_id=OWNER, source_session_id=SESSION, limit=2, after_id=second.next_cursor
    )
    assert [r.id for r in first.entries + second.entries + third.entries] == sorted(
        (r.id for r in created), reverse=True
    )
    assert third.next_cursor is None
    with pytest.raises(ReviewNotFoundError):
        store.list_page(
            owner_user_id=OTHER, source_session_id=SESSION, limit=2, after_id=created[0].id
        )


def _reserve(store: StudioReviewStore, review_id: str):
    return store.reserve_operation(
        review_id,
        owner_user_id=OWNER,
        action="review.start",
        idempotency_key="start",
        request_hash="c" * 64,
    )


def test_reservation_is_idempotent_and_only_one_start_exists(store: StudioReviewStore) -> None:
    captured = _capture(store)
    first = _reserve(store, captured.review.id)
    assert first.created
    assert not _reserve(store, captured.review.id).created
    with pytest.raises(ReviewConflictError):
        store.reserve_operation(
            captured.review.id,
            owner_user_id=OWNER,
            action="review.start",
            idempotency_key="another-key",
            request_hash="d" * 64,
        )
    with pytest.raises(ReviewConflictError):
        store.reserve_operation(
            captured.review.id,
            owner_user_id=OWNER,
            action="review.start",
            idempotency_key="start",
            request_hash="e" * 64,
        )


def test_only_one_concurrent_dispatch_claim_wins(store: StudioReviewStore) -> None:
    captured = _capture(store)
    operation = _reserve(store, captured.review.id).operation
    barrier = threading.Barrier(2)

    def claim() -> bool:
        barrier.wait(timeout=5)
        try:
            store.claim_operation(
                captured.review.id, operation.id, owner_user_id=OWNER, expected_version=1
            )
        except ReviewTransitionError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: claim(), range(2))) == [False, True]
    assert (
        store.get_operation(captured.review.id, operation.id, owner_user_id=OWNER).state
        == "dispatching"
    )


def test_unknown_dispatch_survives_restart_and_cannot_be_relaunched(
    store: StudioReviewStore, db_uri: str
) -> None:
    captured = _capture(store)
    operation = _reserve(store, captured.review.id).operation
    claimed = store.claim_operation(
        captured.review.id, operation.id, owner_user_id=OWNER, expected_version=1
    )
    unknown = store.mark_unknown(
        captured.review.id, operation.id, owner_user_id=OWNER, expected_version=claimed.version
    )
    reopened = StudioReviewStore(db_uri)
    with pytest.raises(ReviewTransitionError):
        reopened.claim_operation(
            captured.review.id, operation.id, owner_user_id=OWNER, expected_version=unknown.version
        )
    with pytest.raises(ReviewConflictError):
        reopened.reserve_operation(
            captured.review.id,
            owner_user_id=OWNER,
            action="review.start",
            idempotency_key="retry-with-another-key",
            request_hash="d" * 64,
        )
    reviewer_session = uuid.uuid4().hex
    running = reopened.reconcile_operation(
        captured.review.id,
        operation.id,
        owner_user_id=OWNER,
        expected_version=unknown.version,
        state="running",
        reviewer_session_id=reviewer_session,
    )
    assert running.state == "running"
    with pytest.raises(ReviewTransitionError):
        reopened.mark_unknown(
            captured.review.id, operation.id, owner_user_id=OWNER, expected_version=claimed.version
        )
    reopened.reconcile_operation(
        captured.review.id,
        operation.id,
        owner_user_id=OWNER,
        expected_version=running.version,
        state="succeeded",
    )
    result = reopened.get(captured.review.id, owner_user_id=OWNER)
    assert result.state == "completed"
    assert result.reviewer_session_id == reviewer_session
    assert result.target.source_session_id != reviewer_session
    events = reopened.list_events(captured.review.id, owner_user_id=OWNER, limit=3)
    rest = reopened.list_events(
        captured.review.id, owner_user_id=OWNER, after_sequence=events.next_cursor, limit=100
    )
    assert [event.sequence for event in events.entries + rest.entries] == list(range(1, 7))
    assert [event.kind for event in events.entries + rest.entries] == [
        "review.captured",
        "operation.reserved",
        "operation.dispatching",
        "operation.unknown",
        "operation.running",
        "operation.succeeded",
    ]


def test_foreign_owner_cannot_reserve_claim_or_read_events(store: StudioReviewStore) -> None:
    captured = _capture(store)
    operation = _reserve(store, captured.review.id).operation
    assert store.get_operation(captured.review.id, operation.id, owner_user_id=OTHER) is None
    with pytest.raises(ReviewNotFoundError):
        store.reserve_operation(
            captured.review.id,
            owner_user_id=OTHER,
            action="review.publish",
            idempotency_key="foreign",
            request_hash="c" * 64,
        )
    with pytest.raises(ReviewNotFoundError):
        store.claim_operation(
            captured.review.id, operation.id, owner_user_id=OTHER, expected_version=1
        )
    with pytest.raises(ReviewNotFoundError):
        store.list_events(captured.review.id, owner_user_id=OTHER)


def test_execution_requires_a_bound_session_and_never_rebinds(store: StudioReviewStore) -> None:
    captured = _capture(store)
    operation = _reserve(store, captured.review.id).operation
    claimed = store.claim_operation(
        captured.review.id, operation.id, owner_user_id=OWNER, expected_version=1
    )
    with pytest.raises(ReviewTransitionError):
        store.reconcile_operation(
            captured.review.id,
            operation.id,
            owner_user_id=OWNER,
            expected_version=claimed.version,
            state="running",
        )
    running = store.reconcile_operation(
        captured.review.id,
        operation.id,
        owner_user_id=OWNER,
        expected_version=claimed.version,
        state="running",
        reviewer_session_id=uuid.uuid4().hex,
    )
    with pytest.raises(ReviewTransitionError):
        store.reconcile_operation(
            captured.review.id,
            operation.id,
            owner_user_id=OWNER,
            expected_version=running.version,
            state="succeeded",
            reviewer_session_id=uuid.uuid4().hex,
        )


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_page_reads_are_bounded(store: StudioReviewStore, limit: int) -> None:
    with pytest.raises(ValueError):
        store.list_page(owner_user_id=OWNER, source_session_id=SESSION, limit=limit)
    with pytest.raises(ValueError):
        store.list_events(uuid.uuid4().hex, owner_user_id=OWNER, limit=limit)


@pytest.mark.parametrize("key", ["", "trailing ", " leading", "line\nbreak", "\u00e9", "x" * 129])
def test_idempotency_keys_have_portable_binary_comparison(
    store: StudioReviewStore, key: str
) -> None:
    with pytest.raises(ValueError, match="visible ASCII"):
        _capture(store, key=key)
    with pytest.raises(ValueError, match="visible ASCII"):
        store.find_operation(owner_user_id=OWNER, action="review.capture", idempotency_key=key)


def test_unrepresentable_event_cursor_is_rejected(store: StudioReviewStore) -> None:
    with pytest.raises(ValueError):
        store.list_events(uuid.uuid4().hex, owner_user_id=OWNER, after_sequence=2**63)


def test_oversized_utf8_snapshot_is_refused_before_persistence(store: StudioReviewStore) -> None:
    target = _target(workspace="/" + "\U0001f986" * 4095)
    with pytest.raises(ValueError, match="byte budget"):
        _capture(store, target=target)
    assert store.list_page(owner_user_id=OWNER, source_session_id=SESSION, limit=1).entries == []


def test_operation_cannot_be_moved_to_another_pass(store: StudioReviewStore) -> None:
    first, second = _capture(store), _capture(store, key="second")
    operation = _reserve(store, first.review.id).operation
    assert store.get_operation(second.review.id, operation.id, owner_user_id=OWNER) is None
    with pytest.raises(ReviewNotFoundError):
        store.claim_operation(
            second.review.id, operation.id, owner_user_id=OWNER, expected_version=operation.version
        )
    with pytest.raises(ReviewConflictError):
        _reserve(store, second.review.id)
    assert (
        store.get_operation(first.review.id, operation.id, owner_user_id=OWNER).state == "pending"
    )


def test_actual_keyset_query_uses_owner_session_index(
    store: StudioReviewStore, db_uri: str
) -> None:
    for number in range(3):
        _capture(store, key=f"plan-{number}")
    first = store.list_page(owner_user_id=OWNER, source_session_id=SESSION, limit=1)
    engine = get_or_create_engine(db_uri)
    queries = []

    def capture_query(_conn, _cursor, statement, parameters, _context, _executemany):
        if "studio_review_passes" in statement and "ORDER BY" in statement:
            queries.append((statement, parameters))

    sa.event.listen(engine, "before_cursor_execute", capture_query)
    try:
        store.list_page(
            owner_user_id=OWNER,
            source_session_id=SESSION,
            limit=1,
            after_id=first.next_cursor,
        )
    finally:
        sa.event.remove(engine, "before_cursor_execute", capture_query)
    assert len(queries) == 1
    statement, parameters = queries[0]
    with engine.connect() as connection:
        plan = connection.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters).all()
    details = " ".join(str(row[-1]) for row in plan)
    assert "ix_studio_review_pass_owner_session" in details
    assert "created_at" in details
    assert "TEMP B-TREE" not in details


def test_project_run_captures_roster_atomically_and_replays(store: StudioReviewStore) -> None:
    project = uuid.uuid4().hex
    target = _target(source_session_id=None, source_project_id=project, repository_path=".")
    options = {
        "owner_user_id": OWNER,
        "idempotency_key": "run",
        "request_hash": "a" * 64,
        "target": target,
        "profiles": [_profile(skill="backend"), _profile(skill="security")],
    }
    run = store.capture_run(**options)
    assert store.capture_run(**options) == run
    assert {review.profile.skill for review in run.reviewers} == {"backend", "security"}
    assert all(review.run_id == run.id and review.target == target for review in run.reviewers)
    assert all(review.reviewer_session_id is None for review in run.reviewers)
    assert store.get_run(run.id, owner_user_id=OTHER) is None
    assert store.list_runs(
        project_id=project, owner_user_id=OWNER, pr_url=target.pr_url, limit=10
    ).entries == [run]
    with pytest.raises(ReviewConflictError):
        store.capture_run(**{**options, "request_hash": "b" * 64})
    assert store.get_run(run.id, owner_user_id=OWNER) == run
    member = run.reviewers[0]
    assert store.get_report_snapshot(member) is None
    assert store.save_report_snapshot(member, '{"summary":"original"}') == '{"summary":"original"}'
    assert (
        store.save_report_snapshot(member, '{"summary":"follow-up"}') == '{"summary":"original"}'
    )
    assert store.get_report_snapshot(member) == '{"summary":"original"}'
    newer = store.capture_run(**{**options, "idempotency_key": "second-run"})
    first_page = store.list_runs(
        project_id=project, owner_user_id=OWNER, pr_url=target.pr_url, limit=1
    )
    second_page = store.list_runs(
        project_id=project,
        owner_user_id=OWNER,
        pr_url=target.pr_url,
        limit=1,
        after_id=first_page.next_cursor,
    )
    assert {first_page.entries[0].id, second_page.entries[0].id} == {run.id, newer.id}
    assert second_page.next_cursor is None


def test_project_run_rolls_back_all_captures_on_invalid_snapshot(store: StudioReviewStore) -> None:
    project = uuid.uuid4().hex
    target = _target(source_session_id=None, source_project_id=project, repository_path=".")
    oversized = _profile().model_copy(update={"skill": "x" * 13000})
    with pytest.raises(ValueError, match="byte budget"):
        store.capture_run(
            owner_user_id=OWNER,
            idempotency_key="invalid",
            request_hash="a" * 64,
            target=target,
            profiles=[_profile(), oversized],
        )
    assert (
        store.list_runs(
            project_id=project, owner_user_id=OWNER, pr_url=target.pr_url, limit=10
        ).entries
        == []
    )
    assert store.list_page(owner_user_id=OWNER, source_project_id=project, limit=10).entries == []
