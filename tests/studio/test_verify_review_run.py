"""The run verifier checks the saved diff and report lineage read-only."""

import json

import httpx
import pytest

from omnigent.studio import setup
from omnigent.studio.reviews.evidence import patch_digest
from omnigent.studio.verify_review_run import collect_review_run


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "patch_drift",
        "phantom_line",
        "wrong_source",
        "rejected",
        "wrong_session",
        "wrong_report",
    ],
)
def test_verify_run_requires_evidence_and_attribution(monkeypatch, fault):
    monkeypatch.setattr(setup, "_headers", lambda *_: {"Authorization": "Bearer test-secret"})
    run_id = "1" * 32
    member_ids = ["2" * 32, "3" * 32]
    base, head = "a" * 40, "b" * 40
    patch = (
        "diff --git a/file b/file\n--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+private diff text\n"
    )
    digest = patch_digest(patch)
    target = {
        "source_project_id": "4" * 32,
        "repository_path": ".",
        "host_id": "5" * 32,
        "workspace": "/repo",
        "pr_url": "https://github.com/example/project/pull/17",
        "repository": "example/project",
        "pr_number": 17,
        "base_sha": base,
        "head_sha": head,
    }
    finding = {
        "title": "Private finding",
        "detail": "The result changes.",
        "severity": "medium",
        "path": "file",
        "side": "head",
        "line": 2 if fault == "phantom_line" else 1,
    }
    root = f"/v1/studio/review-runs/{run_id}"
    responses = {
        root: {
            "id": run_id,
            "target": target,
            "created_at": 1,
            "patch_sha256": digest,
            "reviewers": [
                {
                    "id": member,
                    "run_id": run_id,
                    "owner_user_id": "owner",
                    "target": target,
                    "profile": {
                        "agent_id": "6" * 32,
                        "agent_version": "1",
                        "harness": "codex-native",
                        "skill": "review",
                    },
                    "state": "running",
                    "dispatch_status": "running",
                    "reviewer_session_id": member,
                    "created_at": 1,
                    "updated_at": 2,
                    "version": 3,
                }
                for member in member_ids
            ],
        },
        f"{root}/patch": {
            "base_sha": base,
            "head_sha": head,
            "sha256": digest,
            "patch": patch + "unexpected" if fault == "patch_drift" else patch,
        },
        f"{root}/report": {
            "reviewers": [
                {
                    "review_id": member,
                    "report": {
                        "state": "ready",
                        "summary": "Private summary",
                        "message_id": f"msg-{member}",
                        "citation_validation": "checked",
                        "findings": [finding],
                        "unverified_findings": [{**finding, "line": 999}]
                        if fault == "rejected"
                        else [],
                    },
                }
                for member in member_ids
            ],
            "findings": [
                {
                    "finding": finding,
                    "review_ids": member_ids[:1] if fault == "wrong_source" else member_ids,
                }
            ],
        },
    }

    for member, entry in zip(member_ids, responses[f"{root}/report"]["reviewers"], strict=True):
        responses[f"/v1/sessions/{member}"] = {
            "id": member,
            "root_conversation_id": member,
            "agent_id": "6" * 32,
            "harness": "codex-native",
            "host_id": "7" * 32 if fault == "wrong_session" else target["host_id"],
            "workspace": target["workspace"],
        }
        responses[f"/v1/studio/reviews/{member}/report"] = {
            **entry["report"],
            "summary": "Different report"
            if fault == "wrong_report"
            else entry["report"]["summary"],
        }

    def read(request):
        assert request.method == "GET"
        return httpx.Response(200, json=responses[request.url.path])

    receipt = collect_review_run(
        mode="local",
        server_url="http://127.0.0.1:6767",
        run_id=run_id,
        pr_url=target["pr_url"],
        base_sha=base,
        head_sha=head,
        minimum_reviewers=2,
        transport=httpx.MockTransport(read),
    )
    assert receipt["run_ready"] is (fault == "none")
    serialized = json.dumps(receipt)
    assert "private diff text" not in serialized
    assert "Private finding" not in serialized
    assert "test-secret" not in serialized
