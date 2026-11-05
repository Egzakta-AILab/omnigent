"""Saved-review acceptance requires the requested PR, session and ready report."""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.studio import setup
from omnigent.studio.verify_review import collect_review

REVIEW_ID = "1" * 32
REVIEWER_ID = "2" * 32
SOURCE_ID = "3" * 32
AGENT_ID = "4" * 32
HOST_ID = "5" * 32
PR_URL = "https://github.com/example/project/pull/17"


@pytest.mark.parametrize("outcome", ["ready", "different_pr", "wrong_host", "incomplete"])
def test_receipt_requires_the_full_saved_review_binding(monkeypatch, outcome):
    monkeypatch.setattr(setup, "_headers", lambda *_: {"Authorization": "Bearer test-secret"})
    root = f"/v1/studio/reviews/{REVIEW_ID}"
    seen = []
    responses = {
        root: {
            "id": REVIEW_ID,
            "owner_user_id": None,
            "target": {
                "pr_url": PR_URL if outcome != "different_pr" else f"{PR_URL}0",
                "repository": "example/project",
                "pr_number": 17,
                "source_session_id": SOURCE_ID,
                "host_id": HOST_ID,
                "workspace": "/repo",
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
            },
            "profile": {
                "agent_id": AGENT_ID,
                "agent_version": "1",
                "harness": "codex-native",
                "skill": "review",
            },
            "state": "running",
            "dispatch_status": "running",
            "reviewer_session_id": REVIEWER_ID,
            "created_at": 1,
            "updated_at": 2,
            "version": 4,
        },
        f"/v1/sessions/{REVIEWER_ID}": {
            "id": REVIEWER_ID,
            "root_conversation_id": REVIEWER_ID,
            "agent_id": AGENT_ID,
            "host_id": HOST_ID if outcome != "wrong_host" else "6" * 32,
            "harness": "codex-native",
            "workspace": "/repo",
        },
        f"{root}/report": {
            "state": "incomplete" if outcome == "incomplete" else "ready",
            "summary": "Private report text",
            "message_id": "message-id",
            "findings": [],
        },
    }

    def read(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        seen.append(request.url.path)
        return httpx.Response(200, json=responses[request.url.path])

    receipt = collect_review(
        mode="local",
        server_url="http://127.0.0.1:6767",
        review_id=REVIEW_ID,
        harness="codex-native",
        pr_url=PR_URL,
        transport=httpx.MockTransport(read),
    )
    assert receipt["review_ready"] is (outcome == "ready")
    assert seen == list(responses)[: {"different_pr": 1, "wrong_host": 2}.get(outcome, 3)]
    assert "Private report text" not in json.dumps(receipt)
    assert "test-secret" not in json.dumps(receipt)
