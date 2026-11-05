"""Read structured review findings from the bound native transcript."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from time import time_ns
from typing import Annotated, Literal, TypeVar

from pydantic import Field, StringConstraints, ValidationError, field_validator

from omnigent.entities.conversation import (
    ConversationItem,
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
)
from omnigent.errors import StaleCursorError
from omnigent.stores.conversation_store import ConversationStore
from omnigent.studio.reviews.evidence import Anchor
from omnigent.studio.reviews.models import FrozenModel, ReviewPass, ReviewPlanProposal, Sha

_REPORT_FENCE = "```studio-review-v1"
_MAX_REPORT_BYTES = 48 * 1024
_MAX_FINDINGS = 40
_RECENT_MESSAGE_LIMIT = 24
_RECENT_SKILL_ITEM_LIMIT = 64
_EMPTY_REPLY_GRACE_MS = 5_000
_PROGRESS_PHASES = {"commentary", "partial_answer"}
_ACTIVE_STATUSES = {"running", "waiting"}
_Envelope = TypeVar("_Envelope", bound=FrozenModel)
ReportState = Literal["pending", "incomplete", "ready"]

_Summary = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class ReviewFinding(FrozenModel):
    """An agent-reported concern anchored to one side of the captured diff."""

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    detail: _Summary
    severity: Literal["critical", "high", "medium", "low"]
    path: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    side: Literal["base", "head"]
    line: int = Field(gt=0, le=2**31 - 1)

    @field_validator("path")
    @classmethod
    def repository_relative_path(cls, path: str) -> str:
        if (
            "\\" in path
            or re.match(r"^[A-Za-z]:", path)
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
        ):
            raise ValueError("Use a repository-relative file path")
        return path


class _ReportEnvelope(FrozenModel):
    review_id: str
    base_sha: Sha
    head_sha: Sha
    summary: _Summary
    findings: list[ReviewFinding] = Field(max_length=_MAX_FINDINGS)


class ReviewReport(FrozenModel):
    """View of the report as observed in the reviewer's durable transcript."""

    state: ReportState
    summary: str | None = None
    findings: list[ReviewFinding] = Field(default_factory=list)
    message_id: str | None = None
    citation_validation: Literal["not_checked", "checked", "unavailable"] = "not_checked"
    unverified_findings: list[ReviewFinding] = Field(default_factory=list)


def qualify_citations(report: ReviewReport, anchors: frozenset[Anchor] | None) -> ReviewReport:
    """Keep every assertion visible while separating unsupported citations."""
    if report.state != "ready" or report.citation_validation == "checked":
        return report
    if anchors is None:
        return report.model_copy(update={"citation_validation": "unavailable"})
    valid, rejected = [], []
    for finding in report.findings:
        target = valid if (finding.path, finding.side, finding.line) in anchors else rejected
        target.append(finding)
    return report.model_copy(
        update={
            "citation_validation": "checked",
            "findings": valid,
            "unverified_findings": rejected,
        }
    )


class ReviewerRunReport(FrozenModel):
    review_id: str
    report: ReviewReport


class ConsolidatedFinding(FrozenModel):
    finding: ReviewFinding
    review_ids: list[str]


class ConsolidatedReviewReport(FrozenModel):
    reviewers: list[ReviewerRunReport]
    findings: list[ConsolidatedFinding]
    proposal: ReviewPlanProposal | None = None


def consolidate_reports(reports: list[ReviewerRunReport]) -> ConsolidatedReviewReport:
    """Group exact duplicates and keep disagreements at the same line separate."""
    grouped: dict[str, tuple[ReviewFinding, list[str]]] = {}
    for entry in reports:
        if entry.report.state != "ready":
            continue
        for finding in entry.report.findings:
            _, sources = grouped.setdefault(finding.model_dump_json(), (finding, []))
            if entry.review_id not in sources:
                sources.append(entry.review_id)
    return ConsolidatedReviewReport(
        reviewers=reports,
        findings=[
            ConsolidatedFinding(finding=finding, review_ids=sources)
            for finding, sources in grouped.values()
        ],
    )


def report_instructions(review: ReviewPass) -> str:
    """Describe the narrow result format without embedding repository contents."""
    example = {
        "review_id": review.id,
        "base_sha": review.target.base_sha,
        "head_sha": review.target.head_sha,
        "summary": "Summarize what you reviewed and any limits.",
        "findings": [],
    }
    return (
        f"End your final answer with exactly one {_REPORT_FENCE} fenced JSON block. "
        "Use the exact review ID and SHAs shown below. "
        f"Keep the block under {_MAX_REPORT_BYTES // 1024} KiB and include at most "
        f"{_MAX_FINDINGS} actionable findings. For each finding, supply title (up to 200 "
        "characters), detail (up to 2000 characters), "
        "severity (critical, high, medium, or low), path (repository-relative), "
        "side (base or head), and line (a one-based line on that side of the supplied diff). "
        "Keep the summary under 2000 characters. Use an empty findings array if you found none. "
        "Do not add extra JSON fields.\n"
        f"{_REPORT_FENCE}\n{json.dumps(example, indent=2)}\n```"
    )


def _assistant_response(
    items: list[ConversationItem],
) -> tuple[list[ConversationItem], int | None]:
    """Return the latest response and the preceding saved input time, if observed."""
    response: list[ConversationItem] = []
    for item in items:
        data = item.data
        if not isinstance(data, MessageData) or data.is_meta:
            continue
        if data.role == "user":
            return response, item.created_at if item.status == "completed" else None
        if item.created_by is not None or data.user_authored or data.subagent_return_id:
            continue
        if response and item.response_id != response[0].response_id:
            break
        response.append(item)
    return response, None


def _fenced_payload(
    items: list[ConversationItem], fence: str, model: type[_Envelope]
) -> _Envelope | None:
    """Read one terminal tagged fence without retaining an oversized reply."""
    parts: list[str] = []
    size = 0
    for item in reversed(items):
        data = item.data
        if not isinstance(data, MessageData):
            continue
        text_parts = [
            text
            for block in data.content
            if block.get("type") == "output_text" and isinstance(text := block.get("text"), str)
        ]
        for text in text_parts:
            if len(text) > _MAX_REPORT_BYTES - size:
                return None
            try:
                size += len(text.encode("utf-8"))
            except UnicodeEncodeError:
                return None
            if size > _MAX_REPORT_BYTES:
                return None
        parts.append("".join(text_parts))

    lines = "\n".join(parts).strip().split("\n")
    markers = [index for index, line in enumerate(lines) if line.rstrip("\r") == fence]
    if len(markers) != 1 or lines[-1] != "```":
        return None
    try:
        return model.model_validate_json("\n".join(lines[markers[0] + 1 : -1]), strict=True)
    except ValidationError:
        return None


def _has_claude_skill_launch(
    conversation_store: ConversationStore, session_id: str, reply: ConversationItem, skill: str
) -> bool:
    """Find Claude's positive Skill result paired with its call in this reply's turn."""
    try:
        page = conversation_store.list_items(
            session_id, order="desc", after=reply.id, limit=_RECENT_SKILL_ITEM_LIMIT
        )
    except StaleCursorError:
        return False

    launches: set[str] = set()
    expected_output = f"Launching skill: {skill}"
    for item in page.data:
        data = item.data
        if isinstance(data, MessageData) and data.role == "user" and not data.is_meta:
            break
        if (
            item.response_id != reply.response_id
            or item.status != "completed"
            or item.created_by is not None
        ):
            continue
        if (
            isinstance(data, FunctionCallOutputData)
            and data.call_id
            and data.subagent_return_id is None
            and data.output.strip() == expected_output
        ):
            launches.add(data.call_id)
        if (
            isinstance(data, FunctionCallData)
            and data.call_id in launches
            and data.name == "Skill"
            and data.namespace is None
            and len(data.arguments) <= _MAX_REPORT_BYTES
        ):
            try:
                arguments = json.loads(data.arguments)
            except (ValueError, RecursionError):
                continue
            if isinstance(arguments, dict) and arguments.get("skill") == skill:
                return True
    return False


def read_native_envelope(
    conversation_store: ConversationStore,
    review: ReviewPass,
    fence: str,
    model: type[_Envelope],
    *,
    accepts: Callable[[_Envelope], bool] | None = None,
) -> tuple[ReportState, str | None, _Envelope | None]:
    """Read a terminal native reply from the bound reviewer without changing its state."""
    session_id = review.reviewer_session_id
    if session_id is None or session_id == review.target.source_session_id:
        return "pending", None, None

    recent = conversation_store.list_items(
        session_id, order="desc", type="message", limit=_RECENT_MESSAGE_LIMIT
    )
    response, input_created_at = _assistant_response(recent.data)
    session = conversation_store.get_conversation(session_id)
    live_status = session.live_status if session is not None else None
    if not response:
        # A saved echo distinguishes consumed input from a still-queued request.
        # Give the mirror time to catch up with idle (item timestamps are seconds).
        empty_reply = (
            live_status == "idle"
            and review.dispatch_status == "running"
            and input_created_at is not None
            and time_ns() // 1_000_000 - max(review.updated_at, input_created_at * 1_000)
            >= _EMPTY_REPLY_GRACE_MS
        )
        state = "incomplete" if empty_reply or live_status == "failed" else "pending"
        return state, None, None

    latest = response[0]
    if latest.status == "in_progress" or live_status in _ACTIVE_STATUSES:
        return "pending", None, None

    completed = [
        item
        for item in response
        if isinstance(item.data, MessageData)
        and item.data.phase not in _PROGRESS_PHASES
        and not item.data.interrupted
        and item.status == "completed"
    ]
    # Do not fall back to earlier text after an interrupted or partial final item.
    payload = (
        _fenced_payload(completed, fence, model) if completed and latest is completed[0] else None
    )
    if payload is not None and (accepts is None or accepts(payload)):
        if (
            review.profile.harness == "claude-native"
            and (skill := review.profile.skill) is not None
            and not _has_claude_skill_launch(conversation_store, session_id, latest, skill)
        ):
            return "incomplete", latest.id, None
        return "ready", latest.id, payload

    terminal = (
        live_status in {"idle", "failed"}
        or latest.status in {"incomplete", "failed"}
        or (
            isinstance(latest.data, MessageData)
            and (latest.data.phase == "final_answer" or latest.data.interrupted)
        )
    )
    if terminal:
        return "incomplete", latest.id, None
    return "pending", None, None


def read_review_report(conversation_store: ConversationStore, review: ReviewPass) -> ReviewReport:
    state, message_id, payload = read_native_envelope(
        conversation_store,
        review,
        _REPORT_FENCE,
        _ReportEnvelope,
        accepts=lambda envelope: (
            (envelope.review_id, envelope.base_sha, envelope.head_sha)
            == (review.id, review.target.base_sha, review.target.head_sha)
        ),
    )
    if payload is None:
        return ReviewReport(state=state, message_id=message_id)
    return ReviewReport(
        state="ready", summary=payload.summary, findings=payload.findings, message_id=message_id
    )
