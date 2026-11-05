"""Plan validation is independent of the model and the review runner."""

from __future__ import annotations

import pytest

from omnigent.studio.reviews.models import (
    ReviewerDecision,
    ReviewPass,
    ReviewPlan,
    ReviewProfile,
    ReviewRun,
    ReviewTarget,
)


@pytest.mark.parametrize(
    ("fault", "error"),
    [
        ("wrong_coordinator", "coordinator and captured revision"),
        ("wrong_revision", "coordinator and captured revision"),
        ("duplicate_candidate", "every captured reviewer exactly once"),
        ("missing_candidate", "every captured reviewer exactly once"),
        ("foreign_candidate", "every captured reviewer exactly once"),
        ("required_skipped", "every required reviewer"),
        ("all_skipped", "at least one reviewer"),
        ("none", None),
    ],
)
def test_validate_plan(fault: str, error: str | None) -> None:
    target = ReviewTarget(
        source_project_id="1" * 32,
        repository_path=".",
        host_id="2" * 32,
        workspace="/repo",
        pr_url="https://github.com/example/repo/pull/1",
        repository="example/repo",
        pr_number=1,
        base_sha="a" * 40,
        head_sha="b" * 40,
    )
    coordinator = ReviewPass(
        id="3" * 32,
        run_role="coordinator",
        owner_user_id=None,
        target=target,
        profile=ReviewProfile(
            agent_id="4" * 32, agent_version="1", harness="codex-native", skill="review"
        ),
        state="prepared",
        dispatch_status="not_requested",
        reviewer_session_id=None,
        created_at=0,
        updated_at=0,
        version=1,
    )
    first = coordinator.model_copy(
        update={"id": "5" * 32, "run_role": "candidate" if fault == "all_skipped" else "required"}
    )
    second = coordinator.model_copy(update={"id": "6" * 32, "run_role": "candidate"})
    run = ReviewRun(
        id="7" * 32,
        target=target,
        coordinator=coordinator,
        reviewers=[first, second],
        created_at=0,
    )
    first_decision = ReviewerDecision(
        review_id=first.id,
        selected=fault not in {"required_skipped", "all_skipped"},
        reason="Scope",
    )
    second_decision = ReviewerDecision(
        review_id={"duplicate_candidate": first.id, "foreign_candidate": "8" * 32}.get(
            fault, second.id
        ),
        selected=fault == "required_skipped",
        reason="Scope",
    )
    plan = ReviewPlan(
        coordinator_id="8" * 32 if fault == "wrong_coordinator" else coordinator.id,
        base_sha="d" * 40 if fault == "wrong_revision" else target.base_sha,
        head_sha=target.head_sha,
        decisions=[first_decision]
        if fault == "missing_candidate"
        else [first_decision, second_decision],
    )
    if error:
        with pytest.raises(ValueError, match=error):
            run.validate_plan(plan)
    else:
        run.validate_plan(plan)
        assert run.selected_reviewers() == []
        assert run.model_copy(update={"plan": plan}).selected_reviewers() == [first]
