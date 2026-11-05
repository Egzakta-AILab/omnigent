"""The release matrix keeps absent, mismatched and recorded evidence separate."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.studio import release_a

PR = "https://github.com/example/primary/pull/1"
CHECKOUT = "a" * 40
BASE = "b" * 40
HEAD = "c" * 40
RUN = "1" * 32
REVIEW = "2" * 32
QUESTION = "3" * 32
HOST = "4" * 32


def _lane(server: str) -> dict:
    return {
        "server": server,
        "host_id": HOST,
        "workspace": "/workspace",
        "run_id": RUN,
        "review_id": REVIEW,
        "finding_key": "d" * 64,
        "question_id": QUESTION,
        "candidate_id": "5" * 32,
        "base_sha": BASE,
        "head_sha": HEAD,
    }


def test_unconfigured_matrix_is_incomplete() -> None:
    report = release_a.collect_release_a(pr_url=PR, second_repo="/second", manifest=None)
    assert report["release_ready"] is False
    assert set(report["routes"]) == set(release_a.ROUTES)
    assert {route["status"] for route in report["routes"].values()} == {"missing"}
    assert set(report["cross_route_checks"].values()) == {"missing"}


@pytest.mark.parametrize("url", ["http://127.0.0.1:6767", "https://localhost", "https://[::1]"])
def test_server_lane_cannot_be_backfilled_by_loopback(tmp_path: Path, url: str) -> None:
    manifest = tmp_path / "release.json"
    manifest.write_text(json.dumps({"routes": {"server:codex": _lane(url)}}))
    report = release_a.collect_release_a(pr_url=PR, second_repo="/second", manifest=manifest)
    assert report["routes"]["server:codex"]["status"] == "invalid"
    assert report["release_ready"] is False


@pytest.mark.parametrize("fault", ["none", "wrong_harness", "wrong_source", "altered_artifact"])
def test_release_requires_bound_api_records_and_intact_operator_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    local = "http://127.0.0.1:6767"
    remote = "https://studio.example.test"
    artifact = tmp_path / "operator-record.txt"
    artifact.write_bytes(b"operator record fixture")
    observation = {
        "observed_by": "Release operator",
        "checkout": CHECKOUT,
        "run_id": RUN,
        "artifact": artifact.name,
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    }
    routes = {}
    for name in release_a.ROUTES:
        server = remote if name.startswith("server:") else local
        routes[name] = {
            **_lane(server),
            "workflow": observation,
            "disconnect": observation,
            "restart": observation,
            "desktop_browser": observation,
        }
    manifest = tmp_path / "release.json"
    manifest.write_text(
        json.dumps(
            {
                "checkout": CHECKOUT,
                "routes": routes,
                "provider_head_advanced": {
                    **observation,
                    "pr": PR,
                    "previous_head": HEAD,
                    "new_head": "e" * 40,
                },
            }
        )
    )
    if fault == "altered_artifact":
        artifact.write_bytes(b"changed after the operator reviewed it")
    harnesses = iter(("codex-native", "claude-native", "codex-native", "claude-native"))
    captured_pr = PR.replace("example/primary", "Example/Primary")
    verified_prs = []

    def saved(*_args, **_kwargs):
        harness = next(harnesses)
        member = SimpleNamespace(id=REVIEW, profile=SimpleNamespace(harness=harness))
        target = SimpleNamespace(
            pr_url=captured_pr, base_sha=BASE, head_sha=HEAD, host_id=HOST, workspace="/workspace"
        )
        return (
            SimpleNamespace(id=RUN, target=target, selected_reviewers=lambda: [member]),
            SimpleNamespace(id=QUESTION, harness="other" if fault == "wrong_harness" else harness),
        )

    monkeypatch.setattr(release_a, "_saved_run", saved)
    monkeypatch.setattr(
        release_a.setup,
        "collect_setup",
        lambda **_: {
            "preflight_ready": True,
            "server": {"source_commit": "f" * 40 if fault == "wrong_source" else CHECKOUT},
        },
    )

    def review(**arguments):
        verified_prs.append(arguments["pr_url"])
        return {"review_ready": True}

    monkeypatch.setattr(release_a, "collect_review", review)
    monkeypatch.setattr(release_a, "collect_lifecycle", lambda **_: {"lifecycle_ready": True})
    monkeypatch.setattr(release_a, "_publication", lambda *_: "verified")
    monkeypatch.setattr(release_a, "_second_repository", lambda *_: "verified")
    report = release_a.collect_release_a(pr_url=PR, second_repo="/second", manifest=manifest)
    assert report["release_ready"] is (fault == "none")
    if fault == "none":
        assert verified_prs == [captured_pr] * 4
    if fault == "altered_artifact":
        assert report["routes"]["local:codex"]["api_ready"] is True
        assert report["cross_route_checks"]["provider_head_advanced"] == "digest_mismatch"
