"""Tenant-scoped automated reviews. The worker can freeze one result per revision."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import SqlStudioReviewRevision as Revision
from omnigent.db.db_models import SqlStudioTask, SqlUser, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.studio.reviews.automated import cite
from omnigent.studio.reviews.product_pack import Trigger
from omnigent.studio.rpc import Page

Record = dict[str, Any]


class Reference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_id: UUID


class Decision(Reference):
    proof_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    lens: str = Field(max_length=64)
    path: str = Field(max_length=1024)
    line: int = Field(strict=True, gt=0)
    reason: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class FrozenQuestion(Reference):
    question: str = Field(min_length=1, max_length=2000, pattern=r"\S")


def _load(db: Session, rid: UUID, lock: bool = False) -> Revision:
    row = db.get(Revision, (current_workspace_id(), rid.hex), with_for_update=lock)
    if row is None:
        raise HTTPException(404, "Review revision unavailable")
    return row


def _roles(db: Session, actor: str, capture: Record) -> set[str]:
    require_active_account(db, actor)
    user = db.get(SqlUser, (current_workspace_id(), actor))
    if user is None:
        return set()
    return {
        role
        for role, grants in capture["access"].items()
        if actor in grants and grants[actor] == user.account_generation
    }


def _view(row: Revision, roles: set[str], *, details: bool) -> Record:
    capture = json.loads(row.capture_json)
    result = json.loads(row.result_json) if row.result_json else {}
    summary = {
        key: capture[key]
        for key in (
            "repository",
            "pull_number",
            "base_sha",
            "head_sha",
            "selected_lenses",
            "protected_paths",
        )
    }
    summary.update(
        review_id=str(UUID(row.id)),
        mode=row.mode,
        state=row.state,
        lane=result.get("lane", "human_review"),
    )
    if details:
        summary.update(
            walkthrough=result.get("walkthrough", []),
            proofs=result.get("proofs", []),
            dispositions=json.loads(row.decisions_json),
            services_revision=capture["services_revision"],
            can_decide=bool(roles & {"author", "asked", "lead"}),
            can_ask=bool(roles & {"author", "asked", "lead"}),
            evidence_status=result.get("status", "unverified"),
            shadow=result.get("shadow"),
        )
        for proof in summary["proofs"]:
            proof["allowed_actions"] = []
            if roles & {"author", "asked", "lead"}:
                proof["allowed_actions"].append("dispute")
            if "lead" in roles or ("asked" in roles and proof["severity"] != "blocks_merge"):
                proof["allowed_actions"].append("dismiss")
    return summary


class ReviewQueue:
    def __init__(self, uri: str, clock: Callable[[], float] = time.time):
        engine = get_or_create_engine(uri)
        self.read = make_named_managed_session_maker(
            engine, query_name_prefix="studio.review_queue"
        )
        self.write = make_named_managed_session_maker(
            engine, query_name_prefix="studio.review_queue", immediate=True
        )
        self.clock = clock

    def enqueue(
        self,
        actor: str,
        request: Trigger,
        capture: Record,
        accounts: dict[str, str],
        leads: list[str],
    ) -> Record:
        keys = ("repository", "pull_number", "base_sha", "head_sha")
        identity = request.model_dump(include=set(keys))
        rid = uuid5(
            NAMESPACE_URL,
            json.dumps(
                {**identity, "base_ref": request.base_ref}, separators=(",", ":"), sort_keys=True
            ),
        )

        def insert(db: Session) -> Record:
            require_active_account(db, actor)
            row = db.get(Revision, (current_workspace_id(), rid.hex), with_for_update=True)
            created = row is None
            if row is None:
                author = accounts.get(capture["author_login"].lower())
                asked = {accounts.get(x.lower()) for x in capture["asked_logins"]} - {None, author}
                grants: Record = {}
                for role, ids in (("author", [author]), ("asked", asked), ("lead", leads)):
                    grants[role] = {}
                    for account in ids:
                        if account and (
                            user := db.get(SqlUser, (current_workspace_id(), account))
                        ):
                            grants[role][user.id] = user.account_generation
                links_query = select(SqlStudioTask.links_json).where(
                    SqlStudioTask.workspace_id == current_workspace_id(),
                    SqlStudioTask.links_json.contains(request.head_sha),
                )
                linked = any(
                    pr.get("repository", "").casefold() == request.repository.casefold()
                    and pr.get("number") == request.pull_number
                    and pr.get("head_sha") == request.head_sha
                    for value in db.scalars(links_query)
                    for pr in json.loads(value).get("pull_requests", [])
                )
                row = Revision(
                    id=rid.hex,
                    repository=request.repository,
                    created_at=int(self.clock() * 1000000),
                    state="queued",
                    mode=request.mode,
                    decisions_json="[]",
                    capture_json=json.dumps(
                        {
                            **capture,
                            **request.model_dump(),
                            "access": grants,
                            "linear_linked": linked,
                        }
                    ),
                )
                db.add(row)
            elif request.mode == "full":
                # Each worker runs every selected lens at the exact revision. A queued delta
                # already includes that coverage, so upgrading the receipt never loses work.
                row.mode = "full"
            return {
                **identity,
                "review_id": str(rid),
                "mode": row.mode,
                "status": "queued" if created else "existing",
            }

        try:
            return run_write_transaction(self.write, "enqueue", insert)
        except IntegrityError:
            return run_write_transaction(self.write, "enqueue", insert)

    def claim(self, repository: str) -> tuple[str, Record] | None:
        def claim_row(db: Session) -> tuple[str, Record] | None:
            query = select(Revision).filter_by(
                workspace_id=current_workspace_id(), repository=repository
            )
            running = db.scalar(query.filter_by(active_slot=1).with_for_update())
            now = int(self.clock())
            if running is not None:
                if running.expires_at is None or running.expires_at > now:
                    return None
                running.state, running.active_slot = "unknown", None
                db.flush()
            row = db.scalar(
                query.filter_by(state="queued")
                .order_by(Revision.created_at, Revision.id)
                .limit(1)
                .with_for_update()
            )
            if row is None:
                return None
            row.state, row.active_slot, row.expires_at = "running", 1, now + 1200
            return row.id, json.loads(row.capture_json)

        try:
            return run_write_transaction(self.write, "claim", claim_row)
        except IntegrityError:
            return None

    def finish(self, rid: str, result: Record) -> None:
        def freeze(db: Session) -> None:
            row = _load(db, UUID(rid), True)
            if row.state != "running":
                return
            if row.expires_at is not None and row.expires_at <= int(self.clock()):
                row.state = "unknown"
            else:
                row.state, row.result_json = "finished", json.dumps(result)
            row.active_slot = None

        run_write_transaction(self.write, "finish", freeze)

    def rpc(self, actor: str, method: str, params: Record) -> Record:
        if method == "listQueue":
            page = Page.model_validate(params)
            with self.read("list") as db:
                require_active_account(db, actor)
                query = select(Revision).filter_by(workspace_id=current_workspace_id())
                if page.after:
                    query = query.where(Revision.id > UUID(page.after).hex)
                rows = list(db.scalars(query.order_by(Revision.id).limit(page.limit + 1)))
                return {
                    "items": [_view(row, set(), details=False) for row in rows[: page.limit]],
                    "next_cursor": str(UUID(rows[page.limit - 1].id))
                    if len(rows) > page.limit
                    else None,
                }
        if method in {"dispute", "dismiss"}:
            decision = Decision.model_validate(params)

            def decide(db: Session) -> Record:
                row = _load(db, decision.review_id, True)
                roles = _roles(db, actor, json.loads(row.capture_json))
                view = _view(row, roles, details=True)
                proof = next(
                    (
                        item
                        for item in view["proofs"]
                        if item["id"] == decision.proof_id
                        and (item["lens"], item["path"], item["line"])
                        == (decision.lens, decision.path, decision.line)
                    ),
                    None,
                )
                if not proof:
                    raise HTTPException(404, "Frozen finding unavailable")
                allowed = (
                    {"author", "asked", "lead"}
                    if method == "dispute"
                    else ({"lead"} if proof["severity"] == "blocks_merge" else {"asked", "lead"})
                )
                if not roles & allowed:
                    raise HTTPException(
                        403, "Finding decision requires the assigned reviewer or lead"
                    )
                entry = {
                    **decision.model_dump(mode="json", exclude={"review_id"}),
                    "action": method,
                    "actor": actor,
                }
                if entry not in view["dispositions"]:
                    row.decisions_json = json.dumps([*view["dispositions"], entry])
                return _view(row, roles, details=True)

            return run_write_transaction(self.write, "decide", decide)
        form = (
            FrozenQuestion.model_validate(params)
            if method == "askFrozen"
            else Reference.model_validate(params)
        )
        with self.read("detail") as db:
            row = _load(db, form.review_id)
            roles = _roles(db, actor, json.loads(row.capture_json))
            view = _view(row, roles, details=True)
            if isinstance(form, FrozenQuestion):
                if not view["can_ask"]:
                    raise HTTPException(403, "Frozen questions require review participation")
                return frozen_answer(view, form.question, json.loads(row.capture_json))
            return view


def frozen_answer(view: Record, question: str, capture: Record) -> Record:
    """Retrieve only captured PR lines and verified review material. No host/session is opened."""
    words = set(re.findall(r"[a-z0-9_]{3,}", question.casefold()))
    evidence = [*view["proofs"], *view["walkthrough"]]
    for item in capture["files"]:
        line = 0
        for chunk in (item.get("patch") or "").splitlines():
            if match := re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", chunk):
                line = int(match.group(1))
            elif line and chunk.startswith((" ", "+")):
                if chunk.startswith("+"):
                    evidence.append({"text": chunk[1:], "path": item["filename"], "line": line})
                line += 1

    def score(item: Record) -> int:
        return len(
            words & set(re.findall(r"[a-z0-9_]+", (item["path"] + " " + item["text"]).casefold()))
        )

    matches = sorted((e for e in evidence if score(e)), key=score, reverse=True)[:4]
    return {
        "answer": "\n".join(e["text"] for e in matches)
        or "The frozen evidence does not answer this question.",
        "citations": [
            {
                "path": e["path"],
                "line": e["line"],
                "side": e.get("side", "head"),
                "url": cite(
                    capture["repository"],
                    capture[e.get("side", "head") + "_sha"],
                    e["path"],
                    e["line"],
                ),
            }
            for e in matches
        ],
        "head_sha": view["head_sha"],
    }
