"""Compile data-only Creator panels for the native review UI."""

import hashlib
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from omnigent.extensions.review_panel import ReviewCapability
from omnigent.studio.reviews.models import ReviewRoleDigest, Sha

PANEL_BYTES = 8 * 1024
MAX_PANEL_VERSIONS = 32


class ReviewPanelDefinition(BaseModel):
    """A layout and explicit actions for a scoped list of review checks."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    slot: Literal["review.panel"]
    contract_version: Literal["1"]
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=240)] = ""
    group_by: Literal["file", "directory"]
    checks: Literal["failed", "all"]
    proof_action: Literal["open", "open_and_discuss"]


QA_PANEL = ReviewPanelDefinition(
    slot="review.panel",
    contract_version="1",
    title="QA checks",
    description="Inspect check outcomes attached to findings on this revision.",
    group_by="file",
    checks="failed",
    proof_action="open_and_discuss",
)


class CreatorPanelContract(BaseModel):
    definition_schema: dict[str, object]
    template: ReviewPanelDefinition


class CreatorPanelPreview(BaseModel):
    run_id: str
    base_sha: Sha
    head_sha: Sha
    digest: ReviewRoleDigest
    panel: ReviewPanelDefinition


class CreatorPackageVersion(BaseModel):
    version: int
    digest: ReviewRoleDigest | None
    title: str | None
    contract_version: str | None
    capabilities: tuple[ReviewCapability, ...] | None


class CreatorInstallation(BaseModel):
    revision: int = 0
    enabled: bool = False
    selected_version: int | None = None
    active_panel: ReviewPanelDefinition | None = None
    history: list[CreatorPackageVersion] = Field(default_factory=list)


class CreatorActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: Annotated[int, Field(ge=0)]
    version: Annotated[int, Field(ge=1, le=MAX_PANEL_VERSIONS)] | None
    approved_capabilities: Annotated[tuple[ReviewCapability, ...], Field(max_length=3)] | None = (
        None
    )


class CreatorInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: Annotated[int, Field(ge=0)]
    panel: ReviewPanelDefinition
    digest: ReviewRoleDigest
    approved_capabilities: Annotated[tuple[ReviewCapability, ...], Field(max_length=3)] | None = (
        None
    )


def panel_digest(panel: ReviewPanelDefinition) -> str:
    return hashlib.sha256(panel.model_dump_json().encode()).hexdigest()


def panel_capabilities(panel: ReviewPanelDefinition) -> tuple[ReviewCapability, ...]:
    if panel.proof_action == "open_and_discuss":
        return ("review.read", "review.openProof", "review.attachProof")
    return ("review.read", "review.openProof")


def creator_panel_contract() -> CreatorPanelContract:
    return CreatorPanelContract(
        definition_schema=ReviewPanelDefinition.model_json_schema(),
        template=QA_PANEL,
    )


def compile_creator_panel(
    raw: bytes | bytearray, *, run_id: str, base_sha: str, head_sha: str
) -> CreatorPanelPreview:
    if len(raw) > PANEL_BYTES:
        raise ValueError("Panel definitions must be at most 8 KiB")
    panel = ReviewPanelDefinition.model_validate_json(raw)
    return CreatorPanelPreview(
        run_id=run_id,
        base_sha=base_sha,
        head_sha=head_sha,
        digest=panel_digest(panel),
        panel=panel,
    )
