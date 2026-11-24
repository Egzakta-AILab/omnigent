"""Versioned extension RPCs for org-readable Studio tasks and private sessions."""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
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
from omnigent.stores.studio_review_queue import ReviewQueue
from omnigent.stores.studio_review_store import ReviewNotFoundError, StudioReviewStore
from omnigent.stores.studio_tasks import StudioTaskStore
from omnigent.studio import fleet, rpc
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

    @asynccontextmanager
    async def lease_lifespan(app: FastAPI) -> AsyncIterator[None]:
        async def sweep() -> None:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://studio-worker.internal"
            ) as client:
                while True:
                    if os.getenv("OMNIGENT_STUDIO_DISPATCH_ACCOUNT") and (
                        key_file := os.getenv("OMNIGENT_STUDIO_DISPATCH_TOKEN_FILE")
                    ):
                        try:
                            from pathlib import Path

                            from omnigent.studio.credential_agent import protected_file

                            token = protected_file(Path(key_file)).strip()
                            response = await client.post(
                                "/v1/studio/fleet/reap",
                                json={},
                                headers={"Authorization": f"Bearer {token}"},
                            )
                            response.raise_for_status()
                        except (ValueError, OSError, httpx.HTTPError) as exc:
                            logging.getLogger(__name__).warning(
                                "Studio lease sweep unavailable (%s)", type(exc).__name__
                            )
                    await asyncio.sleep(5)

        worker = asyncio.create_task(sweep())
        try:
            yield
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    router = APIRouter(
        lifespan=lease_lifespan,
        dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)],
    )

    @router.post("/studio/fleet/reap", include_in_schema=False)
    async def reconcile_expired(request: Request) -> dict[str, bool]:
        actor = require_user(request, auth)
        if (
            not actor
            or actor != os.getenv("OMNIGENT_STUDIO_DISPATCH_ACCOUNT")
            or not permissions
            or not effective_admin(await asyncio.to_thread(permissions.is_admin, actor))
        ):
            raise HTTPException(403, "Fleet worker required")
        if not tasks:
            raise fleet.unavailable("Studio dispatch store is unavailable")
        await fleet.reap(request, tasks)
        return {"processed": True}

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
                if scope == "releases" and method in (
                    "previewIssue",
                    "confirmIssue",
                    "createIssueConfirmed",
                ):
                    if extension_id != "egzakta.release":
                        raise HTTPException(403, "Release extension required")
                    if method == "confirmIssue" and (
                        auth is None or not auth.has_browser_consent(request)
                    ):
                        raise HTTPException(403, "Confirm from the authenticated Studio web page")
                    contract = {
                        "previewIssue": rpc.ReleaseFailure,
                        "confirmIssue": rpc.BoundReleaseFailure,
                        "createIssueConfirmed": rpc.ConfirmedReleaseFailure,
                    }[method]
                    params = contract.model_validate(params).model_dump(mode="json")
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
                if method == "forSession":
                    target = rpc.SessionRef.model_validate(params)
                    await require_access(
                        actor, target.session_id.hex, LEVEL_READ, permissions, conversations
                    )
                    rows = await asyncio.to_thread(tasks.tasks_for_session, target.session_id)
                    visible = await asyncio.to_thread(filter_sessions, rows)
                    return {
                        "items": [r for r in visible if str(target.session_id) in r["session_ids"]]
                    }
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
                if method in ("report", "shadowReport"):
                    from omnigent.studio.verification import fleet_report, shadow_report

                    if not effective_admin(await asyncio.to_thread(permissions.is_admin, actor)):
                        raise HTTPException(403, "Studio reporting requires an administrator")
                    window = params.get("window", "7d")
                    if not isinstance(window, str):
                        raise HTTPException(422, "Invalid report window")
                    if method == "report":
                        return await asyncio.to_thread(
                            fleet_report, request, tasks, window, conversations, permissions
                        )
                    return await asyncio.to_thread(shadow_report, tasks, window)
                if method == "inventory":
                    return await fleet.inventory(
                        request,
                        tasks,
                        actor,
                        rpc.FleetPage.model_validate(params),
                        effective_admin(await asyncio.to_thread(permissions.is_admin, actor)),
                    )
                if method == "dispatchFreeAgent":
                    return await fleet.dispatch(
                        request, tasks, actor, rpc.LeaseClaim.model_validate(params)
                    )
                if method in ("wakeWorkspace", "stopWorkspace"):
                    return await asyncio.to_thread(
                        fleet.control,
                        request,
                        actor,
                        method,
                        rpc.WorkspaceRef.model_validate(params),
                    )
                if method == "allocatePreview":
                    if not effective_admin(await asyncio.to_thread(permissions.is_admin, actor)):
                        raise HTTPException(403, "Preview allocation requires a Studio operator")
                    return await asyncio.to_thread(
                        fleet.allocate, request, tasks, actor, rpc.Preview.model_validate(params)
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
                    value = rpc.FinishRound.model_validate(params)
                    result = await asyncio.to_thread(tasks.update_lease, actor, value)
                    if result["state"] != "active":
                        await fleet.reap(request, tasks, value.idempotency_key.hex)
                    return result
                if method == "adminSeats":
                    if not effective_admin(await asyncio.to_thread(permissions.is_admin, actor)):
                        raise HTTPException(403, "Seat administration requires an administrator")
                    return await asyncio.to_thread(
                        tasks.admin_seat, actor, rpc.Seat.model_validate(params)
                    )
            if scope == "reviews" and method in {
                "listQueue",
                "getWalkthrough",
                "dispute",
                "dismiss",
                "askFrozen",
            }:
                return await asyncio.to_thread(
                    ReviewQueue(tasks.storage_location).rpc, actor, method, params
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
