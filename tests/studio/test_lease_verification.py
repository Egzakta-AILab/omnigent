import json
import subprocess
from uuid import uuid4

import httpx
import pytest

from omnigent.studio import lease_ci, lease_turns
from omnigent.studio.lease_verify import verify


@pytest.mark.parametrize("outcome", ["verified", "check_failed", "env_blocked"])
def test_local_verifier_records_clean_commit_and_real_exit(tmp_path, outcome, monkeypatch):
    seed = tmp_path / "seed"
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", str(seed)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(seed),
            "-c",
            "user.email=user@example.test",
            "-c",
            "user.name=User",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "seed",
        ],
        check=True,
    )
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(seed), str(checkout)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "remote",
            "set-url",
            "origin",
            "https://github.com/Egzakta-AILab/KVARK.git",
        ],
        check=True,
    )
    org = tmp_path / "org"
    text, code = {
        "verified": ("2 passed", 0),
        "check_failed": ("1 failed", 1),
        "env_blocked": ("missing local database", 2),
    }[outcome]
    monkeypatch.setenv("STUDIO_SHOULD_NOT_LEAK", "yes")
    org.write_text(
        '#!/bin/sh\n[ -z "$STUDIO_SHOULD_NOT_LEAK" ] || exit 3\n'
        '[ "$1 $2 $3" = "dev test-int --repo" ] || exit 3\n'
        f'echo "{text}"\nexit {code}\n'
    )
    org.chmod(0o700)
    receipt = verify(checkout, seed, org)
    assert receipt["status"] == outcome and receipt["commit_sha"] == receipt["base_sha"]
    assert len(receipt["output_digest"]) == 64
    (checkout / "uncommitted").touch()
    assert verify(checkout, seed, org)["status"] == "env_blocked"


@pytest.mark.parametrize("fault", [None, "branch", "head", "job", "step", "unlinked"])
def test_ci_matches_task_and_trusted_default_branch_integration(fault):
    head = "a" * 40
    prs = [{"repository": lease_ci.REPO, "head_sha": head}] if fault != "unlinked" else []

    def fetch(req):
        assert req.method == "GET"
        if req.url.path.endswith("/KVARK"):
            return httpx.Response(200, json={"default_branch": "main"})
        if req.url.path.endswith("/runs"):
            run = {
                "head_sha": head if fault != "head" else "b" * 40,
                "head_branch": "main" if fault != "branch" else "fork",
                "path": ".github/workflows/nightly.yml",
                "head_repository": {"full_name": lease_ci.REPO},
                "event": "schedule",
                "status": "completed",
                "conclusion": "success",
                "id": 3,
                "run_attempt": 2,
            }
            return httpx.Response(200, json={"workflow_runs": [run]})
        assert req.url.path.endswith("/3/attempts/2/jobs")
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "name": lease_ci.JOB if fault != "job" else "lint",
                        "id": 9,
                        "head_sha": head,
                        "conclusion": "success",
                        "steps": [
                            {
                                "name": "Integration and NATS tests",
                                "conclusion": "skipped" if fault == "step" else "success",
                            }
                        ],
                    }
                ]
            },
        )

    with httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(fetch)
    ) as client:
        proof = lease_ci.trusted_integration(head, prs, client)
    assert bool(proof) == (fault is None)


@pytest.mark.parametrize(
    "ci", [None, "https://github.com/Egzakta-AILab/KVARK/actions/runs/3/job/9"]
)
def test_native_claim_cannot_prove_its_own_pass(monkeypatch, ci):
    key = uuid4().hex
    sha = "a" * 40
    turn = {
        "session_id": uuid4().hex,
        "key": key,
        "round": 1,
        "acceptance": "Verify",
        "prs": [{"repository": lease_ci.REPO, "head_sha": sha}],
    }
    envelope = lease_turns.RoundEnvelope(lease_id=key, round=1, status="verified")
    monkeypatch.setattr(
        lease_turns, "read_native_turn", lambda *a, **k: ("ready", "reply", envelope)
    )
    receipt = {"status": "verified", "base_sha": sha, "commit_sha": sha, "output_digest": "c" * 64}
    monkeypatch.setattr(
        lease_turns,
        "command_receipts",
        lambda *a, **k: [(lease_turns.verify_command(turn["prs"]), json.dumps(receipt))],
    )
    monkeypatch.setattr(lease_turns, "completed_integration", lambda *a: ci)
    observed = lease_turns.observe(None, turn)
    assert observed["status"] == ("verified" if ci else "unverified") and observed["ci_url"] == ci
