"""Dispatch an approved sandbox check once, then collect its revision-bound output."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import time
from contextlib import suppress

from omnigent.server.routes._host_filesystem import (
    HostFsError,
    HostFsUnavailableError,
    write_workspace_from_host,
)
from omnigent.server.routes._studio_project_workspace import ProjectWorkspace
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.studio_review_store import StudioReviewStore
from omnigent.studio.reviews.artifacts import EvidenceCheckSelection
from omnigent.studio.reviews.evidence_files import CapturedEvidenceFile, EvidenceSide
from omnigent.studio.reviews.hosted_check_models import (
    CHECK_CHUNK_BYTES,
    CHECK_HOST_PREFIX,
    CHECK_JOB_SECONDS,
    HostedCheckJob,
    HostedCheckStart,
)
from omnigent.studio.reviews.reproduction import ReproductionReceipt

_logger = logging.getLogger(__name__)


async def _request(
    workspace: ProjectWorkspace, operation: str, payload: dict[str, object]
) -> dict[str, object]:
    connection = workspace.registry.get(workspace.host_id)
    if connection is None or connection.owner != workspace.connection.owner:
        raise HostFsUnavailableError("The approved workspace host is disconnected")
    return await write_workspace_from_host(
        host_registry=workspace.registry,
        host_conn=connection,
        op=CHECK_HOST_PREFIX + operation,
        workspace=workspace.path,
        session_id=workspace.record_id,
        params=payload,
        timeout=10,
    )


async def _collect_file(
    workspace: ProjectWorkspace, job_id: str, side: EvidenceSide, file: CapturedEvidenceFile
) -> bytes:
    content = bytearray()
    while len(content) < file.byte_size:
        payload = await _request(
            workspace,
            "file",
            {"job_id": job_id, "side": side, "filename": file.name, "offset": len(content)},
        )
        encoded = payload.get("data")
        expected = min(CHECK_CHUNK_BYTES, file.byte_size - len(content))
        if not isinstance(encoded, str) or len(encoded) != 4 * ((expected + 2) // 3):
            raise ValueError("The host returned an invalid evidence chunk size")
        try:
            chunk = base64.b64decode(encoded, validate=True)
        except binascii.Error as exc:
            raise ValueError("The host returned an invalid evidence chunk") from exc
        if len(chunk) != expected:
            raise ValueError("The host returned an incomplete evidence chunk")
        content.extend(chunk)
    if hashlib.sha256(content).hexdigest() != file.digest:
        raise ValueError("The host evidence differs from its check receipt")
    return bytes(content)


async def _wait_for_receipt(
    workspace: ProjectWorkspace, job_id: str
) -> ReproductionReceipt | None:
    failures = 0
    while True:
        try:
            response = await _request(workspace, "status", {"job_id": job_id})
        except HostFsUnavailableError:
            failures += 1
            if failures >= 3:
                raise
        else:
            failures = 0
            if response.get("state") == "finished":
                return ReproductionReceipt.model_validate(response.get("receipt"))
            if response.get("state") == "failed":
                return None
            if response.get("state") != "running":
                raise ValueError("The host returned an invalid sandbox status")
        await asyncio.sleep(1)


async def execute_hosted_check(
    workspace: ProjectWorkspace,
    request: HostedCheckStart,
    job: HostedCheckJob,
    *,
    finding_key: str,
    store: StudioReviewStore,
    artifacts: ArtifactStore,
) -> None:
    receipt_digest = None
    files_attached = 0
    state = "unconfirmed"
    accepted = False
    try:
        remaining = max(0.0, job.started_at / 1000 + CHECK_JOB_SECONDS - time.time())
        async with asyncio.timeout(remaining):
            acknowledgement = await _request(workspace, "start", request.model_dump(mode="json"))
            if acknowledgement.get("state") != "accepted":
                raise HostFsUnavailableError("The sandbox check start was unconfirmed")
            accepted = True
            receipt = await _wait_for_receipt(workspace, job.id)
            if receipt is None:
                state = "failed"
            else:
                if (
                    receipt.source != request.source
                    or receipt.proposal_id != request.proposal_id
                    or receipt.image_id != job.image_id
                    or not request.proposal.matches(receipt)
                ):
                    raise ValueError("The host returned a receipt for another check")
                attached = await asyncio.to_thread(
                    store.attach_reproduction,
                    request.source.run_id,
                    finding_key,
                    receipt,
                    owner_user_id=workspace.owner,
                )
                receipt_digest = attached.digest
                for side, result in (("base", receipt.base), ("head", receipt.head)):
                    for file in result.evidence:
                        content = await _collect_file(workspace, job.id, side, file)
                        await asyncio.to_thread(
                            store.attach_finding_artifact,
                            request.source.run_id,
                            finding_key,
                            request.source.patch_sha256,
                            file.media_type,
                            content,
                            owner=workspace.owner,
                            artifacts=artifacts,
                            check_selection=EvidenceCheckSelection(
                                receipt_digest=attached.digest,
                                side=side,
                                name=file.name,
                            ),
                        )
                        files_attached += 1
                state = (
                    "attached"
                    if receipt.base.state == receipt.head.state == "exited"
                    and files_attached == len(request.proposal.evidence_files) * 2
                    else "partial"
                )
    except HostFsError as exc:
        if receipt_digest:
            state = "partial"
        elif not accepted and exc.status < 500:
            state = "failed"
    except (HostFsUnavailableError, TimeoutError):
        state = "partial" if receipt_digest else "unconfirmed"
    except Exception:
        _logger.exception("Could not collect sandbox check %s", job.id)
        state = "partial" if receipt_digest else "unconfirmed"
    finally:
        try:
            await asyncio.to_thread(
                store.finish_hosted_check,
                request.source.run_id,
                request.proposal_id,
                job.model_copy(
                    update={
                        "state": state,
                        "receipt_digest": receipt_digest,
                        "files_attached": files_attached,
                    }
                ),
                owner=workspace.owner,
            )
        except Exception:
            _logger.exception("Could not save the status of sandbox check %s", job.id)
        else:
            if accepted and state in {"attached", "failed"}:
                with suppress(HostFsError, HostFsUnavailableError):
                    await _request(workspace, "release", {"job_id": job.id})
