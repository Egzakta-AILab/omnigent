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

from omnigent.db.compression import encode
from omnigent.db.db_models import (
    SqlStudioReviewEvent,
    SqlStudioReviewOperation,
    SqlStudioReviewPass,
    SqlStudioReviewPatchChunk,
    SqlStudioReviewRun,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.studio.reviews.evidence import (
    MAX_PATCH_CHUNKS,
    MAX_REVIEW_PATCH_BYTES,
    PATCH_CHUNK_BYTES,
    patch_digest,
)
from omnigent.studio.reviews.models import (
    MAX_RUN_REVIEWERS,
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
    ReviewPlan,
    ReviewProfile,
    ReviewRun,
    ReviewRunPage,
    ReviewRunRole,
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


def _held(row: SqlStudioReviewPass) -> bool:
    # Older dispatchers skip this status. Only an approved plan can release candidates.
    return (
        row.run_role is not None
        and row.state == _PASS_STATES["prepared"]
        and row.dispatch_status == _DISPATCH_STATES["cancelled"]
    )


def _review(row: SqlStudioReviewPass) -> ReviewPass:
    return ReviewPass(
        id=row.id,
        run_id=row.run_id,
        run_role=cast(ReviewRunRole | None, row.run_role),
        launch_error=row.launch_error,
        owner_user_id=row.owner_user_id,
        target=ReviewTarget.model_validate_json(row.target_json),
        profile=ReviewProfile.model_validate_json(row.profile_json),
        state=_name(_PASS_STATES, row.state),
        dispatch_status="not_requested"
        if _held(row)
        else _name(_DISPATCH_STATES, row.dispatch_status),
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

    def _insert_capture(
        self,
        session: Session,
        owner_user_id: str | None,
        idempotency_key: str,
        request_hash: str,
        target: ReviewTarget,
        profile: ReviewProfile,
        *,
        now: int,
        run_id: str | None = None,
        run_role: ReviewRunRole | None = None,
    ) -> CaptureResult:
        owner = _owner_scope(owner_user_id)
        digest = _request_values(idempotency_key, request_hash)
        row = SqlStudioReviewPass(
            workspace_id=current_workspace_id(),
            id=uuid.uuid4().hex,
            owner_scope=owner,
            run_id=run_id,
            run_role=run_role,
            owner_user_id=owner_user_id,
            source_session_id=target.source_session_id,
            source_project_id=target.source_project_id,
            target_json=_snapshot(target),
            profile_json=_snapshot(profile),
            state=_PASS_STATES["prepared"],
            dispatch_status=_DISPATCH_STATES["not_requested" if run_role is None else "cancelled"],
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
        return CaptureResult(review=_review(row), operation=_operation(operation), created=True)

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
            return self._insert_capture(
                session,
                owner_user_id,
                idempotency_key,
                request_hash,
                target,
                profile,
                now=time.time_ns() // 1_000_000,
            )

        try:
            return run_write_transaction(self._write, "capture_review", write)
        except IntegrityError:
            with self._session("replay_concurrent_capture") as session:
                existing = self._request_row(session, owner, "review.capture", idempotency_key)
                if existing is None:
                    raise
                return replay(session, existing)

    def _materialize_runs(
        self, session: Session, rows: list[SqlStudioReviewRun], owner: bytes
    ) -> list[ReviewRun]:
        if not rows:
            return []
        members = session.scalars(
            select(SqlStudioReviewPass)
            .where(
                SqlStudioReviewPass.workspace_id == current_workspace_id(),
                SqlStudioReviewPass.owner_scope == owner,
                SqlStudioReviewPass.run_id.in_([row.id for row in rows]),
            )
            .order_by(SqlStudioReviewPass.created_at, SqlStudioReviewPass.id)
        )
        by_run: dict[str, list[ReviewPass]] = {row.id: [] for row in rows}
        coordinators: dict[str, ReviewPass] = {}
        for member in members:
            if member.run_id is None:
                continue
            if member.run_role == "coordinator":
                coordinators[member.run_id] = _review(member)
            else:
                by_run[member.run_id].append(_review(member))
        return [
            ReviewRun(
                id=row.id,
                target=ReviewTarget.model_validate_json(row.target_json),
                reviewers=by_run[row.id],
                created_at=row.created_at,
                patch_sha256=row.patch_sha256,
                coordinator=coordinators.get(row.id),
                plan=ReviewPlan.model_validate_json(row.plan_json) if row.plan_json else None,
            )
            for row in rows
        ]

    def _run_request(
        self, session: Session, owner: bytes, key: bytes, digest: bytes
    ) -> ReviewRun | None:
        row = session.scalar(
            select(SqlStudioReviewRun).where(
                SqlStudioReviewRun.workspace_id == current_workspace_id(),
                SqlStudioReviewRun.owner_scope == owner,
                SqlStudioReviewRun.request_key == key,
            )
        )
        if row is None:
            return None
        if bytes(row.request_hash) != digest:
            raise ReviewConflictError("Idempotency key was used for a different review run")
        return self._materialize_runs(session, [row], owner)[0]

    def find_run_request(
        self, *, owner_user_id: str | None, idempotency_key: str, request_hash: str
    ) -> ReviewRun | None:
        digest = _request_values(idempotency_key, request_hash)
        key = hashlib.sha256(idempotency_key.encode()).digest()
        with self._session("find_review_run_request") as session:
            return self._run_request(session, _owner_scope(owner_user_id), key, digest)

    def capture_run(
        self,
        *,
        owner_user_id: str | None,
        idempotency_key: str,
        request_hash: str,
        target: ReviewTarget,
        profiles: list[ReviewProfile],
        patch: str,
        coordinator: ReviewProfile | None = None,
        required_indices: frozenset[int] = frozenset(),
    ) -> ReviewRun:
        """Persist the complete roster and its capture receipts in one transaction."""
        project_id = target.source_project_id
        if project_id is None or not 1 <= len(profiles) <= MAX_RUN_REVIEWERS:
            raise ValueError("A run requires a project and a bounded reviewer roster")
        if required_indices and (
            coordinator is None or not required_indices.issubset(range(len(profiles)))
        ):
            raise ValueError("Required reviewers must be members of an orchestrated run")
        owner = _owner_scope(owner_user_id)
        key = hashlib.sha256(idempotency_key.encode()).digest()
        digest = _request_values(idempotency_key, request_hash)
        patch_bytes = patch.encode("utf-8")
        if not 1 <= len(patch_bytes) <= MAX_REVIEW_PATCH_BYTES:
            raise ValueError("Review run diff exceeds the patch byte budget")
        patch_sha256 = patch_digest(patch)

        def write(session: Session) -> ReviewRun:
            existing = self._run_request(session, owner, key, digest)
            if existing is not None:
                return existing
            now = time.time_ns() // 1_000_000
            run_id = uuid.uuid4().hex
            session.add(
                SqlStudioReviewRun(
                    workspace_id=current_workspace_id(),
                    id=run_id,
                    owner_scope=owner,
                    project_id=project_id,
                    pr_key=hashlib.sha256(target.pr_url.casefold().encode()).digest(),
                    request_key=key,
                    request_hash=digest,
                    target_json=_snapshot(target),
                    patch_sha256=patch_sha256,
                    created_at=now,
                )
            )
            session.add_all(
                [
                    SqlStudioReviewPatchChunk(
                        workspace_id=current_workspace_id(),
                        run_id=run_id,
                        position=position,
                        payload=patch_bytes[offset : offset + PATCH_CHUNK_BYTES],
                    )
                    for position, offset in enumerate(
                        range(0, len(patch_bytes), PATCH_CHUNK_BYTES)
                    )
                ]
            )
            members = [
                self._insert_capture(
                    session,
                    owner_user_id,
                    f"run:{run_id}:{index}",
                    request_hash,
                    target,
                    profile,
                    now=now,
                    run_id=run_id,
                    run_role=(
                        None
                        if coordinator is None
                        else "required"
                        if index in required_indices
                        else "candidate"
                    ),
                ).review
                for index, profile in enumerate(profiles)
            ]
            coordinator_member = None
            if coordinator is not None:
                coordinator_member = self._insert_capture(
                    session,
                    owner_user_id,
                    f"run:{run_id}:coordinator",
                    request_hash,
                    target,
                    coordinator,
                    now=now,
                    run_id=run_id,
                    run_role="coordinator",
                ).review
            return ReviewRun(
                id=run_id,
                target=target,
                reviewers=sorted(members, key=lambda member: member.id),
                created_at=now,
                patch_sha256=patch_sha256,
                coordinator=coordinator_member,
            )

        try:
            return run_write_transaction(self._write, "capture_review_run", write)
        except IntegrityError:
            with self._session("replay_concurrent_review_run") as session:
                existing = self._run_request(session, owner, key, digest)
                if existing is None:
                    raise
                return existing

    def get_run(self, run_id: str, *, owner_user_id: str | None) -> ReviewRun | None:
        owner = _owner_scope(owner_user_id)
        with self._session("get_owned_review_run") as session:
            row = session.scalar(
                select(SqlStudioReviewRun).where(
                    SqlStudioReviewRun.workspace_id == current_workspace_id(),
                    SqlStudioReviewRun.owner_scope == owner,
                    SqlStudioReviewRun.id == require_uuid(run_id),
                )
            )
            return self._materialize_runs(session, [row], owner)[0] if row else None

    def approve_run_plan(
        self, run_id: str, plan: ReviewPlan, *, owner_user_id: str | None
    ) -> ReviewRun:
        """Freeze one approved roster before any reviewer can start."""
        owner = _owner_scope(owner_user_id)
        serialized = plan.model_dump_json()
        encoded = encode(serialized)
        if encoded is None or len(encoded) > 16_383:
            raise ReviewConflictError("The review plan exceeds its stored byte budget")

        def write(session: Session) -> ReviewRun:
            row = session.scalar(
                select(SqlStudioReviewRun)
                .where(
                    SqlStudioReviewRun.workspace_id == current_workspace_id(),
                    SqlStudioReviewRun.owner_scope == owner,
                    SqlStudioReviewRun.id == require_uuid(run_id),
                )
                .with_for_update()
            )
            if row is None:
                raise ReviewNotFoundError("Review run not found")
            run = self._materialize_runs(session, [row], owner)[0]
            if run.plan is not None:
                if run.plan != plan:
                    raise ReviewConflictError("A different plan was already approved")
                return run
            try:
                run.validate_plan(plan)
            except ValueError as exc:
                raise ReviewConflictError(str(exc)) from exc
            selected = {decision.review_id for decision in plan.decisions if decision.selected}
            candidates = list(
                session.scalars(
                    select(SqlStudioReviewPass)
                    .where(
                        SqlStudioReviewPass.workspace_id == current_workspace_id(),
                        SqlStudioReviewPass.owner_scope == owner,
                        SqlStudioReviewPass.run_id == row.id,
                        SqlStudioReviewPass.id.in_(selected),
                    )
                    .with_for_update()
                )
            )
            if len(candidates) != len(selected) or not all(_held(member) for member in candidates):
                raise ReviewConflictError(
                    "The selected candidates are no longer held for approval"
                )
            for member in candidates:
                member.dispatch_status = _DISPATCH_STATES["not_requested"]
            row.plan_json = serialized
            return run.model_copy(update={"plan": plan})

        return run_write_transaction(self._write, "approve_review_run_plan", write)

    def get_run_patch(self, run_id: str, *, owner_user_id: str | None) -> str | None:
        run_id = require_uuid(run_id)
        with self._session("get_owned_review_run_patch") as session:
            run = session.execute(
                select(SqlStudioReviewRun.patch_sha256).where(
                    SqlStudioReviewRun.workspace_id == current_workspace_id(),
                    SqlStudioReviewRun.owner_scope == _owner_scope(owner_user_id),
                    SqlStudioReviewRun.id == run_id,
                )
            ).one_or_none()
            if run is None:
                raise ReviewNotFoundError("Review run not found")
            digest = run[0]
            if digest is None:
                return None
            chunks = list(
                session.scalars(
                    select(SqlStudioReviewPatchChunk)
                    .where(
                        SqlStudioReviewPatchChunk.workspace_id == current_workspace_id(),
                        SqlStudioReviewPatchChunk.run_id == run_id,
                    )
                    .order_by(SqlStudioReviewPatchChunk.position)
                    .limit(MAX_PATCH_CHUNKS + 1)
                )
            )
        if not 1 <= len(chunks) <= MAX_PATCH_CHUNKS or any(
            chunk.position != position or not 1 <= len(chunk.payload) <= PATCH_CHUNK_BYTES
            for position, chunk in enumerate(chunks)
        ):
            raise ReviewConflictError("The captured review diff failed its integrity check")
        try:
            patch = b"".join(chunk.payload for chunk in chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReviewConflictError("The captured review diff is not valid UTF-8 text") from exc
        if patch_digest(patch) != digest:
            raise ReviewConflictError("The captured review diff failed its integrity check")
        return patch

    def list_runs(
        self,
        *,
        project_id: str,
        owner_user_id: str | None,
        pr_url: str,
        limit: int,
        after_id: str | None = None,
    ) -> ReviewRunPage:
        if not 1 <= limit <= 25:
            raise ValueError("Review run page size must be between 1 and 25")
        owner = _owner_scope(owner_user_id)
        query = select(SqlStudioReviewRun).where(
            SqlStudioReviewRun.workspace_id == current_workspace_id(),
            SqlStudioReviewRun.owner_scope == owner,
            SqlStudioReviewRun.project_id == require_uuid(project_id),
            SqlStudioReviewRun.pr_key == hashlib.sha256(pr_url.casefold().encode()).digest(),
        )
        with self._session("list_project_pr_review_runs") as session:
            if after_id is not None:
                cursor = session.scalar(
                    query.where(SqlStudioReviewRun.id == require_uuid(after_id))
                )
                if cursor is None:
                    raise ReviewNotFoundError("Review run cursor not found")
                query = query.where(
                    tuple_(SqlStudioReviewRun.created_at, SqlStudioReviewRun.id)
                    < (cursor.created_at, cursor.id)
                )
            rows = list(
                session.scalars(
                    query.order_by(
                        SqlStudioReviewRun.created_at.desc(), SqlStudioReviewRun.id.desc()
                    ).limit(limit + 1)
                )
            )
            return ReviewRunPage(
                entries=self._materialize_runs(session, rows[:limit], owner),
                next_cursor=rows[limit - 1].id if len(rows) > limit else None,
            )

    def save_launch_error(self, review: ReviewPass, message: str) -> None:
        """Record a pre-dispatch failure without changing the right to retry."""

        def write(session: Session) -> None:
            row = self._pass_row(session, review.id, _owner_scope(review.owner_user_id), lock=True)
            if (
                row
                and row.run_id
                and (
                    row.dispatch_status == _DISPATCH_STATES["not_requested"]
                    or (row.run_role == "coordinator" and _held(row))
                )
            ):
                row.launch_error = message[:512]

        run_write_transaction(self._write, "save_review_launch_error", write)

    def get_report_snapshot(self, review: ReviewPass) -> str | None:
        with self._session("get_review_report_snapshot") as session:
            row = self._pass_row(session, review.id, _owner_scope(review.owner_user_id))
            return row.report_json if row and row.run_id else None

    def save_report_snapshot(self, review: ReviewPass, report_json: str) -> str:
        """Retain the first observed complete report for this run member."""

        def write(session: Session) -> str:
            row = self._pass_row(session, review.id, _owner_scope(review.owner_user_id), lock=True)
            if row is None or not row.run_id or row.run_id != review.run_id:
                raise ReviewNotFoundError("Review run member not found")
            saved = row.report_json
            if saved is not None:
                return saved
            row.report_json = report_json
            return report_json

        return run_write_transaction(self._write, "save_review_report_snapshot", write)

    def list_page(
        self,
        *,
        owner_user_id: str | None,
        source_session_id: str | None = None,
        source_project_id: str | None = None,
        limit: int,
        after_id: str | None = None,
    ) -> ReviewPage:
        if not 1 <= limit <= 100:
            raise ValueError("Page size must be between 1 and 100")
        owner = _owner_scope(owner_user_id)
        if (source_session_id is None) == (source_project_id is None):
            raise ValueError("Review history requires one origin")
        if source_session_id is not None:
            source_session_id = require_uuid(source_session_id)
            origin = SqlStudioReviewPass.source_session_id == source_session_id
        else:
            assert source_project_id is not None
            source_project_id = require_uuid(source_project_id)
            origin = SqlStudioReviewPass.source_project_id == source_project_id
        with self._session("list_owned_origin_reviews") as session:
            query = select(SqlStudioReviewPass).where(
                SqlStudioReviewPass.workspace_id == current_workspace_id(),
                SqlStudioReviewPass.owner_scope == owner,
                origin,
            )
            if after_id is not None:
                try:
                    cursor = self._pass_row(session, require_uuid(after_id), owner)
                except ValueError:
                    cursor = None
                if cursor is None or (cursor.source_session_id, cursor.source_project_id) != (
                    source_session_id,
                    source_project_id,
                ):
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
        claim_coordinator: bool = False,
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
            if claim_coordinator:
                if action != "review.start" or row.run_role != "coordinator" or not _held(row):
                    raise ReviewConflictError("The coordinator already has a start attempt")
                # Claim in this transaction so an older app can never consume a pending start.
                row.dispatch_status = _DISPATCH_STATES["dispatching"]
                row.reviewer_session_id = row.id
                row.launch_error = None
            elif _held(row):
                raise ReviewConflictError("This review is held for planning")
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
                state=_OP_STATES["dispatching" if claim_coordinator else "pending"],
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(operation)
            event = "operation.dispatching" if claim_coordinator else "operation.reserved"
            self._event(session, row, operation, event, now)
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
                if state == "dispatching":
                    review.launch_error = None
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
        reviewer_session_id: str | None = None,
    ) -> ReviewOperation:
        """Persist dispatching before a caller performs its one external attempt."""
        return self._transition(
            review_id,
            operation_id,
            owner_user_id=owner_user_id,
            expected_version=expected_version,
            allowed=("pending",),
            state="dispatching",
            reviewer_session_id=reviewer_session_id,
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
