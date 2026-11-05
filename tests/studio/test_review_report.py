"""The review view uses final assistant text from the bound transcript."""

from __future__ import annotations

import json
from typing import TypedDict
from unittest.mock import Mock

import pytest

from omnigent.entities.conversation import (
    Conversation,
    ConversationItem,
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
)
from omnigent.entities.pagination import PagedList
from omnigent.stores.conversation_store import ConversationStore
from omnigent.studio.reviews.models import ReviewPass, ReviewProfile, ReviewRole, ReviewTarget
from omnigent.studio.reviews.report import (
    ReviewerRunReport,
    ReviewFinding,
    ReviewReport,
    consolidate_reports,
    read_review_report,
    report_instructions,
)


class _MessageAttributes(TypedDict, total=False):
    status: str
    phase: str
    created_by: str
    interrupted: bool
    user: bool
    meta: bool
    subagent_return: bool


@pytest.fixture
def now_ms(monkeypatch: pytest.MonkeyPatch) -> int:
    fixed_time_ms = 1_700_000_000_250
    monkeypatch.setattr(
        "omnigent.studio.reviews.report.time_ns", lambda: fixed_time_ms * 1_000_000
    )
    return fixed_time_ms


@pytest.fixture
def review() -> ReviewPass:
    return ReviewPass(
        id="a" * 32,
        owner_user_id="owner@example.test",
        target=ReviewTarget(
            source_session_id="b" * 32,
            host_id="c" * 32,
            workspace="/work",
            pr_url="https://github.com/org/project/pull/123",
            repository="org/project",
            pr_number=123,
            base_sha="d" * 40,
            head_sha="e" * 40,
        ),
        profile=ReviewProfile(
            agent_id="f" * 32,
            agent_version="1",
            harness="claude-native",
            skill="code-review",
        ),
        state="running",
        dispatch_status="running",
        reviewer_session_id="1" * 32,
        created_at=0,
        updated_at=0,
        version=2,
    )


def _report(review: ReviewPass, **changes: object) -> str:
    return (
        "```studio-review-v1\n"
        + json.dumps(
            {
                "review_id": review.id,
                "base_sha": review.target.base_sha,
                "head_sha": review.target.head_sha,
                "summary": "The retry can duplicate a write.",
                "findings": [
                    {
                        "title": "Duplicate write",
                        "detail": "Reusing the request can execute the operation twice.",
                        "severity": "high",
                        "path": "src/action handler.py",
                        "side": "head",
                        "line": 17,
                    }
                ],
                **changes,
            }
        )
        + "\n```"
    )


def _message(
    text: str,
    *,
    identifier: str = "message",
    response_id: str = "response",
    created_at: int = 0,
    status: str = "completed",
    phase: str | None = None,
    created_by: str | None = None,
    interrupted: bool = False,
    user: bool = False,
    meta: bool = False,
    subagent_return: bool = False,
) -> ConversationItem:
    return ConversationItem(
        id=identifier,
        type="message",
        response_id=response_id,
        created_at=created_at,
        status=status,
        created_by=created_by,
        data=MessageData(
            role="user" if user else "assistant",
            agent=None if user else "native-reviewer",
            content=[{"type": "input_text" if user else "output_text", "text": text}],
            phase=phase,
            interrupted=interrupted,
            is_meta=meta,
            subagent_return_id="child" if subagent_return else None,
        ),
    )


def _transcript(
    review: ReviewPass,
    *newest_first: ConversationItem,
    live_status: str | None = "idle",
    older_tool_items: tuple[ConversationItem, ...] = (),
) -> Mock:
    session_id = review.reviewer_session_id
    assert session_id is not None
    store = Mock(spec=ConversationStore)
    store.get_conversation.return_value = Conversation(
        id=session_id,
        root_conversation_id=session_id,
        created_at=0,
        updated_at=0,
        live_status=live_status,
    )
    store.list_items.return_value = PagedList(data=list(newest_first))

    def list_items(
        _session_id: str, *, after: str | None = None, **_options: object
    ) -> PagedList[ConversationItem]:
        if after is not None:
            return PagedList(data=list(older_tool_items))
        return store.list_items.return_value

    store.list_items.side_effect = list_items
    return store


def _claude_skill_items(
    review: ReviewPass, *, response_id: str = "response"
) -> tuple[ConversationItem, ConversationItem]:
    call = ConversationItem(
        id="skill-call",
        type="function_call",
        response_id=response_id,
        created_at=0,
        status="completed",
        data=FunctionCallData(
            agent="native-reviewer",
            name="Skill",
            arguments=json.dumps({"skill": review.profile.skill}),
            call_id="toolu_skill",
        ),
    )
    result = ConversationItem(
        id="skill-result",
        type="function_call_output",
        response_id=response_id,
        created_at=0,
        status="completed",
        data=FunctionCallOutputData(
            call_id="toolu_skill", output=f"Launching skill: {review.profile.skill}\n"
        ),
    )
    return result, call


def test_reads_a_claude_report_split_across_persisted_text_blocks(review: ReviewPass) -> None:
    start, end = _report(review).rsplit("\n", 1)
    store = _transcript(
        review,
        _message(end, identifier="last-part"),
        _message(start, identifier="first-part"),
        _message("Reviewing the write path.", identifier="progress"),
        _message("Start review", user=True),
        _message(_report(review, summary="Old report"), response_id="old"),
        live_status="running",
        older_tool_items=_claude_skill_items(review),
    )
    assert read_review_report(store, review).state == "pending"
    store.list_items.assert_called_once()
    store.get_conversation.return_value.live_status = "idle"
    report = read_review_report(store, review)
    assert report.state == "ready"
    assert report.message_id == "last-part"
    assert report.summary == "The retry can duplicate a write."
    assert len(report.findings) == 1
    assert report.findings[0].path == "src/action handler.py"
    assert report.findings[0].side == "head"
    assert report.findings[0].line == 17
    store.list_items.assert_any_call(
        review.reviewer_session_id, order="desc", type="message", limit=24
    )
    store.list_items.assert_called_with(
        review.reviewer_session_id, order="desc", after="last-part", limit=64
    )
    store.append.assert_not_called()


def test_reads_a_role_only_claude_report_without_skill_receipt(review: ReviewPass) -> None:
    role = ReviewRole(id="security", label="Security", digest="a" * 64)
    profile = review.profile.model_copy(update={"role": role, "skill": None})
    review = review.model_copy(update={"profile": profile})
    store = _transcript(review, _message(_report(review), phase="final_answer"))
    assert read_review_report(store, review).state == "ready"


def test_instructions_provide_a_valid_envelope_with_the_bound_revision(review: ReviewPass) -> None:
    review = review.model_copy(
        update={"profile": review.profile.model_copy(update={"harness": "codex-native"})}
    )
    text = report_instructions(review)
    split_at = len(text) // 2
    answer = _message(text, phase="final_answer")
    assert isinstance(answer.data, MessageData)
    answer.data.content = [
        {"type": "output_text", "text": text[:split_at]},
        {"type": "input_text", "text": _report(review)},
        {"type": "output_text", "text": text[split_at:]},
    ]
    store = _transcript(
        review,
        answer,
        _message("```studio-review-v1\ncommentary", phase="commentary"),
        live_status=None,
    )
    report = read_review_report(store, review)
    assert report.state == "ready"
    assert report.findings == []
    store.list_items.assert_called_once()


@pytest.mark.parametrize(
    "case",
    [
        "no_tool",
        "wrong_skill",
        "invalid_arguments",
        "negative_result",
        "different_call",
        "previous_turn",
        "human_attribution",
        "unfinished_result",
    ],
)
def test_claude_report_requires_a_paired_skill_launch_in_its_turn(
    review: ReviewPass, case: str
) -> None:
    result, call = _claude_skill_items(review)

    def with_data(item: ConversationItem, **updates: object) -> ConversationItem:
        return item.model_copy(update={"data": item.data.model_copy(update=updates)})

    cases = {
        "no_tool": (),
        "wrong_skill": (result, with_data(call, arguments='{"skill":"other"}')),
        "invalid_arguments": (result, with_data(call, arguments="invalid JSON")),
        "negative_result": (with_data(result, output="Skill launch denied"), call),
        "different_call": (with_data(result, call_id="toolu_other"), call),
        "previous_turn": _claude_skill_items(review, response_id="previous-response"),
        "human_attribution": (
            result.model_copy(update={"created_by": "owner@example.test"}),
            call,
        ),
        "unfinished_result": (result.model_copy(update={"status": "in_progress"}), call),
    }
    store = _transcript(review, _message(_report(review)), older_tool_items=cases[case])
    report = read_review_report(store, review)
    assert report.state == "incomplete"
    assert report.message_id == "message"
    assert report.findings == []


@pytest.mark.parametrize(
    "changes",
    [
        {"review_id": "2" * 32},
        {"base_sha": "2" * 40},
        {"head_sha": "2" * 40},
        {"findings": None},
        {"unexpected": "do not retain unrecognized report data"},
        {"summary": " "},
    ],
)
def test_rejects_foreign_or_malformed_envelopes(
    review: ReviewPass, changes: dict[str, object]
) -> None:
    store = _transcript(
        review, _message(_report(review, **changes), phase="final_answer"), live_status=None
    )
    report = read_review_report(store, review)
    assert report.state == "incomplete"
    assert report.message_id == "message"
    assert report.summary is None
    assert report.findings == []
    store.list_items.assert_called_once()


@pytest.mark.parametrize(
    "changes",
    [
        {"path": "src/../hidden"},
        {"path": "/tmp/secret"},
        {"path": "C:\\temp\\private"},
        {"path": "src/\x00file"},
        {"side": "unknown"},
        {"line": True},
        {"line": "17"},
        {"line": 0},
    ],
)
def test_rejects_invalid_source_coordinates(
    review: ReviewPass, changes: dict[str, object]
) -> None:
    finding = {
        "title": "Duplicate write",
        "detail": "Fix the retry guard.",
        "severity": "high",
        "path": "src/worker.py",
        "side": "head",
        "line": 17,
        **changes,
    }
    store = _transcript(review, _message(_report(review, findings=[finding])))
    assert read_review_report(store, review).state == "incomplete"


@pytest.mark.parametrize(
    "attributes",
    [
        {"user": True},
        {"created_by": "owner@example.test"},
        {"meta": True},
        {"subagent_return": True},
        {"phase": "commentary"},
        {"interrupted": True},
        {"status": "in_progress"},
    ],
)
def test_ignores_nonfinal_and_nonreviewer_text(
    review: ReviewPass, attributes: _MessageAttributes
) -> None:
    store = _transcript(review, _message(_report(review), **attributes))
    assert read_review_report(store, review).state != "ready"


def test_repair_uses_new_response_without_falling_back_to_old_findings(
    review: ReviewPass, now_ms: int
) -> None:
    old = _message(_report(review), response_id="old")
    repair_prompt = _message("Please repair the report", user=True, created_at=now_ms // 1000)
    store = _transcript(
        review,
        repair_prompt,
        old,
        older_tool_items=_claude_skill_items(review, response_id="another-repair"),
    )
    assert read_review_report(store, review).state == "pending"

    incomplete = _message("Unstructured reply", response_id="repair")
    store.list_items.return_value = PagedList(data=[incomplete, repair_prompt, old])
    assert read_review_report(store, review).state == "incomplete"

    fixed = _message(_report(review, summary="Repaired"), response_id="another-repair")
    store.list_items.return_value = PagedList(data=[fixed, repair_prompt, incomplete, old])
    assert read_review_report(store, review).summary == "Repaired"


@pytest.mark.parametrize(
    ("live_status", "dispatch_status", "dispatch_age_ms", "input_age_ms", "expected"),
    [
        ("idle", "running", 10_000, 10_000, "incomplete"),
        ("idle", "running", 10_000, None, "pending"),
        ("idle", "running", 1_000, 10_000, "pending"),
        ("idle", "running", 10_000, 1_000, "pending"),
        ("running", "running", 10_000, 10_000, "pending"),
        ("waiting", "running", 10_000, 10_000, "pending"),
        ("idle", "unknown", 10_000, 10_000, "pending"),
    ],
)
def test_empty_reply_requires_acknowledged_idle_and_settled_input(
    review: ReviewPass,
    now_ms: int,
    live_status: str,
    dispatch_status: str,
    dispatch_age_ms: int,
    input_age_ms: int | None,
    expected: str,
) -> None:
    review = review.model_copy(
        update={"dispatch_status": dispatch_status, "updated_at": now_ms - dispatch_age_ms}
    )
    items = (
        []
        if input_age_ms is None
        else [_message("Review the patch", user=True, created_at=(now_ms - input_age_ms) // 1000)]
    )
    store = _transcript(review, *items, live_status=live_status)
    report = read_review_report(store, review)
    assert report.state == expected
    assert report.message_id is None
    assert report.findings == []


def test_rejects_excessive_bytes_and_trailing_text(review: ReviewPass) -> None:
    # Multibyte preamble exceeds the byte cap even while under the character cap.
    oversized = ("é" * 25000) + "\n" + _report(review)
    store = _transcript(review, _message(oversized))
    assert read_review_report(store, review).state == "incomplete"

    after_fence = _report(review) + "\nMore work is in progress."
    store.list_items.return_value = PagedList(data=[_message(after_fence)])
    assert read_review_report(store, review).state == "incomplete"


def test_does_not_read_a_missing_or_source_session_binding(review: ReviewPass) -> None:
    store = _transcript(review, _message(_report(review)))
    for session_id in (None, review.target.source_session_id):
        invalid = review.model_copy(update={"reviewer_session_id": session_id})
        assert read_review_report(store, invalid).state == "pending"
    store.list_items.assert_not_called()


def test_consolidation_keeps_provenance_and_disagreements():
    finding = ReviewFinding(
        title="Duplicate write",
        detail="The retry can duplicate a write.",
        severity="high",
        path="src/handler.py",
        side="head",
        line=17,
    )
    disagreement = finding.model_copy(update={"detail": "Only the log message is duplicated."})
    report = ReviewReport(state="ready", findings=[finding])
    result = consolidate_reports(
        [
            ReviewerRunReport(review_id="backend", report=report),
            ReviewerRunReport(
                review_id="security",
                report=report.model_copy(update={"findings": [finding, disagreement]}),
            ),
            ReviewerRunReport(
                review_id="frontend", report=ReviewReport(state="incomplete", findings=[finding])
            ),
        ]
    )
    assert [entry.review_ids for entry in result.findings] == [
        ["backend", "security"],
        ["security"],
    ]
    assert result.findings[1].finding == disagreement
    assert result.reviewers[2].report.state == "incomplete"
