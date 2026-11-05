"""Run an approved check on the workspace host and serve bounded result chunks."""

from __future__ import annotations

import base64
import logging
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from omnigent.studio.reviews.hosted_check_models import (
    CHECK_CHUNK_BYTES,
    CHECK_HOST_PREFIX,
    CHECK_JOB_SECONDS,
    HostedCheckFile,
    HostedCheckLookup,
    HostedCheckStart,
)
from omnigent.studio.reviews.reproduction import EvidenceCollection, ReproductionReceipt, reproduce
from omnigent.studio.workspace import workspace_pr_reference
from omnigent.workspace_fs import WorkspaceReaderError
from omnigent.workspace_repositories import resolve_workspace_github_repository

_logger = logging.getLogger(__name__)
_MAX_SAVED_CHECKS = 4
_MAX_TRACKED_CHECKS = 128


@dataclass
class _HostCheck:
    root: Path
    request: HostedCheckStart
    output: tempfile.TemporaryDirectory[str] | None
    receipt: ReproductionReceipt | None = None
    finished_at: float | None = None


_checks: dict[str, _HostCheck] = {}
_lock = threading.Lock()


def _run_check(check: _HostCheck, checkout: Path) -> None:
    request = check.request
    proposal, source = request.proposal, request.source
    receipt = None
    output = check.output
    assert output is not None
    try:
        receipt = reproduce(
            repository=checkout,
            base=source.base_sha,
            head=source.head_sha,
            image=request.image_id,
            command=proposal.command,
            paths=proposal.paths,
            timeout=proposal.timeout_seconds,
            source=source,
            proposal_id=request.proposal_id,
            collection=(
                EvidenceCollection(proposal.evidence_files, Path(output.name))
                if proposal.evidence_files
                else None
            ),
        )
    except Exception:
        _logger.exception("Sandbox check %s failed on the workspace host", request.job_id)
    finally:
        with _lock:
            check.receipt = receipt
            check.finished_at = time.monotonic()


def _start(root: Path, request: HostedCheckStart) -> dict[str, object]:
    checkout, remote, active = resolve_workspace_github_repository(root, request.repository)
    if not active:
        raise WorkspaceReaderError(
            409, "reference_repository", "Mark the check repository Active."
        )
    workspace_pr_reference(request.source.pr_url, remote)
    with _lock:
        previous = _checks.get(request.job_id)
        if previous is not None:
            if previous.root != root or previous.request != request:
                raise WorkspaceReaderError(409, "check_conflict", "The saved check has changed.")
            return {"state": "accepted"}
        now = time.monotonic()
        for job_id, check in list(_checks.items()):
            if check.finished_at is not None and now - check.finished_at >= CHECK_JOB_SECONDS:
                if check.output is not None:
                    check.output.cleanup()
                del _checks[job_id]
        if any(check.finished_at is None for check in _checks.values()):
            raise WorkspaceReaderError(
                409, "sandbox_busy", "A check is already running on this host."
            )
        if (
            len(_checks) >= _MAX_TRACKED_CHECKS
            or sum(check.output is not None for check in _checks.values()) >= _MAX_SAVED_CHECKS
        ):
            raise WorkspaceReaderError(
                429, "sandbox_busy", "The host has too many recent check results."
            )
        output = tempfile.TemporaryDirectory(prefix="omnigent-check-")
        check = _HostCheck(root, request, output)
        _checks[request.job_id] = check
        try:
            threading.Thread(
                target=_run_check,
                args=(check, checkout),
                name="studio-sandbox-check",
                daemon=False,
            ).start()
        except RuntimeError:
            output.cleanup()
            del _checks[request.job_id]
            raise
        return {"state": "accepted"}


def dispatch_hosted_check(root: Path, op: str, params: dict[str, object]) -> dict[str, object]:
    payload = {key: value for key, value in params.items() if key != "session_id"}
    if op == CHECK_HOST_PREFIX + "start":
        return _start(root, HostedCheckStart.model_validate(payload))
    if op in {CHECK_HOST_PREFIX + "status", CHECK_HOST_PREFIX + "release"}:
        request = HostedCheckLookup.model_validate(payload)
    elif op == CHECK_HOST_PREFIX + "file":
        request = HostedCheckFile.model_validate(payload)
    else:
        raise ValueError("Unknown sandbox check operation")
    with _lock:
        check = _checks.get(request.job_id)
        if check is None or check.root != root:
            raise WorkspaceReaderError(
                404, "check_missing", "Sandbox check unavailable on this host."
            )
        if op == CHECK_HOST_PREFIX + "release":
            if check.finished_at is None:
                raise WorkspaceReaderError(
                    409, "check_running", "The sandbox check is still running."
                )
            if check.output is not None:
                check.output.cleanup()
                check.output = None
            return {"state": "released"}
        receipt = check.receipt
        if not isinstance(request, HostedCheckFile):
            if check.finished_at is None:
                return {"state": "running"}
            return (
                {"state": "finished", "receipt": receipt.model_dump(mode="json")}
                if receipt is not None
                else {"state": "failed"}
            )
        if receipt is None or check.output is None:
            raise WorkspaceReaderError(
                404, "check_file_missing", "The check has no evidence file."
            )
        side = receipt.base if request.side == "base" else receipt.head
        file = next((item for item in side.evidence if item.name == request.filename), None)
        if file is None or request.offset >= file.byte_size:
            raise WorkspaceReaderError(
                404, "check_file_missing", "The check has no evidence file."
            )
        with (Path(check.output.name) / request.side / file.name).open("rb") as stream:
            stream.seek(request.offset)
            chunk = stream.read(min(CHECK_CHUNK_BYTES, file.byte_size - request.offset))
        return {"data": base64.b64encode(chunk).decode("ascii")}
