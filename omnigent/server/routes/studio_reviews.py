"""Owner-private review capture and durable state reads."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from omnigent.db.utils import builtin_agent_id
from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_availability import reported_harness_availability
from omnigent.native.native_coding_agents import native_coding_agent_for_agent_name
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._host_launch import resolve_host_owner
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.server.routes._session_create_validation import (
    validate_session_agent,
    validate_session_model_metadata,
)
from omnigent.server.routes.skills import request_host_skills
from omnigent.server.schemas import SessionEventInput
from omnigent.server.session_orchestration import require_owned_session
from omnigent.server.startup_inputs import initial_input_id
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.studio_review_store import (
    ReviewConflictError,
    ReviewNotFoundError,
    StudioReviewStore,
)
from omnigent.studio.reviews.launch import SKILL_NAME, review_prompt, run_review
from omnigent.studio.reviews.models import (
    CaptureResult,
    ReviewEvent,
    ReviewOperation,
    ReviewPass,
    ReviewProfile,
    ReviewTarget,
)
from omnigent.studio.reviews.report import ReviewReport, read_review_report, report_instructions
from omnigent.studio.reviews.service import (
    CaptureReviewRequest,
    CaptureUnavailableError,
    ReviewService,
)
from omnigent.util.session_lifecycle import is_session_closed
from omnigent.util.uuid import uuid_to_bytes

if TYPE_CHECKING:
    from omnigent.server.routes.sessions.routes_core import ReviewCreate
    from omnigent.server.routes.sessions.routes_events import ReviewSend

CaptureContext = Callable[[Request, str, str | None], Awaitable[ReviewTarget]]
ReadPatch = Callable[[Request, ReviewTarget], Awaitable[str]]
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[!-~]+$"),
]


class ReviewListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object: Literal["list"] = "list"
    data: list[ReviewPass]
    next_cursor: str | None


class ReviewEventsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object: Literal["list"] = "list"
    data: list[ReviewEvent]
    next_cursor: int | None


class StartReviewRequest(BaseModel):
    """Start uses the captured target and profile, without overrides."""

    model_config = ConfigDict(extra="forbid")


def _identifier(value: str) -> str:
    try:
        return uuid_to_bytes(value).hex()
    except ValueError:
        raise HTTPException(status_code=404, detail="Review or session not found") from None


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


async def _qualify_skill(request: Request, review: ReviewPass) -> None:
    """Check the connected owner host and its complete native skill catalog."""
    host_store = getattr(request.app.state, "host_store", None)
    registry = getattr(request.app.state, "host_registry", None)
    if host_store is None or registry is None:
        raise HTTPException(503, "Connected review hosts are unavailable")
    host = await asyncio.to_thread(
        resolve_host_owner,
        user_id=review.owner_user_id,
        host_id=review.target.host_id,
        host_store=host_store,
    )
    conn = registry.get(host.host_id)
    available, reason = reported_harness_availability(
        review.profile.harness, host.configured_harnesses
    )
    if conn is None:
        raise HTTPException(503, "The captured review host is offline")
    if available is not True:
        raise HTTPException(409, f"Review harness is unavailable: {reason or 'readiness unknown'}")
    if SKILL_NAME.fullmatch(review.profile.skill) is None:
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
    if not any(skill.get("name") == review.profile.skill for skill in result.skills):
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

        async def profile() -> ReviewProfile:
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

        async def target() -> ReviewTarget:
            captured = await capture_context(request, source_session_id, body.pr_url)
            await _owned_source(
                source_session_id, user_id, conversation_store, permission_store, auth_provider
            )
            return captured

        try:
            result = await service.capture(
                owner_user_id=user_id,
                source_session_id=source_session_id,
                idempotency_key=idempotency_key,
                body=body,
                resolve_profile=profile,
                resolve_target=target,
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

    @router.get("/sessions/{source_session_id}/studio/reviews", response_model=ReviewListResponse)
    async def list_reviews(
        request: Request,
        source_session_id: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        after_id: str | None = None,
    ) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        source_session_id = _identifier(source_session_id)
        await _owned_source(
            source_session_id, user_id, conversation_store, permission_store, auth_provider
        )
        try:
            page = await asyncio.to_thread(
                review_store.list_page,
                owner_user_id=user_id,
                source_session_id=source_session_id,
                limit=limit,
                after_id=_identifier(after_id) if after_id is not None else None,
            )
        except ReviewNotFoundError:
            raise HTTPException(status_code=404, detail="Review cursor not found") from None
        return {
            "object": "list",
            "data": [r.model_dump(mode="json") for r in page.entries],
            "next_cursor": page.next_cursor,
        }

    @router.get("/studio/reviews/{review_id}", response_model=ReviewPass)
    async def get_review(request: Request, review_id: str) -> dict[str, Any]:
        review = await owned_review(request, review_id)
        return review.model_dump(mode="json")

    @router.post(
        "/studio/reviews/{review_id}/start",
        response_model=ReviewPass,
        dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)],
    )
    async def start_review(
        request: Request,
        review_id: str,
        _body: StartReviewRequest,
        idempotency_key: IdempotencyKey,
    ) -> ReviewPass:
        review = await owned_review(request, review_id)
        owner = review.owner_user_id
        target = review.target
        await _owned_source(
            target.source_session_id, owner, conversation_store, permission_store, auth_provider
        )
        if read_patch is None or create_reviewer is None or send_review is None:
            raise HTTPException(503, "Native review execution is unavailable")

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
            current = await capture_context(request, target.source_session_id, target.pr_url)
            if current != target:
                raise HTTPException(409, "The pull request revision or execution target changed")
            patch = await read_patch(request, target)
            source = await _owned_source(
                target.source_session_id,
                owner,
                conversation_store,
                permission_store,
                auth_provider,
            )
            if (source.host_id, source.workspace) != (target.host_id, target.workspace):
                raise HTTPException(409, "The review execution target changed")
            try:
                return review_prompt(review, patch, report_instructions(review))
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

    @router.get("/studio/reviews/{review_id}/report", response_model=ReviewReport)
    async def get_review_report(
        request: Request, response: Response, review_id: str
    ) -> ReviewReport:
        review = await owned_review(request, review_id)
        response.headers["Cache-Control"] = "no-store"
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
        return await asyncio.to_thread(read_review_report, conversation_store, review)

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
