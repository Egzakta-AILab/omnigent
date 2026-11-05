"""Inspect a saved review through its owning instance without starting work."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import ValidationError

from omnigent.studio import setup
from omnigent.studio.reviews.models import ReviewPass, require_uuid
from omnigent.studio.reviews.report import ReviewReport
from omnigent.studio.reviews.service import CaptureReviewRequest


def collect_review(
    *,
    mode: str,
    server_url: str,
    review_id: str,
    harness: str,
    pr_url: str,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Return a receipt for the captured target, linked session and saved report."""
    if mode not in ("local", "server") or harness not in setup.HARNESSES:
        raise ValueError("Choose local or server mode and a native review harness")
    server_url = setup.validate_server_url(server_url, mode)
    try:
        review_id = require_uuid(review_id)
    except ValueError:
        raise ValueError("Provide a valid review ID") from None
    expected_pr = CaptureReviewRequest.safe_pr_url(pr_url)
    checks: list[dict[str, Any]] = []
    receipt: dict[str, Any] = {
        "schema": "omnigent.studio.saved-review.v1",
        "observed_at": datetime.now(UTC).isoformat(),
        "scope": "saved_report_and_session_binding",
        "mode": mode,
        "server_url": server_url,
        "review_id": review_id,
        "harness": harness,
        "source": setup._checkout_identity(),
        "review_ready": False,
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
        path = f"v1/studio/reviews/{review_id}"
        raw = probe.get("review.capture", path)
        if raw is None:
            return receipt
        try:
            review = ReviewPass.model_validate(raw, strict=True)
        except ValidationError:
            probe.check("review.capture", "unknown", "invalid_review_record")
            return receipt
        if (
            review.id != review_id
            or review.profile.harness != harness
            or review.target.pr_url != expected_pr
            or not review.reviewer_session_id
            or review.reviewer_session_id == review.target.source_session_id
        ):
            probe.check("review.capture", "fail", "review_target_or_profile_mismatch")
            return receipt
        probe.check("review.capture", "pass", "matching_pinned_review")
        receipt.update(base_sha=review.target.base_sha, head_sha=review.target.head_sha)
        try:
            reviewer_id = require_uuid(review.reviewer_session_id)
        except ValueError:
            probe.check("review.session", "unknown", "invalid_reviewer_session_id")
            return receipt
        session = probe.get(
            "review.session",
            f"v1/sessions/{reviewer_id}",
            params=dict.fromkeys(
                ("include_items", "include_usage", "include_liveness", "refresh_state"), "false"
            ),
        )
        if session is None:
            return receipt
        expected = {
            "id": reviewer_id,
            "root_conversation_id": reviewer_id,
            "agent_id": review.profile.agent_id,
            "harness": harness,
            "host_id": review.target.host_id,
            "workspace": review.target.workspace,
        }
        if any(session.get(key) != value for key, value in expected.items()):
            probe.check("review.session", "fail", "reviewer_binding_mismatch")
            return receipt
        probe.check("review.session", "pass", "matching_reviewer_session")
        result = probe.get("review.report", f"{path}/report")
        if result is None:
            return receipt
        try:
            report = ReviewReport.model_validate(result, strict=True)
        except ValidationError:
            probe.check("review.report", "unknown", "invalid_report_response")
            return receipt
        ready = report.state == "ready" and bool(report.message_id) and bool(report.summary)
        probe.check("review.report", "pass" if ready else "fail", report.state)
        receipt.update(review_ready=ready, finding_count=len(report.findings) if ready else None)
    return receipt
