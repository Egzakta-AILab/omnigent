"""Manifest and inspection catalog for the Creator review panel pilot."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

ReviewCapability = Literal["review.read", "review.openProof", "review.attachProof"]


class ReviewPanelContribution(BaseModel):
    """One panel backed by an operator-installed browser bundle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot: Literal["review.panel"]
    contract_version: Literal["1"]
    view: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,80}$")]
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    capabilities: tuple[ReviewCapability, ...] = Field(
        min_length=1, max_length=3, json_schema_extra={"uniqueItems": True}
    )

    @field_validator("capabilities")
    @classmethod
    def unique_capabilities(
        cls, capabilities: tuple[ReviewCapability, ...]
    ) -> tuple[ReviewCapability, ...]:
        if len(set(capabilities)) != len(capabilities):
            raise ValueError("duplicate review capabilities")
        return capabilities


def inspect_review_panel() -> dict[str, object]:
    """Describe the installed pilot contract without opening a review."""
    return {
        "slot": "review.panel",
        "contract_version": "1",
        "manifest_schema": ReviewPanelContribution.model_json_schema(),
        "component": {
            "mount": "#root",
            "lifecycle": "Use defineExtension from the Omnigent web extension SDK.",
            "theme": "Use CSS color-scheme: light dark with Canvas and CanvasText.",
            "layout": "Render in a scrollable panel. Allow narrow widths and keyboard navigation.",
            "trust": "Pilot accepts operator-installed, reviewed bundles only.",
        },
        "capabilities": {
            "review.read": {
                "params": {
                    "findingKey": "optional SHA-256 hex",
                    "offset": "optional integer >= 0",
                },
                "returns": "A page of findings or proof summaries with nextOffset.",
            },
            "review.openProof": {
                "params": {"findingKey": "SHA-256 hex", "receiptDigest": "SHA-256 hex"},
                "returns": "null. Opens the corresponding receipt in the native review UI.",
            },
            "review.attachProof": {
                "params": {"findingKey": "SHA-256 hex", "receiptDigest": "SHA-256 hex"},
                "returns": "null. Selects a receipt in the Ask an agent form.",
            },
        },
    }
