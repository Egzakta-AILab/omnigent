import json
from types import SimpleNamespace

import pytest

from omnigent.studio.verification import fleet_metrics, score_shadow


def fleet(n=21, latency=9999, reviews=None, grants=0, stalls=0, **history):
    observations = {
        "cold_starts": 1,
        "max_cold_start_ms": 180000,
        "wakes": 1,
        "max_wake_ms": 30000,
        "workspaces_observed": 4,
        "max_bot_review": 1,
        "max_other_workspaces": 3,
        "non_admin_shell_grants": 0,
        "leases_observed": 2,
        "silent_stalls": 0,
    }
    attempts = [
        SimpleNamespace(group_id=i + 1, outcome="routed_subscription", elapsed_ms=latency)
        for i in range(n)
    ]
    attempts += [SimpleNamespace(group_id=1, outcome="quota", elapsed_ms=None)] * 2
    return fleet_metrics(
        attempts, reviews or ["verified"], grants, stalls, 1, observations | history
    )


def test_gate_counts_each_dispatch_once_across_retries():
    report = fleet()
    assert report["status"] == "verified"
    assert report["dispatches"] == 21
    assert report["throttled_dispatches"] == 1


@pytest.mark.parametrize(
    "changes,metric",
    [
        ({"n": 20}, "exhausted_or_throttled_percent"),
        ({"latency": 10000}, "failover_ms"),
        ({"max_cold_start_ms": 180001}, "cold_start_ms"),
        ({"max_wake_ms": 30001}, "wake_ms"),
        ({"max_bot_review": 2}, "bot_review_peak"),
        ({"max_other_workspaces": 4}, "other_workspace_peak"),
        ({"reviews": ["verified"] * 9 + ["env_blocked"]}, "env_blocked_review_percent"),
        ({"silent_stalls": 1}, "silent_stalled_leases"),
        ({"stalls": 1}, "silent_stalled_leases"),
        ({"grants": 1}, "non_admin_shell_grants"),
        ({"non_admin_shell_grants": 1}, "non_admin_shell_grants"),
    ],
)
def test_observed_breach_fails(changes, metric):
    report = fleet(**changes)
    assert report["metrics"][metric]["status"] == report["status"] == "check_failed"


def test_missing_telemetry_is_unverified():
    report = fleet_metrics(
        [SimpleNamespace(group_id=None, outcome="quota", elapsed_ms=None)], [None], 0, 0, 1, {}
    )
    assert {m["status"] for m in report["metrics"].values()} == {"unverified"}
    assert report["status"] == "unverified"


def sample():
    revisions, labels = [], []
    for number in range(1, 31):
        key = {
            "repository": "Egzakta-AILab/KVARK",
            "pull_number": number,
            "base_sha": "b" * 40,
            "head_sha": f"{number:040x}",
        }
        revisions.append(
            dict(**key, state="finished", stages={"raw": [], "verified": ["X"], "final": ["X"]})
        )
        labels.append(
            dict(**key, agrees=number <= 27, throttled=number == 1, findings=["X"], blockers=["X"])
        )
    return {"revisions": revisions}, labels


def score(tmp_path, data, labels):
    truth = tmp_path / "truth.json"
    truth.write_text(json.dumps(labels))
    return score_shadow(data, truth)


def test_three_stage_recall_on_thirty_real_pr_identities(tmp_path):
    data, labels = sample()
    report = score(tmp_path, data, labels)
    assert report["status"] == "verified"
    assert report["compared"] == 30 and report["agreed"] == 27
    assert [r["matched"] for r in report["recall"].values()] == [0, 30, 30]


@pytest.mark.parametrize(
    "change",
    ["agreement", "blocker", "throttle", "wrong_revision", "missing_stage", "in_progress"],
)
def test_shadow_gate_rejects_failed_or_missing_evidence(tmp_path, change):
    data, labels = sample()
    row = data["revisions"][0]
    if change == "agreement":
        labels[0]["agrees"] = False
    if change == "blocker":
        row["stages"]["final"] = []
    if change == "throttle":
        labels[1]["throttled"] = True
    if change == "wrong_revision":
        labels[0]["head_sha"] = "a" * 40
    if change == "missing_stage":
        del row["stages"]["verified"]
    if change == "in_progress":
        row["state"] = "claimed"
    expected = "check_failed" if change in {"agreement", "blocker", "throttle"} else "unverified"
    assert score(tmp_path, data, labels)["status"] == expected


def test_shadow_rejects_duplicate_votes_and_ambiguous_labels(tmp_path):
    data, labels = sample()
    with pytest.raises(ValueError, match="ambiguous"):
        score(tmp_path, data, [*labels, dict(labels[0], agrees=False)])
    data["revisions"][1]["pull_number"] = 1
    with pytest.raises(ValueError, match="multiple revisions"):
        score(tmp_path, data, labels)


def test_history_requires_complete_interval_and_workspace(tmp_path, monkeypatch):
    from omnigent.db.db_models import current_workspace_id
    from omnigent.studio.verification import _infrastructure_history

    path = tmp_path / "history.json"
    evidence = {
        "schema_version": "studio-fleet-history@1",
        "from_unix": 60,
        "through_unix": 120,
        "workspace_id": current_workspace_id(),
        "cold_starts": 1,
    }
    path.write_text(json.dumps(evidence))
    monkeypatch.setenv("OMNIGENT_STUDIO_FLEET_HISTORY", str(path))
    assert _infrastructure_history(60, 120) == {"cold_starts": 1}
    assert _infrastructure_history(60, 180) == {}
    path.write_text(json.dumps(dict(evidence, workspace_id=current_workspace_id() + 1)))
    assert _infrastructure_history(60, 120) == {}


def test_sample_selects_latest_revision_of_distinct_prs_in_workspace():
    from contextlib import nullcontext
    from uuid import uuid4

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from omnigent.db.db_models import SqlStudioReviewRevision, current_workspace_id
    from omnigent.studio.verification import shadow_report

    engine = create_engine("sqlite://")
    SqlStudioReviewRevision.__table__.create(engine)
    data, _ = sample()
    with Session(engine) as db:
        for index, (number, repository, workspace) in enumerate(
            [
                (1, "Egzakta-AILab/KVARK", current_workspace_id()),
                (1, "Egzakta-AILab/KVARK", current_workspace_id()),
                (2, "Egzakta-AILab/KVARK", current_workspace_id()),
                (3, "Egzakta-AILab/Services", current_workspace_id()),
                (4, "Egzakta-AILab/KVARK", current_workspace_id() + 1),
            ]
        ):
            capture = dict(data["revisions"][index], pull_number=number)
            db.add(
                SqlStudioReviewRevision(
                    id=uuid4().hex,
                    workspace_id=workspace,
                    repository=repository,
                    created_at=1000000 + index,
                    state="finished",
                    mode="full",
                    decisions_json="[]",
                    capture_json=json.dumps(capture),
                )
            )
        db.flush()
        report = shadow_report(
            SimpleNamespace(clock=lambda: 60, read=lambda _: nullcontext(db)), "7d"
        )
    assert [(r["pull_number"], r["head_sha"]) for r in report["revisions"]] == [
        (2, data["revisions"][2]["head_sha"]),
        (1, data["revisions"][1]["head_sha"]),
    ]
