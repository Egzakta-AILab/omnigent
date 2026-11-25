"""Verify an existing finding, proof, discussion and candidate without starting work."""

from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

import httpx
from pydantic import TypeAdapter, ValidationError

from omnigent.studio import setup
from omnigent.studio.reviews.discussion import FindingDiscussionAnchor, ReviewQuestion
from omnigent.studio.reviews.evidence import patch_digest
from omnigent.studio.reviews.models import ReviewRoleDigest, ReviewRun, require_uuid
from omnigent.studio.reviews.reproduction import (
    AttachedReproduction,
    FixCheckEvidence,
    ReproductionReceipt,
    ReproductionSource,
)
from omnigent.studio.verify_review_run import _matches_pr_url, collect_review_run

_ATTACHMENTS = TypeAdapter(list[AttachedReproduction])
_DIGEST = TypeAdapter(ReviewRoleDigest)


def collect_lifecycle(
    *,
    mode: str,
    server_url: str,
    run_id: str,
    pr_url: str,
    finding_key: str,
    question_id: str,
    candidate_id: str,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    server_url = setup.validate_server_url(server_url, mode)
    run_id, question_id, candidate_id = map(require_uuid, (run_id, question_id, candidate_id))
    finding_key = _DIGEST.validate_python(finding_key)
    checks: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "schema": "omnigent.studio.saved-lifecycle.v1",
        "observed_at": datetime.now(UTC).isoformat(),
        "scope": "saved_review_proof_question_and_candidate",
        "mode": mode,
        "server_url": server_url,
        "run_id": run_id,
        "candidate_id": candidate_id,
        "finding_key": finding_key,
        "question_id": question_id,
        "lifecycle_ready": False,
        "checks": checks,
    }
    try:
        headers = setup._headers(server_url, None)
    except Exception:  # noqa: BLE001 — credential errors may contain private values
        checks.append(
            {"name": "credentials", "status": "unknown", "reason": "credential_resolution_failed"}
        )
        return result

    with httpx.Client(
        base_url=f"{server_url}/",
        headers=headers,
        timeout=10,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    ) as client:
        probe = setup._Probe(client, checks)
        path = f"v1/studio/review-runs/{run_id}"
        try:
            run = ReviewRun.model_validate(probe.get("original.capture", path), strict=True)
            candidate = ReviewRun.model_validate(
                probe.get("candidate.capture", f"v1/studio/review-runs/{candidate_id}"),
                strict=True,
            )
        except ValidationError:
            probe.check("captures", "fail", "invalid_or_unavailable_capture")
            return result
        origin = candidate.target.candidate
        if (
            run.id != run_id
            or candidate.id != candidate_id
            or not _matches_pr_url(run.target.pr_url, pr_url)
            or run.target.candidate is not None
            or run.patch_sha256 is None
            or origin is None
            or (origin.run_id, origin.finding_key) != (run.id, finding_key)
            or candidate.target.base_sha != run.target.head_sha
            or candidate.target
            != run.target.model_copy(
                update={
                    "base_sha": candidate.target.base_sha,
                    "head_sha": candidate.target.head_sha,
                    "candidate": origin,
                }
            )
            or {member.profile.model_dump_json() for member in run.selected_reviewers()}
            != {member.profile.model_dump_json() for member in candidate.selected_reviewers()}
        ):
            probe.check("captures", "fail", "candidate_does_not_match_original_finding_and_roster")
            return result
        probe.check("captures", "pass", "candidate_matches_original_finding_and_roster")
        try:
            question = ReviewQuestion.model_validate(
                probe.get("question", f"{path}/discussion/questions/{question_id}"), strict=True
            )
            response = client.get(f"{path}/findings/{finding_key}/reproductions")
            response.raise_for_status()
            attachments = {
                entry.digest: entry for entry in _ATTACHMENTS.validate_json(response.content)
            }
        except (ValidationError, httpx.HTTPError):
            probe.check("question_and_receipts", "fail", "saved_records_unavailable_or_invalid")
            return result

        source = ReproductionSource(
            run_id=run.id,
            pr_url=run.target.pr_url,
            patch_sha256=run.patch_sha256,
            base_sha=run.target.base_sha,
            head_sha=run.target.head_sha,
        )
        selected = attachments.get(question.proof_digest or "")
        fix = attachments.get(origin.attachment_digest)
        proof = selected.receipt if selected else None
        checked = fix.receipt if fix else None
        question_ok = (
            question.id == question_id
            and question.anchor == FindingDiscussionAnchor(kind="finding", finding_key=finding_key)
            and question.action == "question"
            and question.status == "accepted"
            and question.answer_state == "ready"
            and bool(question.answer)
            and bool(question.citations)
            and isinstance(proof, ReproductionReceipt)
            and proof.source == source
            and (proof.base.commit, proof.head.commit) == (source.base_sha, source.head_sha)
            and proof.head.state == "exited"
            and not proof.head.oom_killed
            and proof.head.exit_code not in (None, 0)
            and sha256(proof.model_dump_json().encode()).hexdigest() == question.proof_digest
        )
        probe.check(
            "question_and_proof", "pass" if question_ok else "fail", "bound_failed_head_check"
        )
        fix_ok = (
            isinstance(checked, FixCheckEvidence)
            and fix is not None
            and fix.candidate_run_id == candidate.id
            and checked.source == source
            and checked.outcome == "fail_to_pass"
            and checked.after.commit == candidate.target.head_sha
            and patch_digest(checked.patch) == candidate.patch_sha256
            and sha256(checked.model_dump_json().encode()).hexdigest() == origin.attachment_digest
        )
        probe.check(
            "candidate_check", "pass" if fix_ok else "fail", "bound_fail_to_pass_candidate"
        )
        if not (question_ok and fix_ok):
            return result

    for name, saved in (("original", run), ("candidate", candidate)):
        verified = collect_review_run(
            mode=mode,
            server_url=server_url,
            run_id=saved.id,
            pr_url=saved.target.pr_url,
            base_sha=saved.target.base_sha,
            head_sha=saved.target.head_sha,
            minimum_reviewers=len(saved.selected_reviewers()),
            transport=transport,
        )
        checks.extend({**entry, "name": f"{name}.{entry['name']}"} for entry in verified["checks"])
        if not verified["run_ready"]:
            return result
    result["lifecycle_ready"] = True
    return result
