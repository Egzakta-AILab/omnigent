"""Wire contracts for an internal discussion on a captured review."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from omnigent.studio.reviews.evidence import Anchor, Side
from omnigent.studio.reviews.models import (
    BoundedText,
    FrozenModel,
    ReviewHarness,
    ReviewProfile,
    ReviewRoleDigest,
    Sha,
    require_uuid,
)
from omnigent.studio.reviews.report import ReportState, ReviewFinding

AccountId = Annotated[str, StringConstraints(pattern=r"^[^\s\x00-\x1f\x7f]{1,128}$")]


class FindingDiscussionAnchor(FrozenModel):
    kind: Literal["finding"]
    finding_key: ReviewRoleDigest


class DiffDiscussionAnchor(FrozenModel):
    kind: Literal["diff"]
    path: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    side: Side
    start: int = Field(strict=True, ge=1, le=2**31 - 1)
    end: int = Field(strict=True, ge=1, le=2**31 - 1)

    @field_validator("path")
    @classmethod
    def validate_path(cls, path: str) -> str:
        return ReviewFinding.repository_relative_path(path)

    @model_validator(mode="after")
    def validate_range(self) -> DiffDiscussionAnchor:
        if not 0 <= self.end - self.start < 100:
            raise ValueError("Choose a range of 1 to 100 lines")
        return self

    def is_in(self, anchors: frozenset[Anchor]) -> bool:
        return all(
            (self.path, self.side, line) in anchors for line in range(self.start, self.end + 1)
        )


DiscussionAnchor = Annotated[
    FindingDiscussionAnchor | DiffDiscussionAnchor, Field(discriminator="kind")
]


class CreateReviewComment(FrozenModel):
    id: str
    anchor: DiscussionAnchor
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]

    @field_validator("id")
    @classmethod
    def canonical_id(cls, value: str) -> str:
        return require_uuid(value)


class ReviewComment(CreateReviewComment):
    author: str | None
    created_at: int


class AskReviewQuestion(CreateReviewComment):
    agent_id: str | None = None
    reviewer_key: ReviewRoleDigest | None = None
    host_id: str | None = None
    workspace: Annotated[str, StringConstraints(min_length=1, max_length=4096)] | None = None
    model: BoundedText | None = None
    reasoning_effort: BoundedText | None = None

    @field_validator("host_id", "agent_id")
    @classmethod
    def canonical_selection(cls, value: str | None) -> str | None:
        return require_uuid(value) if value is not None else None

    @model_validator(mode="after")
    def validate_selection(self) -> AskReviewQuestion:
        if (self.agent_id is None) == (self.reviewer_key is None):
            raise ValueError("Choose an agent or a saved reviewer")
        if (self.host_id is None) != (self.workspace is None):
            raise ValueError("Choose both a host and a directory")
        if self.reviewer_key is not None and (self.model or self.reasoning_effort):
            raise ValueError("A saved reviewer uses its captured model settings")
        return self


class QuestionExecution(FrozenModel):
    host_id: str
    workspace: str
    project_id: str | None = None
    pr_number: int
    profile: ReviewProfile


class QuestionInput(FrozenModel):
    anchor: DiscussionAnchor
    body: str
    execution: QuestionExecution
    prompt_digest: ReviewRoleDigest
    requires_citations: bool = False


QuestionDispatch = Literal["dispatching", "accepted", "unknown"]


class QuestionCitation(FrozenModel):
    path: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    side: Side
    line: int = Field(strict=True, ge=1, le=2**31 - 1)
    quote: Annotated[str, StringConstraints(min_length=1, max_length=400)]


class QuestionAnswer(FrozenModel):
    question_id: str
    base_sha: Sha
    head_sha: Sha
    answer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    citations: list[QuestionCitation] | None = Field(default=None, max_length=6)


class ReviewQuestion(FrozenModel):
    id: str
    anchor: DiscussionAnchor
    body: str
    session_id: str
    harness: ReviewHarness
    status: QuestionDispatch
    answer_state: ReportState = "pending"
    answer: str | None = None
    citations: list[QuestionCitation] | None = None
    published_at: int | None = None
    updated_at: int


@dataclass(frozen=True)
class QuestionRecord:
    view: ReviewQuestion
    snapshot: QuestionInput


class SharedReviewAnswer(FrozenModel):
    id: str
    anchor: DiscussionAnchor
    question: str
    answer: str
    citations: list[QuestionCitation] | None = None
    harness: ReviewHarness
    requested_by: str | None
    published_at: int


class ReviewCollaboratorInput(FrozenModel):
    user_id: AccountId


class ReviewDiscussionFinding(FrozenModel):
    key: ReviewRoleDigest
    finding: ReviewFinding


class ReviewDiscussionReport(FrozenModel):
    key: ReviewRoleDigest
    label: str
    state: ReportState
    summary: str | None


class ReviewDiscussionView(FrozenModel):
    id: str
    project_name: str
    repository: str
    pr_url: str
    pr_number: int
    base_sha: Sha
    head_sha: Sha
    patch_sha256: ReviewRoleDigest
    patch: str
    reports: list[ReviewDiscussionReport]
    findings: list[ReviewDiscussionFinding]
    comments: list[ReviewComment]
    questions: list[ReviewQuestion] = Field(default_factory=list)
    agent_answers: list[SharedReviewAnswer] = Field(default_factory=list)
    can_comment: bool
    can_attach_evidence: bool = False
    can_ask: bool = False
    can_use_review_workspace: bool = False
    review_workspace_host_id: str | None = None
    collaborators: list[str] | None = None
    sharing_available: bool
