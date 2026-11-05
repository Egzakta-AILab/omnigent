"""Match a GitHub inline comment to its original Studio delivery."""

import hashlib
import re
from typing import Any
from urllib.parse import urlsplit

from omnigent.runner.session_prs import PullRequestRef
from omnigent.studio.reviews.discussion import DiffDiscussionAnchor


def delivery_reference(draft_id: str, digest: bytes) -> str:
    return hashlib.sha256(draft_id.encode() + digest).hexdigest()


def delivery_body(body: str, reference: str) -> str:
    return f"{body}\n\n<!-- omnigent-delivery:{reference} -->"


def github_comment_id(pr: PullRequestRef, value: str) -> int | None:
    try:
        link = urlsplit(value)
        linked_pr = PullRequestRef.from_url(link._replace(fragment="").geturl())
    except ValueError:
        return None
    if link.query or linked_pr != pr:
        return None
    match = re.fullmatch(r"discussion_r([1-9][0-9]{0,18})", link.fragment)
    return int(match[1]) if match else None


def matches_github_anchor(
    data: dict[str, Any],
    pr: PullRequestRef,
    anchor: DiffDiscussionAnchor,
    url: str,
    comment_id: int,
    head_sha: str,
) -> bool:
    side = "LEFT" if anchor.side == "base" else "RIGHT"
    is_range = anchor.start != anchor.end
    return (
        data.get("id") == comment_id
        and data.get("html_url") == url
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
    )
