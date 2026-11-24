"""Server-owned seat selection. Only the trusted launch path receives profile references."""

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from time import monotonic, time
from typing import TypeVar
from uuid import uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlStudioApiBudget,
    SqlStudioDispatch,
    SqlStudioReviewPass,
    SqlStudioSeatSlot,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)

T = TypeVar("T")
USAGE_WINDOW = 7 * 86400


def usage_counts(db: Session, now: int) -> dict[str | None, int]:
    org = current_workspace_id()
    return dict(
        db.execute(
            select(SqlStudioDispatch.slot_id, func.count())
            .where(
                SqlStudioDispatch.workspace_id == org,
                SqlStudioDispatch.started_at > now - USAGE_WINDOW,
                SqlStudioDispatch.started_at <= now,
                SqlStudioDispatch.outcome.in_(("reserved", "admitted", "unknown")),
            )
            .group_by(SqlStudioDispatch.slot_id)
        )
        .tuples()
        .all()
    )


def fallback_order() -> list[str]:
    return [
        tier
        for tier, setting in (
            ("api", "OMNIGENT_STUDIO_API_FALLBACK"),
            ("on_prem", "OMNIGENT_STUDIO_LOCAL_FALLBACK"),
        )
        if os.getenv(setting) == "1"
    ]


class PoolExhausted(Exception):
    """No enabled subscription profile is available for this harness."""


class AdmissionUnavailable(RuntimeError):
    pass


class SeatQuota(Exception):
    """The provider explicitly rejected admission before executing any work."""

    def __init__(self, retry_after: int = 60) -> None:
        super().__init__("Seat quota exhausted")
        self.retry_after = max(1, min(retry_after, 86400))


@dataclass(frozen=True)
class Reservation:
    attempt_id: str
    slot_id: str
    profile_ref: str = field(repr=False)
    routing_mode: str = "subscription"


class SeatPool:
    def __init__(self, uri: str, clock: Callable[[], float] = time) -> None:
        self.clock = clock
        self.write = make_named_managed_session_maker(
            get_or_create_engine(uri), query_name_prefix="studio.seats", immediate=True
        )

    def reserve(
        self, harness: str, group_id: str | None = None, *, excluded: frozenset[str] = frozenset()
    ) -> Reservation:
        def claim(db: Session) -> Reservation | None:
            org, now = current_workspace_id(), int(self.clock())
            slots = db.scalars(
                select(SqlStudioSeatSlot)
                .filter_by(
                    workspace_id=org,
                    harness=harness,
                    enabled=True,
                )
                .order_by(SqlStudioSeatSlot.slot_id)
                .with_for_update()
            ).all()
            eligible = [
                slot
                for slot in slots
                if slot.cooldown_until <= now and slot.slot_id not in excluded
            ]
            counts = usage_counts(db, now)
            slot = min(
                eligible,
                key=lambda s: (Fraction(counts.get(s.slot_id, 0), s.weight), s.slot_id),
                default=None,
            )
            attempt_id = uuid4().hex
            db.add(
                SqlStudioDispatch(
                    id=attempt_id,
                    group_id=group_id,
                    harness=harness,
                    started_at=now,
                    slot_id=slot.slot_id if slot else None,
                    outcome="reserved" if slot else "exhausted",
                )
            )
            return (
                Reservation(
                    attempt_id, slot.slot_id, slot.encrypted_profile_ref, slot.routing_mode
                )
                if slot
                else None
            )

        seat = run_write_transaction(self.write, "reserve", claim)
        if seat is None:
            raise PoolExhausted
        return seat

    def finish(self, seat: Reservation, outcome: str, cooldown: int = 0) -> None:
        def record(db: Session) -> None:
            org = current_workspace_id()
            if cooldown:
                slot = db.get(SqlStudioSeatSlot, (org, seat.slot_id), with_for_update=True)
                if slot and (slot.encrypted_profile_ref, slot.routing_mode) == (
                    seat.profile_ref,
                    seat.routing_mode,
                ):
                    slot.cooldown_until = max(slot.cooldown_until, int(self.clock()) + cooldown)
            attempt = db.get(SqlStudioDispatch, (org, seat.attempt_id), with_for_update=True)
            if attempt is None or attempt.outcome != "reserved":
                raise ValueError("Invalid dispatch reservation")
            attempt.outcome = outcome

        run_write_transaction(self.write, "finish", record)

    def dispatch(
        self, harness: str, admit: Callable[[Reservation], T], group_id: str | None = None
    ) -> T:
        deadline = monotonic() + 9.0
        skipped: set[str] = set()
        while monotonic() < deadline:
            seat = self.reserve(harness, group_id, excluded=frozenset(skipped))
            try:
                result = admit(seat)
            except PoolExhausted:
                skipped.add(seat.slot_id)
                self.finish(seat, "unavailable")
                continue
            except SeatQuota as quota:
                self.finish(seat, "quota", quota.retry_after)
                continue
            except Exception:
                # An ambiguous failure may already have started work. Never replay it.
                self.finish(seat, "unknown")
                raise
            self.finish(seat, "admitted")
            return result
        raise PoolExhausted

    def reserve_paid(self, session_id: str, requested: int = 100) -> tuple[str, int] | None:
        """Serialize month and lifetime PR caps in cents before minting a capped key.

        A failed or ambiguous mint holds its allocation. Accounting is deliberately
        conservative across retries, process restarts and UTC month rollover.
        """

        def reserve(db: Session) -> tuple[str, int] | None:
            org = current_workspace_id()
            review = db.get(SqlStudioReviewPass, (org, session_id))
            if not review or review.reviewer_session_id != session_id:
                return None
            target = json.loads(review.target_json)
            pr = f"{target['repository'].casefold()}#{int(target['pr_number'])}"
            month = datetime.fromtimestamp(self.clock(), UTC).strftime("%Y-%m")
            keys = [f"month:{month}", "pr:" + hashlib.sha256(pr.encode()).hexdigest()]
            buckets = []
            for key in keys:
                row = db.get(SqlStudioApiBudget, (org, key), with_for_update=True)
                if row is None:
                    row = SqlStudioApiBudget(budget_key=key, reserved_cents=0)
                    db.add(row)
                    db.flush()
                buckets.append(row)
            monthly, pull = buckets
            cents = min(requested, 25000 - monthly.reserved_cents, 300 - pull.reserved_cents)
            if cents <= 0:
                return None
            monthly.reserved_cents += cents
            pull.reserved_cents += cents
            return pr, cents

        try:
            return run_write_transaction(self.write, "reserve_paid", reserve)
        except IntegrityError:
            return run_write_transaction(self.write, "reserve_paid", reserve)

    def launch_profile(self, harness: str, session_id: str, client: httpx.Client) -> str:
        group_id = uuid4().hex
        start = monotonic()
        outcome = "unknown"
        try:
            profile, outcome = self._route(harness, session_id, client, group_id)
            return profile
        except PoolExhausted:
            outcome = "exhausted"
            raise
        finally:

            def record(db: Session) -> None:
                db.add(
                    SqlStudioDispatch(
                        id=uuid4().hex,
                        group_id=group_id,
                        started_at=int(self.clock()),
                        harness=harness,
                        elapsed_ms=int((monotonic() - start) * 1000),
                        outcome=f"routed_{outcome}",
                    )
                )

            run_write_transaction(self.write, "route_result", record)

    def _route(
        self, harness: str, session_id: str, client: httpx.Client, group_id: str
    ) -> tuple[str, str]:
        """Admit a subscription, then a hard-capped API key, then on-prem inference."""

        attempted_fallback = False

        def admit(seat: Reservation) -> tuple[str, str]:
            nonlocal attempted_fallback
            if seat.routing_mode == "fallback":
                if attempted_fallback:
                    raise PoolExhausted
                attempted_fallback = True
                try:
                    return self._fallback(harness, session_id, client)
                except (AdmissionUnavailable, httpx.TransportError) as exc:
                    raise PoolExhausted from exc
            if seat.routing_mode != "subscription":
                raise AdmissionUnavailable("Unknown seat routing mode")
            return admission_profile(
                client, harness, session_id, "subscription", {"reference": seat.profile_ref}
            ), "subscription"

        try:
            return self.dispatch(harness, admit, group_id)
        except PoolExhausted:
            if attempted_fallback:
                raise
            return self._fallback(harness, session_id, client)

    def _fallback(self, harness: str, session_id: str, client: httpx.Client) -> tuple[str, str]:
        order = fallback_order()
        if "api" in order and (allocation := self.reserve_paid(session_id)):
            pr, cents = allocation
            try:
                return admission_profile(
                    client,
                    harness,
                    session_id,
                    "api",
                    {"pull_request": pr, "max_cents": cents, "lease_id": uuid4().hex},
                ), "api"
            except (SeatQuota, AdmissionUnavailable, httpx.TransportError):
                pass
        if "on_prem" in order:
            return admission_profile(client, harness, session_id, "on_prem", {}), "local"
        raise PoolExhausted


def admission_profile(
    client: httpx.Client, harness: str, session_id: str, mode: str, params: dict
) -> str:
    """Resolve a mounted per-run profile through the deployment-owned credential agent.

    Paid profiles must attest that the credential itself enforces the allocated
    monetary limit. Plain API credentials, shared keys and advisory usage data
    cannot satisfy this contract.
    """
    response = client.post(
        "http://localhost/v1/admit",
        json={
            **params,
            "workspace_id": current_workspace_id(),
            "mode": mode,
            "harness": harness,
            "session_id": session_id,
        },
    )
    if response.status_code == 429:
        retry = response.headers.get("Retry-After", "")
        raise SeatQuota(int(retry) if retry.isdecimal() and len(retry) <= 5 else 60)
    if response.status_code != 200:
        raise AdmissionUnavailable("Studio seat admission failed")
    result = response.json()
    handle = result.get("profile")
    if not isinstance(handle, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", handle):
        raise RuntimeError("Invalid Studio seat profile")
    if mode == "api" and (
        result.get("lease_id") != params["lease_id"]
        or result.get("hard_limit_cents") != params["max_cents"]
        or result.get("isolated_credential") is not True
    ):
        raise AdmissionUnavailable("A hard-capped isolated API credential is required")
    if mode == "on_prem" and result.get("provider") not in ("vllm", "sparkle"):
        raise RuntimeError("An on-prem inference profile is required")
    return handle


def assigned_profile(uri: str, harness: str, session_id: str) -> str:
    socket = os.environ.get("OMNIGENT_STUDIO_SEAT_SOCKET")
    if not socket or not Path(socket).is_absolute():
        raise RuntimeError("Studio seat credential service is unavailable")
    with httpx.Client(
        transport=httpx.HTTPTransport(uds=socket), timeout=1.5, trust_env=False
    ) as client:
        return SeatPool(uri).launch_profile(harness, session_id, client)


def profile_environment(handle: str, harness: str | None) -> dict[str, str]:
    """Resolve an opaque mount below an operator-selected, private host directory."""
    root = os.environ.get("OMNIGENT_STUDIO_SEAT_ROOT")
    if (
        not root
        or not Path(root).is_absolute()
        or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", handle)
    ):
        raise ValueError("Studio seat mount is unavailable")
    key = {"codex-native": "CODEX_HOME", "claude-native": "CLAUDE_CONFIG_DIR"}.get(harness or "")
    parent = Path(root).resolve(strict=True)
    profile = (parent / handle).resolve(strict=True)
    if (
        not key
        or profile.parent != parent
        or not profile.is_dir()
        or profile.stat().st_mode & 0o077
    ):
        raise ValueError("Studio seat mount is unavailable")
    return {key: str(profile)}
