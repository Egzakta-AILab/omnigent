"""Owner-private review capture and durable state reads."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._session_create_validation import (
    validate_session_agent,
    validate_session_model_metadata,
)
from omnigent.server.session_orchestration import require_owned_session
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.studio_review_store import (
    ReviewConflictError,
    ReviewNotFoundError,
    StudioReviewStore,
)
from omnigent.studio.reviews.models import (
    CaptureResult,
    ReviewEvent,
    ReviewOperation,
    ReviewPass,
    ReviewProfile,
    ReviewTarget,
)
from omnigent.studio.reviews.service import (
    CaptureReviewRequest,
    CaptureUnavailableError,
    ReviewService,
)
from omnigent.util.session_lifecycle import is_session_closed
from omnigent.util.uuid import uuid_to_bytes

CaptureContext = Callable[[Request, str, str | None], Awaitable[ReviewTarget]]
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


def create_studio_reviews_router(
    review_store: StudioReviewStore,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    *,
    capture_context: CaptureContext,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Expose capture and reads without launching or steering an execution session."""
    router = APIRouter()
    service = ReviewService(review_store)

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
        user_id = require_user(request, auth_provider)
        review = await asyncio.to_thread(
            review_store.get, _identifier(review_id), owner_user_id=user_id
        )
        if review is None:
            raise HTTPException(status_code=404, detail="Review not found")
        return review.model_dump(mode="json")

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
