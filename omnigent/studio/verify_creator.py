"""Verify a scoped Creator preview against an explicitly selected instance."""

from pathlib import Path
from typing import Literal

import httpx

from omnigent.studio import setup
from omnigent.studio.creator_panel import (
    PANEL_BYTES,
    CreatorInstallation,
    CreatorPanelContract,
    CreatorPanelPreview,
    ReviewPanelDefinition,
    compile_creator_panel,
    panel_capabilities,
    panel_digest,
)
from omnigent.studio.reviews.models import require_uuid


def verify_creator_preview(
    *,
    mode: str,
    server_url: str,
    run_id: str,
    definition: Path | None,
    transport: httpx.BaseTransport | None = None,
) -> CreatorPanelPreview:
    server_url = setup.validate_server_url(server_url, mode)
    run_id = require_uuid(run_id)
    try:
        headers = setup._headers(server_url, None)
    except Exception:  # noqa: BLE001 — credential errors may contain private values
        raise ValueError("Could not load the instance credentials") from None
    path = f"v1/studio/review-runs/{run_id}/creator-preview"
    with httpx.Client(
        base_url=f"{server_url}/", headers=headers, timeout=10, transport=transport
    ) as client:
        try:
            contract = CreatorPanelContract.model_validate(
                client.get(path).raise_for_status().json()
            )
            if definition is None:
                raw = contract.template.model_dump_json().encode()
            else:
                with definition.open("rb") as file:
                    raw = file.read(PANEL_BYTES + 1)
            if len(raw) > PANEL_BYTES:
                raise ValueError("Panel definitions must be at most 8 KiB")
            response = client.post(path, content=raw, headers={"Content-Type": "application/json"})
            preview = CreatorPanelPreview.model_validate_json(response.raise_for_status().content)
        except httpx.HTTPStatusError as exc:
            raise ValueError(
                f"Creator preview rejected (HTTP {exc.response.status_code})"
            ) from None
        except httpx.RequestError:
            raise ValueError("Could not reach the Creator preview API") from None
        except OSError:
            raise ValueError("Could not read the panel definition") from None
    expected = compile_creator_panel(
        raw, run_id=run_id, base_sha=preview.base_sha, head_sha=preview.head_sha
    )
    if preview != expected:
        raise ValueError("The instance returned a preview for a different review or definition")
    return preview


def verify_creator_installation(
    *,
    mode: str,
    server_url: str,
    run_id: str,
    package: Path,
    expect: Literal["enabled", "disabled"],
    version: int | None = None,
    transport: httpx.BaseTransport | None = None,
) -> CreatorInstallation:
    """Read back the exact package and its active or disabled selection."""
    server_url = setup.validate_server_url(server_url, mode)
    run_id = require_uuid(run_id)
    try:
        with package.open("rb") as file:
            raw = file.read(PANEL_BYTES + 1)
    except OSError:
        raise ValueError("Could not read the Creator package") from None
    if len(raw) > PANEL_BYTES:
        raise ValueError("Creator packages must be at most 8 KiB")
    panel = ReviewPanelDefinition.model_validate_json(raw)
    try:
        headers = setup._headers(server_url, None)
    except Exception:  # noqa: BLE001 — credential errors may contain private values
        raise ValueError("Could not load the instance credentials") from None
    path = f"v1/studio/review-runs/{run_id}/creator-installation"
    with httpx.Client(
        base_url=f"{server_url}/", headers=headers, timeout=10, transport=transport
    ) as client:
        try:
            response = client.get(path)
            state = CreatorInstallation.model_validate_json(response.raise_for_status().content)
        except httpx.HTTPStatusError as exc:
            raise ValueError(
                f"Installation read failed (HTTP {exc.response.status_code})"
            ) from None
        except httpx.RequestError:
            raise ValueError("Could not reach the Creator installation API") from None
    selected = next(
        (entry for entry in state.history if entry.version == state.selected_version), None
    )
    if (
        selected is None
        or (version is not None and version != selected.version)
        or selected.digest != panel_digest(panel)
        or selected.contract_version != panel.contract_version
        or selected.capabilities != panel_capabilities(panel)
    ):
        raise ValueError("The selected version does not match the package and capabilities")
    if state.enabled != (expect == "enabled") or state.active_panel != (
        panel if expect == "enabled" else None
    ):
        raise ValueError("The installed package does not match the expected runtime state")
    return state
