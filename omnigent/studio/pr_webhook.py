"""Receive signed GitHub PR deliveries and dispatch private Studio reviews."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Literal

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import (
    BaseModel,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from omnigent.studio.pr_webhook_inbox import Delivery, InboxFull, WebhookInbox
from omnigent.studio.reviews.discussion import (
    MAX_REVIEW_COLLABORATORS,
    AccountId,
    ReportDraftResult,
)
from omnigent.studio.reviews.models import (
    MAX_RUN_REVIEWERS,
    FrozenModel,
    ReviewPass,
    ReviewRun,
    Sha,
    require_uuid,
)
from omnigent.studio.reviews.service import CaptureProjectReviewRequest, CaptureReviewRunRequest
from omnigent.studio.setup import validate_server_url
from omnigent.util.reasoning_effort import EFFORT_VALUES, validate_effort

_logger = logging.getLogger(__name__)
_MAX_BODY = 1024 * 1024
_MAX_CAPTURE_ATTEMPTS = 3
_MAX_DRAFT_ATTEMPTS = 240
_DRAFT_POLL_SECONDS = 15
_REQUEST_SECONDS = 150
_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}
_RECOVERABLE_CAPTURE_STATUS = {408, 429, 500, 502, 503, 504}
_DELIVERED = {"running", "succeeded"}


class ReviewerSettings(FrozenModel):
    agent_id: str
    harness: Literal["codex-native", "claude-native"]
    skill: str | None = None
    profile_id: str | None = None
    profile_digest: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None


class WebhookRoute(FrozenModel):
    id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
    github_repository: Annotated[
        str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$")
    ]
    github_repository_id: int = Field(strict=True, gt=0)
    secret_env: Annotated[str, StringConstraints(pattern=r"^OMNIGENT_STUDIO_HOOK_[A-Z0-9_]+$")]
    project_id: str
    host_id: str
    workspace: str
    repository: str
    base_branches: list[Annotated[str, StringConstraints(min_length=1, max_length=255)]] = Field(
        min_length=1, max_length=32
    )
    review_drafts: bool = False
    review_forks: bool = False
    prepare_inline_drafts: bool = Field(default=False, strict=True)
    reviewers: list[ReviewerSettings] = Field(min_length=1, max_length=MAX_RUN_REVIEWERS)
    coordinator: ReviewerSettings | None = None
    required_reviewer_indices: list[int] = Field(default_factory=list)
    collaborators: Annotated[list[AccountId], Field(max_length=MAX_REVIEW_COLLABORATORS)] = Field(
        default_factory=list
    )
    publishers: Annotated[list[AccountId], Field(max_length=MAX_REVIEW_COLLABORATORS)] = Field(
        default_factory=list
    )

    @field_validator("project_id", "host_id")
    @classmethod
    def canonical_id(cls, value: str) -> str:
        return require_uuid(value)

    @model_validator(mode="after")
    def validate_capture_policy(self) -> WebhookRoute:
        if self.github_repository.split("/")[1] in {".", ".."}:
            raise ValueError("Choose a GitHub owner and repository")
        if not self.workspace.startswith("/") or not self.workspace.isprintable():
            raise ValueError("Use an absolute workspace directory on the connected host")
        self.capture(1, "a" * 40, "b" * 40)
        return self

    @property
    def policy_digest(self) -> str:
        excluded = {
            field
            for field in ("collaborators", "publishers", "prepare_inline_drafts")
            if not getattr(self, field)
        }
        return hashlib.sha256(self.model_dump_json(exclude=excluded).encode()).hexdigest()

    def capture(self, number: int, base: str, head: str) -> CaptureReviewRunRequest:
        target = {
            "pr_url": f"https://github.com/{self.github_repository.lower()}/pull/{number}",
            "host_id": self.host_id,
            "workspace": self.workspace,
            "repository": self.repository,
            "expected_base_sha": base,
            "expected_head_sha": head,
        }
        return CaptureReviewRunRequest.model_validate(
            {
                "reviewers": [{**target, **member.model_dump()} for member in self.reviewers],
                "coordinator": {**target, **self.coordinator.model_dump()}
                if self.coordinator
                else None,
                "required_reviewer_indices": self.required_reviewer_indices,
                "collaborators": self.collaborators,
                "publishers": self.publishers,
            }
        )

    def revision_key(self, number: int, base: str, head: str) -> str:
        identity = [self.project_id, self.github_repository_id, number, base, head]
        return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


class WebhookConfig(FrozenModel):
    routes: list[WebhookRoute] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def unique_routes(self) -> WebhookConfig:
        if len({route.id for route in self.routes}) != len(self.routes):
            raise ValueError("Use each webhook route ID once")
        targets = {(route.project_id, route.github_repository_id) for route in self.routes}
        if len(targets) != len(self.routes):
            raise ValueError("Configure a single route per repository in each project")
        if len({route.secret_env for route in self.routes}) != len(self.routes):
            raise ValueError("Give each webhook route its own secret")
        return self


class _Repository(BaseModel):
    id: int = Field(strict=True, gt=0)
    full_name: str


class _Revision(BaseModel):
    sha: Sha


class _BaseRevision(_Revision):
    ref: str
    repo: _Repository


class _HeadRevision(_Revision):
    repo: _Repository | None


class _PullRequest(BaseModel):
    number: int = Field(strict=True, gt=0)
    state: str
    draft: bool = Field(strict=True)
    base: _BaseRevision
    head: _HeadRevision


class _Event(BaseModel):
    action: str
    number: int = Field(strict=True, gt=0)
    repository: _Repository
    pull_request: _PullRequest


def _check_signature(payload: bytearray, signature: str, secret: bytes) -> None:
    if not re.fullmatch(r"sha256=[0-9a-f]{64}", signature) or not hmac.compare_digest(
        signature, "sha256=" + hmac.digest(secret, payload, "sha256").hex()
    ):
        raise HTTPException(403, "Invalid webhook signature")


class WebhookWorker:
    def __init__(
        self, inbox: WebhookInbox, routes: dict[str, WebhookRoute], studio: httpx.AsyncClient
    ) -> None:
        self.inbox = inbox
        self.routes = routes
        self.studio = studio

    @staticmethod
    def _validated_run(
        raw: object, route: WebhookRoute, request: CaptureReviewRunRequest
    ) -> ReviewRun:
        run = ReviewRun.model_validate(raw)
        expected = request.reviewers[0]
        target = run.target
        if (
            require_uuid(run.id) != run.id
            or target.source_project_id != route.project_id
            or target.host_id != route.host_id
            or target.repository_path != route.repository
            or target.repository.casefold() != route.github_repository.casefold()
            or target.workspace != (route.workspace.rstrip("/") or "/")
            or target.pr_url != expected.pr_url
            or (target.base_sha, target.head_sha)
            != (expected.expected_base_sha, expected.expected_head_sha)
            or run.patch_sha256 is None
        ):
            raise ValueError("Studio returned a different captured review")
        if (run.coordinator is None) != (request.coordinator is None) or len(run.reviewers) != len(
            request.reviewers
        ):
            raise ValueError("Studio returned a different reviewer roster")

        def captured_key(member: ReviewPass) -> tuple[str | None, ...]:
            if member.run_id != run.id or member.target != target:
                raise ValueError("Studio returned a reviewer for a different run")
            profile = member.profile
            role = profile.role
            return (
                member.run_role,
                profile.agent_id,
                profile.harness,
                profile.skill,
                role.id if role else None,
                role.digest if role else None,
                profile.model,
                profile.reasoning_effort,
            )

        def requested_key(
            settings: CaptureProjectReviewRequest, role: str | None
        ) -> tuple[str | None, ...]:
            return (
                role,
                settings.agent_id,
                settings.harness,
                settings.skill,
                settings.profile_id,
                settings.profile_digest,
                settings.model,
                validate_effort(settings.reasoning_effort, "session metadata", EFFORT_VALUES),
            )

        required = set(request.required_reviewer_indices)
        expected_roster = Counter(
            requested_key(
                settings,
                None
                if request.coordinator is None
                else "required"
                if index in required
                else "candidate",
            )
            for index, settings in enumerate(request.reviewers)
        )
        if Counter(captured_key(member) for member in run.reviewers) != expected_roster:
            raise ValueError("Studio returned a different reviewer roster")
        if (
            request.coordinator is not None
            and run.coordinator is not None
            and (
                captured_key(run.coordinator) != requested_key(request.coordinator, "coordinator")
            )
        ):
            raise ValueError("Studio returned a different coordinator profile")
        return run

    @staticmethod
    def _start_state(run: ReviewRun, *, response_confirmed: bool) -> str:
        members = (
            [run.coordinator]
            if run.coordinator is not None and run.plan is None
            else run.selected_reviewers()
        )
        delivered = sum(
            member.reviewer_session_id is not None and member.dispatch_status in _DELIVERED
            for member in members
        )
        if members and delivered == len(members):
            return (
                "awaiting_plan" if run.coordinator is not None and run.plan is None else "started"
            )
        if not response_confirmed or any(
            member.dispatch_status in {"unknown", "dispatching"} for member in members
        ):
            return "unconfirmed"
        return "partial" if delivered else "failed"

    async def _reconcile(
        self, job: Delivery, route: WebhookRoute, request: CaptureReviewRunRequest, run_id: str
    ) -> None:
        try:
            async with asyncio.timeout(_REQUEST_SECONDS):
                response = await self.studio.get(f"v1/studio/review-runs/{run_id}")
            response.raise_for_status()
            run = self._validated_run(response.json(), route, request)
            if run.id != run_id:
                raise ValueError("Studio returned another review")
            state = self._start_state(run, response_confirmed=False)
        except (httpx.HTTPError, TimeoutError, ValueError):
            state = "unconfirmed"
        await self._after_start(job, route, state, "reconciled_without_redispatch")

    async def _after_start(
        self, job: Delivery, route: WebhookRoute, state: str, detail: str
    ) -> None:
        if route.prepare_inline_drafts and state in {"started", "awaiting_plan"}:
            await asyncio.to_thread(self.inbox.finish, job, "drafting", state)
        else:
            await asyncio.to_thread(self.inbox.finish, job, state, detail)

    async def _prepare_drafts(self, job: Delivery) -> None:
        assert job.run_id is not None
        if job.attempts > _MAX_DRAFT_ATTEMPTS:
            await asyncio.to_thread(self.inbox.finish, job, "draft_failed", "report_wait_expired")
            return
        state, detail = "drafting", "report_waiting"
        try:
            async with asyncio.timeout(_REQUEST_SECONDS):
                response = await self.studio.post(
                    f"v1/studio/review-runs/{job.run_id}/discussion/inline-drafts/from-report",
                    json={},
                )
            response.raise_for_status()
            result = ReportDraftResult.model_validate(response.json())
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 409:
                state, detail = "draft_skipped", "revision_or_draft_conflict"
            elif status not in _RECOVERABLE_CAPTURE_STATUS:
                state, detail = "draft_failed", f"draft_http_{status}"
            else:
                detail = f"draft_http_{status}"
        except (httpx.HTTPError, TimeoutError):
            detail = "draft_response_unavailable"
        except ValueError:
            state, detail = "draft_failed", "invalid_draft_receipt"
        else:
            if result.state == "prepared":
                state, detail = "drafted", f"eligible={result.eligible};created={result.created}"
            elif result.state == "incomplete":
                state, detail = "draft_skipped", "report_incomplete"
        await asyncio.to_thread(
            self.inbox.finish,
            job,
            state,
            detail,
            delay=_DRAFT_POLL_SECONDS if state == "drafting" else 0,
        )

    async def dispatch(self, job: Delivery) -> None:
        route = self.routes.get(job.route)
        if route is None or route.policy_digest != job.policy:
            state = {"starting": "unconfirmed", "drafting": "draft_skipped"}.get(
                job.state, "skipped"
            )
            await asyncio.to_thread(self.inbox.finish, job, state, "policy_changed")
            return
        if job.state == "drafting":
            await self._prepare_drafts(job)
            return
        request = CaptureReviewRunRequest.model_validate_json(job.payload)
        if job.state == "starting":
            assert job.run_id is not None
            await self._reconcile(job, route, request, job.run_id)
            return

        if job.attempts > _MAX_CAPTURE_ATTEMPTS:
            await asyncio.to_thread(self.inbox.finish, job, "failed", "capture_attempts_exhausted")
            return
        failure = "capture_unavailable"
        retry = True
        try:
            async with asyncio.timeout(_REQUEST_SECONDS):
                response = await self.studio.post(
                    f"v1/projects/{route.project_id}/studio/review-runs",
                    json=request.model_dump(mode="json", exclude_defaults=True),
                    headers={"Idempotency-Key": f"studio-webhook-{job.key}"},
                )
            response.raise_for_status()
            run = self._validated_run(response.json(), route, request)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 409:
                await asyncio.to_thread(
                    self.inbox.finish, job, "skipped", "revision_or_capture_conflict"
                )
                return
            retry = status in _RECOVERABLE_CAPTURE_STATUS
            failure = f"capture_http_{status}"
        except (httpx.HTTPError, TimeoutError):
            pass
        except ValueError:
            retry, failure = False, "invalid_capture_receipt"
        else:
            if not await asyncio.to_thread(self.inbox.mark_starting, job, run.id):
                return
            try:
                async with asyncio.timeout(_REQUEST_SECONDS):
                    started = await self.studio.post(
                        f"v1/studio/review-runs/{run.id}/start", json={}
                    )
                started.raise_for_status()
                launched = self._validated_run(started.json().get("run"), route, request)
                if launched.id != run.id:
                    raise ValueError("Studio returned another review")
                state = self._start_state(launched, response_confirmed=True)
            except (httpx.HTTPError, TimeoutError, ValueError, AttributeError):
                await self._reconcile(job, route, request, run.id)
            else:
                await self._after_start(job, route, state, "start_response")
            return

        if retry and job.attempts < _MAX_CAPTURE_ATTEMPTS:
            await asyncio.to_thread(
                self.inbox.finish, job, "queued", failure, delay=5 * job.attempts
            )
        else:
            await asyncio.to_thread(self.inbox.finish, job, "failed", failure)

    async def run(self) -> None:
        while True:
            try:
                job = await asyncio.to_thread(self.inbox.claim)
                if job is not None:
                    await self.dispatch(job)
                    continue
            except (sqlite3.Error, ValidationError):
                _logger.exception("Studio webhook inbox could not process a delivery")
            await asyncio.sleep(0.5)


def create_receiver(
    config: WebhookConfig,
    inbox: WebhookInbox,
    studio: httpx.AsyncClient,
    secrets: dict[str, bytes],
) -> FastAPI:
    routes = {route.id: route for route in config.routes}
    if routes.keys() != secrets.keys() or any(len(secret) < 32 for secret in secrets.values()):
        raise ValueError("Set a webhook secret of at least 32 bytes for each configured route")
    if len(set(secrets.values())) != len(secrets):
        raise ValueError("Give each webhook route a different secret")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with studio:
            task = asyncio.create_task(
                WebhookWorker(inbox, routes, studio).run(), name="studio-pr-webhooks"
            )
            try:
                yield
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(
        title="Studio PR webhooks",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.post("/webhooks/github/{route_id}")
    async def deliver(request: Request, route_id: str) -> Response:
        route = routes.get(route_id)
        if route is None:
            raise HTTPException(404, "Webhook route not found")
        signature = request.headers.get("x-hub-signature-256", "")
        if not re.fullmatch(r"sha256=[0-9a-f]{64}", signature):
            raise HTTPException(403, "Invalid webhook signature")
        payload = bytearray()
        async for chunk in request.stream():
            if len(chunk) > _MAX_BODY - len(payload):
                raise HTTPException(413, "Webhook payload exceeds 1 MiB")
            payload.extend(chunk)
        _check_signature(payload, signature, secrets[route_id])
        if request.headers.get("x-github-event") != "pull_request":
            return Response(status_code=204)
        try:
            event = _Event.model_validate_json(payload, strict=True)
        except ValidationError as exc:
            raise HTTPException(422, "Invalid pull request event") from exc
        pr = event.pull_request
        identity = (route.github_repository_id, route.github_repository.casefold())
        if (
            (event.repository.id, event.repository.full_name.casefold()) != identity
            or (pr.base.repo.id, pr.base.repo.full_name.casefold()) != identity
            or event.number != pr.number
        ):
            raise HTTPException(403, "Pull request is outside this webhook route")
        if (
            event.action not in _ACTIONS
            or pr.state != "open"
            or pr.base.ref not in route.base_branches
            or (pr.draft and not route.review_drafts)
            or pr.head.repo is None
            or (pr.head.repo.id != route.github_repository_id and not route.review_forks)
        ):
            return Response(status_code=204)
        capture = route.capture(pr.number, pr.base.sha, pr.head.sha)
        key = route.revision_key(pr.number, pr.base.sha, pr.head.sha)
        try:
            await asyncio.to_thread(
                inbox.enqueue,
                key,
                route_id,
                route.policy_digest,
                capture.model_dump_json(exclude_defaults=True),
            )
        except (InboxFull, sqlite3.Error) as exc:
            raise HTTPException(503, "Webhook inbox is unavailable") from exc
        return Response(status_code=202)

    return app


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True, help="private durable inbox file")
    parser.add_argument(
        "--status", action="store_true", help="print recent saved deliveries and exit"
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--server")
    parser.add_argument("--mode", choices=("local", "server"), default="server")
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8769)
    args = parser.parse_args(argv)
    try:
        inbox = WebhookInbox(args.database.expanduser())
        if args.status:
            print(json.dumps(inbox.recent(), indent=2))
            return 0
        if args.config is None or args.server is None:
            parser.error("Provide --config and --server when serving webhooks")
        with args.config.expanduser().open("rb") as stream:
            raw = stream.read(_MAX_BODY + 1)
        if len(raw) > _MAX_BODY:
            raise ValueError("The webhook configuration exceeds 1 MiB")
        config = WebhookConfig.model_validate_json(raw)
        server = validate_server_url(args.server, args.mode)
        token = os.environ.get("OMNIGENT_STUDIO_REVIEW_TOKEN", "")
        if args.mode == "server" and (
            not token
            or not token.isascii()
            or not token.isprintable()
            or any(c.isspace() for c in token)
        ):
            raise ValueError("Set OMNIGENT_STUDIO_REVIEW_TOKEN for server mode")
        studio = httpx.AsyncClient(
            base_url=f"{server}/",
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=httpx.Timeout(_REQUEST_SECONDS, connect=5),
            follow_redirects=False,
            trust_env=False,
        )
        secrets = {
            route.id: os.environ.get(route.secret_env, "").encode() for route in config.routes
        }
        app = create_receiver(config, inbox, studio, secrets)
    except ValidationError:
        parser.error("The webhook configuration is invalid")
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.error(str(exc))
    uvicorn.run(app, host=args.listen, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
