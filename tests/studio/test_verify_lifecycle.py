"""A saved lifecycle requires an intact receipt and candidate lineage."""

import json
from hashlib import sha256

import httpx
import pytest

from omnigent.studio import setup, verify_lifecycle
from omnigent.studio.reviews.reproduction import FixCheckEvidence, ReproductionReceipt


@pytest.mark.parametrize("tamper", [False, True])
def test_lifecycle_requires_the_selected_unchanged_receipt(monkeypatch, tamper):
    original, candidate, question = "1" * 32, "2" * 32, "3" * 32
    finding = "4" * 64
    patch = "diff --git a/file b/file\n--- a/file\n+++ b/file\n@@ -1 +1 @@\n-before\n+after\n"
    target = {
        "source_project_id": "5" * 32,
        "repository_path": ".",
        "host_id": "6" * 32,
        "workspace": "/repo",
        "pr_url": "https://github.com/example/project/pull/17",
        "repository": "example/project",
        "pr_number": 17,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }
    proof_args = {
        "observed_at": "2026-01-01T00:00:00Z",
        "image_id": "sha256:" + "d" * 64,
        "command": ["check"],
        "timeout_seconds": 20,
        "source": {
            "run_id": original,
            "pr_url": target["pr_url"],
            "patch_sha256": "7" * 64,
            "base_sha": target["base_sha"],
            "head_sha": target["head_sha"],
        },
    }
    base = {
        "commit": target["base_sha"],
        "archive_sha256": "d" * 64,
        "archive_bytes": 1,
        "state": "exited",
        "exit_code": 0,
        "elapsed_ms": 1,
        "output": "",
        "output_sha256": sha256(b"").hexdigest(),
        "output_bytes": 0,
        "output_truncated": False,
    }
    head = {**base, "commit": target["head_sha"], "exit_code": 1}
    after = {**base, "commit": "c" * 40}
    proof = ReproductionReceipt.model_validate({**proof_args, "base": base, "head": head})
    fix = FixCheckEvidence.model_validate(
        {
            **proof_args,
            "before": head,
            "after": after,
            "outcome": "fail_to_pass",
            "included_paths": ["file"],
            "patch": patch,
            "proposed_patch_sha256": sha256(patch.encode()).hexdigest(),
        }
    )
    proof_digest, fix_digest = (
        sha256(receipt.model_dump_json().encode()).hexdigest() for receipt in (proof, fix)
    )
    candidate_target = {
        **target,
        "base_sha": target["head_sha"],
        "head_sha": after["commit"],
        "candidate": {
            "run_id": original,
            "finding_key": finding,
            "attachment_digest": fix_digest,
            "finding_title": "Test finding",
            "finding_detail": "Test detail",
        },
    }

    def capture(run_id, member, bound_target, digest):
        return {
            "id": run_id,
            "target": bound_target,
            "patch_sha256": digest,
            "created_at": 1,
            "reviewers": [
                {
                    "id": member,
                    "run_id": run_id,
                    "owner_user_id": None,
                    "target": bound_target,
                    "profile": {
                        "agent_id": "8" * 32,
                        "agent_version": "1",
                        "harness": "codex-native",
                        "skill": "review",
                    },
                    "state": "running",
                    "dispatch_status": "running",
                    "reviewer_session_id": member,
                    "created_at": 1,
                    "updated_at": 1,
                    "version": 1,
                }
            ],
        }

    path = f"/v1/studio/review-runs/{original}"
    proof_data = json.loads(proof.model_dump_json())
    if tamper:
        proof_data["head"]["exit_code"] = 2
    responses = {
        path: capture(original, "9" * 32, target, "7" * 64),
        f"/v1/studio/review-runs/{candidate}": capture(
            candidate, "a" * 32, candidate_target, sha256(patch.encode()).hexdigest()
        ),
        f"{path}/discussion/questions/{question}": {
            "id": question,
            "anchor": {"kind": "finding", "finding_key": finding},
            "body": "Explain this.",
            "session_id": "b" * 32,
            "harness": "codex-native",
            "status": "accepted",
            "answer_state": "ready",
            "answer": "The check failed.",
            "citations": [{"path": "file", "side": "head", "line": 1, "quote": "after"}],
            "proof_digest": proof_digest,
            "updated_at": 1,
        },
        f"{path}/findings/{finding}/reproductions": [
            {"digest": proof_digest, "attached_at": 1, "receipt": proof_data},
            {
                "digest": fix_digest,
                "attached_at": 1,
                "candidate_run_id": candidate,
                "receipt": json.loads(fix.model_dump_json()),
            },
        ],
    }

    def route(request):
        assert request.method == "GET"
        return httpx.Response(200, json=responses[request.url.path])

    verified = []

    def review_run(**options):
        verified.append(options["run_id"])
        return {"run_ready": True, "checks": []}

    monkeypatch.setattr(setup, "_headers", lambda *_: {})
    monkeypatch.setattr(verify_lifecycle, "collect_review_run", review_run)
    receipt = verify_lifecycle.collect_lifecycle(
        mode="local",
        server_url="http://127.0.0.1:1234",
        run_id=original,
        pr_url=target["pr_url"],
        finding_key=finding,
        question_id=question,
        candidate_id=candidate,
        transport=httpx.MockTransport(route),
    )
    assert receipt["lifecycle_ready"] is not tamper, receipt
    proof_check = next(
        entry for entry in receipt["checks"] if entry["name"] == "question_and_proof"
    )
    assert proof_check["status"] == ("fail" if tamper else "pass")
    assert verified == ([] if tamper else [original, candidate])
