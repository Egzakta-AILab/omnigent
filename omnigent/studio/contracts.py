"""Run the fixed A2 local contracts and retain their deterministic test evidence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_CHECKOUT = Path(__file__).resolve().parents[2]
_TEST_FILES = (
    "tests/studio/test_review_capture.py",
    "tests/stores/test_studio_review_store.py",
    "tests/server/routes/test_studio_reviews.py",
    "tests/db/test_migration_studio_reviews.py",
)


def _junit_counts(path: Path) -> dict[str, int]:
    """Require complete pytest JUnit accounting, including actual test outcomes."""
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if root.tag not in ("testsuite", "testsuites") or not suites:
        raise ValueError("Expected pytest test suites")
    counts = dict.fromkeys(("tests", "failures", "errors", "skipped"), 0)
    for suite in suites:
        reported = {name: int(suite.attrib[name]) for name in counts}
        cases = suite.findall("testcase")
        if any(value < 0 for value in reported.values()) or reported["tests"] != len(cases):
            raise ValueError("Invalid JUnit test accounting")
        for outcome in ("failure", "error", "skipped"):
            name = {"failure": "failures", "error": "errors", "skipped": "skipped"}[outcome]
            if reported[name] != sum(len(case.findall(outcome)) for case in cases):
                raise ValueError("Invalid JUnit outcome accounting")
        for name, count in reported.items():
            counts[name] += count
    return counts


def run_contracts() -> dict[str, Any]:
    """Run only local contract tests; never qualify agents, providers or a server."""
    from omnigent.studio.setup import _checkout_identity

    evidence_root = _CHECKOUT / ".omnigent" / "studio" / "evidence"
    evidence_root.mkdir(parents=True, exist_ok=True)
    evidence_dir = Path(tempfile.mkdtemp(prefix="contracts-", dir=evidence_root))
    log = evidence_dir / "pytest.log"
    junit = evidence_dir / "junit.xml"
    command = [
        sys.executable,
        "-P",
        "-m",
        "pytest",
        *_TEST_FILES,
        "--reruns",
        "0",
        "--num-shards",
        "1",
        "--shard-id",
        "0",
        "--junitxml",
        str(junit),
        "-q",
    ]
    environment = os.environ.copy()
    # Ambient selection or a shared database must not change this local gate.
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("OMNIGENT_TEST_DB_URI", None)
    started_at = datetime.now(UTC).isoformat()
    source = _checkout_identity()
    reasons: list[str] = []
    exit_code = None
    with log.open("w", encoding="utf-8") as stream:
        try:
            exit_code = subprocess.run(
                command,
                cwd=_CHECKOUT,
                env=environment,
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=False,
            ).returncode
        except OSError as exc:
            stream.write(f"Could not start pytest: {exc}\n")
            reasons.append("pytest_start_failed")
        except KeyboardInterrupt:
            stream.write("\nLocal contracts interrupted.\n")
            reasons.append("interrupted")
    if exit_code != 0:
        reasons.append("pytest_nonzero")
    counts = None
    try:
        counts = _junit_counts(junit)
    except (OSError, ET.ParseError, ValueError, KeyError):
        reasons.append("junit_missing_or_invalid")
    if counts is not None:
        if not counts["tests"]:
            reasons.append("no_tests")
        if any(counts[name] for name in ("failures", "errors", "skipped")):
            reasons.append("nonpassing_tests")
    receipt = {
        "schema_version": 1,
        "scope": "a2_deterministic_local_contracts",
        "status": "fail" if reasons else "pass",
        "qualification": {
            "agent_execution": "not_run",
            "provider": "not_run",
            "server": "not_run",
        },
        "database": "isolated_sqlite",
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "command": command,
        "checkout": str(_CHECKOUT),
        "source": source,
        "exit_code": exit_code,
        "counts": counts,
        "reasons": reasons,
        "evidence_dir": str(evidence_dir),
        "pytest_log": str(log),
        "junit_report": str(junit),
        "receipt_path": str(evidence_dir / "receipt.json"),
    }
    (evidence_dir / "receipt.json").write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
    )
    return receipt


def main(*, output: Path | None = None, json_output: bool = False) -> int:
    """Print the retained receipt and fail closed when any local contract is unproved."""
    try:
        receipt = run_contracts()
        serialized = json.dumps(receipt, indent=2) + "\n"
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(serialized, encoding="utf-8")
    except OSError:
        print("Could not retain the local contracts evidence or receipt.", file=sys.stderr)
        return 2
    if json_output:
        print(serialized, end="")
    else:
        print(f"Local A2 contracts: {receipt['status']}")
        print(f"Evidence: {receipt['evidence_dir']}")
        if receipt["reasons"]:
            print(f"Reasons: {', '.join(receipt['reasons'])}")
        print("Agent execution, providers and server deployment are not qualified.")
    return 0 if receipt["status"] == "pass" else 1
