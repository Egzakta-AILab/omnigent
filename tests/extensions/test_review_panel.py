"""The Creator contribution uses a closed manifest and an explicit runtime gate."""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from omnigent.extensions import (
    EXTENSION_API_VERSION,
    ExtensionEntrypoints,
    ExtensionManifest,
    ExtensionValidationError,
    ReviewPanelContribution,
    validate_manifest,
)
from omnigent.extensions.assets import ASSET_SCRIPT, ResolvedAsset, ResolvedBundle
from omnigent.extensions.review_panel import inspect_review_panel
from omnigent.server.routes.extensions import _serialize_manifest

PANEL = {
    "slot": "review.panel",
    "contract_version": "1",
    "view": "qa",
    "title": "QA checks",
    "capabilities": ["review.read", "review.openProof", "review.attachProof"],
}


@pytest.mark.parametrize(
    "changes",
    [
        {"slot": "project.panel"},
        {"contract_version": 1},
        {"contract_version": "2"},
        {"view": "../"},
        {"title": "  "},
        {"permissions": ["sessions.read"]},
        {"capabilities": ["review.read", "review.read"]},
        {"capabilities": ["review.read", "navigation.openExternal"]},
    ],
)
def test_panel_manifest_rejects_invalid_contributions(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ReviewPanelContribution.model_validate(PANEL | changes)


def test_inspection_matches_supported_panel() -> None:
    contribution = ReviewPanelContribution.model_validate(PANEL)
    catalog = inspect_review_panel()
    assert catalog["contract_version"] == contribution.contract_version
    assert catalog["slot"] == contribution.slot
    capabilities = catalog["capabilities"]
    assert isinstance(capabilities, dict)
    assert list(capabilities) == list(contribution.capabilities)


def test_panel_catalog_requires_an_enabled_pilot_and_resolved_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = ExtensionManifest(
        id="acme.qa",
        display_name="QA",
        distribution="omnigent-acme-qa",
        version="1.0.0",
        requires_omnigent=">=0.11,<1",
        extension_api=EXTENSION_API_VERSION,
        review_panel=ReviewPanelContribution.model_validate(PANEL),
    )
    with pytest.raises(ExtensionValidationError, match=r"review\.panel requires"):
        validate_manifest(manifest)
    manifest = replace(manifest, entrypoints=ExtensionEntrypoints(browser="dist/extension.js"))
    validate_manifest(manifest)
    asset = ResolvedAsset(ASSET_SCRIPT, "text/javascript", b"", "a" * 64)
    bundle = ResolvedBundle(manifest.id, "b" * 64, {ASSET_SCRIPT: asset})
    monkeypatch.delenv("OMNIGENT_STUDIO_CREATOR_PILOT", raising=False)
    assert "review_panel" not in _serialize_manifest(manifest, bundle).model_dump()
    monkeypatch.setenv("OMNIGENT_STUDIO_CREATOR_PILOT", "1")
    assert "review_panel" not in _serialize_manifest(manifest).model_dump()
    assert _serialize_manifest(manifest, bundle).model_dump(mode="json")["review_panel"] == PANEL
