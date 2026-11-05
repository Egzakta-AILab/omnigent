"""Wire contracts for an internal discussion on a captured review."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from omnigent.studio.reviews.check_proposal import ProposedCheck
from omnigent.studio.reviews.evidence import Anchor, Side
from omnigent.studio.reviews.hosted_check_models import HostedCheckJob
from omnigent.studio.reviews.models import (
    BoundedText,
    CandidateReviewOrigin,
    FrozenModel,
    ReviewHarness,
    ReviewProfile,
    ReviewRoleDigest,
    Sha,
    require_uuid,
)
from omnigent.studio.reviews.report import ReportState, ReviewFinding

AccountId = Annotated[str, StringConstraints(pattern=r"^[^\s\x00-\x1f\x7f]{1,128}$")]
MAX_REVIEW_COLLABORATORS = 32


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
CommentBody = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
]


class CreateReviewComment(FrozenModel):
    id: str
    anchor: DiscussionAnchor
    body: CommentBody

    @field_validator("id")
    @classmethod
    def canonical_id(cls, value: str) -> str:
        return require_uuid(value)


class ReviewComment(CreateReviewComment):
    author: str | None
    created_at: int


class InlineDraftVersion(FrozenModel):
    version: int = Field(strict=True, ge=1, lt=2**31 - 1)


class EditInlineDraft(InlineDraftVersion):
    body: CommentBody


InlinePublicationState = Literal["draft", "sending", "posted", "unknown"]


class InlineDraft(InlineDraftVersion):
    id: str
    thread: DiscussionAnchor
    anchor: DiffDiscussionAnchor
    body: CommentBody
    created_at: int
    updated_at: int
    publication_state: InlinePublicationState = "draft"
    provider_url: str | None = None


class InlineDraftSelection(InlineDraftVersion):
    id: str

    @field_validator("id")
    @classmethod
    def canonical_id(cls, value: str) -> str:
        return require_uuid(value)


class InlinePublicationRequest(FrozenModel):
    selected: list[InlineDraftSelection] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_selection(self) -> InlinePublicationRequest:
        if len({draft.id for draft in self.selected}) != len(self.selected):
            raise ValueError("Select each draft once")
        return self


class ConfirmInlinePublication(InlinePublicationRequest):
    digest: ReviewRoleDigest
    confirm: Literal[True]

    @field_validator("confirm", mode="before")
    @classmethod
    def explicit_confirmation(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Explicitly confirm publication")
        return value


class InlinePublicationPreview(FrozenModel):
    run_id: str
    provider: str
    pr_url: str
    base_sha: Sha
    head_sha: Sha
    patch_sha256: ReviewRoleDigest
    digest: ReviewRoleDigest
    drafts: list[InlineDraft]


QuestionAction = Literal["question", "propose_fix", "propose_check"]
MAX_PROPOSED_PATCH_BYTES = 24 * 1024


class AskReviewQuestion(CreateReviewComment):
    action: QuestionAction = "question"
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
        if self.action != "question" and not isinstance(self.anchor, FindingDiscussionAnchor):
            raise ValueError("Choose a finding for the proposal")
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
    action: QuestionAction = "question"


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
    proposed_patch: str | None = Field(
        default=None, min_length=1, max_length=MAX_PROPOSED_PATCH_BYTES
    )
    proposed_check: ProposedCheck | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


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
    action: QuestionAction = "question"
    proposed_patch: str | None = None
    proposed_check: ProposedCheck | None = None
    proposed_check_digest: ReviewRoleDigest | None = None
    check_job: HostedCheckJob | None = None
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


class SharedReviewEntry(FrozenModel):
    id: str
    project_name: str
    repository: str
    pr_number: int
    head_sha: Sha
    created_at: int


class SharedReviewPage(FrozenModel):
    entries: list[SharedReviewEntry]
    next_cursor: str | None = None


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
    candidate: CandidateReviewOrigin | None = None
    reports: list[ReviewDiscussionReport]
    findings: list[ReviewDiscussionFinding]
    comments: list[ReviewComment]
    inline_drafts: list[InlineDraft] = Field(default_factory=list)
    questions: list[ReviewQuestion] = Field(default_factory=list)
    agent_answers: list[SharedReviewAnswer] = Field(default_factory=list)
    can_comment: bool
    can_draft_inline: bool = False
    can_publish_inline: bool = False
    can_attach_evidence: bool = False
    can_ask: bool = False
    can_use_review_workspace: bool = False
    check_repository: str | None = None
    review_workspace_host_id: str | None = None
    collaborators: list[str] | None = None
    sharing_available: bool
