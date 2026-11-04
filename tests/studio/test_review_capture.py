"""PR capture keeps authorization, revision identity and target binding explicit."""

from __future__ import annotations

import copy
import uuid
from dataclasses import replace
from typing import Any

import pytest
from fastapi import HTTPException

from omnigent.entities import Conversation
from omnigent.stores.session_pull_request_store import SessionPullRequests, StoredPullRequest
from omnigent.studio.reviews.capture import capture_review_target


@pytest.fixture
def capture_case() -> dict[str, Any]:
    session_id = uuid.uuid4().hex
    source = Conversation(
        id=session_id,
        root_conversation_id=session_id,
        created_at=0,
        updated_at=0,
        host_id=uuid.uuid4().hex,
        workspace="/work/project",
    )
    entry = StoredPullRequest(
        url="https://github.com/example/project/pull/17",
        provider="github",
        host="github.com",
        repository="example/project",
        number=17,
        relationship="attached",
        first_seen_at=0,
        last_seen_at=0,
    )
    return {
        "source": source,
        "after_source": source,
        "entry": entry,
        "stored": SessionPullRequests([entry], synced=True),
        "after_stored": SessionPullRequests([entry], synced=True),
        "info": {
            "available": True,
            "authenticated": True,
            "repo": {"name_with_owner": "example/project"},
            "pr": {
                "url": entry.url,
                "number": 17,
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
            },
        },
        "calls": [],
    }


async def _capture(capture_case: dict[str, Any], pr_url: str | None = None):
    source_reads = association_reads = 0

    async def load_session():
        nonlocal source_reads
        source_reads += 1
        return capture_case["source" if source_reads == 1 else "after_source"]

    async def load_associations():
        nonlocal association_reads
        association_reads += 1
        value = capture_case["stored" if association_reads == 1 else "after_stored"]
        if isinstance(value, Exception):
            raise value
        return value

    async def read_info(source, params):
        capture_case["calls"].append((source.id, params))
        if isinstance(capture_case["info"], Exception):
            raise capture_case["info"]
        return copy.deepcopy(capture_case["info"])

    return await capture_review_target(
        pr_url=pr_url,
        load_session=load_session,
        load_associations=load_associations,
        read_info=read_info,
    )


async def test_capture_uses_selected_association_and_authoritative_revisions(capture_case):
    target = await _capture(capture_case)
    assert target.source_session_id == capture_case["source"].id
    assert target.host_id == capture_case["source"].host_id
    assert target.workspace == "/work/project"
    assert target.pr_url == capture_case["entry"].url
    assert target.repository == "example/project"
    assert target.pr_number == 17
    assert target.base_sha == "a" * 40
    assert target.head_sha == "b" * 40
    assert capture_case["calls"] == [
        (capture_case["source"].id, {"pr_url": target.pr_url, "server_tracked": True})
    ]


@pytest.mark.parametrize("field", ["host_id", "workspace"])
async def test_capture_requires_a_bound_execution_target(capture_case, field):
    capture_case["source"] = replace(capture_case["source"], **{field: None})
    with pytest.raises(HTTPException) as raised:
        await _capture(capture_case)
    assert raised.value.status_code == 409
    assert capture_case["calls"] == []


@pytest.mark.parametrize(
    "stored",
    [None, SessionPullRequests([], synced=False), SessionPullRequests([], synced=True)],
)
async def test_capture_refuses_unknown_or_missing_associations(capture_case, stored):
    capture_case["stored"] = stored
    with pytest.raises(HTTPException) as raised:
        await _capture(capture_case)
    assert raised.value.status_code in (409, 503)
    assert capture_case["calls"] == []


async def test_unassociated_requested_pr_is_not_sent_to_execution_target(capture_case):
    with pytest.raises(HTTPException) as raised:
        await _capture(capture_case, "https://github.com/example/other/pull/9")
    assert raised.value.status_code == 409
    assert capture_case["calls"] == []


@pytest.mark.parametrize("phase", ["stored", "after_stored"])
async def test_association_storage_failure_blocks_capture_without_exposing_error(
    capture_case, phase
):
    capture_case[phase] = RuntimeError("private-storage-connection-detail")
    with pytest.raises(HTTPException) as raised:
        await _capture(capture_case)
    assert raised.value.status_code == 503
    assert "private-storage" not in raised.value.detail


@pytest.mark.parametrize("field", ["available", "authenticated"])
async def test_capture_requires_observed_pr_access(capture_case, field):
    capture_case["info"][field] = False
    with pytest.raises(HTTPException) as raised:
        await _capture(capture_case)
    assert raised.value.status_code == 503


@pytest.mark.parametrize("status", [403, 404, 502, 503])
async def test_capture_normalizes_transport_errors_and_preserves_access_refusals(
    capture_case, status
):
    capture_case["info"] = HTTPException(status, "private-transport-detail")
    with pytest.raises(HTTPException) as raised:
        await _capture(capture_case)
    assert raised.value.status_code == (503 if status >= 500 else status)
    if status >= 500:
        assert "private-transport" not in raised.value.detail


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("url", "https://github.com/example/other/pull/17"),
        ("number", True),
        ("number", 18),
        ("base_sha", None),
        ("head_sha", "unresolved"),
    ],
)
async def test_incomplete_or_mismatched_identity_cannot_be_frozen(capture_case, field, value):
    capture_case["info"]["pr"][field] = value
    with pytest.raises(HTTPException) as raised:
        await _capture(capture_case)
    assert raised.value.status_code == 503


async def test_repository_mismatch_is_refused(capture_case):
    capture_case["info"]["repo"]["name_with_owner"] = "example/other"
    with pytest.raises(HTTPException) as raised:
        await _capture(capture_case)
    assert raised.value.status_code == 503


@pytest.mark.parametrize("field", ["id", "host_id", "workspace"])
async def test_binding_change_during_rpc_invalidates_capture(capture_case, field):
    value = "/other/workspace" if field == "workspace" else uuid.uuid4().hex
    capture_case["after_source"] = replace(capture_case["source"], **{field: value})
    with pytest.raises(HTTPException) as raised:
        await _capture(capture_case)
    assert raised.value.status_code == 409


async def test_pr_unlinked_while_resource_is_read_cannot_be_captured(capture_case):
    capture_case["after_stored"] = SessionPullRequests([], synced=True)
    with pytest.raises(HTTPException) as raised:
        await _capture(capture_case)
    assert raised.value.status_code == 409
