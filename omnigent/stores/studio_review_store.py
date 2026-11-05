"""Private review snapshots, idempotent reservations and single-claim dispatch."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from itertools import groupby
from typing import Any, Literal, TypeVar, cast

from pydantic import TypeAdapter
from sqlalchemy import func, select, tuple_, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.account_authority import account_generation, require_active_account
from omnigent.db.compression import encode
from omnigent.db.db_models import (
    SqlStudioFindingArtifact,
    SqlStudioInlineDraft,
    SqlStudioReviewCollaborator,
    SqlStudioReviewComment,
    SqlStudioReviewEvent,
    SqlStudioReviewOperation,
    SqlStudioReviewPass,
    SqlStudioReviewPatchChunk,
    SqlStudioReviewQuestion,
    SqlStudioReviewReceiptChunk,
    SqlStudioReviewRun,
    SqlUser,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.studio.reviews.artifacts import (
    EvidenceCheck,
    EvidenceCheckSelection,
    EvidenceStorageError,
    FindingArtifact,
    inspect_evidence,
)
from omnigent.studio.reviews.discussion import (
    CreateReviewComment,
    DiffDiscussionAnchor,
    DiscussionAnchor,
    FindingDiscussionAnchor,
    InlineDraft,
    InlineDraftSelection,
    InlinePublicationState,
    QuestionAnswer,
    QuestionDispatch,
    QuestionInput,
    QuestionRecord,
    ReviewComment,
    ReviewQuestion,
    SharedReviewAnswer,
)
from omnigent.studio.reviews.evidence import (
    MAX_PATCH_CHUNKS,
    MAX_REVIEW_PATCH_BYTES,
    PATCH_CHUNK_BYTES,
    patch_digest,
)
from omnigent.studio.reviews.evidence_files import EvidenceMediaType
from omnigent.studio.reviews.hosted_check_models import (
    CHECK_JOB_SECONDS,
    ApproveHostedCheck,
    HostedCheckJob,
    proposal_digest,
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
from omnigent.studio.reviews.questions import question_session_id
from omnigent.studio.reviews.reproduction import (
    MAX_REPRODUCTION_BYTES,
    MAX_REPRODUCTION_CHUNKS,
    REPRODUCTION_CHUNK_BYTES,
    AttachedReproduction,
    ReproductionReceipt,
    ReproductionSource,
    ReviewCheckReceipt,
    ReviewCheckUpload,
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
_MAX_RECEIPTS_PER_RUN = 24
_MAX_RECEIPTS_PER_FINDING = 4
_MAX_ARTIFACTS_PER_RUN = 12
_MAX_ARTIFACTS_PER_FINDING = 6
_MAX_COLLABORATORS = 32
_MAX_DISCUSSION_COMMENTS = 200
_MAX_DISCUSSION_QUESTIONS = 32
_MAX_INLINE_DRAFTS = 128
_DISCUSSION_ANCHOR = TypeAdapter(DiscussionAnchor)


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
            owner_generation = require_active_account(session, owner_user_id)
            now = time.time_ns() // 1_000_000
            run_id = uuid.uuid4().hex
            session.add(
                SqlStudioReviewRun(
                    workspace_id=current_workspace_id(),
                    id=run_id,
                    owner_scope=owner,
                    owner_generation=owner_generation,
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

    @staticmethod
    def _run_row(
        session: Session, run_id: str, owner: bytes, *, lock: bool = False
    ) -> SqlStudioReviewRun | None:
        query = select(SqlStudioReviewRun).where(
            SqlStudioReviewRun.workspace_id == current_workspace_id(),
            SqlStudioReviewRun.owner_scope == owner,
            SqlStudioReviewRun.id == require_uuid(run_id),
        )
        return session.scalar(query.with_for_update() if lock else query)

    def get_run(self, run_id: str, *, owner_user_id: str | None) -> ReviewRun | None:
        owner = _owner_scope(owner_user_id)
        with self._session("get_owned_review_run") as session:
            row = self._run_row(session, run_id, owner)
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
            row = self._run_row(session, run_id, owner, lock=True)
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

    @staticmethod
    def _finding_artifact_key(run_id: str, finding_key: str, digest: str) -> str:
        return f"studio-evidence/{current_workspace_id()}/{run_id}/{finding_key}/{digest}"

    @staticmethod
    def _serialize_finding_artifact(metadata: FindingArtifact) -> str:
        serialized = metadata.model_dump_json()
        encoded = encode(serialized)
        if encoded is None or len(encoded) > 16_383:
            raise ValueError("Evidence metadata exceeds its size limit")
        return serialized

    def _match_evidence_check(
        self,
        run_id: str,
        finding_key: str,
        selection: EvidenceCheckSelection,
        *,
        owner: str | None,
        digest: str,
        media_type: EvidenceMediaType,
        size: int,
    ) -> EvidenceCheck:
        entries = self.list_reproductions(run_id, finding_key, owner_user_id=owner, actor=owner)
        receipt = next(
            (entry.receipt for entry in entries if entry.digest == selection.receipt_digest), None
        )
        if isinstance(receipt, ReproductionReceipt):
            side = receipt.base if selection.side == "base" else receipt.head
            for item in side.evidence:
                if (item.name, item.digest, item.media_type, item.byte_size) == (
                    selection.name,
                    digest,
                    media_type,
                    size,
                ):
                    return EvidenceCheck(**selection.model_dump(), commit=side.commit)
        raise ReviewConflictError("The file does not match the selected check receipt")

    def attach_finding_artifact(
        self,
        run_id: str,
        finding_key: str,
        patch_sha256: str,
        media_type: EvidenceMediaType,
        content: bytes,
        *,
        owner: str | None,
        artifacts: ArtifactStore,
        check_selection: EvidenceCheckSelection | None = None,
    ) -> FindingArtifact:
        measurements = inspect_evidence(media_type, content)
        digest = hashlib.sha256(content).hexdigest()
        binary_key, binary_digest = bytes.fromhex(finding_key), bytes.fromhex(digest)
        check = (
            self._match_evidence_check(
                run_id,
                finding_key,
                check_selection,
                owner=owner,
                digest=digest,
                media_type=media_type,
                size=len(content),
            )
            if check_selection is not None
            else None
        )

        def write(session: Session) -> FindingArtifact:
            self._lock_discussion_writer(session, run_id, owner, owner)
            run = self._run_row(session, run_id, _owner_scope(owner))
            if run is None:
                raise ReviewNotFoundError("Review run not found")
            if run.patch_sha256 != patch_sha256:
                raise ReviewConflictError("The evidence does not match the saved diff")
            target = ReviewTarget.model_validate_json(run.target_json)
            artifact = SqlStudioFindingArtifact
            scope = (artifact.workspace_id == current_workspace_id(), artifact.run_id == run_id)
            keys = list(
                session.execute(
                    select(artifact.finding_key, artifact.digest)
                    .where(*scope)
                    .limit(_MAX_ARTIFACTS_PER_RUN + 1)
                )
            )
            if (binary_key, binary_digest) in keys:
                row = session.get(
                    artifact, (current_workspace_id(), run_id, binary_key, binary_digest)
                )
                assert row is not None
                metadata = FindingArtifact.model_validate_json(row.metadata_json)
                if check is None or check in metadata.checks:
                    return metadata
                if len(metadata.checks) >= 8:
                    raise ReviewConflictError(
                        "This evidence file has reached its check link limit"
                    )
                metadata = metadata.model_copy(update={"checks": (*metadata.checks, check)})
                row.metadata_json = self._serialize_finding_artifact(metadata)
                return metadata
            if (
                len(keys) >= _MAX_ARTIFACTS_PER_RUN
                or sum(key == binary_key for key, _ in keys) >= _MAX_ARTIFACTS_PER_FINDING
            ):
                raise ReviewConflictError("This review has reached its evidence file limit")
            metadata = FindingArtifact(
                digest=digest,
                source=ReproductionSource(
                    run_id=run_id,
                    pr_url=target.pr_url,
                    patch_sha256=patch_sha256,
                    base_sha=target.base_sha,
                    head_sha=target.head_sha,
                ),
                media_type=media_type,
                byte_size=len(content),
                attached_at=time.time_ns() // 1_000_000,
                measurements=measurements,
                checks=(check,) if check is not None else (),
            )
            serialized = self._serialize_finding_artifact(metadata)
            # Write the deterministic key before publishing metadata. Retrying after
            # a transaction rollback writes the same bytes to the same key.
            try:
                artifacts.put(self._finding_artifact_key(run_id, finding_key, digest), content)
            except Exception as exc:
                raise EvidenceStorageError("The evidence file could not be stored") from exc
            session.add(
                artifact(
                    workspace_id=current_workspace_id(),
                    run_id=run_id,
                    finding_key=binary_key,
                    digest=binary_digest,
                    attached_at=metadata.attached_at,
                    metadata_json=serialized,
                )
            )
            return metadata

        return run_write_transaction(self._write, "attach_finding_artifact", write)

    def list_finding_artifacts(
        self,
        run_id: str,
        finding_key: str,
        *,
        owner: str | None,
        actor: str | None,
    ) -> list[FindingArtifact]:
        with self._session("list_finding_artifacts") as session:
            self._require_discussion_access(session, run_id, actor, owner, lock=False)
            artifact = SqlStudioFindingArtifact
            rows = session.scalars(
                select(artifact.metadata_json)
                .where(
                    artifact.workspace_id == current_workspace_id(),
                    artifact.run_id == run_id,
                    artifact.finding_key == bytes.fromhex(finding_key),
                )
                .order_by(artifact.attached_at.desc(), artifact.digest)
                .limit(_MAX_ARTIFACTS_PER_FINDING)
            )
            return [FindingArtifact.model_validate_json(row) for row in rows]

    def read_finding_artifact(
        self,
        run_id: str,
        finding_key: str,
        digest: str,
        *,
        owner: str | None,
        actor: str | None,
        artifacts: ArtifactStore,
    ) -> tuple[FindingArtifact, bytes]:
        with self._session("read_finding_artifact") as session:
            self._require_discussion_access(session, run_id, actor, owner, lock=False)
            row = session.get(
                SqlStudioFindingArtifact,
                (
                    current_workspace_id(),
                    run_id,
                    bytes.fromhex(finding_key),
                    bytes.fromhex(digest),
                ),
            )
            if row is None:
                raise ReviewNotFoundError("Evidence file not found")
            metadata = FindingArtifact.model_validate_json(row.metadata_json)
        try:
            content = artifacts.get(self._finding_artifact_key(run_id, finding_key, digest))
        except Exception as exc:
            raise EvidenceStorageError("The evidence file could not be retrieved") from exc
        if (
            metadata.digest != digest
            or len(content) != metadata.byte_size
            or hashlib.sha256(content).hexdigest() != digest
        ):
            raise ReviewConflictError("The evidence file failed its integrity check")
        return metadata, content

    def attach_reproduction(
        self,
        run_id: str,
        finding_key: str,
        receipt: ReviewCheckReceipt,
        *,
        owner_user_id: str | None,
    ) -> AttachedReproduction:
        owner = _owner_scope(owner_user_id)
        key = bytes.fromhex(finding_key)
        payload = receipt.model_dump_json().encode("utf-8")
        if not 1 <= len(payload) <= MAX_REPRODUCTION_BYTES:
            raise ReviewConflictError("The reproduction receipt exceeds its stored byte budget")
        digest = hashlib.sha256(payload).digest()

        def write(session: Session) -> AttachedReproduction:
            run = self._run_row(session, run_id, owner, lock=True)
            if run is None:
                raise ReviewNotFoundError("Review run not found")
            target = ReviewTarget.model_validate_json(run.target_json)
            source = receipt.source
            if (
                run.patch_sha256 is None
                or source
                != ReproductionSource(
                    run_id=run.id,
                    pr_url=target.pr_url,
                    patch_sha256=run.patch_sha256,
                    base_sha=target.base_sha,
                    head_sha=target.head_sha,
                )
                or (
                    isinstance(receipt, ReproductionReceipt)
                    and (receipt.base.commit, receipt.head.commit)
                    != (target.base_sha, target.head_sha)
                )
            ):
                raise ReviewConflictError(
                    "The receipt does not match this saved PR revision and diff"
                )
            if isinstance(receipt, ReproductionReceipt) and receipt.proposal_id is not None:
                self._require_discussion_access(
                    session, run_id, owner_user_id, owner_user_id, lock=False
                )
                row = self._question_row(
                    session, run_id, receipt.proposal_id, owner_user_id, run.owner_generation
                )
                proposal = self._question_record(row).view if row is not None else None
                if (
                    proposal is None
                    or proposal.action != "propose_check"
                    or proposal.anchor
                    != FindingDiscussionAnchor(kind="finding", finding_key=finding_key)
                    or proposal.proposed_check is None
                    or not proposal.proposed_check.matches(receipt)
                ):
                    raise ReviewConflictError(
                        "The receipt does not match the saved check proposal"
                    )
            headers = list(
                session.execute(
                    select(
                        SqlStudioReviewReceiptChunk.finding_key,
                        SqlStudioReviewReceiptChunk.receipt_digest,
                        SqlStudioReviewReceiptChunk.attached_at,
                    )
                    .where(
                        SqlStudioReviewReceiptChunk.workspace_id == current_workspace_id(),
                        SqlStudioReviewReceiptChunk.run_id == run.id,
                        SqlStudioReviewReceiptChunk.position == 0,
                    )
                    .limit(_MAX_RECEIPTS_PER_RUN + 1)
                )
            )
            matching = [entry for entry in headers if entry.finding_key == key]
            for entry in matching:
                if entry.receipt_digest == digest:
                    return AttachedReproduction(
                        digest=digest.hex(), attached_at=entry.attached_at, receipt=receipt
                    )
            if len(headers) >= _MAX_RECEIPTS_PER_RUN or len(matching) >= _MAX_RECEIPTS_PER_FINDING:
                raise ReviewConflictError("This review has reached its reproduction receipt limit")
            attached = time.time_ns() // 1_000_000
            session.add_all(
                SqlStudioReviewReceiptChunk(
                    workspace_id=current_workspace_id(),
                    run_id=run.id,
                    finding_key=key,
                    receipt_digest=digest,
                    position=position,
                    attached_at=attached,
                    payload=payload[offset : offset + REPRODUCTION_CHUNK_BYTES],
                )
                for position, offset in enumerate(range(0, len(payload), REPRODUCTION_CHUNK_BYTES))
            )
            return AttachedReproduction(digest=digest.hex(), attached_at=attached, receipt=receipt)

        return run_write_transaction(self._write, "attach_review_reproduction", write)

    def list_reproductions(
        self,
        run_id: str,
        finding_key: str,
        *,
        owner_user_id: str | None,
        actor: str | None,
    ) -> list[AttachedReproduction]:
        with self._session("list_review_reproductions") as session:
            if actor != owner_user_id:
                self._require_discussion_access(session, run_id, actor, owner_user_id, lock=False)
            elif self._run_row(session, run_id, _owner_scope(owner_user_id)) is None:
                raise ReviewNotFoundError("Review run not found")
            chunk = SqlStudioReviewReceiptChunk
            rows = list(
                session.scalars(
                    select(chunk)
                    .where(
                        chunk.workspace_id == current_workspace_id(),
                        chunk.run_id == run_id,
                        chunk.finding_key == bytes.fromhex(finding_key),
                    )
                    .order_by(chunk.attached_at.desc(), chunk.receipt_digest, chunk.position)
                    .limit(_MAX_RECEIPTS_PER_FINDING * MAX_REPRODUCTION_CHUNKS + 1)
                )
            )
            results = []
            for digest, parts in groupby(rows, key=lambda row: row.receipt_digest):
                chunks = list(parts)
                payload = b"".join(part.payload for part in chunks)
                if (
                    len(results) >= _MAX_RECEIPTS_PER_FINDING
                    or any(
                        part.position != index
                        or not 1 <= len(part.payload) <= REPRODUCTION_CHUNK_BYTES
                        for index, part in enumerate(chunks)
                    )
                    or hashlib.sha256(payload).digest() != digest
                ):
                    raise ReviewConflictError(
                        "The reproduction receipt failed its integrity check"
                    )
                results.append(
                    AttachedReproduction(
                        digest=digest.hex(),
                        attached_at=chunks[0].attached_at,
                        receipt=ReviewCheckUpload.model_validate_json(payload).root,
                    )
                )
            return results

    @staticmethod
    def _discussion_owner_current(
        session: Session, run: SqlStudioReviewRun, owner: str | None
    ) -> bool:
        if owner is None:
            return run.owner_generation is None
        account = session.get(SqlUser, (current_workspace_id(), owner), populate_existing=True)
        if account is not None and (account.id != owner or account.deleted_at is not None):
            return False
        generation = account.account_generation if account is not None else None
        return run.owner_generation == generation and account_generation(owner) in (
            None,
            generation,
        )

    def review_discussion_owner_current(self, run_id: str, owner: str | None) -> bool:
        with self._session("check_review_discussion_owner") as session:
            run = self._run_row(session, run_id, _owner_scope(owner))
            return run is not None and self._discussion_owner_current(session, run, owner)

    @staticmethod
    def _collaborator(
        session: Session, run_id: str, actor: str
    ) -> SqlStudioReviewCollaborator | None:
        return session.scalar(
            select(SqlStudioReviewCollaborator)
            .where(
                SqlStudioReviewCollaborator.workspace_id == current_workspace_id(),
                SqlStudioReviewCollaborator.run_id == require_uuid(run_id),
                SqlStudioReviewCollaborator.user_key == _owner_scope(actor),
            )
            .execution_options(populate_existing=True)
        )

    @staticmethod
    def _active_collaborator(session: Session, grant: SqlStudioReviewCollaborator) -> bool:
        users = {
            row.id: row
            for row in session.scalars(
                select(SqlUser)
                .where(
                    SqlUser.workspace_id == current_workspace_id(),
                    SqlUser.id.in_([grant.user_id, grant.owner_user_id]),
                )
                .execution_options(populate_existing=True)
            )
        }
        actor, owner = users.get(grant.user_id), users.get(grant.owner_user_id)
        authenticated_generation = account_generation(grant.user_id)
        return (
            actor is not None
            and actor.deleted_at is None
            and actor.account_generation == grant.user_generation
            and authenticated_generation in (None, grant.user_generation)
            and (
                (owner is None and grant.owner_generation is None)
                or (
                    owner is not None
                    and owner.deleted_at is None
                    and owner.account_generation == grant.owner_generation
                )
            )
        )

    def discussion_owner(self, run_id: str, actor: str) -> str | None:
        with self._session("resolve_review_collaborator") as session:
            grant = self._collaborator(session, run_id, actor)
            return (
                grant.owner_user_id
                if grant and self._active_collaborator(session, grant)
                else None
            )

    def list_review_collaborators(self, run_id: str, *, owner: str) -> list[str]:
        with self._session("list_review_collaborators") as session:
            if self._run_row(session, run_id, _owner_scope(owner)) is None:
                raise ReviewNotFoundError("Review run not found")
            return list(
                session.scalars(
                    select(SqlStudioReviewCollaborator.user_id)
                    .where(
                        SqlStudioReviewCollaborator.workspace_id == current_workspace_id(),
                        SqlStudioReviewCollaborator.run_id == run_id,
                    )
                    .order_by(SqlStudioReviewCollaborator.user_id)
                    .limit(_MAX_COLLABORATORS)
                )
            )

    def set_review_collaborator(
        self,
        run_id: str,
        user_id: str,
        *,
        owner: str,
        target_generation: str | None = None,
        revoke: bool = False,
    ) -> None:
        def write(session: Session) -> None:
            owner_generation = require_active_account(
                session, owner, related_accounts=None if revoke else {user_id: target_generation}
            )
            run = self._run_row(session, run_id, _owner_scope(owner), lock=True)
            if run is None or not self._discussion_owner_current(session, run, owner):
                raise ReviewNotFoundError("Review run not found")
            grant = self._collaborator(session, run_id, user_id)
            if revoke:
                if grant is not None:
                    session.delete(grant)
                return
            if grant is None:
                count = session.scalar(
                    select(func.count())
                    .select_from(SqlStudioReviewCollaborator)
                    .where(
                        SqlStudioReviewCollaborator.workspace_id == current_workspace_id(),
                        SqlStudioReviewCollaborator.run_id == run_id,
                    )
                )
                if count is not None and count >= _MAX_COLLABORATORS:
                    raise ReviewConflictError("This review has reached its collaborator limit")
                grant = SqlStudioReviewCollaborator(
                    workspace_id=current_workspace_id(),
                    run_id=run_id,
                    user_key=_owner_scope(user_id),
                    user_id=user_id,
                    owner_user_id=owner,
                )
                session.add(grant)
            grant.owner_generation = owner_generation
            grant.user_generation = target_generation

        run_write_transaction(self._write, "change_review_collaborator", write)

    @staticmethod
    def _comment(row: SqlStudioReviewComment) -> ReviewComment:
        return ReviewComment(
            id=row.id,
            anchor=_DISCUSSION_ANCHOR.validate_json(row.anchor_json),
            author=row.author_user_id,
            body=row.body,
            created_at=row.created_at,
        )

    def _require_discussion_access(
        self, session: Session, run_id: str, actor: str | None, owner: str | None, *, lock: bool
    ) -> None:
        run = self._run_row(session, run_id, _owner_scope(owner), lock=lock)
        if run is None or not self._discussion_owner_current(session, run, owner):
            raise ReviewNotFoundError("Review run not found")
        if actor != owner:
            grant = self._collaborator(session, run_id, actor) if actor is not None else None
            if (
                grant is None
                or grant.owner_user_id != owner
                or not self._active_collaborator(session, grant)
            ):
                raise ReviewNotFoundError("Review run not found")

    def list_discussion_comments(
        self, run_id: str, *, actor: str | None, owner: str | None
    ) -> list[ReviewComment]:
        with self._session("read_review_discussion") as session:
            self._require_discussion_access(session, run_id, actor, owner, lock=False)
            rows = session.scalars(
                select(SqlStudioReviewComment)
                .where(
                    SqlStudioReviewComment.workspace_id == current_workspace_id(),
                    SqlStudioReviewComment.run_id == run_id,
                )
                .order_by(SqlStudioReviewComment.created_at, SqlStudioReviewComment.id)
                .limit(_MAX_DISCUSSION_COMMENTS)
            )
            return [self._comment(row) for row in rows]

    def post_discussion_comment(
        self, run_id: str, comment: CreateReviewComment, *, actor: str | None, owner: str | None
    ) -> ReviewComment:
        if len(comment.body.encode("utf-8")) > 8000:
            raise ReviewConflictError("The comment exceeds its stored byte budget")

        def write(session: Session) -> ReviewComment:
            generation = self._lock_discussion_writer(session, run_id, actor, owner)
            existing = session.get(
                SqlStudioReviewComment, (current_workspace_id(), run_id, comment.id)
            )
            anchor_json = comment.anchor.model_dump_json()
            if existing is not None:
                if (
                    existing.author_user_id,
                    existing.author_generation,
                    existing.anchor_json,
                    existing.body,
                ) != (actor, generation, anchor_json, comment.body):
                    raise ReviewConflictError("This comment ID was used for a different message")
                return self._comment(existing)
            count = session.scalar(
                select(func.count())
                .select_from(SqlStudioReviewComment)
                .where(
                    SqlStudioReviewComment.workspace_id == current_workspace_id(),
                    SqlStudioReviewComment.run_id == run_id,
                )
            )
            if count is not None and count >= _MAX_DISCUSSION_COMMENTS:
                raise ReviewConflictError("This review has reached its discussion limit")
            row = SqlStudioReviewComment(
                workspace_id=current_workspace_id(),
                run_id=run_id,
                id=comment.id,
                author_user_id=actor,
                author_generation=generation,
                anchor_json=anchor_json,
                body=comment.body,
                created_at=time.time_ns() // 1_000_000,
            )
            session.add(row)
            return self._comment(row)

        return run_write_transaction(self._write, "post_review_discussion_comment", write)

    def _lock_discussion_writer(
        self, session: Session, run_id: str, actor: str | None, owner: str | None
    ) -> str | None:
        related = None
        if actor != owner and owner is not None:
            run = self._run_row(session, run_id, _owner_scope(owner))
            if run is None:
                raise ReviewNotFoundError("Review run not found")
            related = {owner: run.owner_generation}
        generation = require_active_account(session, actor, related_accounts=related)
        self._require_discussion_access(session, run_id, actor, owner, lock=True)
        return generation

    @staticmethod
    def _inline_draft(row: SqlStudioInlineDraft) -> InlineDraft:
        return InlineDraft(
            id=row.id,
            thread=_DISCUSSION_ANCHOR.validate_json(row.thread_json),
            anchor=DiffDiscussionAnchor.model_validate_json(row.anchor_json),
            body=row.body,
            version=row.version,
            created_at=row.created_at,
            updated_at=row.updated_at,
            publication_state=cast(InlinePublicationState, row.publication_state),
            provider_url=row.provider_url,
        )

    def list_inline_drafts(
        self, run_id: str, *, actor: str | None, owner: str | None
    ) -> list[InlineDraft]:
        with self._session("list_inline_review_drafts") as session:
            self._require_discussion_access(session, run_id, actor, owner, lock=False)
            generation = account_generation(actor) if actor is not None else None
            rows = session.scalars(
                select(SqlStudioInlineDraft)
                .where(
                    SqlStudioInlineDraft.workspace_id == current_workspace_id(),
                    SqlStudioInlineDraft.run_id == run_id,
                    SqlStudioInlineDraft.actor_scope == _owner_scope(actor),
                    SqlStudioInlineDraft.actor_generation == generation,
                    SqlStudioInlineDraft.discarded.is_(False),
                )
                .order_by(SqlStudioInlineDraft.created_at, SqlStudioInlineDraft.id)
                .limit(_MAX_INLINE_DRAFTS)
            )
            return [self._inline_draft(row) for row in rows]

    def create_inline_draft(
        self,
        run_id: str,
        request: CreateReviewComment,
        anchor: DiffDiscussionAnchor,
        *,
        actor: str | None,
        owner: str | None,
    ) -> InlineDraft:
        intent = hashlib.sha256(request.model_dump_json().encode()).digest()
        scope = _owner_scope(actor)

        def write(session: Session) -> InlineDraft:
            generation = self._lock_discussion_writer(session, run_id, actor, owner)
            row = session.get(SqlStudioInlineDraft, (current_workspace_id(), run_id, request.id))
            if row is not None:
                if (row.actor_scope, row.actor_generation, row.intent_hash) != (
                    scope,
                    generation,
                    intent,
                ) or row.discarded:
                    raise ReviewConflictError("This draft ID is unavailable")
                return self._inline_draft(row)
            count = session.scalar(
                select(func.count())
                .select_from(SqlStudioInlineDraft)
                .where(
                    SqlStudioInlineDraft.workspace_id == current_workspace_id(),
                    SqlStudioInlineDraft.run_id == run_id,
                )
            )
            if count is not None and count >= _MAX_INLINE_DRAFTS:
                raise ReviewConflictError("This review has reached its inline draft limit")
            now = time.time_ns() // 1_000_000
            row = SqlStudioInlineDraft(
                workspace_id=current_workspace_id(),
                run_id=run_id,
                id=request.id,
                actor_scope=scope,
                actor_generation=generation,
                intent_hash=intent,
                thread_json=request.anchor.model_dump_json(),
                anchor_json=anchor.model_dump_json(),
                body=request.body,
                version=1,
                discarded=False,
                publication_state="draft",
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            return self._inline_draft(row)

        return run_write_transaction(self._write, "create_inline_review_draft", write)

    def change_inline_draft(
        self,
        run_id: str,
        draft_id: str,
        *,
        actor: str | None,
        owner: str | None,
        version: int,
        body: str | None,
    ) -> InlineDraft | None:
        def write(session: Session) -> InlineDraft | None:
            generation = self._lock_discussion_writer(session, run_id, actor, owner)
            row = session.get(SqlStudioInlineDraft, (current_workspace_id(), run_id, draft_id))
            if row is None or (row.actor_scope, row.actor_generation) != (
                _owner_scope(actor),
                generation,
            ):
                raise ReviewNotFoundError("Inline draft not found")
            if row.discarded:
                if body is None and row.version == version + 1:
                    return None
                raise ReviewNotFoundError("Inline draft not found")
            if row.publication_state != "draft":
                raise ReviewConflictError("This draft is locked for publication")
            if body is not None and row.version in (version, version + 1) and row.body == body:
                return self._inline_draft(row)
            if row.version != version:
                raise ReviewConflictError("This draft changed. Reload it before saving.")
            row.body = body or ""
            row.discarded = body is None
            row.version += 1
            row.updated_at = time.time_ns() // 1_000_000
            return None if row.discarded else self._inline_draft(row)

        return run_write_transaction(self._write, "change_inline_review_draft", write)

    def claim_inline_publication(
        self,
        run_id: str,
        *,
        owner: str | None,
        selected: list[InlineDraftSelection],
        digest: str,
    ) -> tuple[list[InlineDraft], bool]:
        token = bytes.fromhex(digest)

        def write(session: Session) -> tuple[list[InlineDraft], bool]:
            generation = self._lock_discussion_writer(session, run_id, owner, owner)
            rows = []
            for choice in selected:
                row = session.get(
                    SqlStudioInlineDraft, (current_workspace_id(), run_id, choice.id)
                )
                if (
                    row is None
                    or row.discarded
                    or (row.actor_scope, row.actor_generation) != (_owner_scope(owner), generation)
                    or row.version != choice.version
                ):
                    raise ReviewConflictError("The selected drafts changed. Create a new preview.")
                rows.append(row)
            if all(row.publication_digest == token for row in rows):
                return [self._inline_draft(row) for row in rows], False
            if any(row.publication_state != "draft" for row in rows):
                raise ReviewConflictError("A selected draft is already claimed for publication")
            for row in rows:
                row.publication_digest = token
                row.publication_state = "sending"
                row.updated_at = time.time_ns() // 1_000_000
            return [self._inline_draft(row) for row in rows], True

        return run_write_transaction(self._write, "claim_inline_review_publication", write)

    def finish_inline_publication(
        self, run_id: str, draft_id: str, *, digest: str, provider_url: str | None
    ) -> InlineDraft:
        def write(session: Session) -> InlineDraft:
            row = session.get(
                SqlStudioInlineDraft,
                (current_workspace_id(), run_id, draft_id),
                with_for_update=True,
            )
            if row is None or row.publication_digest != bytes.fromhex(digest):
                raise ReviewConflictError("The publication reservation was lost")
            if row.publication_state == "sending":
                row.publication_state = "posted" if provider_url else "unknown"
                row.provider_url = provider_url
                row.updated_at = time.time_ns() // 1_000_000
            return self._inline_draft(row)

        return run_write_transaction(self._write, "finish_inline_review_publication", write)

    @staticmethod
    def _question_record(row: SqlStudioReviewQuestion) -> QuestionRecord:
        snapshot = QuestionInput.model_validate_json(row.input_json)
        answer = QuestionAnswer.model_validate_json(row.answer_json) if row.answer_json else None
        check = answer.proposed_check if answer else None
        job = (
            HostedCheckJob.model_validate_json(row.check_job_json) if row.check_job_json else None
        )
        if (
            job
            and job.state == "running"
            and time.time() * 1000 >= job.started_at + CHECK_JOB_SECONDS * 1000
        ):
            job = job.model_copy(update={"state": "unconfirmed"})
        return QuestionRecord(
            snapshot=snapshot,
            view=ReviewQuestion(
                id=row.id,
                anchor=snapshot.anchor,
                body=snapshot.body,
                session_id=question_session_id(row.run_id, row.id),
                harness=snapshot.execution.profile.harness,
                status=cast(QuestionDispatch, row.state),
                answer_state="ready" if answer else "pending",
                answer=answer.answer if answer else None,
                citations=answer.citations if answer else None,
                action=snapshot.execution.action,
                proposed_patch=answer.proposed_patch if answer else None,
                proposed_check=check,
                proposed_check_digest=proposal_digest(check) if check is not None else None,
                check_job=job,
                published_at=row.published_at,
                updated_at=row.updated_at,
            ),
        )

    @staticmethod
    def _question_row(
        session: Session, run_id: str, question_id: str, actor: str | None, generation: str | None
    ) -> SqlStudioReviewQuestion | None:
        row = session.get(
            SqlStudioReviewQuestion, (current_workspace_id(), run_id, require_uuid(question_id))
        )
        if row is None or (row.actor_scope, row.actor_generation) != (
            _owner_scope(actor),
            generation,
        ):
            return None
        return row

    def get_discussion_question(
        self,
        run_id: str,
        question_id: str,
        *,
        actor: str | None,
        owner: str | None,
        intent: str | None = None,
    ) -> QuestionRecord | None:
        with self._session("get_private_discussion_question") as session:
            self._require_discussion_access(session, run_id, actor, owner, lock=False)
            generation = account_generation(actor) if actor is not None else None
            row = self._question_row(session, run_id, question_id, actor, generation)
            if row is not None and intent is not None and row.intent_hash != bytes.fromhex(intent):
                raise ReviewConflictError("This question ID was used for a different request")
            return self._question_record(row) if row is not None else None

    def claim_hosted_check(
        self, run_id: str, question_id: str, approval: ApproveHostedCheck, *, owner: str | None
    ) -> tuple[HostedCheckJob, bool]:
        def write(session: Session) -> tuple[HostedCheckJob, bool]:
            generation = self._lock_discussion_writer(session, run_id, owner, owner)
            row = self._question_row(session, run_id, question_id, owner, generation)
            if row is None:
                raise ReviewNotFoundError("Sandbox check proposal not found")
            question = self._question_record(row).view
            if (
                question.action != "propose_check"
                or question.proposed_check_digest != approval.proposal_digest
                or not isinstance(question.anchor, FindingDiscussionAnchor)
            ):
                raise ReviewConflictError("The sandbox check differs from the approved proposal")
            if question.check_job is not None:
                if question.check_job.image_id != approval.image_id:
                    raise ReviewConflictError("This proposal already has a sandbox check")
                return question.check_job, False
            job = HostedCheckJob(
                id=uuid.uuid4().hex,
                state="running",
                image_id=approval.image_id,
                started_at=time.time_ns() // 1_000_000,
            )
            row.check_job_json = job.model_dump_json()
            return job, True

        return run_write_transaction(self._write, "claim_hosted_sandbox_check", write)

    def finish_hosted_check(
        self, run_id: str, question_id: str, job: HostedCheckJob, *, owner: str | None
    ) -> None:
        def write(session: Session) -> None:
            generation = self._lock_discussion_writer(session, run_id, owner, owner)
            row = self._question_row(session, run_id, question_id, owner, generation)
            if row is None or row.check_job_json is None:
                raise ReviewNotFoundError("Sandbox check not found")
            current = HostedCheckJob.model_validate_json(row.check_job_json)
            if current.id == job.id and current.state == "running":
                row.check_job_json = job.model_dump_json()

        run_write_transaction(self._write, "finish_hosted_sandbox_check", write)

    def claim_discussion_question(
        self,
        run_id: str,
        question_id: str,
        snapshot: QuestionInput,
        intent: str,
        *,
        actor: str | None,
        owner: str | None,
    ) -> tuple[QuestionRecord, bool]:
        serialized = snapshot.model_dump_json()
        if len(serialized.encode("utf-8")) > _MAX_SNAPSHOT_BYTES:
            raise ReviewConflictError("The captured question exceeds its stored byte budget")
        intent_bytes = bytes.fromhex(intent)

        def write(session: Session) -> tuple[QuestionRecord, bool]:
            generation = self._lock_discussion_writer(session, run_id, actor, owner)
            existing = session.get(
                SqlStudioReviewQuestion, (current_workspace_id(), run_id, question_id)
            )
            if existing is not None:
                if (existing.actor_scope, existing.actor_generation, existing.intent_hash) != (
                    _owner_scope(actor),
                    generation,
                    intent_bytes,
                ):
                    raise ReviewConflictError("This question ID was used for a different request")
                return self._question_record(existing), False
            count = session.scalar(
                select(func.count())
                .select_from(SqlStudioReviewQuestion)
                .where(
                    SqlStudioReviewQuestion.workspace_id == current_workspace_id(),
                    SqlStudioReviewQuestion.run_id == run_id,
                )
            )
            if count is not None and count >= _MAX_DISCUSSION_QUESTIONS:
                raise ReviewConflictError("This review has reached its question limit")
            now = time.time_ns() // 1_000_000
            row = SqlStudioReviewQuestion(
                workspace_id=current_workspace_id(),
                run_id=run_id,
                id=question_id,
                actor_scope=_owner_scope(actor),
                actor_user_id=actor,
                actor_generation=generation,
                intent_hash=intent_bytes,
                input_json=serialized,
                state="dispatching",
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            return self._question_record(row), True

        return run_write_transaction(self._write, "claim_review_discussion_question", write)

    def update_discussion_question(
        self,
        run_id: str,
        question_id: str,
        *,
        actor: str | None,
        owner: str | None,
        status: QuestionDispatch | None = None,
        answer: QuestionAnswer | None = None,
        publish: bool = False,
    ) -> ReviewQuestion:
        def write(session: Session) -> ReviewQuestion:
            generation = self._lock_discussion_writer(session, run_id, actor, owner)
            row = self._question_row(session, run_id, question_id, actor, generation)
            if row is None:
                raise ReviewNotFoundError("Review question not found")
            if status is not None and row.state == "dispatching":
                row.state = status
                row.updated_at = time.time_ns() // 1_000_000
            if answer is not None and row.answer_json is None:
                row.answer_json = answer.model_dump_json()
            if publish and row.published_at is None:
                action = QuestionInput.model_validate_json(row.input_json).execution.action
                if action == "propose_fix":
                    raise ReviewConflictError("Attach a checked patch to share a fix proposal")
                if action == "propose_check":
                    raise ReviewConflictError("Attach the check receipt to share the result")
                if row.answer_json is None:
                    raise ReviewConflictError("The question has no confirmed answer to share")
                row.published_at = time.time_ns() // 1_000_000
            return self._question_record(row).view

        return run_write_transaction(self._write, "update_review_discussion_question", write)

    def list_discussion_questions(
        self, run_id: str, *, actor: str | None, owner: str | None
    ) -> tuple[list[ReviewQuestion], list[SharedReviewAnswer]]:
        with self._session("list_review_discussion_questions") as session:
            self._require_discussion_access(session, run_id, actor, owner, lock=False)
            actor_scope = _owner_scope(actor)
            generation = account_generation(actor) if actor is not None else None
            rows = session.scalars(
                select(SqlStudioReviewQuestion)
                .where(
                    SqlStudioReviewQuestion.workspace_id == current_workspace_id(),
                    SqlStudioReviewQuestion.run_id == run_id,
                    (SqlStudioReviewQuestion.actor_scope == actor_scope)
                    | SqlStudioReviewQuestion.published_at.is_not(None),
                )
                .order_by(SqlStudioReviewQuestion.created_at, SqlStudioReviewQuestion.id)
                .limit(_MAX_DISCUSSION_QUESTIONS)
            )
            mine, shared = [], []
            for row in rows:
                owned = (row.actor_scope, row.actor_generation) == (actor_scope, generation)
                if not owned and row.published_at is None:
                    continue
                question = self._question_record(row).view
                if owned:
                    mine.append(question)
                if question.published_at is not None and question.answer is not None:
                    shared.append(
                        SharedReviewAnswer(
                            id=question.id,
                            anchor=question.anchor,
                            question=question.body,
                            answer=question.answer,
                            citations=question.citations,
                            harness=question.harness,
                            requested_by=row.actor_user_id,
                            published_at=question.published_at,
                        )
                    )
            return mine, shared

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
