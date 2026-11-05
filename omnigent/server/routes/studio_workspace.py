"""Owner-scoped workspace configuration over the connected host tunnel."""

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
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.server.routes._studio_project_workspace import (
    ProjectWorkspace,
    require_owned_project,
    resolve_project_workspace,
    resolve_workspace_host,
)
from omnigent.server.routes.studio_reviews import _identifier, _owned_source
from omnigent.stores import ConversationStore, PermissionStore
from omnigent.stores.project_store import ProjectStore
from omnigent.studio.reviews.models import ReviewProfileEditor, ReviewProfileEditorWrite

_WRITE_GATES = [Depends(require_json_content_type), Depends(require_trusted_origin)]
_SelectedHost = Annotated[str | None, Query(alias="host_id", min_length=1, max_length=128)]
_SelectedWorkspace = Annotated[str | None, Query(alias="workspace", min_length=1, max_length=4096)]


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


async def _host_operation(
    target: ProjectWorkspace,
    op: str,
    *,
    params: dict[str, Any] | None = None,
    write: bool = False,
    settings: bool = False,
) -> dict[str, Any]:
    params = dict(params or {})
    if write:
        binding, separator, revision = params["revision"].partition(":")
        if not separator or not revision or binding != target.binding:
            raise HTTPException(409, "The workspace changed. Reload its settings before saving.")
        params["revision"] = revision

    call = write_workspace_from_host if write else read_workspace_from_host
    try:
        result = await call(
            host_registry=target.registry,
            host_conn=target.connection,
            op=op,
            workspace=target.path,
            session_id=target.record_id,
            params=params,
        )
    except HostFsError as exc:
        if exc.status in (400, 404, 409, 422):
            raise HTTPException(exc.status, exc.message) from exc
        raise HTTPException(503, "The host could not access the workspace") from exc
    except HostFsUnavailableError as exc:
        raise HTTPException(503, "The workspace host did not respond") from exc

    if not settings:
        return result
    if not isinstance(result.get("revision"), str):
        raise HTTPException(503, "The host returned invalid workspace settings")
    return {**result, "revision": f"{target.binding}:{result['revision']}"}


def create_studio_workspace_router(
    conversation_store: ConversationStore,
    project_store: ProjectStore | None,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> APIRouter:
    settings_router = APIRouter(prefix="/{scope}/{record_id}/studio")
    project_router = APIRouter(prefix="/projects/{record_id}/studio")

    async def workspace(
        request: Request,
        scope: Literal["sessions", "projects"],
        record_id: str,
        selected_host: _SelectedHost = None,
        selected_workspace: _SelectedWorkspace = None,
    ) -> ProjectWorkspace:
        owner = require_user(request, auth_provider)
        if scope == "sessions":
            source = await _owned_source(
                _identifier(record_id), owner, conversation_store, permission_store, auth_provider
            )
            host_id, path = source.host_id, source.workspace
        else:
            if selected_host is not None or selected_workspace is not None:
                if selected_host is None or selected_workspace is None:
                    raise HTTPException(400, "Choose both a host and a workspace directory.")
                return await resolve_project_workspace(
                    request,
                    project_store,
                    project_id=record_id,
                    owner_id=owner,
                    host_id=selected_host,
                    workspace=selected_workspace,
                )
            project = await require_owned_project(project_store, record_id, owner)
            config = project.config or {}
            host_id, path = config.get("host_id"), config.get("workspace")
        if not isinstance(host_id, str) or not host_id or not isinstance(path, str) or not path:
            raise HTTPException(409, "Save a host and workspace directory first")
        registry, connection = await resolve_workspace_host(request, owner, host_id)
        return ProjectWorkspace(record_id, owner, host_id, path, registry, connection)

    Target = Annotated[ProjectWorkspace, Depends(workspace)]

    async def project_workspace(
        request: Request,
        record_id: str,
        host_id: Annotated[str, Query(min_length=1, max_length=128)],
        workspace: Annotated[str, Query(min_length=1, max_length=4096)],
    ) -> ProjectWorkspace:
        return await resolve_project_workspace(
            request,
            project_store,
            project_id=record_id,
            owner_id=require_user(request, auth_provider),
            host_id=host_id,
            workspace=workspace,
        )

    ProjectTarget = Annotated[ProjectWorkspace, Depends(project_workspace)]

    @settings_router.get("/review-profiles/editor", response_model=ReviewProfileEditor)
    async def get_profile_editor(response: Response, target: Target) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return await _host_operation(target, "studio_review_profile_editor", settings=True)

    @settings_router.put(
        "/review-profiles/editor", response_model=ReviewProfileEditor, dependencies=_WRITE_GATES
    )
    async def save_profile_editor(
        response: Response, target: Target, body: ReviewProfileEditorWrite
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return await _host_operation(
            target,
            "studio_review_profile_editor",
            params=body.model_dump(mode="json"),
            write=True,
            settings=True,
        )

    @project_router.get("/review-profiles")
    async def get_review_profiles(response: Response, target: ProjectTarget) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return await _host_operation(target, "studio_review_profiles")

    @settings_router.get("/repositories")
    async def get_repository_settings(
        response: Response,
        target: Target,
        depth: Annotated[int | None, Query(ge=0, le=5)] = None,
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return await _host_operation(
            target, "studio_repository_settings", params={"depth": depth}, settings=True
        )

    @settings_router.put("/repositories", dependencies=_WRITE_GATES)
    async def save_repository_settings(
        response: Response, target: Target, body: _RepositorySettingsWrite
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return await _host_operation(
            target,
            "studio_repository_settings",
            params=body.model_dump(mode="json", exclude_unset=True),
            write=True,
            settings=True,
        )

    @project_router.get("/files")
    async def get_file_preview(
        response: Response,
        target: ProjectTarget,
        path: Annotated[str, Query(min_length=1, max_length=4096)],
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return await _host_operation(target, "studio_project_file_preview", params={"path": path})

    @project_router.get("/pull-requests")
    async def list_pull_requests(
        response: Response,
        target: ProjectTarget,
        repository: Annotated[str, Query(min_length=1, max_length=512)],
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return await _host_operation(
            target, "studio_project_pull_requests", params={"repository": repository}
        )

    @project_router.get("/pull-requests/preview")
    async def get_pull_request_preview(
        response: Response,
        target: ProjectTarget,
        repository: Annotated[str, Query(min_length=1, max_length=512)],
        pr_url: Annotated[str, Query(min_length=1, max_length=2048)],
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        result = await _host_operation(
            target,
            "studio_project_pull_request",
            params={"repository": repository, "pr_url": pr_url, "include_files": True},
        )
        return {**result, "canonical_workspace": target.path}

    @project_router.get("/pull-requests/patch")
    async def get_pull_request_patch(
        response: Response,
        target: ProjectTarget,
        repository: Annotated[str, Query(min_length=1, max_length=512)],
        pr_url: Annotated[str, Query(min_length=1, max_length=2048)],
        base_sha: Annotated[str, Query(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")],
        head_sha: Annotated[str, Query(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")],
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return await _host_operation(
            target,
            "studio_project_pull_request_patch",
            params={
                "repository": repository,
                "pr_url": pr_url,
                "base_sha": base_sha,
                "head_sha": head_sha,
            },
        )

    router = APIRouter()
    router.include_router(settings_router)
    router.include_router(project_router)
    return router
