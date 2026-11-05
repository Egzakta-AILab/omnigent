"""The run verifier checks the saved diff and report lineage read-only."""

import json

import httpx
import pytest

from omnigent.studio import setup
from omnigent.studio.reviews.evidence import patch_digest
from omnigent.studio.reviews.reproduction import ReproductionError
from omnigent.studio.verify_review_run import collect_review_run, read_reproduction_source


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
        "coordinated",
        "skipped_started",
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

    if fault in {"coordinated", "skipped_started"}:
        run = responses[root]
        required = run["reviewers"][0]
        required["run_role"] = "required"
        coordinator_id, skipped_id = "8" * 32, "9" * 32
        run["coordinator"] = {
            **required,
            "id": coordinator_id,
            "reviewer_session_id": coordinator_id,
            "run_role": "coordinator",
        }
        run["reviewers"].append(
            {
                **required,
                "id": skipped_id,
                "run_role": "candidate",
                "reviewer_session_id": skipped_id if fault == "skipped_started" else None,
            }
        )
        run["plan"] = {
            "coordinator_id": coordinator_id,
            "base_sha": base,
            "head_sha": head,
            "decisions": [
                {"review_id": member, "selected": member != skipped_id, "reason": "Scope"}
                for member in [*member_ids, skipped_id]
            ],
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

    if fault == "none":
        source_args = {
            "mode": "local",
            "server_url": "http://127.0.0.1:6767",
            "run_id": run_id,
            "transport": httpx.MockTransport(read),
        }
        source = read_reproduction_source(**source_args, pr_url=target["pr_url"])
        assert (source.base_sha, source.head_sha, source.patch_sha256) == (base, head, digest)
        alternate = target["pr_url"].replace("/example/", "/Example/")
        assert read_reproduction_source(**source_args, pr_url=alternate) == source
        with pytest.raises(ReproductionError, match="does not match"):
            read_reproduction_source(**source_args, pr_url=target["pr_url"] + "0")

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
    assert receipt["run_ready"] is (fault in {"none", "coordinated"})
    if fault == "coordinated":
        assert receipt["reviewer_count"] == 2
        assert receipt["skipped_reviewer_count"] == 1
    serialized = json.dumps(receipt)
    assert "private diff text" not in serialized
    assert "Private finding" not in serialized
    assert "test-secret" not in serialized
