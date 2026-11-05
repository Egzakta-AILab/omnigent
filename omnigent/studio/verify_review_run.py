"""Verify saved review run provenance and citations without launching work."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import ValidationError

from omnigent.runner.session_prs import PullRequestRef
from omnigent.studio import setup
from omnigent.studio.reviews.discussion import ReviewQuestion
from omnigent.studio.reviews.evidence import changed_anchors, patch_digest
from omnigent.studio.reviews.models import MAX_RUN_REVIEWERS, ReviewRun, require_uuid
from omnigent.studio.reviews.report import (
    ConsolidatedReviewReport,
    ReviewReport,
    consolidate_reports,
)
from omnigent.studio.reviews.reproduction import ReproductionError, ReproductionSource
from omnigent.studio.reviews.service import CaptureReviewRequest

if TYPE_CHECKING:
    from omnigent.studio.reviews.check_proposal import ProposedCheck


def read_check_proposal(
    *,
    mode: str,
    server_url: str,
    run_id: str,
    proposal_id: str,
    transport: httpx.BaseTransport | None = None,
) -> ProposedCheck:
    server_url = setup.validate_server_url(server_url, mode)
    run_id, proposal_id = require_uuid(run_id), require_uuid(proposal_id)
    try:
        headers = setup._headers(server_url, None)
    except Exception:  # noqa: BLE001 — credential errors may contain private values
        raise ReproductionError("The instance credentials could not be loaded") from None
    with httpx.Client(
        base_url=f"{server_url}/",
        headers=headers,
        timeout=5,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    ) as client:
        path = f"v1/studio/review-runs/{run_id}/discussion/questions/{proposal_id}"
        raw = setup._Probe(client, []).get("check.proposal", path)
    try:
        proposal = ReviewQuestion.model_validate(raw)
    except ValidationError:
        raise ReproductionError("The saved sandbox proposal could not be read") from None
    if (
        proposal.id != proposal_id
        or proposal.action != "propose_check"
        or proposal.answer_state != "ready"
        or proposal.proposed_check is None
    ):
        raise ReproductionError("The agent has no confirmed sandbox check proposal")
    return proposal.proposed_check


def _matches_pr_url(saved: str, expected: str | None) -> bool:
    if saved == expected:
        return True
    if expected is None:
        return False
    try:
        return PullRequestRef.from_url(saved) == PullRequestRef.from_url(expected)
    except ValueError:
        return False


def read_reproduction_source(
    *,
    mode: str,
    server_url: str,
    run_id: str,
    pr_url: str,
    transport: httpx.BaseTransport | None = None,
) -> ReproductionSource:
    """Read the frozen revision and diff digest from an owned review run."""
    server_url = setup.validate_server_url(server_url, mode)
    run_id = require_uuid(run_id)
    expected_pr = CaptureReviewRequest.safe_pr_url(pr_url)
    try:
        headers = setup._headers(server_url, None)
    except Exception:  # noqa: BLE001 — credential errors may contain private values
        raise ReproductionError("The instance credentials could not be loaded") from None
    with httpx.Client(
        base_url=f"{server_url}/",
        headers=headers,
        timeout=5,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    ) as client:
        path = f"v1/studio/review-runs/{run_id}"
        raw = setup._Probe(client, []).get("run.capture", path)
    try:
        run = ReviewRun.model_validate(raw, strict=True)
        if (
            run.id != run_id
            or not _matches_pr_url(run.target.pr_url, expected_pr)
            or run.patch_sha256 is None
        ):
            raise ReproductionError(
                "The saved run does not match the requested PR and captured diff"
            )
        return ReproductionSource(
            run_id=run.id,
            pr_url=run.target.pr_url,
            patch_sha256=run.patch_sha256,
            base_sha=run.target.base_sha,
            head_sha=run.target.head_sha,
        )
    except ValidationError:
        raise ReproductionError("A captured review run could not be read") from None


def collect_review_run(
    *,
    mode: str,
    server_url: str,
    run_id: str,
    pr_url: str,
    base_sha: str,
    head_sha: str,
    minimum_reviewers: int = 1,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Check the saved roster, reviewer reports, and independently parsed anchors."""
    if mode not in ("local", "server") or not 1 <= minimum_reviewers <= MAX_RUN_REVIEWERS:
        raise ValueError("Choose a supported mode and reviewer count")
    server_url = setup.validate_server_url(server_url, mode)
    run_id = require_uuid(run_id)
    expected_pr = CaptureReviewRequest.safe_pr_url(pr_url)
    if any(
        len(sha) not in (40, 64) or any(c not in "0123456789abcdef" for c in sha)
        for sha in (base_sha, head_sha)
    ):
        raise ValueError("Provide the full lowercase base and head commit SHAs")
    checks: list[dict[str, Any]] = []
    receipt: dict[str, Any] = {
        "schema": "omnigent.studio.review-run-citations.v1",
        "observed_at": datetime.now(UTC).isoformat(),
        "scope": "saved_roster_reports_and_diff_citations",
        "mode": mode,
        "server_url": server_url,
        "run_id": run_id,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "run_ready": False,
        "checks": checks,
    }
    try:
        headers = setup._headers(server_url, None)
    except Exception:  # noqa: BLE001 — credential errors may contain private values
        checks.append(
            {"name": "credentials", "status": "unknown", "reason": "credential_resolution_failed"}
        )
        return receipt

    with httpx.Client(
        base_url=f"{server_url}/",
        headers=headers,
        timeout=5,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    ) as client:
        probe = setup._Probe(client, checks)
        path = f"v1/studio/review-runs/{run_id}"
        raw = probe.get("run.capture", path)
        if raw is None:
            return receipt
        try:
            run = ReviewRun.model_validate(raw, strict=True)
        except ValidationError:
            probe.check("run.capture", "fail", "invalid_run_record")
            return receipt
        if (
            run.id != run_id
            or (run.target.pr_url, run.target.base_sha, run.target.head_sha)
            != (expected_pr, base_sha, head_sha)
            or run.target.source_project_id is None
        ):
            probe.check("run.capture", "fail", "review_target_mismatch")
            return receipt
        probe.check("run.capture", "pass", "matching_pr_revision")
        reviewers = run.selected_reviewers()
        reviewer_ids = {member.id for member in reviewers}
        if run.coordinator is not None:
            coordinator = run.coordinator
            try:
                if (
                    run.plan is None
                    or coordinator.run_id != run.id
                    or coordinator.run_role != "coordinator"
                    or coordinator.target != run.target
                    or coordinator.reviewer_session_id != coordinator.id
                    or coordinator.id in {member.id for member in run.reviewers}
                ):
                    raise ValueError("Missing approved plan or coordinator binding")
                run.validate_plan(run.plan)
            except ValueError:
                probe.check("run.plan", "fail", "unverified_plan_or_coordinator")
                return receipt
            if any(
                member.reviewer_session_id is not None
                for member in run.reviewers
                if member.id not in reviewer_ids
            ):
                probe.check("run.plan", "fail", "skipped_reviewer_started")
                return receipt
            probe.check("run.plan", "pass", "approved_roster_includes_required_reviewers")
            receipt["skipped_reviewer_count"] = len(run.reviewers) - len(reviewers)
        roster_valid = minimum_reviewers <= len(reviewer_ids) == len(
            reviewers
        ) <= MAX_RUN_REVIEWERS and all(
            member.run_id == run.id
            and member.target == run.target
            and member.reviewer_session_id == member.id
            for member in reviewers
        )
        probe.check("run.roster", "pass" if roster_valid else "fail", "unique_roster_members")
        receipt["reviewer_count"] = len(reviewer_ids)

        evidence = probe.get("run.diff", f"{path}/patch")
        if evidence is None:
            return receipt
        patch = evidence.get("patch")
        if (
            not isinstance(patch, str)
            or not run.patch_sha256
            or evidence.get("sha256") != run.patch_sha256
            or (evidence.get("base_sha"), evidence.get("head_sha")) != (base_sha, head_sha)
            or patch_digest(patch) != run.patch_sha256
        ):
            probe.check("run.diff", "fail", "captured_diff_binding_mismatch")
            return receipt
        try:
            anchors = changed_anchors(patch)
        except ValueError:
            probe.check("run.diff", "fail", "malformed_captured_diff")
            return receipt
        probe.check("run.diff", "pass", "verified_patch_digest")
        receipt["patch_sha256"] = run.patch_sha256

        raw = probe.get("run.report", f"{path}/report", params={"include_finding_keys": "true"})
        if raw is None:
            return receipt
        try:
            consolidated = ConsolidatedReviewReport.model_validate(raw, strict=True)
        except ValidationError:
            probe.check("run.report", "fail", "invalid_report_response")
            return receipt
        reports = consolidated.reviewers
        ready = (
            len(reports) == len(reviewer_ids)
            and {entry.review_id for entry in reports} == reviewer_ids
            and all(
                entry.report.state == "ready"
                and entry.report.summary
                and entry.report.message_id
                and entry.report.citation_validation == "checked"
                for entry in reports
            )
        )
        probe.check("run.report", "pass" if ready else "fail", "attributable_complete_reports")
        rejected = sum(len(entry.report.unverified_findings) for entry in reports)
        citations_valid = rejected == 0 and all(
            (finding.path, finding.side, finding.line) in anchors
            for entry in reports
            for finding in entry.report.findings
        )
        probe.check(
            "run.citations", "pass" if citations_valid else "fail", "captured_changed_lines"
        )
        originals = {entry.review_id: entry.report for entry in reports}
        bindings_valid = roster_valid and ready
        if bindings_valid:
            for member in reviewers:
                try:
                    member_id = require_uuid(member.id)
                except ValueError:
                    bindings_valid = False
                    break
                if member_id != member.id:
                    bindings_valid = False
                    break
                session = probe.get(
                    "run.reviewer_session",
                    f"v1/sessions/{member_id}",
                    params=dict.fromkeys(
                        ("include_items", "include_usage", "include_liveness", "refresh_state"),
                        "false",
                    ),
                )
                expected = {
                    "id": member_id,
                    "root_conversation_id": member_id,
                    "agent_id": member.profile.agent_id,
                    "harness": member.profile.harness,
                    "host_id": run.target.host_id,
                    "workspace": run.target.workspace,
                }
                if session is None or any(
                    session.get(key) != value for key, value in expected.items()
                ):
                    bindings_valid = False
                    break
                original = probe.get(
                    "run.reviewer_report", f"v1/studio/reviews/{member_id}/report"
                )
                try:
                    if ReviewReport.model_validate(original, strict=True) != originals[member_id]:
                        bindings_valid = False
                        break
                except ValidationError:
                    bindings_valid = False
                    break
        probe.check(
            "run.originals",
            "pass" if bindings_valid else "fail",
            "matching_reviewer_sessions_and_reports",
        )
        expected_findings = consolidate_reports(reports, include_finding_keys=True).findings
        lineage = len(expected_findings) == len(consolidated.findings) and all(
            actual.finding == expected.finding
            and actual.review_ids == expected.review_ids
            and actual.finding_key in (None, expected.finding_key)
            for actual, expected in zip(consolidated.findings, expected_findings, strict=True)
        )
        probe.check("run.consolidation", "pass" if lineage else "fail", "exact_finding_lineage")
        receipt.update(
            run_ready=bool(
                roster_valid and ready and citations_valid and lineage and bindings_valid
            ),
            finding_count=len(consolidated.findings),
            rejected_citations=rejected,
        )
    return receipt
