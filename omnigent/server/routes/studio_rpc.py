"""Versioned extension RPCs for org-readable Studio tasks and private sessions."""

import asyncio
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError

from omnigent.db.account_authority import account_generation
from omnigent.db.db_models import current_workspace_id
from omnigent.db.utils import shared_read_scope
from omnigent.extensions import ExtensionPluginState
from omnigent.extensions.studio_rpc import (
    StudioRpcContext,
    invoke_studio_handler,
    load_studio_handlers,
    studio_package_method,
)
from omnigent.server.auth import (
    LEVEL_READ,
    RESERVED_USER_LOCAL,
    RESERVED_USER_PUBLIC,
    AuthProvider,
    SharingMode,
    effective_admin,
)
from omnigent.server.permissions import check_session_access
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.studio_review_store import ReviewNotFoundError, StudioReviewStore
from omnigent.stores.studio_tasks import StudioTaskStore
from omnigent.studio import rpc
from omnigent.studio.linear_write import write_linear


def create_studio_rpc_router(
    extensions: ExtensionPluginState,
    available_bundles: frozenset[str],
    tasks: StudioTaskStore | None,
    reviews: StudioReviewStore | None,
    conversations: ConversationStore,
    permissions: PermissionStore | None,
    auth: AuthProvider | None,
) -> APIRouter:
    handlers = load_studio_handlers(extensions)
    router = APIRouter(
        dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)]
    )

    @router.post("/extensions/{extension_id}/studio/v1/{scope}/{method}")
    async def invoke(
        extension_id: str,
        scope: str,
        method: str,
        params: dict[str, Any],
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        actor = require_user(request, auth)
        if (
            not actor
            or actor in (RESERVED_USER_PUBLIC, RESERVED_USER_LOCAL)
            or permissions is None
        ):
            raise HTTPException(401, "An authenticated Studio account is required")
        permission_store = permissions
        manifest = extensions.get(extension_id)
        if (
            not manifest
            or (manifest.entrypoints.browser and extension_id not in available_bundles)
            or f"studio.{scope}" not in manifest.permissions
        ):
            raise HTTPException(403, "Extension does not have this Studio permission")
        if tasks is None:
            raise HTTPException(
                503, {"status": "env_blocked", "reason": "Studio database unavailable"}
            )

        # The browser host's own registered method chooses scope and method.
        # A raw caller still goes through the same authentication, grant and ACL checks.
        def filter_sessions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            ids = {sid for row in rows for sid in row["session_ids"]}
            with shared_read_scope():
                visible = {
                    sid
                    for sid in ids
                    if check_session_access(
                        actor, UUID(sid).hex, LEVEL_READ, permission_store, conversations
                    )
                }
            return [
                {**row, "session_ids": [sid for sid in row["session_ids"] if sid in visible]}
                for row in rows
            ]

        async def filtered(task: dict[str, Any]) -> dict[str, Any]:
            return (await asyncio.to_thread(filter_sessions, [task]))[0]

        try:
            if studio_package_method(scope, method):
                context = StudioRpcContext(
                    actor,
                    current_workspace_id(),
                    tasks.storage_location,
                    account_generation(actor),
                )
                return await asyncio.to_thread(
                    invoke_studio_handler, handlers, scope, method, params, context
                )
            if scope == "tasks":
                if method == "list":
                    rows = await asyncio.to_thread(
                        tasks.task_list, rpc.Page.model_validate(params)
                    )
                    return {"items": await asyncio.to_thread(filter_sessions, rows)}
                if method == "getOrCreate":
                    return await filtered(
                        await asyncio.to_thread(
                            tasks.task_for_issue, actor, rpc.TaskKey.model_validate(params)
                        )
                    )
                if method == "updateAcceptance":
                    return await filtered(
                        await asyncio.to_thread(
                            tasks.change_task, actor, rpc.Acceptance.model_validate(params)
                        )
                    )
                if method == "linkPr":
                    return await filtered(
                        await asyncio.to_thread(
                            tasks.change_task, actor, rpc.PullRequest.model_validate(params)
                        )
                    )
                if method == "linkSession":
                    link = rpc.SessionLink.model_validate(params)
                    await require_access(
                        actor, link.session_id.hex, LEVEL_READ, permissions, conversations
                    )
                    return await filtered(await asyncio.to_thread(tasks.change_task, actor, link))
                if method == "createHandoff":
                    return await asyncio.to_thread(
                        tasks.handoff, actor, rpc.Handoff.model_validate(params)
                    )
                if method in ("previewLinear", "confirmLinear"):
                    if method == "confirmLinear" and (
                        auth is None or not auth.has_browser_consent(request)
                    ):
                        raise HTTPException(403, "Confirm from the authenticated Studio web page")
                    return await asyncio.to_thread(
                        tasks.linear,
                        actor,
                        extension_id,
                        (
                            rpc.LinearIssueChange if "change" in params else rpc.LinearComment
                        ).model_validate(params),
                        method,
                    )
                if method == "syncLinearConfirmed":
                    comment = (
                        rpc.ConfirmedIssueChange if "change" in params else rpc.ConfirmedComment
                    ).model_validate(params)
                    intent = await asyncio.to_thread(
                        tasks.linear, actor, extension_id, comment, method
                    )
                    return await write_linear(
                        intent["linear_workspace"],
                        intent["linear_issue_id"],
                        comment,
                    )
            if scope == "inbox":
                if method == "list":
                    return {
                        "items": await asyncio.to_thread(
                            tasks.inbox, actor, rpc.Page.model_validate(params)
                        )
                    }
                if method == "respond":
                    return await asyncio.to_thread(
                        tasks.respond, actor, rpc.Response.model_validate(params)
                    )
            if scope == "fleet":
                if method == "inventory":
                    return await asyncio.to_thread(
                        tasks.inventory, actor, rpc.Page.model_validate(params)
                    )
                if method == "claimLease":
                    return await asyncio.to_thread(
                        tasks.update_lease, actor, rpc.LeaseClaim.model_validate(params)
                    )
                if method == "heartbeat":
                    return await asyncio.to_thread(
                        tasks.update_lease, actor, rpc.LeaseRef.model_validate(params)
                    )
                if method == "finishRound":
                    return await asyncio.to_thread(
                        tasks.update_lease, actor, rpc.FinishRound.model_validate(params)
                    )
                if method == "adminSeats":
                    if not effective_admin(await asyncio.to_thread(permissions.is_admin, actor)):
                        raise HTTPException(403, "Seat administration requires an administrator")
                    return await asyncio.to_thread(
                        tasks.admin_seat, actor, rpc.Seat.model_validate(params)
                    )
            if (scope, method) == ("reviews", "listRuns"):
                page = rpc.Page.model_validate(params)
                mode = getattr(request.app.state, "sharing_mode", lambda: SharingMode.ON)()
                if not reviews or mode not in (SharingMode.ON, SharingMode.READ_ONLY):
                    return {"items": [], "next_cursor": None}
                shared = await asyncio.to_thread(
                    reviews.list_shared_reviews,
                    actor,
                    limit=min(page.limit, 25),
                    after_id=UUID(page.after).hex if page.after else None,
                )
                return {
                    "items": [entry.model_dump(mode="json") for entry in shared.entries],
                    "next_cursor": shared.next_cursor,
                }
            if (scope, method) == ("releases", "list"):
                return {
                    "items": await asyncio.to_thread(
                        tasks.task_list, rpc.Page.model_validate(params), releases=True
                    )
                }
        except ReviewNotFoundError as exc:
            raise HTTPException(404, "Shared review cursor unavailable") from exc
        except (ValidationError, ValueError) as exc:
            raise HTTPException(422, "Invalid Studio v1 parameters") from exc
        raise HTTPException(404, "Unknown Studio v1 method")

    return router
