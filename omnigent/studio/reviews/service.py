"""Capture immutable review context without dispatching an agent."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator, model_validator

from omnigent.stores.studio_review_store import (
    ReviewConflictError,
    StudioReviewStore,
)
from omnigent.studio.reviews.models import (
    BoundedText,
    CaptureResult,
    ReviewProfile,
    ReviewRole,
    ReviewRoleDigest,
    ReviewRoleId,
    ReviewTarget,
    Sha,
)
from omnigent.util.uuid import uuid_to_bytes


class CaptureUnavailableError(RuntimeError):
    """The existing session resources could not establish review context."""


class CaptureReviewRequest(BaseModel):
    """Caller intent; execution location and actual revisions come from the server."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_base_sha: Sha
    expected_head_sha: Sha
    agent_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    harness: Literal["codex-native", "claude-native"]
    skill: BoundedText | None = None
    model: Annotated[str, StringConstraints(min_length=1, max_length=256)] | None = None
    reasoning_effort: Annotated[str, StringConstraints(min_length=1, max_length=256)] | None = None
    pr_url: Annotated[str, StringConstraints(min_length=1, max_length=2048)] | None = None
    profile_id: ReviewRoleId | None = None
    profile_digest: ReviewRoleDigest | None = None

    @model_validator(mode="after")
    def require_reviewer_guidance(self) -> CaptureReviewRequest:
        if (self.profile_id is None) != (self.profile_digest is None):
            raise ValueError("A review profile selection requires its ID and digest")
        if self.skill is None and self.profile_id is None:
            raise ValueError("Choose a review profile or a skill")
        return self

    @field_validator("agent_id")
    @classmethod
    def canonical_agent_id(cls, value: str) -> str:
        return uuid_to_bytes(value).hex()

    @field_validator("pr_url")
    @classmethod
    def safe_pr_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme == "https"
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and all(ord(char) > 32 and ord(char) != 127 for char in value)
            )
            _ = parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Use an HTTPS PR URL without credentials, queries or fragments")
        return value.rstrip("/")


def capture_fingerprint(source_session_id: str, body: CaptureReviewRequest) -> str:
    """Identify caller intent separately from mutable remote context or agent metadata."""
    payload = {
        "action": "review.capture",
        "source_session_id": source_session_id,
        # Skill-only captures must continue to replay their original keys.
        **body.model_dump(
            exclude={"profile_id", "profile_digest"} if body.profile_id is None else None
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ReviewService:
    """Shared capture action used by transport adapters after authorization."""

    def __init__(self, store: StudioReviewStore) -> None:
        self.store = store

    async def capture(
        self,
        *,
        owner_user_id: str | None,
        source_session_id: str,
        idempotency_key: str,
        body: CaptureReviewRequest,
        resolve_target: Callable[[], Awaitable[ReviewTarget]],
        resolve_profile: Callable[[], Awaitable[ReviewProfile]],
        resolve_role: Callable[[ReviewTarget], Awaitable[ReviewRole]],
    ) -> CaptureResult:
        """Replay exact intent or freeze a fresh target and validated reviewer profile."""
        request_hash = capture_fingerprint(source_session_id, body)
        existing = await asyncio.to_thread(
            self.store.find_operation,
            owner_user_id=owner_user_id,
            action="review.capture",
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            if existing.request_hash != request_hash:
                raise ReviewConflictError("Idempotency key was used for another review request")
            review = await asyncio.to_thread(
                self.store.get, existing.review_id, owner_user_id=owner_user_id
            )
            if review is None or existing.state != "succeeded":
                raise CaptureUnavailableError("Review capture requires reconciliation")
            return CaptureResult(review=review, operation=existing, created=False)

        profile = await resolve_profile()
        target = await resolve_target()
        if target.source_session_id != source_session_id:
            raise CaptureUnavailableError("Review context did not match the selected session")
        if (target.base_sha, target.head_sha) != (body.expected_base_sha, body.expected_head_sha):
            raise ReviewConflictError(
                "PR revision changed; refresh the PR before capturing a review"
            )
        if body.profile_id is not None:
            profile = profile.model_copy(update={"role": await resolve_role(target)})
        return await asyncio.to_thread(
            self.store.capture,
            owner_user_id=owner_user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            target=target,
            profile=profile,
        )
