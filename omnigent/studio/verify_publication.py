"""Read back a confirmed Studio inline comment from GitHub."""

import json
import re
import subprocess
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from omnigent.runner.session_prs import PullRequestRef
from omnigent.studio import setup
from omnigent.studio.reviews.discussion import InlineDraft, InlinePublicationPreview
from omnigent.studio.reviews.models import ReviewRun, require_uuid
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


def _comment_id(pr: PullRequestRef, value: str) -> int | None:
    link = urlsplit(value)
    if link.query or not _matches_pr_url(link._replace(fragment="").geturl(), pr.url):
        return None
    match = re.fullmatch(r"discussion_r([1-9][0-9]{0,18})", link.fragment)
    return int(match[1]) if match else None


def _matches_comment(
    data: dict[str, Any],
    pr: PullRequestRef,
    draft: InlineDraft,
    comment_id: int,
    *,
    head_sha: str,
    author: str,
) -> bool:
    anchor = draft.anchor
    side = "LEFT" if anchor.side == "base" else "RIGHT"
    is_range = anchor.start != anchor.end
    user = data.get("user")
    return (
        data.get("id") == comment_id
        and data.get("html_url") == draft.provider_url
        and str(data.get("pull_request_url", "")).casefold()
        == f"https://api.github.com/repos/{pr.repository}/pulls/{pr.number}".casefold()
        and data.get("in_reply_to_id") is None
        and data.get("subject_type") == "line"
        and data.get("path") == anchor.path
        and data.get("side") == side
        and data.get("original_commit_id") == head_sha
        and data.get("original_line") == anchor.end
        and data.get("original_start_line") == (anchor.start if is_range else None)
        and (not is_range or data.get("start_side") == side)
        and data.get("body") == draft.body
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
        try:
            comment_id = _comment_id(pr, draft.provider_url or "")
        except ValueError:
            comment_id = None
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
