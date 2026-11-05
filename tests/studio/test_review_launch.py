"""PR revision evidence and the once-only boundary around native start."""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import pytest

from omnigent.stores.studio_review_store import ReviewConflictError, StudioReviewStore
from omnigent.studio.reviews.launch import (
    MAX_REVIEW_PATCH_BYTES,
    review_prompt,
    run_review,
    verified_patch,
)
from omnigent.studio.reviews.models import ReviewPass, ReviewProfile, ReviewRole, ReviewTarget


@pytest.fixture
def target() -> ReviewTarget:
    return ReviewTarget(
        source_session_id=uuid.uuid4().hex,
        host_id=uuid.uuid4().hex,
        workspace="/repo",
        pr_url="https://github.com/example/project/pull/42",
        repository="example/project",
        pr_number=42,
        base_sha="a" * 40,
        head_sha="b" * 40,
    )


@pytest.mark.parametrize(
    "override",
    [
        {"head_sha": "c" * 40},
        {"base_sha": None},
        {"unavailable_reason": "timeout"},
        {"patch": "diff --git a/file b/file\n" + "é" * MAX_REVIEW_PATCH_BYTES},
    ],
)
def test_review_requires_a_bounded_revision_verified_patch(target, override):
    payload = {
        "object": "session.github.pr_diff",
        "base_sha": target.base_sha,
        "head_sha": target.head_sha,
        "patch": "diff --git a/file b/file\n@@ -1 +1 @@\n-old\n+new\n",
    }
    assert verified_patch(payload, target) == payload["patch"]
    with pytest.raises(ValueError, match="diff"):
        verified_patch({**payload, **override}, target)


@pytest.fixture
def captured(db_uri: str, target: ReviewTarget) -> tuple[StudioReviewStore, ReviewPass]:
    store = StudioReviewStore(db_uri)
    result = store.capture(
        owner_user_id="owner@example.test",
        idempotency_key="capture",
        request_hash=hashlib.sha256(b"capture").hexdigest(),
        target=target,
        profile=ReviewProfile(
            agent_id=uuid.uuid4().hex, agent_version="1", harness="codex-native", skill="review"
        ),
    )
    return store, result.review


def test_workspace_role_guides_private_review_of_captured_diff(captured):
    _, review = captured
    role = ReviewRole(id="orchestrator", label="Orchestrator", digest="a" * 64)
    review = review.model_copy(
        update={"profile": review.profile.model_copy(update={"skill": None, "role": role})}
    )
    body = "Route backend changes to the backend lens."
    prompt = review_prompt(
        review,
        "diff --git a/main.py b/main.py",
        "Return structured findings.",
        role_instructions=body,
    )
    assert "Apply its routing rules yourself" in prompt
    assert "Do not spawn other reviewers" in prompt
    assert prompt.index(body) < prompt.index("Return structured findings.")
    assert '"untrusted_patch": "diff --git a/main.py b/main.py"' in prompt
    assert review.target.head_sha in prompt
    assert "$review" not in prompt
    with pytest.raises(ValueError, match="captured review guidance"):
        review_prompt(review, "diff --git a/main.py b/main.py", "Return structured findings.")


@pytest.mark.parametrize("lose_ack", [False, True])
async def test_parallel_start_and_replay_never_repeat_native_dispatch(captured, lose_ack):
    store, review = captured
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def prepare():
        return "pinned review prompt"

    async def dispatch(session_id, prompt):
        calls.append((session_id, prompt))
        entered.set()
        await release.wait()
        if lose_ack:
            raise TimeoutError("result lost")

    attempt = asyncio.create_task(
        run_review(store, review, "start", prepare=prepare, dispatch=dispatch)
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        replay = await run_review(store, review, "start", prepare=prepare, dispatch=dispatch)
        assert replay.dispatch_status == "dispatching"
        assert replay.reviewer_session_id == review.id
        with pytest.raises(ReviewConflictError):
            await run_review(store, review, "other-key", prepare=prepare, dispatch=dispatch)
    finally:
        release.set()
        finished = await attempt

    reopened = StudioReviewStore(store.storage_location)
    replay = await run_review(reopened, review, "start", prepare=prepare, dispatch=dispatch)
    assert replay == finished
    assert replay.dispatch_status == ("unknown" if lose_ack else "running")
    assert calls == [(review.id, "pinned review prompt")]
