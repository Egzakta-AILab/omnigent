"""Wire contracts for an internal discussion on a captured review."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from omnigent.studio.reviews.evidence import Anchor, Side
from omnigent.studio.reviews.models import FrozenModel, ReviewRoleDigest, Sha, require_uuid
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
    can_comment: bool
    collaborators: list[str] | None = None
    sharing_available: bool
