"""Signed delivery intake, durable revision deduplication, and safe start recovery."""

import asyncio
import hmac
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from omnigent.studio.pr_webhook import (
    WebhookConfig,
    WebhookRoute,
    WebhookWorker,
    create_receiver,
)
from omnigent.studio.pr_webhook_inbox import WebhookInbox

_SECRET = b"a local webhook test secret of at least 32 bytes"
_BASE, _HEAD, _NEXT = "a" * 40, "b" * 40, "c" * 40


@pytest.fixture
def route() -> WebhookRoute:
    return WebhookRoute.model_validate(
        {
            "id": "example",
            "github_repository": "Example/Project",
            "github_repository_id": 12,
            "secret_env": "OMNIGENT_STUDIO_HOOK_EXAMPLE",
            "project_id": "1" * 32,
            "host_id": "2" * 32,
            "workspace": "/workspace",
            "repository": "nested",
            "base_branches": ["main"],
            "reviewers": [
                {"agent_id": "3" * 32, "harness": "codex-native", "skill": "review"},
            ],
        }
    )


def _event(
    *,
    action: str = "opened",
    head: str = _HEAD,
    draft: bool = False,
    repo_id: int = 12,
    base_ref: str = "main",
    head_repo: int = 12,
) -> dict:
    repo = {"id": repo_id, "full_name": "Example/Project"}
    return {
        "action": action,
        "number": 17,
        "repository": repo,
        "pull_request": {
            "number": 17,
            "state": "open",
            "draft": draft,
            "title": "Unicode fixture π",
            "base": {"sha": _BASE, "repo": repo, "ref": base_ref},
            "head": {"sha": head, "repo": {"id": head_repo, "full_name": "Example/Project"}},
        },
    }


async def _send(receiver: httpx.AsyncClient, event: dict, *, corrupt: bool = False) -> int:
    payload = json.dumps(event, ensure_ascii=False).encode()
    signature = hmac.digest(_SECRET, payload, "sha256").hex()
    response = await receiver.post(
        "/webhooks/github/example",
        content=payload + (b" " if corrupt else b""),
        headers={"X-Hub-Signature-256": f"sha256={signature}", "X-GitHub-Event": "pull_request"},
    )
    return response.status_code


def _run(
    route: WebhookRoute, *, launched: bool = False, coordinated: bool = False, wrong: bool = False
) -> dict:
    run_id, member_id = "4" * 32, "5" * 32
    target = {
        "source_project_id": route.project_id,
        "repository_path": route.repository,
        "host_id": route.host_id,
        "workspace": route.workspace,
        "pr_url": "https://github.com/example/project/pull/17",
        "repository": "example/project",
        "pr_number": 17,
        "base_sha": _BASE,
        "head_sha": _NEXT if wrong else _HEAD,
    }
    member = {
        "id": member_id,
        "run_id": run_id,
        "owner_user_id": None,
        "target": target,
        "profile": {
            "agent_id": route.reviewers[0].agent_id,
            "agent_version": "1",
            "harness": "codex-native",
            "skill": "review",
        },
        "state": "running" if launched else "prepared",
        "dispatch_status": "running" if launched else "not_requested",
        "reviewer_session_id": member_id if launched else None,
        "created_at": 1,
        "updated_at": 1,
        "version": 1,
    }
    reviewers = [
        {
            **member,
            "id": f"{index + 10:032x}",
            "run_role": ("required" if index in route.required_reviewer_indices else "candidate")
            if coordinated
            else None,
            "profile": {
                **member["profile"],
                "agent_id": settings.agent_id,
                "harness": settings.harness,
                "skill": settings.skill,
                "model": settings.model,
                "reasoning_effort": settings.reasoning_effort,
            },
        }
        for index, settings in enumerate(route.reviewers)
    ]
    return {
        "id": run_id,
        "target": target,
        "reviewers": reviewers[::-1],
        "coordinator": {**member, "id": "6" * 32, "run_role": "coordinator"}
        if coordinated
        else None,
        "patch_sha256": "f" * 64,
        "created_at": 1,
    }


@pytest.mark.asyncio
async def test_only_signed_in_scope_revisions_enter_the_durable_inbox(
    tmp_path: Path, route: WebhookRoute
) -> None:
    database = tmp_path / "reviews.db"
    inbox = WebhookInbox(database)
    async with httpx.AsyncClient() as studio:
        app = create_receiver(WebhookConfig(routes=[route]), inbox, studio, {route.id: _SECRET})
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://receiver"
        ) as receiver:
            assert await _send(receiver, _event(), corrupt=True) == 403
            assert await _send(receiver, _event(repo_id=99)) == 403
            wrong_base = _event()
            wrong_base["pull_request"]["base"]["repo"] = {"id": 99, "full_name": "Example/Project"}
            assert await _send(receiver, wrong_base) == 403
            assert await _send(receiver, _event(base_ref="develop")) == 204
            assert await _send(receiver, _event(head_repo=99)) == 204
            assert await _send(receiver, _event(draft=True)) == 204
            assert await _send(receiver, _event(action="closed")) == 204
            assert inbox.recent() == []
            assert await _send(receiver, _event()) == 202
            assert await _send(receiver, _event(action="reopened")) == 202
            assert await _send(receiver, _event(action="synchronize", head=_NEXT)) == 202
    reopened = WebhookInbox(database)
    assert len(reopened.recent()) == 2
    first, competing = await asyncio.gather(
        asyncio.to_thread(inbox.claim), asyncio.to_thread(reopened.claim)
    )
    assert first is not None and competing is not None and first.key != competing.key
    assert inbox.claim() is None
    assert {
        json.loads(job.payload)["reviewers"][0]["expected_head_sha"] for job in (first, competing)
    } == {_HEAD, _NEXT}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "receipt", "expected"),
    [
        (200, {"state": "prepared", "eligible": 1, "created": 0}, "drafted"),
        (200, {"state": "incomplete"}, "draft_skipped"),
        (409, {}, "draft_skipped"),
    ],
)
async def test_draft_polling_survives_restart_without_start_or_publication_replay(
    tmp_path: Path, route: WebhookRoute, status: int, receipt: dict, expected: str
) -> None:
    reviewers = [
        route.reviewers[0].model_copy(update={"model": "review-model"}),
        route.reviewers[0].model_copy(update={"model": "second-model"}),
    ]
    route = route.model_copy(update={"prepare_inline_drafts": True, "reviewers": reviewers})
    database = tmp_path / "reviews.db"
    inbox = WebhookInbox(database)
    key = route.revision_key(17, _BASE, _HEAD)
    assert inbox.enqueue(
        key, route.id, route.policy_digest, route.capture(17, _BASE, _HEAD).model_dump_json()
    )
    calls = []
    capture_path = f"/v1/projects/{route.project_id}/studio/review-runs"
    run_path = f"/v1/studio/review-runs/{'4' * 32}"
    draft_path = f"{run_path}/discussion/inline-drafts/from-report"

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == capture_path:
            return httpx.Response(200, json=_run(route))
        if request.url.path == f"{run_path}/start":
            return httpx.Response(200, json={"run": _run(route, launched=True)})
        assert request.url.path == draft_path and request.method == "POST"
        if calls.count(draft_path) == 1:
            return httpx.Response(200, json={"state": "waiting"})
        if calls.count(draft_path) == 2:
            raise httpx.ReadTimeout("lost draft acknowledgement")
        return httpx.Response(status, json=receipt)

    async with httpx.AsyncClient(
        base_url="http://studio", transport=httpx.MockTransport(handle)
    ) as studio:
        worker = WebhookWorker(inbox, {route.id: route}, studio)
        claimed = inbox.claim()
        assert claimed is not None
        await worker.dispatch(claimed)
        assert inbox.recent()[0]["state"] == "drafting"
        for _ in range(3):
            inbox = WebhookInbox(database)
            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE deliveries SET available_at = 0")
            claimed = inbox.claim()
            assert claimed is not None and claimed.state == "drafting"
            await WebhookWorker(inbox, {route.id: route}, studio).dispatch(claimed)
    assert calls == [capture_path, f"{run_path}/start", draft_path, draft_path, draft_path]
    assert inbox.recent()[0]["state"] == expected
    assert inbox.claim() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "expected", "methods"),
    [
        ("none", "started", ["POST", "POST"]),
        ("stale", "skipped", ["POST"]),
        ("wrong_capture", "failed", ["POST"]),
        ("capture_retry", "started", ["POST", "POST", "POST"]),
        ("lost_ack", "started", ["POST", "POST", "GET"]),
        ("unknown", "unconfirmed", ["POST", "POST", "GET"]),
        ("restart", "unconfirmed", ["GET"]),
        ("coordinator", "awaiting_plan", ["POST", "POST"]),
    ],
)
async def test_worker_starts_once_and_recovers_a_lost_acknowledgment_read_only(
    tmp_path: Path, route: WebhookRoute, fault: str, expected: str, methods: list[str]
) -> None:
    if fault == "coordinator":
        route = route.model_copy(update={"coordinator": route.reviewers[0]})
    if fault == "none":
        route = route.model_copy(
            update={
                "collaborators": ["reviewer@example.test"],
                "publishers": ["reviewer@example.test"],
            }
        )
    inbox = WebhookInbox(tmp_path / "reviews.db")
    request = route.capture(17, _BASE, _HEAD)
    key = route.revision_key(17, _BASE, _HEAD)
    assert inbox.enqueue(key, route.id, route.policy_digest, request.model_dump_json())
    job = inbox.claim()
    assert job is not None
    run_id = "4" * 32
    if fault == "restart":
        assert inbox.mark_starting(job, run_id)
        with sqlite3.connect(inbox.path) as connection:
            connection.execute("UPDATE deliveries SET available_at = 0")
        recovered = WebhookInbox(inbox.path).claim()
        assert recovered is not None and recovered.state == "starting"
        job = recovered
    observed = []

    def handle(call: httpx.Request) -> httpx.Response:
        observed.append(call.method)
        if "/projects/" in call.url.path:
            assert call.headers["idempotency-key"] == f"studio-webhook-{key}"
            payload = json.loads(call.content)
            assert payload == request.model_dump(mode="json", exclude_defaults=True)
            assert payload.get("collaborators", []) == route.collaborators
            assert payload.get("publishers", []) == route.publishers
            if fault == "stale":
                return httpx.Response(409)
            if fault == "capture_retry" and len(observed) == 1:
                return httpx.Response(503)
            return httpx.Response(
                200,
                json=_run(
                    route, wrong=fault == "wrong_capture", coordinated=fault == "coordinator"
                ),
            )
        if call.method == "POST":
            assert call.url.path == f"/v1/studio/review-runs/{run_id}/start"
            if fault in {"lost_ack", "unknown"}:
                raise httpx.ReadTimeout("acknowledgment lost")
            return httpx.Response(
                200, json={"run": _run(route, launched=True, coordinated=fault == "coordinator")}
            )
        assert call.url.path == f"/v1/studio/review-runs/{run_id}"
        return httpx.Response(200, json=_run(route, launched=fault == "lost_ack"))

    async with httpx.AsyncClient(
        base_url="http://studio", transport=httpx.MockTransport(handle)
    ) as studio:
        worker = WebhookWorker(inbox, {route.id: route}, studio)
        await worker.dispatch(job)
        if fault == "capture_retry":
            assert inbox.recent()[0]["state"] == "queued"
            with sqlite3.connect(inbox.path) as connection:
                connection.execute("UPDATE deliveries SET available_at = 0")
            retry = WebhookInbox(inbox.path).claim()
            assert retry is not None and retry.attempts == 2
            await worker.dispatch(retry)
    assert observed == methods
    assert inbox.recent()[0]["state"] == expected
    assert not inbox.enqueue(key, route.id, route.policy_digest, request.model_dump_json())
    assert inbox.claim() is None
