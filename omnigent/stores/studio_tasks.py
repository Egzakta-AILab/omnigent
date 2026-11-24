"""Transactional task coordination for one authenticated Studio organization."""

import hashlib
import json
import secrets
import time
from collections.abc import Callable
from typing import Any, TypeVar
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import (
    SqlStudioAgentLease,
    SqlStudioSeatSlot,
    SqlStudioTask,
    SqlStudioTaskHandoff,
    SqlUser,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.studio import rpc
from omnigent.studio.seat_pool import fallback_order, usage_counts

T = TypeVar("T")
Record = dict[str, Any]
LEASE_SECONDS = 20 * 60


def _task(row: SqlStudioTask) -> Record:
    links = json.loads(row.links_json)
    return {
        "task_id": str(UUID(row.id)),
        "linear_workspace": str(UUID(row.linear_workspace)),
        "linear_issue_id": str(UUID(row.linear_issue_id)),
        "acceptance_check": row.acceptance_check,
        "pull_requests": links.get("pull_requests", []),
        "session_ids": links.get("session_ids", []),
    }


def _require_task(db: Session, task_id: UUID, *, lock: bool = False) -> SqlStudioTask:
    row = db.get(SqlStudioTask, (current_workspace_id(), task_id.hex), with_for_update=lock)
    if row is None:
        raise HTTPException(404, "Studio task not found")
    return row


def _handoff(row: SqlStudioTaskHandoff) -> Record:
    return {
        **json.loads(row.packet_json),
        "sender": row.sender,
        "response": json.loads(row.response_json) if row.response_json else None,
    }


def _lease(row: SqlStudioAgentLease, now: int) -> Record:
    if row.state == "active" and row.lease_expires_at <= now:
        row.state = "unknown"
    return {
        "idempotency_key": str(UUID(row.idempotency_key)),
        "task_id": str(UUID(row.task_id)),
        "harness": row.harness,
        "state": row.state,
        "fix_round": row.fix_round,
        "lease_expires_at": row.lease_expires_at,
    }


def _seat(row: SqlStudioSeatSlot) -> Record:
    return {
        "slot_id": row.slot_id,
        "routing_mode": row.routing_mode,
        "harness": row.harness,
        "enabled": row.enabled,
        "weight": row.weight,
        "cooldown_until": row.cooldown_until,
    }


class StudioTaskStore:
    def __init__(self, uri: str, clock: Callable[[], float] = time.time) -> None:
        self.storage_location = uri
        engine = get_or_create_engine(uri)
        self.clock = clock
        self.read = make_named_managed_session_maker(engine, query_name_prefix="studio.tasks")
        self.write = make_named_managed_session_maker(
            engine, query_name_prefix="studio.tasks", immediate=True
        )

    def _change(self, actor: str, name: str, change: Callable[[Session], T]) -> T:
        def apply(db: Session) -> T:
            require_active_account(db, actor)
            return change(db)

        # Simultaneous first inserts can race before there is a row to lock.
        # The unique key determines the winner. Retry in a fresh transaction
        # to read its committed intent and distinguish replay from conflict.
        try:
            return run_write_transaction(self.write, name, apply)
        except IntegrityError:
            return run_write_transaction(self.write, name, apply)

    def task_for_issue(self, actor: str, key: rpc.TaskKey) -> Record:
        def find(db: Session) -> Record:
            row = db.scalar(
                select(SqlStudioTask).filter_by(
                    workspace_id=current_workspace_id(),
                    linear_workspace=key.linear_workspace.hex,
                    linear_issue_id=key.linear_issue_id.hex,
                )
            )
            if row is None:
                row = SqlStudioTask(
                    id=uuid4().hex,
                    linear_workspace=key.linear_workspace.hex,
                    linear_issue_id=key.linear_issue_id.hex,
                )
                db.add(row)
                db.flush()
            return _task(row)

        return self._change(actor, "get_or_create", find)

    def task_list(self, page: rpc.Page, *, releases: bool = False) -> list[Record]:
        with self.read("list_tasks") as db:
            query = select(SqlStudioTask).filter_by(workspace_id=current_workspace_id())
            if page.after:
                query = query.where(SqlStudioTask.id > UUID(page.after).hex)
            if releases:
                query = query.where(SqlStudioTask.links_json.contains('"pull_requests"'))
            rows = [
                _task(row)
                for row in db.scalars(query.order_by(SqlStudioTask.id).limit(page.limit))
            ]
            return (
                [{"task_id": r["task_id"], "pull_requests": r["pull_requests"]} for r in rows]
                if releases
                else rows
            )

    def tasks_for_session(self, session_id: UUID) -> list[Record]:
        sid = str(session_id)
        with self.read("tasks_for_session") as db:
            query = (
                select(SqlStudioTask)
                .filter_by(workspace_id=current_workspace_id())
                .where(SqlStudioTask.links_json.contains(json.dumps(sid)))
                .order_by(SqlStudioTask.id)
            )
            tasks = (_task(row) for row in db.scalars(query))
            return [task for task in tasks if sid in task["session_ids"]]

    def change_task(
        self, actor: str, value: rpc.Acceptance | rpc.PullRequest | rpc.SessionLink
    ) -> Record:
        def change(db: Session) -> Record:
            row = _require_task(db, value.task_id, lock=True)
            links = json.loads(row.links_json)
            if isinstance(value, rpc.Acceptance):
                row.acceptance_check = value.acceptance_check
            elif isinstance(value, rpc.SessionLink):
                ids = links.setdefault("session_ids", [])
                sid = str(value.session_id)
                if sid not in ids:
                    if len(ids) >= 64:
                        raise HTTPException(409, "Task session link limit reached")
                    ids.append(sid)
            else:
                prs = links.setdefault("pull_requests", [])
                item = value.model_dump(exclude={"task_id"})
                for i, prior in enumerate(prs):
                    if (prior["repository"].casefold(), prior["number"]) == (
                        value.repository.casefold(),
                        value.number,
                    ):
                        prs[i] = item
                        break
                else:
                    if len(prs) >= 64:
                        raise HTTPException(409, "Task pull request limit reached")
                    prs.append(item)
            row.links_json = json.dumps(links)
            return _task(row)

        return self._change(actor, "update_task", change)

    def handoff(self, actor: str, value: rpc.Handoff) -> Record:
        def create(db: Session) -> Record:
            _require_task(db, value.task_id)
            row = db.get(SqlStudioTaskHandoff, (current_workspace_id(), value.request_id.hex))
            sender_generation = require_active_account(db, actor)
            recipient_account = db.get(SqlUser, (current_workspace_id(), value.recipient))
            if (recipient_account and recipient_account.deleted_at is not None) or (
                sender_generation and recipient_account is None
            ):
                raise HTTPException(404, "Inbox recipient not found")
            recipient_generation = (
                recipient_account.account_generation if recipient_account else None
            )
            packet = value.model_dump(mode="json")
            if row is None:
                row = SqlStudioTaskHandoff(
                    request_id=value.request_id.hex,
                    task_id=value.task_id.hex,
                    schema_version=value.schema_version,
                    sender=actor,
                    sender_generation=sender_generation,
                    recipient_generation=recipient_generation,
                    recipient=value.recipient,
                    packet_json=json.dumps(packet),
                )
                db.add(row)
            elif (row.sender, row.sender_generation, row.recipient_generation) != (
                actor,
                sender_generation,
                recipient_generation,
            ) or json.loads(row.packet_json) != packet:
                raise HTTPException(409, "Handoff request ID already used")
            return _handoff(row)

        return self._change(actor, "handoff", create)

    def inbox(self, actor: str, page: rpc.Page) -> list[Record]:
        with self.read("inbox") as db:
            query = select(SqlStudioTaskHandoff).filter_by(
                workspace_id=current_workspace_id(),
                recipient=actor,
                recipient_generation=require_active_account(db, actor),
            )
            if page.after:
                query = query.where(SqlStudioTaskHandoff.request_id > UUID(page.after).hex)
            return [
                _handoff(row)
                for row in db.scalars(
                    query.order_by(SqlStudioTaskHandoff.request_id).limit(page.limit)
                )
            ]

    def respond(self, actor: str, value: rpc.Response) -> Record:
        def change(db: Session) -> Record:
            row = db.get(
                SqlStudioTaskHandoff,
                (current_workspace_id(), value.request_id.hex),
                with_for_update=True,
            )
            if row is None or (row.recipient, row.recipient_generation) != (
                actor,
                require_active_account(db, actor),
            ):
                raise HTTPException(404, "Inbox card not found")
            reply = value.model_dump(mode="json", exclude={"request_id"})
            if row.response_json and json.loads(row.response_json) != reply:
                raise HTTPException(409, "Inbox card already answered")
            row.response_json = json.dumps(reply)
            return _handoff(row)

        return self._change(actor, "respond", change)

    def update_lease(self, actor: str, value: rpc.LeaseClaim | rpc.LeaseRef) -> Record:
        def change(db: Session) -> Record:
            now = int(self.clock())
            registration = require_active_account(db, actor)
            row = db.get(
                SqlStudioAgentLease,
                (current_workspace_id(), value.idempotency_key.hex),
                with_for_update=True,
            )
            if isinstance(value, rpc.LeaseClaim):
                if row is None:
                    _require_task(db, value.task_id)
                    row = SqlStudioAgentLease(
                        idempotency_key=value.idempotency_key.hex,
                        task_id=value.task_id.hex,
                        actor=actor,
                        actor_generation=registration,
                        harness=value.harness,
                        state="active",
                        fix_round=0,
                        lease_expires_at=now + LEASE_SECONDS,
                    )
                    db.add(row)
                elif (row.actor, row.actor_generation, row.task_id, row.harness) != (
                    actor,
                    registration,
                    value.task_id.hex,
                    value.harness,
                ):
                    raise HTTPException(409, "Dispatch key already used")
                return _lease(row, now)
            if row is None or (row.actor, row.actor_generation) != (actor, registration):
                raise HTTPException(404, "Dispatch not found")
            _lease(row, now)
            if isinstance(value, rpc.FinishRound):
                if value.round == row.fix_round:
                    if value.passed != (row.state == "passed"):
                        raise HTTPException(409, "Round result differs from its receipt")
                    return _lease(row, now)
                if row.state != "active" or value.round != row.fix_round + 1:
                    raise HTTPException(409, "Dispatch round is closed or out of sequence")
                row.fix_round = value.round
                if value.passed:
                    row.state = "passed"
                elif value.round == 3:
                    row.state = "escalated"
                    task = _require_task(db, UUID(row.task_id))
                    evidence = [
                        f"Dispatch {UUID(row.idempotency_key)} failed all three rounds.",
                        f"Native harness: {row.harness}.",
                    ]
                    for pr in _task(task)["pull_requests"][:10]:
                        evidence.append(f"{pr['repository']}#{pr['number']} at {pr['head_sha']}")
                    card = rpc.Handoff(
                        task_id=UUID(row.task_id),
                        schema_version="studio-handoff@1",
                        request_id=uuid4(),
                        recipient=actor,
                        question=(
                            "The free agent reached its three-round limit. "
                            "Review the evidence and choose the next action."
                        ),
                        evidence=evidence,
                    )
                    db.add(
                        SqlStudioTaskHandoff(
                            request_id=card.request_id.hex,
                            task_id=row.task_id,
                            schema_version=card.schema_version,
                            sender="Studio Fleet",
                            sender_generation=None,
                            recipient=actor,
                            recipient_generation=registration,
                            packet_json=card.model_dump_json(),
                        )
                    )

            if row.state == "active":
                row.lease_expires_at = now + LEASE_SECONDS
            return _lease(row, now)

        return self._change(actor, "lease", change)

    def begin_agent(self, actor: str, value: rpc.LeaseClaim) -> Record:
        self.update_lease(actor, value)

        def claim(db: Session) -> Record:
            row = db.get(
                SqlStudioAgentLease,
                (current_workspace_id(), value.idempotency_key.hex),
                with_for_update=True,
            )
            assert row is not None
            receipt = _lease(row, int(self.clock()))
            if row.actor != actor or row.actor_generation != require_active_account(db, actor):
                raise HTTPException(404, "Dispatch not found")
            if row.session_start or row.state != "active":
                return {**receipt, "launch": False}
            task = _require_task(db, value.task_id)
            if not task.acceptance_check.strip():
                raise HTTPException(409, "A shared acceptance check is required")
            row.session_id = uuid4().hex
            row.session_start = "pending"
            return {
                **receipt,
                "launch": True,
                "session_id": row.session_id,
                "acceptance_check": task.acceptance_check,
            }

        return self._change(actor, "begin_agent", claim)

    def complete_agent(
        self, actor: str, key: UUID, started: bool, *, queued: bool = False
    ) -> None:
        def complete(db: Session) -> None:
            row = db.get(
                SqlStudioAgentLease, (current_workspace_id(), key.hex), with_for_update=True
            )
            if row is None or (row.actor, row.actor_generation) != (
                actor,
                require_active_account(db, actor),
            ):
                raise HTTPException(404, "Dispatch not found")
            if queued:
                row.session_start = None
                row.session_id = None
                return
            row.session_start = "started" if started else "unknown"
            if not started:
                row.state = "unknown"

        self._change(actor, "complete_agent", complete)

    def task_prs(self, task_id: UUID) -> list[Record]:
        with self.read("task_previews") as db:
            return _task(_require_task(db, task_id))["pull_requests"]

    def expired_sessions(self, after: str = "", only: str | None = None) -> list[tuple[str, str]]:
        def scan(db: Session) -> list[tuple[str, str]]:
            now = int(self.clock())
            query = (
                select(SqlStudioAgentLease)
                .where(
                    SqlStudioAgentLease.workspace_id == current_workspace_id(),
                    SqlStudioAgentLease.session_id.is_not(None),
                    SqlStudioAgentLease.session_start.in_(("pending", "started", "unknown")),
                    (SqlStudioAgentLease.state != "active")
                    | (SqlStudioAgentLease.lease_expires_at <= now),
                )
                .order_by(SqlStudioAgentLease.idempotency_key)
            )
            if after:
                query = query.where(SqlStudioAgentLease.idempotency_key > after)
            if only:
                query = query.where(SqlStudioAgentLease.idempotency_key == only)
            query = query.limit(100).with_for_update()
            rows = db.scalars(query).all()
            for row in rows:
                _lease(row, now)
            return [(row.idempotency_key, row.session_id) for row in rows if row.session_id]

        return run_write_transaction(self.write, "expired_sessions", scan)

    def mark_stopped(self, key: str, session_id: str) -> None:
        def stop(db: Session) -> None:
            row = db.get(SqlStudioAgentLease, (current_workspace_id(), key), with_for_update=True)
            if row and row.session_id == session_id and row.state != "active":
                row.session_start = "stopped"

        run_write_transaction(self.write, "agent_stopped", stop)

    def require_task_pr(self, actor: str, value: rpc.Preview) -> None:
        def verify(db: Session) -> None:
            task = _task(_require_task(db, value.task_id))
            if value.model_dump(exclude={"task_id"}) not in task["pull_requests"]:
                raise HTTPException(409, "The shared task does not reference this exact head")

        self._change(actor, "require_task_pr", verify)

    def inventory(self, actor: str, page: rpc.Page, can_admin: bool = False) -> Record:
        def read(db: Session) -> Record:
            query = select(SqlStudioAgentLease).filter_by(
                workspace_id=current_workspace_id(),
                actor=actor,
                actor_generation=require_active_account(db, actor),
            )
            if page.after:
                query = query.where(SqlStudioAgentLease.idempotency_key > UUID(page.after).hex)
            leases = db.scalars(
                query.order_by(SqlStudioAgentLease.idempotency_key)
                .limit(page.limit)
                .with_for_update()
            )
            seats = db.scalars(
                select(SqlStudioSeatSlot)
                .filter_by(workspace_id=current_workspace_id())
                .order_by(SqlStudioSeatSlot.slot_id)
            )
            counts = usage_counts(db, int(self.clock())) if can_admin else {}
            return {
                "can_admin_seats": can_admin,
                "fallback_order": fallback_order() if can_admin else [],
                "leases": [_lease(row, int(self.clock())) for row in leases],
                "seats": [
                    {**_seat(row), "usage_7d": counts.get(row.slot_id, 0)}
                    if can_admin
                    else _seat(row)
                    for row in seats
                ],
            }

        return self._change(actor, "inventory", read)

    def admin_seat(self, actor: str, value: rpc.Seat) -> Record:
        def change(db: Session) -> Record:
            row = db.get(
                SqlStudioSeatSlot, (current_workspace_id(), value.slot_id), with_for_update=True
            )
            if (
                value.routing_mode == "fallback"
                and not fallback_order()
                and (row is None or row.routing_mode != "fallback")
            ):
                raise HTTPException(409, "No fallback tier is enabled")
            if row is None:
                if not value.encrypted_profile_ref:
                    raise HTTPException(422, "A new seat requires a profile reference")
                row = SqlStudioSeatSlot(
                    **value.model_dump(exclude={"routing_mode"}),
                    routing_mode=value.routing_mode or "subscription",
                    cooldown_until=0,
                )
                db.add(row)
            else:
                if row.harness != value.harness and not value.encrypted_profile_ref:
                    raise HTTPException(422, "Changing harness requires a profile reference")
                mode = value.routing_mode or row.routing_mode
                ref = value.encrypted_profile_ref or row.encrypted_profile_ref
                if (ref, mode, value.harness) != (
                    row.encrypted_profile_ref,
                    row.routing_mode,
                    row.harness,
                ):
                    row.cooldown_until = 0
                row.enabled, row.harness = value.enabled, value.harness
                if "weight" in value.model_fields_set:
                    row.weight = value.weight
                row.routing_mode, row.encrypted_profile_ref = mode, ref
            return _seat(row)

        return self._change(actor, "admin_seat", change)

    def linear(
        self,
        actor: str,
        extension: str,
        value: rpc.LinearComment | rpc.LinearIssueChange,
        action: str,
    ) -> Record:
        def bind(db: Session) -> Record:
            row = _require_task(db, value.task_id, lock=action != "previewLinear")
            result = {
                "linear_workspace": str(UUID(row.linear_workspace)),
                "linear_issue_id": str(UUID(row.linear_issue_id)),
            }
            if isinstance(value, rpc.LinearIssueChange):
                result.update(
                    change=value.change,
                    value=str(value.value),
                    body=f"Set {value.change} to {value.value}",
                )
            else:
                result["body"] = value.body
            if action == "previewLinear":
                return result
            now = int(self.clock())
            active = {
                key: entry
                for key, entry in json.loads(row.confirmations_json).items()
                if entry["expires"] > now
            }
            intent = hashlib.sha256(
                json.dumps(
                    [
                        actor,
                        require_active_account(db, actor),
                        extension,
                        str(value.request_id),
                        result,
                    ],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            if action == "confirmLinear":
                if len(active) >= 128 and intent not in active:
                    raise HTTPException(429, "Too many pending confirmations")
                token = secrets.token_urlsafe(32)
                active[intent] = {
                    "digest": hashlib.sha256(token.encode()).hexdigest(),
                    "expires": now + 180,
                }
                result = {"confirmation_token": token}
            elif isinstance(value, (rpc.ConfirmedComment, rpc.ConfirmedIssueChange)):
                digest = hashlib.sha256(value.confirmation_token.encode()).hexdigest()
                entry = active.pop(intent, None)
                if not entry or not secrets.compare_digest(entry["digest"], digest):
                    raise HTTPException(
                        403, "Human confirmation required for this exact Linear update"
                    )
            row.confirmations_json = json.dumps(active)
            return result

        return self._change(actor, "linear_confirmation", bind)
