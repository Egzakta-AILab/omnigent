"""Frozen review snapshots and the durable operation/event wire contracts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

from omnigent.models.model_override import validate_model_override
from omnigent.util.reasoning_effort import EFFORT_VALUES, validate_effort
from omnigent.util.uuid import normalize_uuid

BoundedText = Annotated[str, StringConstraints(min_length=1, max_length=256)]
Sha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")]
ReviewRoleId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
ReviewRoleDigest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ReviewProfileEditorRevision = Annotated[
    str, StringConstraints(pattern=r"^(?:[0-9a-f]{64}:)?(?:[0-9a-f]{64}|missing)$")
]
ReviewHarness = Literal["codex-native", "claude-native"]
ReviewRoleLabel = Annotated[str, StringConstraints(min_length=1, max_length=128)]
ReviewRoleDescription = Annotated[str, StringConstraints(max_length=512)]
MAX_REVIEW_INSTRUCTION_BYTES = 8 * 1024
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


class ReviewRoleDefaults(FrozenModel):
    harness: ReviewHarness | None = None
    model: BoundedText | None = None
    reasoning_effort: BoundedText | None = None

    @field_validator("model")
    @classmethod
    def normalize_model(cls, model: str | None) -> str | None:
        return validate_model_override(model) if model is not None else None

    @field_validator("reasoning_effort")
    @classmethod
    def normalize_effort(cls, effort: str | None) -> str | None:
        return (
            validate_effort(effort, "review profile", EFFORT_VALUES)
            if effort is not None
            else None
        )


class ReviewRole(FrozenModel):
    id: ReviewRoleId
    label: ReviewRoleLabel
    description: ReviewRoleDescription = ""
    digest: ReviewRoleDigest
    defaults: ReviewRoleDefaults | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class ReviewProfileDraft(FrozenModel):
    id: ReviewRoleId
    label: ReviewRoleLabel
    description: ReviewRoleDescription = ""
    instructions: Annotated[str, StringConstraints(min_length=1, max_length=8192)]
    archived: bool = Field(strict=True)
    defaults: ReviewRoleDefaults | None = None

    @field_validator("label", "description")
    @classmethod
    def normalize_metadata(cls, value: str, info: ValidationInfo) -> str:
        value = value.strip()
        if (value and not value.isprintable()) or (info.field_name == "label" and not value):
            raise ValueError("Use a single line of text")
        return value

    @field_validator("instructions")
    @classmethod
    def normalize_instructions(cls, value: str) -> str:
        value = value.strip()
        if (
            not value
            or "\x00" in value
            or len(value.encode("utf-8")) > MAX_REVIEW_INSTRUCTION_BYTES
        ):
            raise ValueError("Enter review instructions up to 8 KiB")
        return value


class ReviewProfileEditorEntry(ReviewProfileDraft):
    source: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    managed: bool


class ReviewProfileEditor(FrozenModel):
    revision: ReviewProfileEditorRevision
    profiles: list[ReviewProfileEditorEntry] = Field(max_length=64)


class ReviewProfileEditorWrite(FrozenModel):
    revision: ReviewProfileEditorRevision
    profile: ReviewProfileDraft


class ReviewProfile(FrozenModel):
    agent_id: str
    agent_version: BoundedText
    harness: ReviewHarness
    skill: BoundedText | None = None
    model: BoundedText | None = None
    reasoning_effort: BoundedText | None = None
    role: ReviewRole | None = None

    @field_validator("agent_id")
    @classmethod
    def normalize_agent_id(cls, value: str) -> str:
        return require_uuid(value)


class ReviewTarget(FrozenModel):
    source_session_id: str | None = None
    source_project_id: str | None = None
    repository_path: Annotated[str, StringConstraints(min_length=1, max_length=512)] | None = None
    host_id: str
    workspace: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    pr_url: Annotated[str, StringConstraints(min_length=1, max_length=2048)]
    repository: BoundedText
    pr_number: int = Field(gt=0)
    base_sha: Sha
    head_sha: Sha

    @field_validator("source_session_id", "source_project_id", "host_id")
    @classmethod
    def normalize_target_id(cls, value: str | None) -> str | None:
        return require_uuid(value) if value is not None else None

    @model_validator(mode="after")
    def validate_origin(self) -> ReviewTarget:
        if (self.source_session_id is None) == (self.source_project_id is None):
            raise ValueError("A review requires one origin")
        if (self.source_project_id is None) != (self.repository_path is None):
            raise ValueError("A project review requires a workspace repository path")
        return self

    @property
    def origin_id(self) -> str:
        if self.source_session_id is not None:
            return self.source_session_id
        assert self.source_project_id is not None
        return self.source_project_id


MAX_RUN_REVIEWERS = 16
ReviewRunRole = Literal["coordinator", "candidate", "required"]


class ReviewerDecision(FrozenModel):
    review_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    selected: bool = Field(strict=True)
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]

    @field_validator("reason")
    @classmethod
    def printable_reason(cls, value: str) -> str:
        if not value.isprintable():
            raise ValueError("Use a single line for the selection reason")
        return value


class ReviewPlan(FrozenModel):
    coordinator_id: str
    base_sha: Sha
    head_sha: Sha
    decisions: list[ReviewerDecision] = Field(min_length=1, max_length=MAX_RUN_REVIEWERS)


class ReviewPlanProposal(FrozenModel):
    state: Literal["pending", "incomplete", "ready"]
    plan: ReviewPlan | None = None
    message_id: str | None = None
    digest: str | None = None
    error: str | None = None


class ReviewPass(FrozenModel):
    id: str
    run_id: str | None = None
    run_role: ReviewRunRole | None = None
    launch_error: str | None = None
    owner_user_id: str | None
    target: ReviewTarget
    profile: ReviewProfile
    state: ReviewState
    dispatch_status: DispatchStatus
    reviewer_session_id: str | None
    created_at: int
    updated_at: int
    version: int


class ReviewRun(FrozenModel):
    """One deliberately selected roster reviewing one captured PR revision."""

    id: str
    target: ReviewTarget
    reviewers: list[ReviewPass]
    created_at: int
    patch_sha256: str | None = None
    coordinator: ReviewPass | None = None
    plan: ReviewPlan | None = None

    def validate_plan(self, plan: ReviewPlan) -> None:
        if self.coordinator is None or (plan.coordinator_id, plan.base_sha, plan.head_sha) != (
            self.coordinator.id,
            self.target.base_sha,
            self.target.head_sha,
        ):
            raise ValueError("The plan does not match the coordinator and captured revision")
        decisions = {decision.review_id: decision.selected for decision in plan.decisions}
        if len(decisions) != len(plan.decisions) or decisions.keys() != {
            reviewer.id for reviewer in self.reviewers
        }:
            raise ValueError("The plan must name every captured reviewer exactly once")
        if any(
            reviewer.run_role == "required" and not decisions[reviewer.id]
            for reviewer in self.reviewers
        ):
            raise ValueError("The plan must select every required reviewer")
        if not any(decisions.values()):
            raise ValueError("The plan must select at least one reviewer")

    def selected_reviewers(self) -> list[ReviewPass]:
        if self.coordinator is None:
            return self.reviewers
        if self.plan is None:
            return []
        selected = {decision.review_id for decision in self.plan.decisions if decision.selected}
        return [reviewer for reviewer in self.reviewers if reviewer.id in selected]


class ReviewRunPage(FrozenModel):
    entries: list[ReviewRun]
    next_cursor: str | None


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
