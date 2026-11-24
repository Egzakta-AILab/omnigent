from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from omnigent.db.db_models import workspace_scope
from omnigent.stores.studio_tasks import LEASE_SECONDS, StudioTaskStore
from omnigent.studio import rpc


def test_concurrent_issue_claim_and_paging(db_uri):
    store = StudioTaskStore(db_uri)
    key = rpc.TaskKey(linear_workspace=uuid4(), linear_issue_id=uuid4())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda actor: store.task_for_issue(actor, key), ["alice", "bob"] * 3)
        )
    assert len({item["task_id"] for item in results}) == 1
    second = store.task_for_issue("bob", key.model_copy(update={"linear_issue_id": uuid4()}))
    first_page = store.task_list(rpc.Page(limit=1))
    next_page = store.task_list(rpc.Page(limit=1, after=first_page[0]["task_id"]))
    assert {item["task_id"] for item in first_page + next_page} == {
        results[0]["task_id"],
        second["task_id"],
    }
    with workspace_scope(45):
        assert store.task_list(rpc.Page()) == []
        assert store.task_for_issue("alice", key)["task_id"] != results[0]["task_id"]


def test_handoff_replay_inbox_and_mutation(db_uri):
    store = StudioTaskStore(db_uri)
    task = store.task_for_issue(
        "alice", rpc.TaskKey(linear_workspace=uuid4(), linear_issue_id=uuid4())
    )
    card = rpc.Handoff(
        task_id=UUID(task["task_id"]),
        schema_version="studio-handoff@1",
        request_id=uuid4(),
        recipient="bob",
        question="Does the evidence meet acceptance?",
    )
    assert store.handoff("alice", card) == store.handoff("alice", card)
    assert store.inbox("alice", rpc.Page()) == []
    assert len(store.inbox("bob", rpc.Page())) == 1
    with pytest.raises(HTTPException, match="already used"):
        store.handoff("alice", card.model_copy(update={"recipient": "eve"}))
    accept = rpc.Response(request_id=card.request_id, decision="accept")
    with pytest.raises(HTTPException, match="not found"):
        store.respond("eve", accept)
    assert store.respond("bob", accept) == store.respond("bob", accept)
    with pytest.raises(HTTPException, match="already answered"):
        store.respond("bob", accept.model_copy(update={"decision": "away"}))


def test_lease_expiry_and_round_cap(db_uri):
    now = [100.0]
    store = StudioTaskStore(db_uri, clock=lambda: now[0])
    task = store.task_for_issue(
        "owner", rpc.TaskKey(linear_workspace=uuid4(), linear_issue_id=uuid4())
    )
    claim = rpc.LeaseClaim(
        task_id=UUID(task["task_id"]), idempotency_key=uuid4(), harness="pi-native"
    )
    started = store.update_lease("owner", claim)
    assert started["lease_expires_at"] == 100 + LEASE_SECONDS
    assert started == store.update_lease("owner", claim)
    now[0] += LEASE_SECONDS
    assert store.update_lease("owner", claim)["state"] == "unknown"
    assert (
        store.update_lease("owner", rpc.LeaseRef(idempotency_key=claim.idempotency_key))["state"]
        == "unknown"
    )
    other = claim.model_copy(update={"idempotency_key": uuid4()})
    store.update_lease("owner", other)
    for number in (1, 2, 3):
        end = rpc.FinishRound(idempotency_key=other.idempotency_key, round=number, passed=False)
        result = store.update_lease("owner", end)
        assert store.update_lease("owner", end) == result
        assert result["fix_round"] == number
    assert result["state"] == "escalated"
    with pytest.raises(HTTPException, match="differs"):
        store.update_lease("owner", end.model_copy(update={"passed": True}))
    assert len(store.inventory("owner", rpc.Page())["leases"]) == 2


async def test_linear_checks_target_and_recovers_same_provider_receipt(monkeypatch):
    import httpx

    from omnigent.studio import linear_write

    workspace, issue, receipt = str(uuid4()), str(uuid4()), uuid4()
    published = {}
    comment = rpc.LinearComment(task_id=uuid4(), request_id=receipt, body="message")

    def reply(request):
        import json

        body = json.loads(request.content)
        query = body["query"]
        if "StudioCommentTarget" in query:
            data = {
                "viewer": {"id": "actor"},
                "organization": {"id": workspace},
                "issue": {"id": issue},
            }
        elif "StudioIssueUpdate" in query:
            update = body["variables"]["input"]
            assert list(update) in (["assigneeId"], ["stateId"])
            field, value = next(iter(update.items()))
            data = {
                "issueUpdate": {"success": True, "issue": {"id": issue, field[:-2]: {"id": value}}}
            }
        elif "StudioConfirmedComment" in query:
            comment = body["variables"]["input"]
            assert comment["id"] == str(receipt)
            if published:
                return httpx.Response(200, json={"errors": [{"message": "Duplicate ID"}]})
            published.update(comment)
            data = {"commentCreate": {"success": True, "comment": {"id": str(receipt)}}}
        else:
            data = {
                "comment": {
                    "id": str(receipt),
                    "body": published["body"],
                    "user": {"id": "actor"},
                    "issue": {"id": issue},
                }
            }
        return httpx.Response(200, json={"data": data})

    client = httpx.AsyncClient
    monkeypatch.setenv("OMNIGENT_STUDIO_LINEAR_TOKEN", "test-token")
    monkeypatch.setattr(
        linear_write.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(reply), **kwargs),
    )
    with pytest.raises(HTTPException, match="workspace"):
        await linear_write.write_linear(str(uuid4()), issue, comment)
    assert not published
    for _ in range(2):
        assert await linear_write.write_linear(workspace, issue, comment) == {
            "comment_id": str(receipt)
        }
    with pytest.raises(HTTPException, match="different"):
        await linear_write.write_linear(
            workspace, issue, comment.model_copy(update={"body": "other message"})
        )

    for change in ("assignee", "status"):
        update = rpc.LinearIssueChange(
            task_id=uuid4(), request_id=uuid4(), change=change, value=uuid4()
        )
        assert await linear_write.write_linear(workspace, issue, update) == {
            "linear_issue_id": issue,
            "change": change,
            "value": str(update.value),
        }


def test_reregistered_accounts_cannot_reuse_handoffs_leases_or_linear_confirmation(db_uri):
    from omnigent.server.accounts_store import SqlAlchemyAccountStore

    accounts = SqlAlchemyAccountStore(db_uri)
    original = accounts.create_user_with_password("alice", "original-hash")
    accounts.create_user_with_password("bob", "original-hash")
    store = StudioTaskStore(db_uri)
    task = store.task_for_issue(
        "alice", rpc.TaskKey(linear_workspace=uuid4(), linear_issue_id=uuid4())
    )
    card = rpc.Handoff(
        task_id=UUID(task["task_id"]),
        schema_version="studio-handoff@1",
        request_id=uuid4(),
        recipient="alice",
        question="Private context",
    )
    store.handoff("bob", card)
    old_sender = card.model_copy(update={"recipient": "bob", "request_id": uuid4()})
    store.handoff("alice", old_sender)
    claim = rpc.LeaseClaim(task_id=card.task_id, idempotency_key=uuid4(), harness="pi-native")
    store.update_lease("alice", claim)
    comment = rpc.LinearComment(task_id=card.task_id, request_id=uuid4(), body="Approved")
    ticket = store.linear("alice", "example.work", comment, "confirmLinear")
    assert accounts.delete_user("alice") is True
    new = accounts.create_user_with_password("alice", "replacement-hash")
    assert new.account_generation != original.account_generation
    assert store.inbox("alice", rpc.Page()) == []
    with pytest.raises(HTTPException, match="not found"):
        store.respond("alice", rpc.Response(request_id=card.request_id, decision="accept"))
    with pytest.raises(HTTPException, match="already used"):
        store.handoff("alice", old_sender)
    with pytest.raises(HTTPException, match="not found"):
        store.update_lease("alice", rpc.LeaseRef(idempotency_key=claim.idempotency_key))
    with pytest.raises(HTTPException, match="confirmation"):
        store.linear(
            "alice",
            "example.work",
            rpc.ConfirmedComment(**comment.model_dump(), **ticket),
            "syncLinearConfirmed",
        )
