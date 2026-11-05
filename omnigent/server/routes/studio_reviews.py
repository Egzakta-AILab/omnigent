"""Owner-private review capture and durable state reads."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeVar, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from omnigent.db.utils import builtin_agent_id
from omnigent.entities import Conversation, Project
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_availability import reported_harness_availability
from omnigent.native.native_coding_agents import native_coding_agent_for_agent_name
from omnigent.runner.session_prs import PullRequestRef
from omnigent.server.auth import (
    RESERVED_USER_LOCAL,
    RESERVED_USER_PUBLIC,
    AuthProvider,
    SharingMode,
)
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._host_filesystem import (
    HostFsError,
    HostFsUnavailableError,
    read_workspace_from_host,
)
from omnigent.server.routes._host_launch import resolve_host_owner
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.server.routes._session_create_validation import (
    validate_session_agent,
    validate_session_model_metadata,
)
from omnigent.server.routes._studio_project_workspace import (
    ProjectWorkspace,
    require_owned_project,
    resolve_project_workspace,
)
from omnigent.server.routes._workspace_validation import (
    WorkspaceValidationError,
    validate_workspace,
)
from omnigent.server.routes.skills import request_host_skills
from omnigent.server.schemas import SessionEventInput
from omnigent.server.session_orchestration import require_owned_session
from omnigent.server.startup_inputs import initial_input_id
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.project_store import ProjectStore
from omnigent.stores.studio_review_store import (
    ReviewConflictError,
    ReviewNotFoundError,
    StudioReviewStore,
)
from omnigent.studio.reviews.discussion import (
    AccountId,
    AskReviewQuestion,
    CreateReviewComment,
    DiscussionAnchor,
    FindingDiscussionAnchor,
    QuestionDispatch,
    QuestionExecution,
    QuestionInput,
    QuestionRecord,
    ReviewCollaboratorInput,
    ReviewComment,
    ReviewDiscussionFinding,
    ReviewDiscussionReport,
    ReviewDiscussionView,
    ReviewQuestion,
)
from omnigent.studio.reviews.evidence import (
    Anchor,
    Side,
    changed_anchors,
    changed_lines,
    displayed_anchors,
)
from omnigent.studio.reviews.launch import SKILL_NAME, review_prompt, run_review, verified_patch
from omnigent.studio.reviews.models import (
    CaptureResult,
    ReviewEvent,
    ReviewHarness,
    ReviewOperation,
    ReviewPass,
    ReviewPlanProposal,
    ReviewProfile,
    ReviewRole,
    ReviewRun,
    ReviewRunPage,
    ReviewTarget,
    Sha,
)
from omnigent.studio.reviews.plan import plan_digest, plan_instructions, read_plan_proposal
from omnigent.studio.reviews.questions import question_prompt, read_question_answer
from omnigent.studio.reviews.report import (
    ConsolidatedReviewReport,
    ReviewerRunReport,
    ReviewFinding,
    ReviewReport,
    consolidate_reports,
    qualify_citations,
    read_review_report,
    report_instructions,
)
from omnigent.studio.reviews.reproduction import (
    MAX_REPRODUCTION_BYTES,
    AttachedReproduction,
    ReproductionReceipt,
    SharedAttachedReproduction,
)
from omnigent.studio.reviews.service import (
    CaptureProjectReviewRequest,
    CaptureReviewRequest,
    CaptureReviewRunRequest,
    CaptureUnavailableError,
    ReviewService,
    review_run_fingerprint,
)
from omnigent.util.session_lifecycle import is_session_closed
from omnigent.util.uuid import uuid_to_bytes

if TYPE_CHECKING:
    from omnigent.server.host_registry import HostConnection, HostRegistry
    from omnigent.server.routes.sessions.routes_core import ReviewCreate
    from omnigent.server.routes.sessions.routes_events import ReviewSend
    from omnigent.stores.host_store import Host

CaptureContext = Callable[[Request, str, str | None], Awaitable[ReviewTarget]]
ReadPatch = Callable[[Request, ReviewTarget], Awaitable[str]]
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[!-~]+$"),
]
FindingKey = Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")]
_RequestBody = TypeVar("_RequestBody", bound=BaseModel)
_logger = logging.getLogger(__name__)


def _discussion_reviewer_key(review_id: str) -> str:
    return hashlib.sha256(f"discussion:{review_id}".encode()).hexdigest()


async def _bounded_json(
    request: Request, model: type[_RequestBody], *, limit: int
) -> _RequestBody:
    payload = bytearray()
    async for chunk in request.stream():
        if len(chunk) > limit - len(payload):
            raise HTTPException(413, f"Request exceeds {limit // 1024} KiB")
        payload.extend(chunk)
    try:
        return model.model_validate_json(payload, strict=True)
    except ValidationError as exc:
        raise HTTPException(422, "The request body is invalid") from exc


def _discussion_request_schema(model: type[BaseModel]) -> dict[str, object]:
    schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
    schema.pop("$defs", None)
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": schema}},
        }
    }


@dataclass(frozen=True)
class DiscussionAccess:
    run: ReviewRun
    project: Project
    actor: str | None
    owner: str | None
    mode: SharingMode

    @property
    def can_comment(self) -> bool:
        return self.actor == self.owner or self.mode == SharingMode.ON


class ReviewListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object: Literal["list"] = "list"
    data: list[ReviewPass]
    next_cursor: str | None


class StartReviewRunResponse(BaseModel):
    run: ReviewRun
    errors: dict[str, str]


class ReviewRunDiffResponse(BaseModel):
    base_sha: Sha
    head_sha: Sha
    sha256: str
    patch: str
    source_line: str | None = None


class ReviewEventsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object: Literal["list"] = "list"
    data: list[ReviewEvent]
    next_cursor: int | None


class ReviewRolesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profiles: list[ReviewRole] = Field(max_length=64)


class _WorkspaceRole(ReviewRole):
    instructions: Annotated[str, StringConstraints(min_length=1, max_length=8192)]

    def metadata(self) -> ReviewRole:
        return ReviewRole.model_validate(self.model_dump(exclude={"instructions"}))


class StartReviewRequest(BaseModel):
    """Start uses the captured target and profile, without overrides."""

    model_config = ConfigDict(extra="forbid")


class StartReviewRunRequest(StartReviewRequest):
    proposal_digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None


_WRITE_GATES = [Depends(require_json_content_type), Depends(require_trusted_origin)]


def _identifier(value: str) -> str:
    try:
        return uuid_to_bytes(value).hex()
    except ValueError:
        raise HTTPException(status_code=404, detail="Review or session not found") from None


async def _read_project_review_resource(
    workspace: ProjectWorkspace, op: str, params: dict[str, Any]
) -> dict[str, Any]:
    try:
        return await read_workspace_from_host(
            host_registry=workspace.registry,
            host_conn=workspace.connection,
            op=op,
            workspace=workspace.path,
            session_id=workspace.record_id,
            params=params,
        )
    except HostFsError as exc:
        if exc.status in (400, 404, 409, 422):
            raise HTTPException(exc.status, exc.message) from exc
        raise HTTPException(503, "The review host could not read this pull request") from exc
    except HostFsUnavailableError as exc:
        raise HTTPException(503, "The review host could not read this pull request") from exc


async def _capture_project_patch(workspace: ProjectWorkspace, target: ReviewTarget) -> str:
    payload = await _read_project_review_resource(
        workspace,
        "studio_project_pull_request_patch",
        {
            "repository": target.repository_path,
            "pr_url": target.pr_url,
            "base_sha": target.base_sha,
            "head_sha": target.head_sha,
            "capture": True,
            "require_active": True,
        },
    )
    try:
        return verified_patch(payload, target)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


async def _capture_project_target(
    workspace: ProjectWorkspace, repository_path: str, pr_url: str
) -> ReviewTarget:
    try:
        reference = PullRequestRef.from_url(pr_url)
    except ValueError as exc:
        raise HTTPException(400, "Choose a valid GitHub pull request URL") from exc
    if reference.provider != "github":
        raise HTTPException(400, "Studio review capture currently supports GitHub pull requests")
    payload = await _read_project_review_resource(
        workspace,
        "studio_project_pull_request",
        {
            "repository": repository_path,
            "pr_url": reference.url,
            "include_files": False,
            "require_active": True,
        },
    )
    pr = payload.get("pr")
    if (
        not isinstance(pr, dict)
        or pr.get("url") != reference.url
        or type(pr.get("number")) is not int
        or pr["number"] != reference.number
    ):
        raise HTTPException(503, "The review host returned incomplete pull request identity")
    base_sha, head_sha = pr.get("base_sha"), pr.get("head_sha")
    if not isinstance(base_sha, str) or not isinstance(head_sha, str):
        raise HTTPException(503, "The review host returned incomplete pull request revisions")
    try:
        return ReviewTarget(
            source_project_id=workspace.record_id,
            host_id=workspace.host_id,
            workspace=workspace.path,
            repository_path=repository_path,
            pr_url=reference.url,
            repository=reference.repository,
            pr_number=reference.number,
            base_sha=base_sha,
            head_sha=head_sha,
        )
    except ValidationError as exc:
        raise HTTPException(
            503, "The review host returned incomplete pull request revisions"
        ) from exc


async def _owned_source(
    source_session_id: str,
    user_id: str | None,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
    auth_provider: AuthProvider | None,
    *,
    private: bool = False,
) -> Conversation:
    if auth_provider is not None:
        if permission_store is None or user_id is None:
            raise HTTPException(status_code=503, detail="Review ownership checks are unavailable")
        try:
            return await asyncio.to_thread(
                require_owned_session,
                source_session_id,
                user_id,
                conversation_store,
                permission_store,
                private=private,
            )
        except OmnigentError as exc:
            if exc.code in (ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND):
                raise HTTPException(status_code=404, detail="Session not found") from None
            raise
    conversation = await asyncio.to_thread(conversation_store.get_conversation, source_session_id)
    if (
        conversation is None
        or conversation.archived
        or is_session_closed(conversation.labels, conversation.title)
    ):
        raise HTTPException(status_code=404, detail="Session not found")
    return conversation


async def _connected_review_host(
    request: Request, owner_id: str | None, host_id: str
) -> tuple[Host, HostRegistry, HostConnection]:
    host_store = getattr(request.app.state, "host_store", None)
    registry = getattr(request.app.state, "host_registry", None)
    if host_store is None or registry is None:
        raise HTTPException(503, "Connected review hosts are unavailable")
    host = await asyncio.to_thread(
        resolve_host_owner,
        user_id=owner_id,
        host_id=host_id,
        host_store=host_store,
    )
    conn = registry.get(host.host_id)
    if conn is None or (owner_id is not None and conn.owner != owner_id):
        raise HTTPException(503, "The review host is offline")
    return host, registry, conn


async def _read_review_profiles(
    request: Request,
    *,
    owner_id: str | None,
    host_id: str,
    workspace: str,
    session_id: str,
    profile_id: str | None = None,
) -> dict[str, Any]:
    _, registry, conn = await _connected_review_host(request, owner_id, host_id)
    try:
        return await read_workspace_from_host(
            host_registry=registry,
            host_conn=conn,
            op="studio_review_profiles",
            workspace=workspace,
            session_id=session_id,
            params={"profile_id": profile_id} if profile_id is not None else {},
        )
    except HostFsError as exc:
        if profile_id is not None and exc.status in (400, 404):
            raise HTTPException(
                409, "The workspace review profile is unavailable. Reload profiles."
            ) from exc
        if exc.status == 400:
            raise HTTPException(
                400, "The workspace review-profiles.yaml catalog is invalid"
            ) from exc
        raise HTTPException(503, "The review host could not read its workspace profiles") from exc
    except HostFsUnavailableError as exc:
        raise HTTPException(503, "The review host could not read its workspace profiles") from exc


async def _read_selected_role(
    request: Request,
    owner_id: str | None,
    target: ReviewTarget,
    profile_id: str,
    expected_digest: str,
) -> _WorkspaceRole:
    payload = await _read_review_profiles(
        request,
        owner_id=owner_id,
        host_id=target.host_id,
        workspace=target.workspace,
        session_id=target.origin_id,
        profile_id=profile_id,
    )
    try:
        role = _WorkspaceRole.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(503, "The review host returned an invalid workspace profile") from exc
    if role.id != profile_id or role.digest != expected_digest:
        raise HTTPException(409, "The workspace review profile changed. Reload profiles.")
    return role


async def _qualify_skill(request: Request, review: ReviewPass) -> None:
    """Check the connected owner host, native harness and any selected skill."""
    host, registry, conn = await _connected_review_host(
        request, review.owner_user_id, review.target.host_id
    )
    available, reason = reported_harness_availability(
        review.profile.harness, host.configured_harnesses
    )
    if available is not True:
        raise HTTPException(409, f"Review harness is unavailable: {reason or 'readiness unknown'}")
    skill_name = review.profile.skill
    if skill_name is None:
        return
    if SKILL_NAME.fullmatch(skill_name) is None:
        raise HTTPException(422, "Choose a skill with a supported native invocation name")
    try:
        result = await request_host_skills(
            host_registry=registry,
            host_conn=conn,
            harness=review.profile.harness,
            path=review.target.workspace,
            agent_id=review.profile.agent_id,
            agent_version=review.profile.agent_version,
        )
    except HTTPException as exc:
        raise HTTPException(503, "The review host could not discover its skills") from exc
    if result.status != "ok" or result.warnings or result.agent_id != review.profile.agent_id:
        raise HTTPException(503, "The review host did not return a complete skill inventory")
    if not any(skill.get("name") == skill_name for skill in result.skills):
        raise HTTPException(409, "The captured skill is unavailable on the review host")


def create_studio_reviews_router(
    review_store: StudioReviewStore,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    *,
    capture_context: CaptureContext,
    read_patch: ReadPatch | None = None,
    create_reviewer: ReviewCreate | None = None,
    send_review: ReviewSend | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    project_store: ProjectStore | None = None,
) -> APIRouter:
    """Expose owner-private captures, native starts and saved review reports."""
    router = APIRouter()
    service = ReviewService(review_store)

    async def owned_review(request: Request, review_id: str) -> ReviewPass:
        user_id = require_user(request, auth_provider)
        review = await asyncio.to_thread(
            review_store.get, _identifier(review_id), owner_user_id=user_id
        )
        if review is None:
            raise HTTPException(404, "Review not found")
        return review

    @router.get(
        "/sessions/{source_session_id}/studio/review-profiles",
        response_model=ReviewRolesResponse,
    )
    async def list_review_profiles(
        request: Request, response: Response, source_session_id: str
    ) -> ReviewRolesResponse:
        user_id = require_user(request, auth_provider)
        source = await _owned_source(
            _identifier(source_session_id),
            user_id,
            conversation_store,
            permission_store,
            auth_provider,
        )
        response.headers["Cache-Control"] = "no-store"
        if not source.host_id or not source.workspace:
            return ReviewRolesResponse(profiles=[])
        payload = await _read_review_profiles(
            request,
            owner_id=user_id,
            host_id=source.host_id,
            workspace=source.workspace,
            session_id=source.id,
        )
        try:
            return ReviewRolesResponse.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(
                503, "The review host returned an invalid profile catalog"
            ) from exc

    async def resolve_profile(user_id: str | None, body: CaptureReviewRequest) -> ReviewProfile:
        agent = await validate_session_agent(
            user_id=user_id,
            agent_id=body.agent_id,
            agent_store=agent_store,
            permission_store=permission_store,
            conversation_store=conversation_store,
        )
        model, effort = validate_session_model_metadata(
            model_override=body.model, reasoning_effort=body.reasoning_effort
        )
        return ReviewProfile(
            agent_id=agent.id,
            agent_version=str(agent.version),
            harness=body.harness,
            skill=body.skill,
            model=model,
            reasoning_effort=effort,
        )

    async def resolve_role(
        request: Request, user_id: str | None, body: CaptureReviewRequest, captured: ReviewTarget
    ) -> ReviewRole:
        assert body.profile_id is not None and body.profile_digest is not None
        selected = await _read_selected_role(
            request, user_id, captured, body.profile_id, body.profile_digest
        )
        return selected.metadata()

    async def save_capture(
        request: Request,
        response: Response,
        user_id: str | None,
        body: CaptureReviewRequest,
        idempotency_key: str,
        target: Callable[[], Awaitable[ReviewTarget]],
        *,
        source_session_id: str | None = None,
        source_project_id: str | None = None,
    ) -> dict[str, Any]:
        async def profile() -> ReviewProfile:
            return await resolve_profile(user_id, body)

        async def role(captured: ReviewTarget) -> ReviewRole:
            return await resolve_role(request, user_id, body, captured)

        try:
            result = await service.capture(
                owner_user_id=user_id,
                source_session_id=source_session_id,
                source_project_id=source_project_id,
                idempotency_key=idempotency_key,
                body=body,
                resolve_profile=profile,
                resolve_target=target,
                resolve_role=role,
            )
        except ReviewConflictError:
            raise HTTPException(
                status_code=409, detail="Review request or PR revision changed"
            ) from None
        except CaptureUnavailableError:
            raise HTTPException(
                status_code=503, detail="Review capture requires reconciliation"
            ) from None
        response.status_code = 201 if result.created else 200
        return result.model_dump(mode="json")

    @router.post(
        "/sessions/{source_session_id}/studio/reviews",
        status_code=201,
        response_model=CaptureResult,
    )
    async def capture_review(
        request: Request,
        response: Response,
        source_session_id: str,
        body: CaptureReviewRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        source_session_id = _identifier(source_session_id)
        await _owned_source(
            source_session_id, user_id, conversation_store, permission_store, auth_provider
        )

        async def target() -> ReviewTarget:
            captured = await capture_context(request, source_session_id, body.pr_url)
            source = await _owned_source(
                source_session_id, user_id, conversation_store, permission_store, auth_provider
            )
            if body.profile_id and (source.host_id, source.workspace) != (
                captured.host_id,
                captured.workspace,
            ):
                raise HTTPException(409, "The review execution target changed")
            return captured

        return await save_capture(
            request,
            response,
            user_id,
            body,
            idempotency_key,
            target,
            source_session_id=source_session_id,
        )

    @router.post(
        "/projects/{project_id}/studio/reviews",
        status_code=201,
        response_model=CaptureResult,
        dependencies=_WRITE_GATES,
    )
    async def capture_project_review(
        request: Request,
        response: Response,
        project_id: str,
        body: CaptureProjectReviewRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        project_id = _identifier(project_id)
        await require_owned_project(project_store, project_id, user_id)

        async def target() -> ReviewTarget:
            workspace = await resolve_project_workspace(
                request,
                project_store,
                project_id=project_id,
                owner_id=user_id,
                host_id=body.host_id,
                workspace=body.workspace,
            )
            return await _capture_project_target(workspace, body.repository, body.pr_url)

        return await save_capture(
            request, response, user_id, body, idempotency_key, target, source_project_id=project_id
        )

    async def owned_run(request: Request, run_id: str) -> ReviewRun:
        user_id = require_user(request, auth_provider)
        run = await asyncio.to_thread(
            review_store.get_run, _identifier(run_id), owner_user_id=user_id
        )
        if run is None or run.target.source_project_id is None:
            raise HTTPException(404, "Review run not found")
        await require_owned_project(project_store, run.target.source_project_id, user_id)
        return run

    async def saved_run_patch(run_id: str, owner: str | None) -> str | None:
        try:
            return await asyncio.to_thread(review_store.get_run_patch, run_id, owner_user_id=owner)
        except ReviewNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ReviewConflictError as exc:
            raise HTTPException(409, str(exc)) from exc

    async def run_anchors(run_id: str, owner: str | None) -> frozenset[Anchor] | None:
        patch = await saved_run_patch(run_id, owner)
        return changed_anchors(patch) if patch is not None else None

    @router.post(
        "/projects/{project_id}/studio/review-runs",
        response_model=ReviewRun,
        dependencies=_WRITE_GATES,
    )
    async def capture_project_review_run(
        request: Request,
        project_id: str,
        body: CaptureReviewRunRequest,
        idempotency_key: IdempotencyKey,
    ) -> ReviewRun:
        user_id = require_user(request, auth_provider)
        project_id = _identifier(project_id)
        await require_owned_project(project_store, project_id, user_id)
        digest = review_run_fingerprint(project_id, body)
        try:
            existing = await asyncio.to_thread(
                review_store.find_run_request,
                owner_user_id=user_id,
                idempotency_key=idempotency_key,
                request_hash=digest,
            )
            if existing is not None:
                return existing
            first = body.reviewers[0]
            workspace = await resolve_project_workspace(
                request,
                project_store,
                project_id=project_id,
                owner_id=user_id,
                host_id=first.host_id,
                workspace=first.workspace,
            )
            target = await _capture_project_target(workspace, first.repository, first.pr_url)
            if (target.base_sha, target.head_sha) != (
                first.expected_base_sha,
                first.expected_head_sha,
            ):
                raise HTTPException(
                    409, "PR revision changed; refresh the PR before capturing a run"
                )
            patch = await _capture_project_patch(workspace, target)
            try:
                changed_anchors(patch)
            except ValueError as exc:
                raise HTTPException(409, "The review diff contains a malformed text hunk") from exc

            async def capture_profile(member: CaptureProjectReviewRequest) -> ReviewProfile:
                profile = await resolve_profile(user_id, member)
                if member.profile_id is not None:
                    role = await resolve_role(request, user_id, member, target)
                    profile = profile.model_copy(update={"role": role})
                return profile

            profiles = [await capture_profile(member) for member in body.reviewers]
            coordinator = await capture_profile(body.coordinator) if body.coordinator else None
            return await asyncio.to_thread(
                review_store.capture_run,
                owner_user_id=user_id,
                idempotency_key=idempotency_key,
                request_hash=digest,
                target=target,
                profiles=profiles,
                patch=patch,
                coordinator=coordinator,
                required_indices=frozenset(body.required_reviewer_indices),
            )
        except ReviewConflictError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/projects/{project_id}/studio/review-runs", response_model=ReviewRunPage)
    async def list_project_review_runs(
        request: Request,
        response: Response,
        project_id: str,
        pr_url: Annotated[str, Query(min_length=1, max_length=2048)],
        limit: Annotated[int, Query(ge=1, le=25)] = 10,
        after_id: str | None = None,
    ) -> ReviewRunPage:
        user_id = require_user(request, auth_provider)
        project_id = _identifier(project_id)
        await require_owned_project(project_store, project_id, user_id)
        try:
            ref = PullRequestRef.from_url(pr_url)
        except ValueError as exc:
            raise HTTPException(400, "Choose a valid pull request URL") from exc
        response.headers["Cache-Control"] = "no-store"
        try:
            return await asyncio.to_thread(
                review_store.list_runs,
                project_id=project_id,
                owner_user_id=user_id,
                pr_url=ref.url,
                limit=limit,
                after_id=_identifier(after_id) if after_id else None,
            )
        except ReviewNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/studio/review-runs/{run_id}", response_model=ReviewRun)
    async def get_project_review_run(
        request: Request, response: Response, run_id: str
    ) -> ReviewRun:
        response.headers["Cache-Control"] = "no-store"
        return await owned_run(request, run_id)

    @router.get("/studio/review-runs/{run_id}/patch", response_model=ReviewRunDiffResponse)
    async def get_review_run_patch(
        request: Request,
        response: Response,
        run_id: str,
        path: Annotated[str, Query(min_length=1, max_length=1024)] | None = None,
        side: Side | None = None,
        line: Annotated[int, Query(ge=1, le=2**31 - 1)] | None = None,
    ) -> ReviewRunDiffResponse:
        run = await owned_run(request, run_id)
        patch = await saved_run_patch(run.id, require_user(request, auth_provider))
        if patch is None or run.patch_sha256 is None:
            raise HTTPException(404, "This review run has no captured diff")
        if any(value is not None for value in (path, side, line)) and any(
            value is None for value in (path, side, line)
        ):
            raise HTTPException(422, "A citation needs a path, side and line")
        source_line = (
            changed_lines(patch).get((path, side, line))
            if path is not None and side is not None and line is not None
            else None
        )
        response.headers["Cache-Control"] = "no-store"
        return ReviewRunDiffResponse(
            base_sha=run.target.base_sha,
            head_sha=run.target.head_sha,
            sha256=run.patch_sha256,
            patch=patch,
            source_line=source_line,
        )

    async def review_history(
        user_id: str | None,
        limit: int,
        after_id: str | None,
        *,
        source_session_id: str | None = None,
        source_project_id: str | None = None,
    ) -> ReviewListResponse:
        try:
            page = await asyncio.to_thread(
                review_store.list_page,
                owner_user_id=user_id,
                source_session_id=source_session_id,
                source_project_id=source_project_id,
                limit=limit,
                after_id=_identifier(after_id) if after_id is not None else None,
            )
        except ReviewNotFoundError:
            raise HTTPException(status_code=404, detail="Review cursor not found") from None
        return ReviewListResponse(data=page.entries, next_cursor=page.next_cursor)

    @router.get("/sessions/{source_session_id}/studio/reviews", response_model=ReviewListResponse)
    async def list_reviews(
        request: Request,
        source_session_id: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        after_id: str | None = None,
    ) -> ReviewListResponse:
        user_id = require_user(request, auth_provider)
        source_session_id = _identifier(source_session_id)
        await _owned_source(
            source_session_id, user_id, conversation_store, permission_store, auth_provider
        )
        return await review_history(user_id, limit, after_id, source_session_id=source_session_id)

    @router.get("/projects/{project_id}/studio/reviews", response_model=ReviewListResponse)
    async def list_project_reviews(
        request: Request,
        response: Response,
        project_id: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        after_id: str | None = None,
    ) -> ReviewListResponse:
        user_id = require_user(request, auth_provider)
        project_id = _identifier(project_id)
        await require_owned_project(project_store, project_id, user_id)
        response.headers["Cache-Control"] = "no-store"
        return await review_history(user_id, limit, after_id, source_project_id=project_id)

    @router.get("/studio/reviews/{review_id}", response_model=ReviewPass)
    async def get_review(request: Request, review_id: str) -> dict[str, Any]:
        review = await owned_review(request, review_id)
        return review.model_dump(mode="json")

    @router.get("/sessions/{reviewer_session_id}/studio/review", response_model=ReviewPass)
    async def get_session_review(
        request: Request, response: Response, reviewer_session_id: str
    ) -> ReviewPass:
        reviewer_session_id = _identifier(reviewer_session_id)
        review = await owned_review(request, reviewer_session_id)
        if review.reviewer_session_id != reviewer_session_id:
            raise HTTPException(404, "Review not found")
        await _owned_source(
            reviewer_session_id,
            review.owner_user_id,
            conversation_store,
            permission_store,
            auth_provider,
            private=True,
        )
        response.headers["Cache-Control"] = "no-store"
        return review

    @router.post(
        "/studio/reviews/{review_id}/start",
        response_model=ReviewPass,
        dependencies=_WRITE_GATES,
    )
    async def start_review(
        request: Request,
        review_id: str,
        _body: StartReviewRequest,
        idempotency_key: IdempotencyKey,
    ) -> ReviewPass:
        review = await owned_review(request, review_id)
        run = await owned_run(request, review.run_id) if review.run_id is not None else None
        if (
            run is not None
            and (run.coordinator is None or run.coordinator.id != review.id)
            and all(member.id != review.id for member in run.selected_reviewers())
        ):
            raise HTTPException(409, "This reviewer requires selection in an approved plan")
        try:
            return await launch_member(request, review, idempotency_key, run=run)
        except (HTTPException, OmnigentError) as exc:
            if review.run_id is not None:
                await asyncio.to_thread(
                    review_store.save_launch_error,
                    review,
                    str(exc.detail if isinstance(exc, HTTPException) else exc.message),
                )
            raise

    async def current_project_patch(
        request: Request, target: ReviewTarget, owner: str | None, run_id: str | None
    ) -> str:
        assert target.source_project_id is not None and target.repository_path is not None
        workspace = await resolve_project_workspace(
            request,
            project_store,
            project_id=target.source_project_id,
            owner_id=owner,
            host_id=target.host_id,
            workspace=target.workspace,
        )
        current = await _capture_project_target(workspace, target.repository_path, target.pr_url)
        if current != target:
            raise HTTPException(409, "The pull request revision or execution target changed")
        patch = await _capture_project_patch(workspace, target)
        if run_id is not None:
            captured = await saved_run_patch(run_id, owner)
            if captured is not None and captured != patch:
                raise HTTPException(409, "The pull request diff differs from the saved review run")
        return patch

    async def launch_member(
        request: Request, review: ReviewPass, idempotency_key: str, *, run: ReviewRun | None
    ) -> ReviewPass:
        owner = review.owner_user_id
        target = review.target
        project_id = target.source_project_id
        if project_id is not None:
            await require_owned_project(project_store, project_id, owner)
        else:
            await _owned_source(
                target.origin_id, owner, conversation_store, permission_store, auth_provider
            )
        if (
            create_reviewer is None
            or send_review is None
            or (project_id is None and read_patch is None)
        ):
            raise HTTPException(503, "Native review execution is unavailable")
        planning = review.run_role == "coordinator"

        async def validate_profile() -> None:
            profile = review.profile
            agent = await validate_session_agent(
                user_id=owner,
                agent_id=profile.agent_id,
                agent_store=agent_store,
                permission_store=permission_store,
                conversation_store=conversation_store,
            )
            native = native_coding_agent_for_agent_name(agent.name)
            if (
                native is None
                or native.harness != profile.harness
                or agent.session_id is not None
                or not agent.operator_authored
                or agent.id != builtin_agent_id(agent.name)
                or str(agent.version) != profile.agent_version
            ):
                raise HTTPException(409, "Choose a current built-in Codex or Claude reviewer")

        async def prepare() -> str:
            await validate_profile()
            await _qualify_skill(request, review)
            if project_id is None:
                current = await capture_context(request, target.origin_id, target.pr_url)
                if current != target:
                    raise HTTPException(
                        409, "The pull request revision or execution target changed"
                    )
            role_instructions = None
            if (role := review.profile.role) is not None:
                selected_role = await _read_selected_role(
                    request, owner, target, role.id, role.digest
                )
                if selected_role.metadata() != role:
                    raise HTTPException(
                        409, "The workspace review profile changed. Reload profiles."
                    )
                role_instructions = selected_role.instructions
            if project_id is not None:
                patch = await current_project_patch(request, target, owner, review.run_id)
            else:
                assert read_patch is not None
                patch = await read_patch(request, target)
                source = await _owned_source(
                    target.origin_id,
                    owner,
                    conversation_store,
                    permission_store,
                    auth_provider,
                )
                if (source.host_id, source.workspace) != (target.host_id, target.workspace):
                    raise HTTPException(409, "The review execution target changed")
            try:
                instructions = (
                    plan_instructions(run) if planning and run else report_instructions(review)
                )
                return review_prompt(
                    review, patch, instructions, role_instructions=role_instructions
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None

        async def dispatch(session_id: str, prompt: str) -> None:
            session = await create_reviewer(request, owner, review, session_id)
            if (
                session.id != session_id
                or not session.runner_id
                or session.agent_id != review.profile.agent_id
                or session.harness != review.profile.harness
                or (session.host_id, session.workspace) != (target.host_id, target.workspace)
            ):
                raise RuntimeError("Reviewer session binding did not match the capture")
            reviewer = await _owned_source(
                session_id,
                owner,
                conversation_store,
                permission_store,
                auth_provider,
                private=True,
            )
            if (
                reviewer.parent_conversation_id is not None
                or reviewer.agent_id != review.profile.agent_id
                or reviewer.runner_id != session.runner_id
                or (reviewer.host_id, reviewer.workspace) != (target.host_id, target.workspace)
                or (project_id is not None and reviewer.project_id != project_id)
            ):
                raise RuntimeError("Reviewer session binding changed before delivery")
            await validate_profile()
            result = await send_review(
                request,
                session_id,
                SessionEventInput(
                    type="message",
                    data={
                        "role": "user",
                        "content": [{"type": "input_text", "text": prompt}],
                        "stable_id": initial_input_id(session_id),
                    },
                ),
                session.runner_id,
            )
            pending_id = result.get("pending_id")
            if (
                result.get("queued") is not True
                or not isinstance(pending_id, str)
                or not pending_id
            ):
                raise RuntimeError("Native review input was not acknowledged")

        try:
            return await run_review(
                review_store, review, idempotency_key, prepare=prepare, dispatch=dispatch
            )
        except ReviewConflictError as exc:
            raise HTTPException(409, str(exc)) from None

    @router.post(
        "/studio/review-runs/{run_id}/start",
        response_model=StartReviewRunResponse,
        dependencies=_WRITE_GATES,
    )
    async def start_review_run(
        request: Request, run_id: str, body: StartReviewRunRequest
    ) -> StartReviewRunResponse:
        run = await owned_run(request, run_id)
        slots = asyncio.Semaphore(4)

        async def launch(review: ReviewPass) -> tuple[str, str] | None:
            if review.dispatch_status != "not_requested":
                return None
            async with slots:
                try:
                    await launch_member(
                        request, review, f"studio-review-start-{review.id}", run=run
                    )
                except (HTTPException, OmnigentError) as exc:
                    message = str(exc.detail if isinstance(exc, HTTPException) else exc.message)[
                        :512
                    ]
                    await asyncio.to_thread(review_store.save_launch_error, review, message)
                    return review.id, message
            return None

        if run.coordinator is not None and run.plan is None:
            if body.proposal_digest is None:
                members = [run.coordinator]
            else:
                proposal = await current_proposal(run)
                if proposal.plan is None or proposal.digest != body.proposal_digest:
                    raise HTTPException(409, "The proposed plan changed or is unavailable")
                owner = run.coordinator.owner_user_id
                await current_project_patch(request, run.target, owner, run.id)
                try:
                    run = await asyncio.to_thread(
                        review_store.approve_run_plan,
                        run.id,
                        proposal.plan,
                        owner_user_id=owner,
                    )
                except ReviewConflictError as exc:
                    raise HTTPException(409, str(exc)) from exc
                members = run.selected_reviewers()
        else:
            if body.proposal_digest is not None and (
                run.plan is None or plan_digest(run.plan) != body.proposal_digest
            ):
                raise HTTPException(409, "The supplied plan is not the approved roster")
            members = run.selected_reviewers()
        outcomes = await asyncio.gather(*(launch(review) for review in members))
        return StartReviewRunResponse(
            run=await owned_run(request, run.id),
            errors=dict(outcome for outcome in outcomes if outcome is not None),
        )

    async def verify_report_session(review: ReviewPass) -> None:
        if review.reviewer_session_id is not None:
            conv = await asyncio.to_thread(
                conversation_store.get_conversation, review.reviewer_session_id
            )
            if conv is not None:
                await _owned_source(
                    conv.id,
                    review.owner_user_id,
                    conversation_store,
                    permission_store,
                    auth_provider,
                    private=True,
                )
                if (conv.agent_id, conv.host_id, conv.workspace) != (
                    review.profile.agent_id,
                    review.target.host_id,
                    review.target.workspace,
                ):
                    raise HTTPException(409, "The linked reviewer session binding changed")

    async def current_proposal(run: ReviewRun) -> ReviewPlanProposal:
        assert run.coordinator is not None
        await verify_report_session(run.coordinator)
        return await asyncio.to_thread(read_plan_proposal, conversation_store, run)

    async def verified_report(
        review: ReviewPass, anchors: frozenset[Anchor] | None
    ) -> ReviewReport:
        await verify_report_session(review)
        if review.run_id is not None:
            snapshot = await asyncio.to_thread(review_store.get_report_snapshot, review)
            if snapshot is not None:
                return qualify_citations(ReviewReport.model_validate_json(snapshot), anchors)
        report = await asyncio.to_thread(read_review_report, conversation_store, review)
        if review.run_id is not None and report.state == "ready":
            snapshot = await asyncio.to_thread(
                review_store.save_report_snapshot,
                review,
                report.model_dump_json(exclude={"citation_validation", "unverified_findings"}),
            )
            return qualify_citations(ReviewReport.model_validate_json(snapshot), anchors)
        return report

    @router.get("/studio/reviews/{review_id}/report", response_model=ReviewReport)
    async def get_review_report(
        request: Request, response: Response, review_id: str
    ) -> ReviewReport:
        review = await owned_review(request, review_id)
        response.headers["Cache-Control"] = "no-store"
        anchors = (
            await run_anchors(review.run_id, review.owner_user_id)
            if review.run_id is not None
            else None
        )
        return await verified_report(review, anchors)

    async def read_consolidation(
        run: ReviewRun, anchors: frozenset[Anchor] | None, *, include_finding_keys: bool
    ) -> ConsolidatedReviewReport:
        async def read_member(review: ReviewPass) -> ReviewerRunReport:
            try:
                report = await verified_report(review, anchors)
            except HTTPException as exc:
                if exc.status_code not in (403, 404, 409):
                    raise
                report = ReviewReport(state="incomplete")
            return ReviewerRunReport(review_id=review.id, report=report)

        return consolidate_reports(
            await asyncio.gather(*(read_member(member) for member in run.selected_reviewers())),
            include_finding_keys=include_finding_keys,
        )

    @router.get("/studio/review-runs/{run_id}/report", response_model=ConsolidatedReviewReport)
    async def get_review_run_report(
        request: Request, response: Response, run_id: str, include_finding_keys: bool = False
    ) -> ConsolidatedReviewReport:
        run = await owned_run(request, run_id)
        response.headers["Cache-Control"] = "no-store"
        anchors = await run_anchors(run.id, require_user(request, auth_provider))
        report = await read_consolidation(run, anchors, include_finding_keys=include_finding_keys)
        if run.coordinator is not None and run.plan is None:
            report = report.model_copy(update={"proposal": await current_proposal(run)})
        return report

    async def require_saved_finding(
        request: Request, run_id: str, finding_key: str, *, allow_collaborator: bool = False
    ) -> tuple[ReviewRun, str | None]:
        try:
            run = await owned_run(request, run_id)
            owner = require_user(request, auth_provider)
        except HTTPException as exc:
            if not allow_collaborator or exc.status_code != 404:
                raise
            access = await discussion_access(request, run_id)
            run, owner = access.run, access.owner
        anchors = await run_anchors(run.id, owner)
        if anchors is not None:
            report = await read_consolidation(run, anchors, include_finding_keys=True)
            if any(entry.finding_key == finding_key for entry in report.findings):
                return run, owner
        raise HTTPException(404, "The finding was not found in this captured diff")

    @router.get(
        "/studio/review-runs/{run_id}/findings/{finding_key}/reproductions",
        response_model=list[AttachedReproduction | SharedAttachedReproduction],
    )
    async def get_finding_reproductions(
        request: Request, response: Response, run_id: str, finding_key: FindingKey
    ) -> list[AttachedReproduction | SharedAttachedReproduction]:
        run, owner = await require_saved_finding(
            request, run_id, finding_key, allow_collaborator=True
        )
        response.headers["Cache-Control"] = "no-store"
        actor = require_user(request, auth_provider)
        try:
            entries = await asyncio.to_thread(
                review_store.list_reproductions,
                run.id,
                finding_key,
                owner_user_id=owner,
                actor=actor,
            )
            return [
                entry if actor == owner else SharedAttachedReproduction.from_attachment(entry)
                for entry in entries
            ]
        except ReviewNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ReviewConflictError as exc:
            raise HTTPException(409, str(exc)) from exc

    async def discussion_access(request: Request, run_id: str) -> DiscussionAccess:
        run_id = _identifier(run_id)
        actor = require_user(request, auth_provider)
        mode = getattr(request.app.state, "sharing_mode", lambda: SharingMode.ON)()
        owner = actor
        run = await asyncio.to_thread(review_store.get_run, run_id, owner_user_id=owner)
        if (
            run is None
            and actor is not None
            and 0 < len(actor) <= 128
            and mode in (SharingMode.ON, SharingMode.READ_ONLY)
        ):
            owner = await asyncio.to_thread(review_store.discussion_owner, run_id, actor)
            if owner is not None:
                run = await asyncio.to_thread(review_store.get_run, run_id, owner_user_id=owner)
        if run is None or run.target.source_project_id is None:
            raise HTTPException(404, "Review run not found")
        if not await asyncio.to_thread(
            review_store.review_discussion_owner_current, run.id, owner
        ):
            raise HTTPException(404, "Review discussion not found for this account")
        project = await require_owned_project(project_store, run.target.source_project_id, owner)
        return DiscussionAccess(run, project, actor, owner, mode)

    @router.get("/studio/review-runs/{run_id}/discussion", response_model=ReviewDiscussionView)
    async def get_review_discussion(
        request: Request, response: Response, run_id: str
    ) -> ReviewDiscussionView:
        access = await discussion_access(request, run_id)
        run, target = access.run, access.run.target
        patch = await saved_run_patch(run.id, access.owner)
        if patch is None or run.patch_sha256 is None:
            raise HTTPException(404, "This review has no captured diff")
        report = await read_consolidation(run, changed_anchors(patch), include_finding_keys=True)
        profiles = {member.id: member.profile for member in run.reviewers}
        reports = []
        for member in report.reviewers:
            profile = profiles[member.review_id]
            reports.append(
                ReviewDiscussionReport(
                    key=_discussion_reviewer_key(member.review_id),
                    label=profile.role.label if profile.role else profile.skill or profile.harness,
                    state=member.report.state,
                    summary=member.report.summary,
                )
            )
        try:
            comments = await asyncio.to_thread(
                review_store.list_discussion_comments,
                run.id,
                actor=access.actor,
                owner=access.owner,
            )
            questions, answers = await asyncio.to_thread(
                review_store.list_discussion_questions,
                run.id,
                actor=access.actor,
                owner=access.owner,
            )
            collaborators = (
                await asyncio.to_thread(
                    review_store.list_review_collaborators, run.id, owner=access.owner
                )
                if access.actor == access.owner and access.owner is not None
                else None
            )
        except ReviewNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        response.headers["Cache-Control"] = "no-store"
        return ReviewDiscussionView(
            id=run.id,
            project_name=access.project.name,
            repository=target.repository,
            pr_url=target.pr_url,
            pr_number=target.pr_number,
            base_sha=target.base_sha,
            head_sha=target.head_sha,
            patch_sha256=run.patch_sha256,
            patch=patch,
            reports=reports,
            findings=[
                ReviewDiscussionFinding(key=entry.finding_key, finding=entry.finding)
                for entry in report.findings
                if entry.finding_key is not None
            ],
            comments=comments,
            questions=questions,
            agent_answers=answers,
            can_comment=access.can_comment,
            can_ask=access.can_comment and create_reviewer is not None and send_review is not None,
            can_use_review_workspace=access.actor == access.owner,
            collaborators=collaborators,
            sharing_available=(
                access.actor == access.owner
                and access.owner not in (None, RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC)
                and access.mode == SharingMode.ON
                and permission_store is not None
            ),
        )

    async def validate_discussion_anchor(
        run: ReviewRun, anchor: DiscussionAnchor, patch: str
    ) -> ReviewFinding | None:
        if isinstance(anchor, FindingDiscussionAnchor):
            report = await read_consolidation(
                run, changed_anchors(patch), include_finding_keys=True
            )
            for entry in report.findings:
                if entry.finding_key == anchor.finding_key:
                    return entry.finding
        elif anchor.is_in(displayed_anchors(patch)):
            return None
        raise HTTPException(422, "The discussion anchor is outside the captured diff")

    @router.post(
        "/studio/review-runs/{run_id}/discussion/comments",
        response_model=ReviewComment,
        dependencies=_WRITE_GATES,
        openapi_extra=_discussion_request_schema(CreateReviewComment),
    )
    async def create_review_discussion_comment(request: Request, run_id: str) -> ReviewComment:
        access = await discussion_access(request, run_id)
        if not access.can_comment or (access.actor is not None and len(access.actor) > 128):
            raise HTTPException(403, "This account cannot comment on the review")
        body = await _bounded_json(request, CreateReviewComment, limit=20 * 1024)
        patch = await saved_run_patch(access.run.id, access.owner)
        if patch is None:
            raise HTTPException(404, "This review has no captured diff")
        await validate_discussion_anchor(access.run, body.anchor, patch)
        try:
            return await asyncio.to_thread(
                review_store.post_discussion_comment,
                access.run.id,
                body,
                actor=access.actor,
                owner=access.owner,
            )
        except ReviewNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ReviewConflictError as exc:
            raise HTTPException(409, str(exc)) from exc

    async def question_execution(
        request: Request, access: DiscussionAccess, body: AskReviewQuestion
    ) -> QuestionExecution:
        target = access.run.target
        actor = access.actor
        if body.host_id is None or body.workspace is None:
            if actor != access.owner:
                raise HTTPException(403, "Choose your own connected host and directory")
            host_id, workspace, project_id = (
                target.host_id,
                target.workspace,
                target.source_project_id,
            )
        else:
            host_id, workspace, project_id = body.host_id, body.workspace, None
        _, registry, connection = await _connected_review_host(request, actor, host_id)
        try:
            canonical = await validate_workspace(
                host_registry=registry, host_id=host_id, workspace=workspace, spec_cwd=None
            )
        except WorkspaceValidationError as exc:
            raise HTTPException(400, exc.message) from exc
        if registry.get(host_id) is not connection:
            raise HTTPException(503, "The question host reconnected. Try again.")
        captured = (
            next(
                (
                    member.profile
                    for member in access.run.selected_reviewers()
                    if _discussion_reviewer_key(member.id) == body.reviewer_key
                ),
                None,
            )
            if body.reviewer_key is not None
            else None
        )
        agent_id = captured.agent_id if captured is not None else body.agent_id
        if agent_id is None:
            raise HTTPException(422, "Choose a reviewer from the captured roster")
        agent = await validate_session_agent(
            user_id=actor,
            agent_id=agent_id,
            agent_store=agent_store,
            permission_store=permission_store,
            conversation_store=conversation_store,
        )
        native = native_coding_agent_for_agent_name(agent.name)
        harness = native.harness if native is not None else None
        if (
            harness not in ("codex-native", "claude-native")
            or agent.session_id is not None
            or not agent.operator_authored
            or agent.id != builtin_agent_id(agent.name)
            or (
                captured is not None
                and (captured.agent_version != str(agent.version) or captured.harness != harness)
            )
        ):
            raise HTTPException(409, "Choose a current built-in Codex or Claude agent")
        model, effort = validate_session_model_metadata(
            model_override=captured.model if captured else body.model,
            reasoning_effort=captured.reasoning_effort if captured else body.reasoning_effort,
        )
        return QuestionExecution(
            host_id=host_id,
            workspace=canonical,
            project_id=project_id,
            pr_number=target.pr_number,
            profile=ReviewProfile(
                agent_id=agent.id,
                agent_version=str(agent.version),
                harness=cast(ReviewHarness, harness),
                model=model,
                reasoning_effort=effort,
            ),
        )

    async def question_record(
        access: DiscussionAccess, question_id: str, *, intent: str | None = None
    ) -> QuestionRecord | None:
        try:
            return await asyncio.to_thread(
                review_store.get_discussion_question,
                access.run.id,
                _identifier(question_id),
                actor=access.actor,
                owner=access.owner,
                intent=intent,
            )
        except ReviewNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ReviewConflictError as exc:
            raise HTTPException(409, str(exc)) from exc

    async def verify_question_session(
        access: DiscussionAccess, record: QuestionRecord
    ) -> Conversation:
        session = await _owned_source(
            record.view.session_id,
            access.actor,
            conversation_store,
            permission_store,
            auth_provider,
            private=True,
        )
        execution = record.snapshot.execution
        if (
            session.parent_conversation_id is not None
            or session.agent_id != execution.profile.agent_id
            or (session.host_id, session.workspace, session.project_id)
            != (execution.host_id, execution.workspace, execution.project_id)
        ):
            raise HTTPException(409, "The private question session binding changed")
        return session

    @router.post(
        "/studio/review-runs/{run_id}/discussion/questions",
        response_model=ReviewQuestion,
        dependencies=_WRITE_GATES,
        openapi_extra=_discussion_request_schema(AskReviewQuestion),
    )
    async def ask_review_question(request: Request, run_id: str) -> ReviewQuestion:
        access = await discussion_access(request, run_id)
        if not access.can_comment or (access.actor is not None and len(access.actor) > 128):
            raise HTTPException(403, "This account cannot ask an agent in the review")
        if create_reviewer is None or send_review is None:
            raise HTTPException(503, "Native question sessions are unavailable")
        body = await _bounded_json(request, AskReviewQuestion, limit=44 * 1024)
        intent = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        existing = await question_record(access, body.id, intent=intent)
        if existing is not None:
            return existing.view
        patch = await saved_run_patch(access.run.id, access.owner)
        if patch is None:
            raise HTTPException(404, "This review has no captured diff")
        finding = await validate_discussion_anchor(access.run, body.anchor, patch)
        try:
            prompt = question_prompt(body, access.run.target, patch, finding)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        execution = await question_execution(request, access, body)
        snapshot = QuestionInput(
            anchor=body.anchor,
            body=body.body,
            execution=execution,
            prompt_digest=hashlib.sha256(prompt.rstrip().encode()).hexdigest(),
        )
        try:
            record, created = await asyncio.to_thread(
                review_store.claim_discussion_question,
                access.run.id,
                body.id,
                snapshot,
                intent,
                actor=access.actor,
                owner=access.owner,
            )
        except ReviewNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ReviewConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not created:
            return record.view
        session_id = record.view.session_id

        async def settle(status: QuestionDispatch) -> ReviewQuestion:
            try:
                return await asyncio.to_thread(
                    review_store.update_discussion_question,
                    access.run.id,
                    body.id,
                    actor=access.actor,
                    owner=access.owner,
                    status=status,
                )
            except ReviewNotFoundError as exc:
                raise HTTPException(404, str(exc)) from exc

        try:
            created_session = await create_reviewer(request, access.actor, execution, session_id)
            if (
                created_session.id != session_id
                or not created_session.runner_id
                or created_session.harness != execution.profile.harness
            ):
                raise RuntimeError("Question runner did not match its binding")
            # A revoked collaborator must not send the prompt after the host starts.
            current = await discussion_access(request, run_id)
            if not current.can_comment or await question_record(current, body.id) is None:
                raise HTTPException(404, "Review question not found")
            session = await verify_question_session(current, record)
            if session.runner_id != created_session.runner_id:
                raise RuntimeError("Question runner changed before delivery")
            response = await send_review(
                request,
                session_id,
                SessionEventInput(
                    type="message",
                    data={
                        "role": "user",
                        "content": [{"type": "input_text", "text": prompt}],
                        "stable_id": initial_input_id(session_id),
                    },
                ),
                created_session.runner_id,
            )
            pending_id = response.get("pending_id")
            if (
                response.get("queued") is not True
                or not isinstance(pending_id, str)
                or not pending_id
            ):
                raise RuntimeError("Question input acknowledgement is unavailable")
        except asyncio.CancelledError:
            await settle("unknown")
            raise
        except Exception as exc:  # noqa: BLE001 — side effects may have happened before failure
            _logger.warning(
                "Review question %s dispatch is unconfirmed (%s)", body.id, type(exc).__name__
            )
            return await settle("unknown")
        return await settle("accepted")

    @router.get(
        "/studio/review-runs/{run_id}/discussion/questions/{question_id}",
        response_model=ReviewQuestion,
    )
    async def get_review_question(
        request: Request, response: Response, run_id: str, question_id: str
    ) -> ReviewQuestion:
        access = await discussion_access(request, run_id)
        record = await question_record(access, question_id)
        if record is None:
            raise HTTPException(404, "Review question not found")
        response.headers["Cache-Control"] = "no-store"
        view = record.view
        if view.answer is not None:
            return view
        if await asyncio.to_thread(conversation_store.get_conversation, view.session_id) is None:
            return view
        await verify_question_session(access, record)
        state, answer = await asyncio.to_thread(
            read_question_answer,
            conversation_store,
            session_id=view.session_id,
            question_id=view.id,
            target=access.run.target,
            prompt_digest=record.snapshot.prompt_digest,
            updated_at=view.updated_at,
            running=view.status == "accepted",
        )
        if answer is None:
            return view.model_copy(update={"answer_state": state})
        try:
            return await asyncio.to_thread(
                review_store.update_discussion_question,
                access.run.id,
                view.id,
                actor=access.actor,
                owner=access.owner,
                answer=answer,
            )
        except ReviewNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post(
        "/studio/review-runs/{run_id}/discussion/questions/{question_id}/share",
        response_model=ReviewQuestion,
        dependencies=_WRITE_GATES,
        openapi_extra=_discussion_request_schema(StartReviewRequest),
    )
    async def share_review_question_answer(
        request: Request, run_id: str, question_id: str
    ) -> ReviewQuestion:
        access = await discussion_access(request, run_id)
        if not access.can_comment:
            raise HTTPException(403, "This account cannot share an answer on the review")
        await _bounded_json(request, StartReviewRequest, limit=1024)
        try:
            return await asyncio.to_thread(
                review_store.update_discussion_question,
                access.run.id,
                _identifier(question_id),
                actor=access.actor,
                owner=access.owner,
                publish=True,
            )
        except ReviewNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ReviewConflictError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post(
        "/studio/review-runs/{run_id}/discussion/collaborators",
        status_code=204,
        dependencies=_WRITE_GATES,
        openapi_extra=_discussion_request_schema(ReviewCollaboratorInput),
    )
    async def share_review_discussion(request: Request, run_id: str) -> Response:
        run = await owned_run(request, run_id)
        owner = require_user(request, auth_provider)
        mode = getattr(request.app.state, "sharing_mode", lambda: SharingMode.ON)()
        if (
            owner is None
            or len(owner) > 128
            or owner in (RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC)
            or mode != SharingMode.ON
            or permission_store is None
        ):
            raise HTTPException(403, "Review sharing is unavailable on this instance")
        if run.patch_sha256 is None:
            raise HTTPException(404, "This review has no captured diff")
        body = await _bounded_json(request, ReviewCollaboratorInput, limit=2 * 1024)
        if body.user_id in (owner, RESERVED_USER_PUBLIC, RESERVED_USER_LOCAL):
            raise HTTPException(422, "Choose another Omnigent account")
        account = await asyncio.to_thread(permission_store.get_user, body.user_id)
        if account is None or account.id != body.user_id:
            raise HTTPException(404, "Omnigent account not found")
        try:
            await asyncio.to_thread(
                review_store.set_review_collaborator,
                run.id,
                account.id,
                owner=owner,
                target_generation=account.account_generation,
            )
        except ReviewNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ReviewConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        return Response(status_code=204)

    @router.delete(
        "/studio/review-runs/{run_id}/discussion/collaborators",
        status_code=204,
        dependencies=[Depends(require_trusted_origin)],
    )
    async def revoke_review_discussion(
        request: Request, run_id: str, user_id: AccountId
    ) -> Response:
        run = await owned_run(request, run_id)
        owner = require_user(request, auth_provider)
        if owner is None:
            raise HTTPException(404, "Review collaborator not found")
        try:
            await asyncio.to_thread(
                review_store.set_review_collaborator, run.id, user_id, owner=owner, revoke=True
            )
        except ReviewNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        return Response(status_code=204)

    @router.post(
        "/studio/review-runs/{run_id}/findings/{finding_key}/reproductions",
        response_model=AttachedReproduction,
        dependencies=_WRITE_GATES,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ReproductionReceipt"}
                    }
                },
            },
        },
    )
    async def attach_finding_reproduction(
        request: Request, response: Response, run_id: str, finding_key: FindingKey
    ) -> AttachedReproduction:
        run, owner = await require_saved_finding(request, run_id, finding_key)
        response.headers["Cache-Control"] = "no-store"
        receipt = await _bounded_json(request, ReproductionReceipt, limit=MAX_REPRODUCTION_BYTES)
        try:
            return await asyncio.to_thread(
                review_store.attach_reproduction, run.id, finding_key, receipt, owner_user_id=owner
            )
        except ReviewNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ReviewConflictError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get(
        "/studio/reviews/{review_id}/operations/{operation_id}", response_model=ReviewOperation
    )
    async def get_operation(request: Request, review_id: str, operation_id: str) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        operation = await asyncio.to_thread(
            review_store.get_operation,
            _identifier(review_id),
            _identifier(operation_id),
            owner_user_id=user_id,
        )
        if operation is None:
            raise HTTPException(status_code=404, detail="Review operation not found")
        return operation.model_dump(mode="json")

    @router.get("/studio/reviews/{review_id}/events", response_model=ReviewEventsResponse)
    async def list_events(
        request: Request,
        review_id: str,
        after_sequence: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        try:
            page = await asyncio.to_thread(
                review_store.list_events,
                _identifier(review_id),
                owner_user_id=user_id,
                after_sequence=after_sequence,
                limit=limit,
            )
        except ReviewNotFoundError:
            raise HTTPException(status_code=404, detail="Review not found") from None
        return {
            "object": "list",
            "data": [e.model_dump(mode="json") for e in page.entries],
            "next_cursor": page.next_cursor,
        }

    return router
