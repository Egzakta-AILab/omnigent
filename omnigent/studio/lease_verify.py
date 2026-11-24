"""Run the provisioned KVARK verification command on a clean, committed local checkout."""

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def verify(
    checkout: Path, seed: Path, org: Path, expected_sha: str | None = None
) -> dict[str, Any]:
    env = {k: os.environ[k] for k in ("HOME", "PATH", "LANG", "TMPDIR") if k in os.environ}
    env.update(NO_COLOR="1", TERM="dumb", GIT_TERMINAL_PROMPT="0")

    def git(repo: Path, *args: str) -> str:
        return subprocess.check_output(
            ["/usr/bin/git", "-C", str(repo), *args],
            env=env,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).strip()

    receipt: dict[str, Any] = {
        "status": "env_blocked",
        "commit_sha": None,
        "base_sha": None,
        "output_digest": None,
    }
    try:
        origin = git(checkout, "remote", "get-url", "origin").removesuffix(".git")
        if origin != "https://github.com/Egzakta-AILab/KVARK":
            return receipt
        base, head = git(seed, "rev-parse", "HEAD"), git(checkout, "rev-parse", "HEAD")
        if not all(re.fullmatch(r"[0-9a-f]{40}", v) for v in (base, head)):
            return receipt
        if expected_sha and expected_sha not in (base, head):
            return receipt
        git(checkout, "merge-base", "--is-ancestor", base, head)
        if git(checkout, "status", "--porcelain"):
            return receipt
        receipt.update(base_sha=base, commit_sha=head)
        with tempfile.TemporaryFile() as log:
            with subprocess.Popen(
                [str(org), "dev", "test-int", "--repo", str(checkout)],
                cwd=checkout,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            ) as proc:
                try:
                    code = proc.wait(timeout=600)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    raise
            log.seek(0)
            receipt["output_digest"] = hashlib.file_digest(log, "sha256").hexdigest()
            log.seek(max(0, log.seek(0, os.SEEK_END) - 8192))
            tail = log.read().decode(errors="replace")
        if git(checkout, "rev-parse", "HEAD") != head or git(checkout, "status", "--porcelain"):
            return {**receipt, "status": "unverified"}
        # Require both the trusted org command exit and pytest's actual executed-test summary.
        if code == 0 and re.search(r"\b[1-9]\d* passed\b", tail):
            receipt["status"] = "verified"
        elif code == 1 and re.search(r"\b[1-9]\d* failed\b", tail):
            receipt["status"] = "check_failed"
    except (OSError, subprocess.SubprocessError):
        pass
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha")
    args = parser.parse_args()
    home = Path.home()
    print(
        json.dumps(
            verify(
                home / "KVARK",
                Path("/opt/studio/sources/KVARK"),
                home / ".local/bin/org",
                args.expected_sha,
            ),
            sort_keys=True,
        )
    )
