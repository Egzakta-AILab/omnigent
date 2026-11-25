"""Local acceptance command invocation and fail-closed evidence accounting."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from omnigent.studio import contracts, setup

_PASS = (
    '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0">'
    '<testcase name="contract"/></testsuite></testsuites>'
)


@pytest.fixture
def local_checkout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(contracts, "_CHECKOUT", tmp_path)
    monkeypatch.setattr(setup, "_checkout_identity", lambda: {"commit": "a" * 40, "dirty": True})
    return tmp_path


def test_invocation_retains_unique_evidence_and_uses_current_python(
    monkeypatch: pytest.MonkeyPatch, local_checkout: Path
) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k incomplete --reruns 9 --num-shards 8 --shard-id 2")
    monkeypatch.setenv("OMNIGENT_TEST_DB_URI", "postgresql://shared.invalid/test")
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert command[:14] == [
            sys.executable,
            "-P",
            "-m",
            "pytest",
            "tests/studio/test_review_capture.py",
            "tests/stores/test_studio_review_store.py",
            "tests/server/routes/test_studio_reviews.py",
            "tests/db/test_migration_studio_reviews.py",
            "--reruns",
            "0",
            "--num-shards",
            "1",
            "--shard-id",
            "0",
        ]
        assert kwargs["cwd"] == local_checkout
        assert "PYTEST_ADDOPTS" not in kwargs["env"]
        assert "OMNIGENT_TEST_DB_URI" not in kwargs["env"]
        assert kwargs["stderr"] == subprocess.STDOUT
        assert kwargs["check"] is False
        kwargs["stdout"].write("pytest output retained\n")
        Path(command[command.index("--junitxml") + 1]).write_text(_PASS)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(contracts.subprocess, "run", run)
    first, second = contracts.run_contracts(), contracts.run_contracts()
    assert len(calls) == 2
    assert first["evidence_dir"] != second["evidence_dir"]
    for receipt in (first, second):
        directory = Path(receipt["evidence_dir"])
        assert directory.parent == local_checkout / ".omnigent" / "studio" / "evidence"
        assert directory.name.startswith("contracts-")
        assert receipt["status"] == "pass"
        assert receipt["counts"] == {"tests": 1, "failures": 0, "errors": 0, "skipped": 0}
        assert set(receipt["qualification"].values()) == {"not_run"}
        assert receipt["source"] == {"commit": "a" * 40, "dirty": True}
        assert Path(receipt["pytest_log"]).read_text() == "pytest output retained\n"
        assert json.loads(Path(receipt["receipt_path"]).read_text()) == receipt


@pytest.mark.parametrize(
    ("report", "exit_code", "reason"),
    [
        (_PASS, 1, "pytest_nonzero"),
        (
            _PASS.replace('tests="1"', 'tests="0"').replace('<testcase name="contract"/>', ""),
            0,
            "no_tests",
        ),
        (None, 0, "junit_missing_or_invalid"),
        ("<testsuites>", 0, "junit_missing_or_invalid"),
        (_PASS.replace('tests="1"', 'tests="2"'), 0, "junit_missing_or_invalid"),
        (
            _PASS.replace('<testcase name="contract"/>', "<testcase><skipped/></testcase>"),
            0,
            "junit_missing_or_invalid",
        ),
        *[
            (
                _PASS.replace(f'{counter}="0"', f'{counter}="1"').replace(
                    '<testcase name="contract"/>', f"<testcase><{outcome}/></testcase>"
                ),
                0,
                "nonpassing_tests",
            )
            for counter, outcome in (
                ("skipped", "skipped"),
                ("failures", "failure"),
                ("errors", "error"),
            )
        ],
    ],
)
def test_nonpassing_or_incomplete_evidence_never_passes(
    monkeypatch: pytest.MonkeyPatch,
    local_checkout: Path,
    report: str | None,
    exit_code: int,
    reason: str,
) -> None:
    def run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if report is not None:
            Path(command[command.index("--junitxml") + 1]).write_text(report)
        return subprocess.CompletedProcess(command, exit_code)

    monkeypatch.setattr(contracts.subprocess, "run", run)
    receipt = contracts.run_contracts()
    assert receipt["status"] == "fail"
    assert reason in receipt["reasons"]
    assert Path(receipt["receipt_path"]).exists()


def test_pytest_start_failure_is_retained(
    monkeypatch: pytest.MonkeyPatch, local_checkout: Path
) -> None:
    def unavailable(*_: Any, **__: Any) -> None:
        raise OSError("pytest could not start")

    monkeypatch.setattr(contracts.subprocess, "run", unavailable)
    receipt = contracts.run_contracts()
    assert receipt["status"] == "fail"
    assert "pytest_start_failed" in receipt["reasons"]
    assert "pytest could not start" in Path(receipt["pytest_log"]).read_text()


def test_contracts_cli_dispatches_without_setup_and_writes_failed_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    receipt = {"status": "fail", "evidence_dir": "/retained/local/contracts"}
    monkeypatch.setattr(contracts, "run_contracts", lambda: receipt)
    output = tmp_path / "copy" / "receipt.json"
    assert setup.main(["contracts", "--json", "--output", str(output)]) == 1
    assert json.loads(capsys.readouterr().out) == receipt
    assert json.loads(output.read_text()) == receipt
