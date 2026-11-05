"""Authorize a project workspace before sending any requests to its host."""

import asyncio
import hashlib
import json
from dataclasses import dataclass

from fastapi import HTTPException, Request

from omnigent.entities import Project
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes._host_launch import resolve_host_owner
from omnigent.server.routes._workspace_validation import (
    WorkspaceValidationError,
    validate_workspace,
)
from omnigent.stores.project_store import ProjectStore


@dataclass(frozen=True)
class ProjectWorkspace:
    record_id: str
    owner: str | None
    host_id: str
    path: str
    registry: HostRegistry
    connection: HostConnection

    @property
    def binding(self) -> str:
        return hashlib.sha256(json.dumps([self.host_id, self.path]).encode()).hexdigest()


async def resolve_workspace_host(
    request: Request, owner_id: str | None, host_id: str
) -> tuple[HostRegistry, HostConnection]:
    store = getattr(request.app.state, "host_store", None)
    registry = getattr(request.app.state, "host_registry", None)
    if store is None or registry is None:
        raise HTTPException(503, "Workspace hosts are unavailable")
    host = await asyncio.to_thread(
        resolve_host_owner, user_id=owner_id, host_id=host_id, host_store=store
    )
    connection = registry.get(host.host_id)
    if connection is None or (owner_id is not None and connection.owner != owner_id):
        raise HTTPException(503, "Connect the workspace host to continue")
    return registry, connection


async def require_owned_project(
    project_store: ProjectStore | None, project_id: str, owner_id: str | None
) -> Project:
    if project_store is not None:
        project = await asyncio.to_thread(project_store.get, project_id, user_id=owner_id)
        if project is not None:
            return project
    raise HTTPException(404, "Project not found")


async def resolve_project_workspace(
    request: Request,
    project_store: ProjectStore | None,
    *,
    project_id: str,
    owner_id: str | None,
    host_id: str,
    workspace: str,
) -> ProjectWorkspace:
    """Resolve the owner's explicitly selected host directory on the host."""
    await require_owned_project(project_store, project_id, owner_id)
    if not workspace.isprintable():
        raise HTTPException(400, "Choose a valid workspace path.")
    registry, connection = await resolve_workspace_host(request, owner_id, host_id)
    try:
        path = await validate_workspace(
            host_registry=registry, host_id=host_id, workspace=workspace, spec_cwd=None
        )
    except WorkspaceValidationError as exc:
        raise HTTPException(400, exc.message) from exc
    if registry.get(host_id) is not connection:
        raise HTTPException(503, "The workspace host reconnected. Try again.")
    return ProjectWorkspace(project_id, owner_id, host_id, path, registry, connection)
