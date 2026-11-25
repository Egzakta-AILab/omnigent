"""Read back a confirmed Studio inline comment from GitHub."""

import json
import subprocess
from datetime import UTC, datetime
from typing import Any

import httpx

from omnigent.runner.session_prs import PullRequestRef
from omnigent.studio import setup
from omnigent.studio.reviews.discussion import InlineDraft, InlinePublicationPreview
from omnigent.studio.reviews.models import ReviewRun, require_uuid
from omnigent.studio.reviews.publication import (
    delivery_body,
    github_comment_id,
    matches_github_anchor,
)
from omnigent.studio.verify_review_run import _matches_pr_url


def _github_comment(pr: PullRequestRef, comment_id: int) -> dict[str, Any]:
    response = subprocess.run(
        [
            "gh",
            "api",
            "--hostname",
            pr.host,
            "--method",
            "GET",
            f"repos/{pr.repository}/pulls/comments/{comment_id}",
        ],
        capture_output=True,
        timeout=15,
        check=True,
    )
    data = json.loads(response.stdout)
    if not isinstance(data, dict):
        raise ValueError("Expected a GitHub review comment")
    return data


def _matches_comment(
    data: dict[str, Any],
    pr: PullRequestRef,
    draft: InlineDraft,
    comment_id: int,
    *,
    head_sha: str,
    author: str,
) -> bool:
    user = data.get("user")
    bodies = [draft.body]
    if draft.publication_reference:
        bodies.append(delivery_body(draft.body, draft.publication_reference))
    return (
        matches_github_anchor(
            data, pr, draft.anchor, draft.provider_url or "", comment_id, head_sha
        )
        and data.get("body") in bodies
        and isinstance(user, dict)
        and isinstance(user.get("login"), str)
        and user["login"].casefold() == author.casefold()
    )


def collect_publication(
    *,
    mode: str,
    server_url: str,
    preview: InlinePublicationPreview,
    draft_id: str,
    author: str,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Verify a saved publication against its retained preview and a provider GET."""
    server_url = setup.validate_server_url(server_url, mode)
    run_id, draft_id = require_uuid(preview.run_id), require_uuid(draft_id)
    pr = PullRequestRef.from_url(preview.pr_url)
    if (preview.provider, pr.provider, pr.host) != ("github", "github", "github.com"):
        raise ValueError("This read-back verifier supports github.com review comments")
    expected = [item for item in preview.drafts if item.id == draft_id]
    if len(expected) != 1 or expected[0].publication_state != "draft" or not author.strip():
        raise ValueError(
            "Choose one draft from the retained preview and an expected GitHub author"
        )
    checks: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "schema": "omnigent.studio.github-publication.v1",
        "observed_at": datetime.now(UTC).isoformat(),
        "scope": "studio_receipt_and_github_comment_readback",
        "run_id": run_id,
        "draft_id": draft_id,
        "pr_url": pr.url,
        "head_sha": preview.head_sha,
        "preview_digest": preview.digest,
        "publication_ready": False,
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
            run = ReviewRun.model_validate(probe.get("run", path), strict=True)
            discussion = probe.get("discussion", f"{path}/discussion")
            drafts = (discussion or {}).get("publication_drafts")
            if drafts is None:
                drafts = (discussion or {}).get("inline_drafts")
            if not isinstance(drafts, list):
                raise ValueError
            matches = [
                InlineDraft.model_validate(item, strict=True)
                for item in drafts
                if isinstance(item, dict) and item.get("id") == draft_id
            ]
        except ValueError:
            probe.check("studio_receipt", "fail", "saved_records_unavailable_or_invalid")
            return result
        if (
            run.id != run_id
            or run.target.candidate is not None
            or not _matches_pr_url(run.target.pr_url, pr.url)
            or (run.target.base_sha, run.target.head_sha, run.patch_sha256)
            != (preview.base_sha, preview.head_sha, preview.patch_sha256)
            or len(matches) != 1
            or (matches[0].anchor, matches[0].body, matches[0].version)
            != (expected[0].anchor, expected[0].body, expected[0].version)
        ):
            probe.check("studio_receipt", "fail", "saved_draft_differs_from_preview")
            return result
        [draft] = matches
        comment_id = github_comment_id(pr, draft.provider_url or "")
        if draft.publication_state != "posted" or comment_id is None:
            probe.check(
                "studio_receipt", "fail", f"publication_{draft.publication_state}_unverified"
            )
            return result
        probe.check("studio_receipt", "pass", "posted_draft_matches_preview")
        try:
            data = _github_comment(pr, comment_id)
        except (OSError, subprocess.SubprocessError, ValueError):
            probe.check("github_comment", "unknown", "provider_readback_unavailable")
            return result
        matched = _matches_comment(
            data, pr, draft, comment_id, head_sha=preview.head_sha, author=author
        )
        probe.check(
            "github_comment", "pass" if matched else "fail", "original_diff_anchor_body_and_author"
        )
        if matched:
            result.update(
                publication_ready=True, comment_id=comment_id, provider_url=draft.provider_url
            )
        return result
