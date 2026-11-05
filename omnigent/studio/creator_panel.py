"""Compile data-only Creator panels for the native review UI."""

import hashlib
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from omnigent.studio.reviews.models import ReviewRoleDigest, Sha

PANEL_BYTES = 8 * 1024


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
        digest=hashlib.sha256(panel.model_dump_json().encode()).hexdigest(),
        panel=panel,
    )
