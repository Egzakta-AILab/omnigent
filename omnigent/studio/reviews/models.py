"""Frozen review snapshots and the durable operation/event wire contracts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from omnigent.util.uuid import normalize_uuid

BoundedText = Annotated[str, StringConstraints(min_length=1, max_length=256)]
Sha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")]
ReviewAction = Literal[
    "review.capture", "review.start", "review.continue", "review.cancel", "review.publish"
]
OperationState = Literal[
    "pending", "dispatching", "running", "succeeded", "failed", "cancelled", "unknown"
]
ReviewState = Literal["prepared", "running", "completed", "failed", "cancelled", "unknown"]
DispatchStatus = Literal[
    "not_requested", "dispatching", "running", "succeeded", "failed", "cancelled", "unknown"
]


def require_uuid(value: str) -> str:
    """Normalize an existing Omnigent ID while rejecting the optional null form."""
    normalized = normalize_uuid(value)
    if normalized is None:
        raise ValueError("An identifier is required")
    return normalized


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ReviewProfile(FrozenModel):
    agent_id: str
    agent_version: BoundedText
    harness: Literal["codex-native", "claude-native"]
    skill: BoundedText
    model: BoundedText | None = None
    reasoning_effort: BoundedText | None = None

    @field_validator("agent_id")
    @classmethod
    def normalize_agent_id(cls, value: str) -> str:
        return require_uuid(value)


class ReviewTarget(FrozenModel):
    source_session_id: str
    host_id: str
    workspace: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    pr_url: Annotated[str, StringConstraints(min_length=1, max_length=2048)]
    repository: BoundedText
    pr_number: int = Field(gt=0)
    base_sha: Sha
    head_sha: Sha

    @field_validator("source_session_id", "host_id")
    @classmethod
    def normalize_target_id(cls, value: str) -> str:
        return require_uuid(value)


class ReviewPass(FrozenModel):
    id: str
    owner_user_id: str | None
    target: ReviewTarget
    profile: ReviewProfile
    state: ReviewState
    dispatch_status: DispatchStatus
    reviewer_session_id: str | None
    created_at: int
    updated_at: int
    version: int


class ReviewOperation(FrozenModel):
    id: str
    review_id: str
    action: ReviewAction
    idempotency_key: str
    request_hash: str
    state: OperationState
    version: int
    created_at: int
    updated_at: int


class ReviewEvent(FrozenModel):
    review_id: str
    sequence: int
    operation_id: str
    kind: str
    created_at: int


class CaptureResult(FrozenModel):
    review: ReviewPass
    operation: ReviewOperation
    created: bool


class OperationResult(FrozenModel):
    operation: ReviewOperation
    created: bool


class ReviewPage(FrozenModel):
    entries: list[ReviewPass]
    next_cursor: str | None


class EventPage(FrozenModel):
    entries: list[ReviewEvent]
    next_cursor: int | None
