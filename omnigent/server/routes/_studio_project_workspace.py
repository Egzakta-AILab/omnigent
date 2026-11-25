"""Authorize a project workspace before sending any requests to its host."""

import asyncio
import hashlib
import json
from dataclasses import dataclass

from fastapi import HTTPException, Request

from omnigent.entities import Conversation, Project
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes._host_launch import resolve_host_owner
from omnigent.server.routes._workspace_validation import (
    WorkspaceValidationError,
    validate_workspace,
)
from omnigent.server.session_orchestration import require_owned_session
from omnigent.stores import ConversationStore, PermissionStore
from omnigent.stores.project_store import ProjectStore
from omnigent.util.session_lifecycle import is_session_closed
from omnigent.util.uuid import uuid_to_bytes


def studio_identifier(value: str) -> str:
    try:
        return uuid_to_bytes(value).hex()
    except ValueError:
        raise HTTPException(status_code=404, detail="Review or session not found") from None


async def require_studio_session(
    source_session_id: str,
    user_id: str | None,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
    auth_provider: AuthProvider | None,
    *,
    private: bool = False,
) -> Conversation:
    if auth_provider is not None:
        if permission_store is None or user_id is None:
            raise HTTPException(status_code=503, detail="Session ownership checks are unavailable")
        try:
            return await asyncio.to_thread(
                require_owned_session,
                source_session_id,
                user_id,
                conversation_store,
                permission_store,
                private=private,
            )
        except OmnigentError as exc:
            if exc.code in (ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND):
                raise HTTPException(status_code=404, detail="Session not found") from None
            raise
    conversation = await asyncio.to_thread(conversation_store.get_conversation, source_session_id)
    if (
        conversation is None
        or conversation.archived
        or is_session_closed(conversation.labels, conversation.title)
    ):
        raise HTTPException(status_code=404, detail="Session not found")
    return conversation


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


async def uses_isolated_task_review(
    project_store: ProjectStore | None, project_id: str | None, owner_id: str | None
) -> bool:
    if project_id is None:
        return False
    project = await require_owned_project(project_store, project_id, owner_id)
    source = project.config.get("source_key")
    return isinstance(source, str) and source.startswith("linear:")


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
