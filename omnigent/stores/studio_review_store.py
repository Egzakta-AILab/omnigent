"""Private review snapshots, idempotent reservations and single-claim dispatch."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any, Literal, TypeVar, cast

from sqlalchemy import select, tuple_, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlStudioReviewEvent,
    SqlStudioReviewOperation,
    SqlStudioReviewPass,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.studio.reviews.models import (
    CaptureResult,
    DispatchStatus,
    EventPage,
    OperationResult,
    OperationState,
    ReviewAction,
    ReviewEvent,
    ReviewOperation,
    ReviewPage,
    ReviewPass,
    ReviewProfile,
    ReviewState,
    ReviewTarget,
    require_uuid,
)

_ACTIONS: dict[ReviewAction, int] = {
    "review.capture": 1,
    "review.start": 2,
    "review.continue": 3,
    "review.cancel": 4,
    "review.publish": 5,
}
_OP_STATES: dict[OperationState, int] = {
    "pending": 1,
    "dispatching": 2,
    "running": 3,
    "succeeded": 4,
    "failed": 5,
    "cancelled": 6,
    "unknown": 7,
}
_PASS_STATES: dict[ReviewState, int] = {
    "prepared": 1,
    "running": 2,
    "completed": 3,
    "failed": 4,
    "cancelled": 5,
    "unknown": 6,
}
_DISPATCH_STATES: dict[DispatchStatus, int] = {
    "not_requested": 1,
    "dispatching": 2,
    "running": 3,
    "succeeded": 4,
    "failed": 5,
    "cancelled": 6,
    "unknown": 7,
}
_T = TypeVar("_T")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_MAX_SNAPSHOT_BYTES = 12_000


class ReviewConflictError(RuntimeError):
    """The same request key or lifecycle version names a different operation."""


class ReviewNotFoundError(LookupError):
    """The requested review or operation is not owned in this workspace."""


class ReviewTransitionError(ReviewConflictError):
    """An action has already been claimed or its outcome needs reconciliation."""


def _owner_scope(owner_user_id: str | None) -> bytes:
    if owner_user_id is not None and len(owner_user_id) > 256:
        raise ValueError("Owner identity is too long")
    return hashlib.sha256(json.dumps([owner_user_id], ensure_ascii=False).encode()).digest()


def _validate_key(idempotency_key: str) -> None:
    if not 1 <= len(idempotency_key) <= 128 or any(
        not 33 <= ord(char) <= 126 for char in idempotency_key
    ):
        raise ValueError("Idempotency key must contain 1 to 128 visible ASCII characters")


def _request_values(idempotency_key: str, request_hash: str) -> bytes:
    _validate_key(idempotency_key)
    if not _HASH.fullmatch(request_hash):
        raise ValueError("Request hash must be a SHA-256 hex digest")
    return bytes.fromhex(request_hash)


def _snapshot(value: ReviewTarget | ReviewProfile) -> str:
    encoded = value.model_dump_json()
    if len(encoded.encode()) > _MAX_SNAPSHOT_BYTES:
        raise ValueError("Review snapshot exceeds the stored byte budget")
    return encoded


def _name(codes: dict[_T, int], code: int) -> _T:
    return next(name for name, value in codes.items() if value == code)


def _review(row: SqlStudioReviewPass) -> ReviewPass:
    return ReviewPass(
        id=row.id,
        owner_user_id=row.owner_user_id,
        target=ReviewTarget.model_validate_json(row.target_json),
        profile=ReviewProfile.model_validate_json(row.profile_json),
        state=_name(_PASS_STATES, row.state),
        dispatch_status=_name(_DISPATCH_STATES, row.dispatch_status),
        reviewer_session_id=row.reviewer_session_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
    )


def _operation(row: SqlStudioReviewOperation) -> ReviewOperation:
    return ReviewOperation(
        id=row.id,
        review_id=row.review_id,
        action=_name(_ACTIONS, row.action),
        idempotency_key=row.idempotency_key,
        request_hash=bytes(row.request_hash).hex(),
        state=_name(_OP_STATES, row.state),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class StudioReviewStore:
    """Review state in the existing operational database, never transcript storage."""

    def __init__(self, storage_location: str) -> None:
        self.storage_location = storage_location
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.studio_review_store"
        )
        self._write = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.studio_review_store", immediate=True
        )

    def _pass_row(
        self, session: Session, review_id: str, owner: bytes, *, lock: bool = False
    ) -> SqlStudioReviewPass | None:
        query = select(SqlStudioReviewPass).where(
            SqlStudioReviewPass.workspace_id == current_workspace_id(),
            SqlStudioReviewPass.id == review_id,
            SqlStudioReviewPass.owner_scope == owner,
        )
        return session.execute(query.with_for_update() if lock else query).scalar_one_or_none()

    def _request_row(
        self, session: Session, owner: bytes, action: ReviewAction, key: str
    ) -> SqlStudioReviewOperation | None:
        return session.execute(
            select(SqlStudioReviewOperation).where(
                SqlStudioReviewOperation.workspace_id == current_workspace_id(),
                SqlStudioReviewOperation.owner_scope == owner,
                SqlStudioReviewOperation.action == _ACTIONS[action],
                SqlStudioReviewOperation.idempotency_key == key,
            )
        ).scalar_one_or_none()

    @staticmethod
    def _event(
        session: Session,
        review: SqlStudioReviewPass,
        operation: SqlStudioReviewOperation,
        kind: str,
        now: int,
        *,
        initial: bool = False,
    ) -> None:
        if not initial:
            review.version += 1
        review.updated_at = now
        session.add(
            SqlStudioReviewEvent(
                workspace_id=current_workspace_id(),
                review_id=review.id,
                sequence=review.version,
                operation_id=operation.id,
                kind=kind,
                created_at=now,
            )
        )

    def find_operation(
        self, *, owner_user_id: str | None, action: ReviewAction, idempotency_key: str
    ) -> ReviewOperation | None:
        _validate_key(idempotency_key)
        owner = _owner_scope(owner_user_id)
        with self._session("find_idempotent_operation") as session:
            row = self._request_row(session, owner, action, idempotency_key)
            return None if row is None else _operation(row)

    def get(self, review_id: str, *, owner_user_id: str | None) -> ReviewPass | None:
        try:
            review_id = require_uuid(review_id)
        except ValueError:
            return None
        with self._session("get_owned_review") as session:
            row = self._pass_row(session, review_id, _owner_scope(owner_user_id))
            return None if row is None else _review(row)

    def capture(
        self,
        *,
        owner_user_id: str | None,
        idempotency_key: str,
        request_hash: str,
        target: ReviewTarget,
        profile: ReviewProfile,
    ) -> CaptureResult:
        owner = _owner_scope(owner_user_id)
        digest = _request_values(idempotency_key, request_hash)
        target_json, profile_json = _snapshot(target), _snapshot(profile)

        def replay(session: Session, operation: SqlStudioReviewOperation) -> CaptureResult:
            if bytes(operation.request_hash) != digest:
                raise ReviewConflictError("Idempotency key was used for a different request")
            review = self._pass_row(session, operation.review_id, owner)
            if review is None:
                raise ReviewConflictError("Capture receipt has no owned review")
            return CaptureResult(
                review=_review(review), operation=_operation(operation), created=False
            )

        def write(session: Session) -> CaptureResult:
            existing = self._request_row(session, owner, "review.capture", idempotency_key)
            if existing is not None:
                return replay(session, existing)
            now = time.time_ns() // 1_000_000
            row = SqlStudioReviewPass(
                workspace_id=current_workspace_id(),
                id=uuid.uuid4().hex,
                owner_scope=owner,
                owner_user_id=owner_user_id,
                source_session_id=target.source_session_id,
                target_json=target_json,
                profile_json=profile_json,
                state=_PASS_STATES["prepared"],
                dispatch_status=_DISPATCH_STATES["not_requested"],
                reviewer_session_id=None,
                created_at=now,
                updated_at=now,
                version=1,
            )
            operation = SqlStudioReviewOperation(
                workspace_id=current_workspace_id(),
                id=uuid.uuid4().hex,
                owner_scope=owner,
                review_id=row.id,
                action=_ACTIONS["review.capture"],
                idempotency_key=idempotency_key,
                request_hash=digest,
                start_once=None,
                state=_OP_STATES["succeeded"],
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add_all([row, operation])
            self._event(session, row, operation, "review.captured", now, initial=True)
            return CaptureResult(
                review=_review(row), operation=_operation(operation), created=True
            )

        try:
            return run_write_transaction(self._write, "capture_review", write)
        except IntegrityError:
            with self._session("replay_concurrent_capture") as session:
                existing = self._request_row(session, owner, "review.capture", idempotency_key)
                if existing is None:
                    raise
                return replay(session, existing)

    def list_page(
        self,
        *,
        owner_user_id: str | None,
        source_session_id: str,
        limit: int,
        after_id: str | None = None,
    ) -> ReviewPage:
        if not 1 <= limit <= 100:
            raise ValueError("Page size must be between 1 and 100")
        owner = _owner_scope(owner_user_id)
        source_session_id = require_uuid(source_session_id)
        with self._session("list_owned_session_reviews") as session:
            query = select(SqlStudioReviewPass).where(
                SqlStudioReviewPass.workspace_id == current_workspace_id(),
                SqlStudioReviewPass.owner_scope == owner,
                SqlStudioReviewPass.source_session_id == source_session_id,
            )
            if after_id is not None:
                try:
                    cursor = self._pass_row(session, require_uuid(after_id), owner)
                except ValueError:
                    cursor = None
                if cursor is None or cursor.source_session_id != source_session_id:
                    raise ReviewNotFoundError("Review cursor not found")
                query = query.where(
                    tuple_(SqlStudioReviewPass.created_at, SqlStudioReviewPass.id)
                    < (cursor.created_at, cursor.id)
                )
            rows = (
                session.execute(
                    query.order_by(
                        SqlStudioReviewPass.created_at.desc(), SqlStudioReviewPass.id.desc()
                    ).limit(limit + 1)
                )
                .scalars()
                .all()
            )
            entries = [_review(row) for row in rows[:limit]]
            return ReviewPage(
                entries=entries,
                next_cursor=entries[-1].id if len(rows) > limit else None,
            )

    def get_operation(
        self, review_id: str, operation_id: str, *, owner_user_id: str | None
    ) -> ReviewOperation | None:
        try:
            review_id, operation_id = require_uuid(review_id), require_uuid(operation_id)
        except ValueError:
            return None
        with self._session("get_owned_review_operation") as session:
            row = session.execute(
                select(SqlStudioReviewOperation).where(
                    SqlStudioReviewOperation.workspace_id == current_workspace_id(),
                    SqlStudioReviewOperation.id == operation_id,
                    SqlStudioReviewOperation.review_id == review_id,
                    SqlStudioReviewOperation.owner_scope == _owner_scope(owner_user_id),
                )
            ).scalar_one_or_none()
            return None if row is None else _operation(row)

    def list_events(
        self,
        review_id: str,
        *,
        owner_user_id: str | None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> EventPage:
        if not 1 <= limit <= 100 or not 0 <= after_sequence <= 9_223_372_036_854_775_807:
            raise ValueError("Invalid event page bounds")
        try:
            review_id = require_uuid(review_id)
        except ValueError:
            raise ReviewNotFoundError("Review not found") from None
        with self._session("list_owned_review_events") as session:
            if self._pass_row(session, review_id, _owner_scope(owner_user_id)) is None:
                raise ReviewNotFoundError("Review not found")
            rows = (
                session.execute(
                    select(SqlStudioReviewEvent)
                    .where(
                        SqlStudioReviewEvent.workspace_id == current_workspace_id(),
                        SqlStudioReviewEvent.review_id == review_id,
                        SqlStudioReviewEvent.sequence > after_sequence,
                    )
                    .order_by(SqlStudioReviewEvent.sequence)
                    .limit(limit + 1)
                )
                .scalars()
                .all()
            )
            entries = [
                ReviewEvent(
                    review_id=row.review_id,
                    sequence=row.sequence,
                    operation_id=row.operation_id,
                    kind=row.kind,
                    created_at=row.created_at,
                )
                for row in rows[:limit]
            ]
            return EventPage(
                entries=entries, next_cursor=entries[-1].sequence if len(rows) > limit else None
            )

    def reserve_operation(
        self,
        review_id: str,
        *,
        owner_user_id: str | None,
        action: ReviewAction,
        idempotency_key: str,
        request_hash: str,
    ) -> OperationResult:
        try:
            review_id = require_uuid(review_id)
        except ValueError:
            raise ReviewNotFoundError("Review not found") from None
        if action not in _ACTIONS or action == "review.capture":
            raise ValueError("Capture uses the capture method")
        owner = _owner_scope(owner_user_id)
        digest = _request_values(idempotency_key, request_hash)

        def write(session: Session) -> OperationResult:
            row = self._pass_row(session, review_id, owner, lock=True)
            if row is None:
                raise ReviewNotFoundError("Review not found")
            existing = self._request_row(session, owner, action, idempotency_key)
            if existing is not None:
                if existing.review_id != row.id or bytes(existing.request_hash) != digest:
                    raise ReviewConflictError("Idempotency key was used for a different request")
                return OperationResult(operation=_operation(existing), created=False)
            now = time.time_ns() // 1_000_000
            operation = SqlStudioReviewOperation(
                workspace_id=current_workspace_id(),
                id=uuid.uuid4().hex,
                owner_scope=owner,
                review_id=row.id,
                action=_ACTIONS[action],
                idempotency_key=idempotency_key,
                request_hash=digest,
                start_once=1 if action == "review.start" else None,
                state=_OP_STATES["pending"],
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(operation)
            self._event(session, row, operation, "operation.reserved", now)
            return OperationResult(operation=_operation(operation), created=True)

        try:
            return run_write_transaction(self._write, "reserve_review_operation", write)
        except IntegrityError as exc:
            existing = self.find_operation(
                owner_user_id=owner_user_id, action=action, idempotency_key=idempotency_key
            )
            if (
                existing is not None
                and existing.review_id == review_id
                and existing.request_hash == request_hash
            ):
                return OperationResult(operation=existing, created=False)
            raise ReviewConflictError(
                "A start is already reserved, or the request key conflicts"
            ) from exc

    def _transition(
        self,
        review_id: str,
        operation_id: str,
        *,
        owner_user_id: str | None,
        expected_version: int,
        allowed: tuple[OperationState, ...],
        state: OperationState,
        reviewer_session_id: str | None = None,
    ) -> ReviewOperation:
        try:
            review_id, operation_id = require_uuid(review_id), require_uuid(operation_id)
        except ValueError:
            raise ReviewNotFoundError("Review operation not found") from None
        owner = _owner_scope(owner_user_id)
        if reviewer_session_id is not None:
            reviewer_session_id = require_uuid(reviewer_session_id)

        def write(session: Session) -> ReviewOperation:
            review = self._pass_row(session, review_id, owner, lock=True)
            if review is None:
                raise ReviewNotFoundError("Review not found")
            operation = session.execute(
                select(SqlStudioReviewOperation)
                .where(
                    SqlStudioReviewOperation.workspace_id == current_workspace_id(),
                    SqlStudioReviewOperation.id == operation_id,
                    SqlStudioReviewOperation.review_id == review_id,
                    SqlStudioReviewOperation.owner_scope == owner,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if operation is None:
                raise ReviewNotFoundError("Operation not found")
            if (
                operation.version != expected_version
                or _name(_OP_STATES, operation.state) not in allowed
            ):
                raise ReviewTransitionError("Operation was claimed or requires reconciliation")
            if reviewer_session_id is not None and review.reviewer_session_id not in (
                None,
                reviewer_session_id,
            ):
                raise ReviewTransitionError("Reviewer session is already bound")
            if (
                operation.action == _ACTIONS["review.start"]
                and state in ("running", "succeeded")
                and reviewer_session_id is None
                and review.reviewer_session_id is None
            ):
                raise ReviewTransitionError("Observed execution requires a reviewer session")
            now = time.time_ns() // 1_000_000
            result = cast(
                "CursorResult[Any]",
                session.execute(
                    update(SqlStudioReviewOperation)
                    .where(
                        SqlStudioReviewOperation.workspace_id == current_workspace_id(),
                        SqlStudioReviewOperation.id == operation_id,
                        SqlStudioReviewOperation.owner_scope == owner,
                        SqlStudioReviewOperation.version == expected_version,
                        SqlStudioReviewOperation.state == operation.state,
                    )
                    .values(state=_OP_STATES[state], version=expected_version + 1, updated_at=now)
                ),
            )
            if result.rowcount != 1:
                raise ReviewTransitionError("Operation version changed")
            if operation.action == _ACTIONS["review.start"]:
                review.dispatch_status = _DISPATCH_STATES[cast("DispatchStatus", state)]
                review.state = _PASS_STATES[
                    "prepared"
                    if state == "dispatching"
                    else "completed"
                    if state == "succeeded"
                    else cast("ReviewState", state)
                ]
                if reviewer_session_id is not None:
                    review.reviewer_session_id = reviewer_session_id
            self._event(session, review, operation, f"operation.{state}", now)
            return _operation(operation)

        return run_write_transaction(self._write, "advance_review_operation", write)

    def claim_operation(
        self,
        review_id: str,
        operation_id: str,
        *,
        owner_user_id: str | None,
        expected_version: int,
    ) -> ReviewOperation:
        """Persist dispatching before a caller performs its one external attempt."""
        return self._transition(
            review_id,
            operation_id,
            owner_user_id=owner_user_id,
            expected_version=expected_version,
            allowed=("pending",),
            state="dispatching",
        )

    def mark_unknown(
        self,
        review_id: str,
        operation_id: str,
        *,
        owner_user_id: str | None,
        expected_version: int,
    ) -> ReviewOperation:
        return self._transition(
            review_id,
            operation_id,
            owner_user_id=owner_user_id,
            expected_version=expected_version,
            allowed=("dispatching", "running"),
            state="unknown",
        )

    def reconcile_operation(
        self,
        review_id: str,
        operation_id: str,
        *,
        owner_user_id: str | None,
        expected_version: int,
        state: Literal["running", "succeeded", "failed", "cancelled"],
        reviewer_session_id: str | None = None,
    ) -> ReviewOperation:
        """Accept an externally verified outcome; callers validate any session binding."""
        if state not in ("running", "succeeded", "failed", "cancelled"):
            raise ValueError("Reconciliation needs an observed outcome")
        return self._transition(
            review_id,
            operation_id,
            owner_user_id=owner_user_id,
            expected_version=expected_version,
            allowed=("dispatching", "unknown", "running"),
            state=state,
            reviewer_session_id=reviewer_session_id,
        )
