"""Read-only Studio release gates over authenticated, revision-bound evidence."""

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
from fastapi import Request
from sqlalchemy import func, select

from omnigent.db.db_models import (
    SqlHost,
    SqlStudioDispatch,
    SqlStudioReviewRevision,
    current_workspace_id,
)
from omnigent.stores.studio_tasks import StudioTaskStore
from omnigent.studio.pooled_hosts import PROVIDER, pooled_target

SHA = re.compile("[a-f0-9]{40}")


def window_seconds(window: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]?d", window):
        raise ValueError("Use a window of 1d to 99d")
    return int(window[:-1]) * 86400


def _metric(
    value: int | float | None, limit: int | float, *, strict: bool = False, complete: bool = True
) -> dict[str, Any]:
    failed = value is not None and (value >= limit if strict else value > limit)
    return {
        "value": value,
        "limit": limit,
        "comparison": "<" if strict else "<=",
        "status": "check_failed"
        if failed
        else "verified"
        if complete and value is not None
        else "unverified",
    }


def _checked_count(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Expected a non-negative integer observation")
    return value


def _infrastructure_history(start: int, end: int) -> dict[str, Any]:
    """Read the operator's tenant-bound Coder/authorization audit export at minute resolution."""
    source = os.getenv("OMNIGENT_STUDIO_FLEET_HISTORY")
    if not source:
        return {}
    data = json.loads(Path(source).read_text())
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != "studio-fleet-history@1"
        or type(data.get("workspace_id")) is not int
        or data["workspace_id"] != current_workspace_id()
        or data.get("from_unix") != start
        or data.get("through_unix") != end
    ):
        return {}
    keys = (
        "cold_starts",
        "max_cold_start_ms",
        "wakes",
        "max_wake_ms",
        "max_bot_review",
        "max_other_workspaces",
        "workspaces_observed",
        "non_admin_shell_grants",
        "silent_stalls",
        "leases_observed",
    )
    return {key: _checked_count(data[key]) for key in keys if data.get(key) is not None}


def fleet_metrics(
    attempts: list[Any],
    review_statuses: list[str | None],
    current_grants: int,
    current_stalls: int,
    pooled: int,
    history: dict[str, Any],
) -> dict[str, Any]:
    groups: dict[str, list[Any]] = {}
    complete = True
    for row in attempts:
        if not row.group_id:
            complete = False
        else:
            groups.setdefault(row.group_id, []).append(row)
    throttled = 0
    latencies = []
    failovers_complete = True
    for rows in groups.values():
        outcomes = {r.outcome for r in rows}
        ends = [r for r in rows if r.outcome.startswith("routed_")]
        if len(ends) != 1 or "routed_unknown" in outcomes:
            complete = False
        if outcomes & {"quota", "exhausted", "routed_exhausted"}:
            throttled += 1
        if "quota" in outcomes:
            if (
                len(ends) != 1
                or ends[0].outcome not in {"routed_subscription", "routed_api", "routed_local"}
                or type(ends[0].elapsed_ms) is not int
                or ends[0].elapsed_ms < 0
            ):
                failovers_complete = False
            else:
                latencies.append(ends[0].elapsed_ms)
    # Percentages with an incomplete denominator cannot establish either side of the gate.
    percent = 100 * throttled / len(groups) if groups and complete else None
    reviews_complete = all(
        status in {"verified", "env_blocked", "check_failed", "unverified"}
        for status in review_statuses
    )
    blocked_percent = (
        100 * review_statuses.count("env_blocked") / len(review_statuses)
        if review_statuses and reviews_complete
        else None
    )
    shell = max(current_grants, history.get("non_admin_shell_grants", 0))
    stalls = max(current_stalls, history.get("silent_stalls", 0))
    measured_capacity = bool(history.get("workspaces_observed"))
    metrics = {
        "cold_start_ms": _metric(
            history.get("max_cold_start_ms"), 180000, complete=bool(history.get("cold_starts"))
        ),
        "wake_ms": _metric(history.get("max_wake_ms"), 30000, complete=bool(history.get("wakes"))),
        "bot_review_peak": _metric(history.get("max_bot_review"), 1, complete=measured_capacity),
        "other_workspace_peak": _metric(
            history.get("max_other_workspaces"), 3, complete=measured_capacity
        ),
        "env_blocked_review_percent": _metric(blocked_percent, 10, strict=True),
        "silent_stalled_leases": _metric(
            stalls, 0, complete=bool(history.get("leases_observed")) and "silent_stalls" in history
        ),
        "exhausted_or_throttled_percent": _metric(percent, 5, strict=True),
        "failover_ms": _metric(
            max(latencies, default=None),
            10000,
            strict=True,
            complete=complete and failovers_complete,
        ),
        "non_admin_shell_grants": _metric(
            shell, 0, complete=pooled > 0 and "non_admin_shell_grants" in history
        ),
    }
    statuses = {entry["status"] for entry in metrics.values()}
    status = (
        "check_failed"
        if "check_failed" in statuses
        else "unverified"
        if "unverified" in statuses
        else "verified"
    )
    return {
        "status": status,
        "metrics": metrics,
        "dispatches": len(groups),
        "throttled_dispatches": throttled,
        "reviews": len(review_statuses),
        "pooled_hosts": pooled,
        "non_admin_shell_grants": shell,
        "outcomes": dict(Counter(r.outcome for r in attempts)),
    }


def fleet_report(
    request: Request, tasks: StudioTaskStore, window: str, conversations: Any, permissions: Any
) -> dict[str, Any]:
    from omnigent.db.db_models import SqlStudioAgentLease
    from omnigent.server.auth import LEVEL_OWNER

    now = int(tasks.clock())
    end = now - now % 60
    start = end - window_seconds(window)
    with tasks.read("fleet_report") as db:
        org = current_workspace_id()
        dispatches = db.execute(
            select(
                SqlStudioDispatch.group_id, SqlStudioDispatch.outcome, SqlStudioDispatch.elapsed_ms
            ).where(
                SqlStudioDispatch.workspace_id == org,
                SqlStudioDispatch.started_at >= start,
                SqlStudioDispatch.started_at < end,
            )
        ).all()
        review_statuses = [
            json.loads(raw or "{}").get("status")
            for raw in db.scalars(
                select(SqlStudioReviewRevision.result_json).where(
                    SqlStudioReviewRevision.workspace_id == org,
                    SqlStudioReviewRevision.created_at >= start * 1_000_000,
                    SqlStudioReviewRevision.created_at < end * 1_000_000,
                )
            )
        ]
        stalls = (
            db.scalar(
                select(func.count())
                .select_from(SqlStudioAgentLease)
                .where(
                    SqlStudioAgentLease.workspace_id == org,
                    SqlStudioAgentLease.state == "active",
                    SqlStudioAgentLease.lease_expires_at <= now,
                )
            )
            or 0
        )
        hosts = list(
            db.scalars(
                select(SqlHost.host_id).filter_by(
                    workspace_id=org, sandbox_provider=PROVIDER, deleted_at=None
                )
            )
        )
    pooled = grants = 0
    for host in hosts:
        if not pooled_target(conversations, request.app.state.host_store, None, host):
            continue
        pooled += 1
        after = None
        while True:
            ids = conversations.list_host_conversation_ids(host, after=after, limit=500)
            for group in permissions.list_for_sessions(ids).values():
                # Interactive terminal attach requires ownership. Reading a transcript is separate.
                grants += sum(
                    1
                    for g in group
                    if g.level >= LEVEL_OWNER and not permissions.is_admin(g.user_id)
                )
            if len(ids) < 500:
                break
            after = ids[-1]
    try:
        history = _infrastructure_history(start, end)
    except (OSError, ValueError, TypeError, KeyError):
        history = {}
    return {
        "window": window,
        "from_unix": start,
        "through_unix": end,
        **fleet_metrics(list(dispatches), review_statuses, grants, stalls, pooled, history),
    }


def shadow_report(tasks: StudioTaskStore, window: str) -> dict[str, Any]:
    now = int(tasks.clock() * 1_000_000)
    since = now - window_seconds(window) * 1_000_000
    # Pick the latest revision of each distinct KVARK PR first. Label availability never
    # influences the sample. Never award two votes for two pushes to the same PR.
    revisions = []
    seen = set()
    with tasks.read("shadow_report") as db:
        query = (
            select(SqlStudioReviewRevision)
            .where(
                SqlStudioReviewRevision.workspace_id == current_workspace_id(),
                SqlStudioReviewRevision.repository == "Egzakta-AILab/KVARK",
                SqlStudioReviewRevision.created_at.between(since, now),
            )
            .order_by(SqlStudioReviewRevision.created_at.desc(), SqlStudioReviewRevision.id.desc())
            .execution_options(yield_per=10)
        )
        for row in db.scalars(query):
            capture = json.loads(row.capture_json)
            number = capture["pull_number"]
            if type(number) is not int or number <= 0:
                raise ValueError("Invalid captured pull request number")
            if number in seen:
                continue
            seen.add(number)
            result = json.loads(row.result_json or "{}")
            revisions.append(
                {
                    "repository": row.repository,
                    "pull_number": number,
                    "head_sha": capture["head_sha"],
                    "base_sha": capture["base_sha"],
                    "review_id": row.id,
                    "stages": result.get("shadow"),
                    "state": row.state,
                    "status": result.get("status", "unverified"),
                }
            )
            if len(revisions) == 30:
                break
    return {"window": window, "revisions": revisions}


def _pr_revision(item: dict[str, Any]) -> tuple[str, int, str, str]:
    number = item["pull_number"]
    if (
        item.get("repository") != "Egzakta-AILab/KVARK"
        or type(number) is not int
        or number <= 0
        or any(
            not isinstance(item.get(k), str) or not SHA.fullmatch(item[k])
            for k in ("base_sha", "head_sha")
        )
    ):
        raise ValueError("Invalid KVARK pull request revision")
    return item["repository"], number, item["base_sha"], item["head_sha"]


def _finding_set(value: Any) -> set[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(x, str) or not x.strip() for x in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("Invalid or duplicate finding reference")
    return set(value)


def score_shadow(data: dict[str, Any], truth_file: Path | None) -> dict[str, Any]:
    stages = ("raw", "verified", "final")
    revisions = data["revisions"]
    if not truth_file:
        return {
            "status": "unverified",
            "reason": "Human comparison evidence is required",
            "revisions": len(revisions),
        }
    truth = json.loads(truth_file.read_text())
    if not isinstance(truth, list):
        raise ValueError("Expected a list of human observations")
    by_revision = {}
    for item in truth:
        key = _pr_revision(item)
        if (
            key in by_revision
            or type(item.get("agrees")) is not bool
            or type(item.get("throttled")) is not bool
        ):
            raise ValueError("Duplicate, incomplete or ambiguous human review label")
        expected = _finding_set(item["findings"])
        blockers = _finding_set(item["blockers"])
        if not blockers <= expected:
            raise ValueError("Blockers must reference labeled findings")
        by_revision[key] = (item, expected, blockers)
    seen = set()
    totals: Counter[str] = Counter()
    missed = []
    missing = []
    for row in revisions:
        key = _pr_revision(row)
        if key[:2] in seen:
            raise ValueError("The sample contains multiple revisions of one pull request")
        seen.add(key[:2])
        labels = by_revision.get(key)
        if (
            labels is None
            or row.get("state") != "finished"
            or not isinstance(row.get("stages"), dict)
        ):
            missing.append(key[1])
            continue
        evidence = row["stages"]
        if set(stages) - evidence.keys():
            missing.append(key[1])
            continue
        item, expected, blockers = labels
        totals["compared"] += 1
        totals["agreed"] += int(item["agrees"])
        totals["throttled"] += int(item["throttled"])
        totals["findings"] += len(expected)
        for stage in stages:
            observed = _finding_set(evidence[stage])
            totals[stage] += len(observed & expected)
            if stage == "final":
                missed.extend(
                    {"pull_number": key[1], "head_sha": key[3], "finding": x}
                    for x in sorted(blockers - observed)
                )
    n = totals["compared"]
    status = "verified" if n == 30 and not missing and totals["findings"] else "unverified"
    if missed or (n == 30 and (totals["agreed"] < 27 or totals["throttled"] / n >= 0.05)):
        status = "check_failed"
    return {
        "status": status,
        "compared": n,
        "agreed": totals["agreed"],
        "throttled": totals["throttled"],
        "findings": totals["findings"],
        "missed_blockers": len(missed),
        "misses": missed,
        "missing_prs": missing,
        "recall": {
            stage: {
                "matched": totals[stage],
                "expected": totals["findings"],
                "fraction": totals[stage] / totals["findings"] if totals["findings"] else None,
            }
            for stage in stages
        },
    }


def release_report(data: dict[str, Any], candidate: str) -> dict[str, Any]:
    head = data.get("head_sha")
    cases = data.get("scenarios") or []
    pinned = {
        c["id"]
        for c in cases
        if c.get("id")
        and c.get("status") == "passed"
        and c.get("evidence")
        and all(item.get("commit_sha") == head for item in c["evidence"])
    }
    ready = bool(
        isinstance(head, str)
        and SHA.fullmatch(head)
        and SHA.fullmatch(data.get("services_revision") or "")
        and data.get("candidate_id") == candidate
        and len(cases) == len(pinned) == 88
        and data.get("signed_off") is True
    )
    failed = any(c.get("status") == "failed" for c in cases)
    return {
        "candidate_id": candidate,
        "head_sha": head,
        "pinned_passes": len(pinned),
        "status": "verified" if ready else "check_failed" if failed else "unverified",
    }


def run_gate(args: Any) -> int:
    from omnigent.studio.setup import _headers, validate_server_url

    try:
        url = validate_server_url(args.server or os.getenv("OMNIGENT_SERVER_URL", ""), "server")
        if args.command == "release":
            route = "egzakta.release/studio/v1/releases/getCandidate"
            params = {"candidate_id": args.candidate}
        else:
            window_seconds(args.window)
            route = "egzakta.fleet/studio/v1/fleet/" + (
                "report" if args.command == "fleet" else "shadowReport"
            )
            params = {"window": args.window}
        with httpx.Client(
            base_url=url, headers=_headers(url, None), timeout=15, trust_env=False
        ) as client:
            response = client.post("v1/extensions/" + route, json=params)
            response.raise_for_status()
            data = response.json()
        report = (
            release_report(data, args.candidate)
            if args.command == "release"
            else score_shadow(data, args.truth)
            if args.command == "shadow-report"
            else data
        )
    except (ValueError, OSError, KeyError, TypeError, httpx.HTTPError):
        report = {
            "status": "env_blocked",
            "reason": "Authenticated server or comparison evidence unavailable",
        }
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("status") == "verified" else 1
