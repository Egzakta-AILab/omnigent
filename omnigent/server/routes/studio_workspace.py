"""Owner-scoped workspace configuration over the connected host tunnel."""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._host_filesystem import (
    HostFsError,
    HostFsUnavailableError,
    read_workspace_from_host,
    write_workspace_from_host,
)
from omnigent.server.routes._host_launch import resolve_host_owner
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.server.routes.studio_reviews import _identifier, _owned_source
from omnigent.stores import ConversationStore, PermissionStore
from omnigent.stores.project_store import ProjectStore
from omnigent.studio.reviews.models import ReviewProfileEditor, ReviewProfileEditorWrite

_WRITE_GATES = [Depends(require_json_content_type), Depends(require_trusted_origin)]


@dataclass(frozen=True)
class _Workspace:
    record_id: str
    owner: str | None
    host_id: str
    path: str

    @property
    def binding(self) -> str:
        return hashlib.sha256(json.dumps([self.host_id, self.path]).encode()).hexdigest()


class _RepositoryAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    role: Literal["active", "reference", "excluded"]
    origin: Annotated[str, StringConstraints(max_length=512)] | None = None


class _RepositorySettingsWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: Annotated[str, StringConstraints(min_length=1, max_length=160)]
    depth: int = Field(ge=0, le=5, strict=True)
    repositories: list[_RepositoryAssignment] = Field(max_length=64)


async def _host_settings(
    request: Request,
    target: _Workspace,
    op: str,
    *,
    params: dict[str, Any] | None = None,
    write: bool = False,
) -> dict[str, Any]:
    params = dict(params or {})
    if write:
        binding, separator, revision = params["revision"].partition(":")
        if not separator or not revision or binding != target.binding:
            raise HTTPException(409, "The workspace changed. Reload its settings before saving.")
        params["revision"] = revision

    store = getattr(request.app.state, "host_store", None)
    registry = getattr(request.app.state, "host_registry", None)
    if store is None or registry is None:
        raise HTTPException(503, "Workspace hosts are unavailable")
    host = await asyncio.to_thread(
        resolve_host_owner, user_id=target.owner, host_id=target.host_id, host_store=store
    )
    connection = registry.get(host.host_id)
    if connection is None:
        raise HTTPException(503, "Connect the workspace host to continue")
    call = write_workspace_from_host if write else read_workspace_from_host
    try:
        result = await call(
            host_registry=registry,
            host_conn=connection,
            op=op,
            workspace=target.path,
            session_id=target.record_id,
            params=params,
        )
    except HostFsError as exc:
        if exc.status in (400, 404, 409, 422):
            raise HTTPException(exc.status, exc.message) from exc
        raise HTTPException(503, "The host could not access workspace settings") from exc
    except HostFsUnavailableError as exc:
        raise HTTPException(503, "The workspace host did not respond") from exc

    if not isinstance(result.get("revision"), str):
        raise HTTPException(503, "The host returned invalid workspace settings")
    return {**result, "revision": f"{target.binding}:{result['revision']}"}


def create_studio_workspace_router(
    conversation_store: ConversationStore,
    project_store: ProjectStore | None,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> APIRouter:
    router = APIRouter(prefix="/{scope}/{record_id}/studio")

    async def workspace(
        request: Request, scope: Literal["sessions", "projects"], record_id: str
    ) -> _Workspace:
        owner = require_user(request, auth_provider)
        if scope == "sessions":
            source = await _owned_source(
                _identifier(record_id), owner, conversation_store, permission_store, auth_provider
            )
            host_id, path = source.host_id, source.workspace
        else:
            if project_store is None:
                raise HTTPException(404, "Project not found")
            project = await asyncio.to_thread(project_store.get, record_id, user_id=owner)
            if project is None:
                raise HTTPException(404, "Project not found")
            config = project.config or {}
            host_id, path = config.get("host_id"), config.get("workspace")
        if not isinstance(host_id, str) or not host_id or not isinstance(path, str) or not path:
            raise HTTPException(409, "Save a host and workspace directory first")
        return _Workspace(record_id, owner, host_id, path)

    Target = Annotated[_Workspace, Depends(workspace)]

    @router.get("/review-profiles/editor", response_model=ReviewProfileEditor)
    async def get_profile_editor(
        request: Request, response: Response, target: Target
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return await _host_settings(request, target, "studio_review_profile_editor")

    @router.put(
        "/review-profiles/editor", response_model=ReviewProfileEditor, dependencies=_WRITE_GATES
    )
    async def save_profile_editor(
        request: Request, response: Response, target: Target, body: ReviewProfileEditorWrite
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return await _host_settings(
            request,
            target,
            "studio_review_profile_editor",
            params=body.model_dump(mode="json"),
            write=True,
        )

    @router.get("/repositories")
    async def get_repository_settings(
        request: Request,
        response: Response,
        target: Target,
        depth: Annotated[int | None, Query(ge=0, le=5)] = None,
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return await _host_settings(
            request, target, "studio_repository_settings", params={"depth": depth}
        )

    @router.put("/repositories", dependencies=_WRITE_GATES)
    async def save_repository_settings(
        request: Request, response: Response, target: Target, body: _RepositorySettingsWrite
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return await _host_settings(
            request,
            target,
            "studio_repository_settings",
            params=body.model_dump(mode="json", exclude_unset=True),
            write=True,
        )

    return router
