"""Freeze verified PR context through the session's existing resource transport."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError

from omnigent.entities import Conversation
from omnigent.stores.session_pull_request_store import SessionPullRequests, StoredPullRequest
from omnigent.studio.reviews.models import ReviewTarget


def _associated_pr(stored: SessionPullRequests | None, pr_url: str | None) -> StoredPullRequest:
    if stored is None or not stored.synced:
        raise HTTPException(503, "Pull request associations are not ready for review capture")
    entry = stored.get(pr_url) if pr_url else next(iter(stored.entries), None)
    if entry is None:
        raise HTTPException(
            409, "Associate the pull request with this session before reviewing it"
        )
    if entry.provider != "github":
        raise HTTPException(400, "Studio review capture currently supports GitHub pull requests")
    return entry


async def capture_review_target(
    *,
    pr_url: str | None,
    load_session: Callable[[], Awaitable[Conversation]],
    load_associations: Callable[[], Awaitable[SessionPullRequests | None]],
    read_info: Callable[[Conversation, dict[str, Any]], Awaitable[dict[str, Any]]],
) -> ReviewTarget:
    """Read pinned revisions and reject a changed binding or uncertain association."""
    source = await load_session()
    if not source.host_id or not source.workspace:
        raise HTTPException(409, "Select a host and workspace before capturing a review")
    try:
        stored = await load_associations()
    except Exception as exc:
        raise HTTPException(503, "Pull request associations are unavailable") from exc
    entry = _associated_pr(stored, pr_url)
    try:
        info = await read_info(source, {"pr_url": entry.url, "server_tracked": True})
    except HTTPException as exc:
        if exc.status_code < 500:
            raise
        raise HTTPException(
            503, "The selected execution target cannot read this pull request"
        ) from exc
    if info.get("available") is not True or info.get("authenticated") is not True:
        raise HTTPException(503, "The selected execution target cannot read this pull request")
    pr = info.get("pr")
    repo = info.get("repo")
    if (
        not isinstance(pr, dict)
        or pr.get("url") != entry.url
        or type(pr.get("number")) is not int
        or pr["number"] != entry.number
        or not isinstance(repo, dict)
        or not isinstance(repo.get("name_with_owner"), str)
        or repo["name_with_owner"].casefold() != entry.repository.casefold()
    ):
        raise HTTPException(503, "The execution target returned incomplete pull request identity")
    base_sha, head_sha = pr.get("base_sha"), pr.get("head_sha")
    if not isinstance(base_sha, str) or not isinstance(head_sha, str):
        raise HTTPException(503, "The execution target returned incomplete review revisions")
    try:
        target = ReviewTarget(
            source_session_id=source.id,
            host_id=source.host_id,
            workspace=source.workspace,
            pr_url=entry.url,
            repository=entry.repository,
            pr_number=entry.number,
            base_sha=base_sha,
            head_sha=head_sha,
        )
    except ValidationError:
        raise HTTPException(
            503, "The execution target returned incomplete review revisions"
        ) from None
    current = await load_session()
    if (
        current.id != target.source_session_id
        or current.host_id != target.host_id
        or current.workspace != target.workspace
    ):
        raise HTTPException(409, "The session execution target changed during review capture")
    try:
        confirmed = _associated_pr(await load_associations(), entry.url)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, "Pull request associations are unavailable") from exc
    if (
        confirmed.host != entry.host
        or confirmed.repository != entry.repository
        or confirmed.number != entry.number
    ):
        raise HTTPException(409, "The pull request association changed during review capture")
    return target
