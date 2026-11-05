"""Persist and load one native Creator panel selection per project and account."""

from __future__ import annotations

import hashlib
import json

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlStudioCreatorInstallation,
    SqlStudioCreatorPackage,
    current_workspace_id,
)
from omnigent.extensions.review_panel import ReviewCapability
from omnigent.studio.creator_panel import (
    MAX_PANEL_VERSIONS,
    CreatorActivationRequest,
    CreatorInstallation,
    CreatorInstallRequest,
    CreatorPackageVersion,
    ReviewPanelDefinition,
    panel_capabilities,
    panel_digest,
)

CreatorScope = tuple[int, str, bytes]
PackageMap = dict[int, tuple[SqlStudioCreatorPackage, ReviewPanelDefinition | None]]


class CreatorInstallationConflict(ValueError):
    """The requested change needs a fresh state or permission grant."""


def creator_scope(project_id: str, actor: str | None, generation: str | None) -> CreatorScope:
    actor_scope = hashlib.sha256(json.dumps([actor, generation]).encode()).digest()
    return (current_workspace_id(), project_id, actor_scope)


def _definition(row: SqlStudioCreatorPackage) -> ReviewPanelDefinition | None:
    try:
        panel = ReviewPanelDefinition.model_validate_json(row.definition_json)
    except ValidationError:
        return None
    return panel if panel_digest(panel) == bytes(row.digest).hex() else None


def _packages(session: Session, scope: CreatorScope, latest_version: int) -> PackageMap:
    rows = session.scalars(
        select(SqlStudioCreatorPackage)
        .where(
            SqlStudioCreatorPackage.workspace_id == scope[0],
            SqlStudioCreatorPackage.project_id == scope[1],
            SqlStudioCreatorPackage.actor_scope == scope[2],
            SqlStudioCreatorPackage.version <= latest_version,
        )
        .order_by(SqlStudioCreatorPackage.version.desc())
    )
    return {row.version: (row, _definition(row)) for row in rows}


def _catalog(state: SqlStudioCreatorInstallation, packages: PackageMap) -> CreatorInstallation:
    selected = packages.get(state.selected_version)
    return CreatorInstallation(
        revision=state.revision,
        selected_version=state.selected_version,
        enabled=state.enabled,
        active_panel=selected[1] if state.enabled and selected else None,
        history=[
            CreatorPackageVersion(
                version=row.version,
                digest=bytes(row.digest).hex() if len(row.digest) == 32 else None,
                title=panel.title if panel is not None else None,
                contract_version=panel.contract_version if panel is not None else None,
                capabilities=panel_capabilities(panel) if panel is not None else None,
            )
            for row, panel in sorted(packages.values(), key=lambda entry: -entry[0].version)
        ],
    )


def read_creator_installation(session: Session, scope: CreatorScope) -> CreatorInstallation:
    state = session.get(SqlStudioCreatorInstallation, scope)
    if state is None:
        return CreatorInstallation()
    return _catalog(state, _packages(session, scope, state.latest_version))


def _require_grant(
    old_panel: ReviewPanelDefinition | None,
    new_panel: ReviewPanelDefinition,
    approved: tuple[ReviewCapability, ...] | None,
) -> None:
    capabilities = panel_capabilities(new_panel)
    previous = panel_capabilities(old_panel) if old_panel is not None else ()
    if approved is not None and sorted(approved) != sorted(capabilities):
        raise CreatorInstallationConflict("The approved capabilities do not match this package")
    if previous != capabilities and approved is None:
        raise CreatorInstallationConflict("Confirm the capabilities before enabling this package")


def mutate_creator_installation(
    session: Session,
    scope: CreatorScope,
    request: CreatorInstallRequest | CreatorActivationRequest,
) -> CreatorInstallation:
    """Change a selection while the caller holds the project write lock."""
    state = session.get(SqlStudioCreatorInstallation, scope)
    revision = state.revision if state is not None else 0
    if request.expected_revision != revision:
        raise CreatorInstallationConflict(
            "The installation changed. Reload it before applying changes"
        )
    if state is None:
        if isinstance(request, CreatorActivationRequest):
            raise CreatorInstallationConflict("There is no saved Creator package")
        state = SqlStudioCreatorInstallation(
            workspace_id=scope[0],
            project_id=scope[1],
            actor_scope=scope[2],
            revision=0,
            latest_version=0,
            selected_version=0,
            enabled=False,
        )
        session.add(state)
    packages = _packages(session, scope, state.latest_version)
    previous = packages.get(state.selected_version)
    old_panel = previous[1] if previous else None
    if isinstance(request, CreatorInstallRequest):
        digest = panel_digest(request.panel)
        if request.digest != digest:
            raise CreatorInstallationConflict("The package digest does not match the preview")
        _require_grant(old_panel, request.panel, request.approved_capabilities)
        version = next(
            (
                row.version
                for row, panel in packages.values()
                if panel is not None and bytes(row.digest).hex() == digest
            ),
            None,
        )
        if version is None:
            if state.latest_version >= MAX_PANEL_VERSIONS:
                raise CreatorInstallationConflict(
                    "This project has reached the Creator version limit"
                )
            version = state.latest_version + 1
            row = SqlStudioCreatorPackage(
                workspace_id=scope[0],
                project_id=scope[1],
                actor_scope=scope[2],
                version=version,
                digest=bytes.fromhex(digest),
                definition_json=request.panel.model_dump_json(),
            )
            session.add(row)
            state.latest_version = version
            packages[version] = (row, request.panel)
    else:
        version = request.version
        if version is not None:
            package = packages.get(version)
            if package is None or package[1] is None:
                raise CreatorInstallationConflict("This package version is unavailable")
            _require_grant(old_panel, package[1], request.approved_capabilities)
    if version is None:
        if state.enabled:
            state.enabled = False
            state.revision += 1
    elif not state.enabled or state.selected_version != version:
        state.enabled = True
        state.selected_version = version
        state.revision += 1
    return _catalog(state, packages)
