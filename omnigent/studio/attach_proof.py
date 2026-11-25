"""Attach a saved reproduction and its matching files to a finding."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

import httpx

from omnigent.studio.reviews.artifacts import inspect_evidence
from omnigent.studio.reviews.evidence_files import EVIDENCE_LIMITS
from omnigent.studio.reviews.reproduction import (
    MAX_REPRODUCTION_BYTES,
    ReproductionReceipt,
)


def evidence_directory(receipt: Path) -> Path:
    return receipt.with_name(f"{receipt.name}.files")


def _read_regular_file(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Evidence must be a regular file")
        content = stream.read(limit + 1)
    if not 0 < len(content) <= limit:
        raise ValueError("Evidence is empty or exceeds its size limit")
    return content


def attach_proof(client: httpx.Client, *, receipt_path: Path, finding_key: str) -> int:
    if not re.fullmatch(r"[0-9a-f]{64}", finding_key):
        raise ValueError("Provide the saved finding key")
    try:
        receipt = ReproductionReceipt.model_validate_json(
            _read_regular_file(receipt_path, MAX_REPRODUCTION_BYTES)
        )
    except ValueError as exc:
        raise ValueError("Choose a valid reproduction receipt") from exc
    if receipt.source is None:
        raise ValueError("Run the check against a saved review before attaching it")

    files = []
    for side, result in (("base", receipt.base), ("head", receipt.head)):
        for manifest in result.evidence:
            directory = evidence_directory(receipt_path) / side
            content = _read_regular_file(
                directory / manifest.name, EVIDENCE_LIMITS[manifest.media_type]
            )
            if (
                len(content) != manifest.byte_size
                or hashlib.sha256(content).hexdigest() != manifest.digest
            ):
                raise ValueError(f"{side} {manifest.name} differs from its check receipt")
            inspect_evidence(manifest.media_type, content)
            files.append((side, manifest, content))

    prefix = f"v1/studio/review-runs/{receipt.source.run_id}/findings/{finding_key}"
    payload = receipt.model_dump_json().encode()
    response = client.post(
        f"{prefix}/reproductions", content=payload, headers={"Content-Type": "application/json"}
    )
    response.raise_for_status()
    digest = hashlib.sha256(payload).hexdigest()
    if response.json().get("digest") != digest:
        raise ValueError("The server returned a different reproduction receipt")
    for side, manifest, content in files:
        response = client.post(
            f"{prefix}/evidence",
            params={
                "patch_sha256": receipt.source.patch_sha256,
                "receipt_digest": digest,
                "side": side,
                "filename": manifest.name,
            },
            content=content,
            headers={"Content-Type": manifest.media_type},
        )
        response.raise_for_status()
        saved = response.json()
        if saved.get("digest") != manifest.digest or not any(
            entry.get("receipt_digest") == digest
            and entry.get("side") == side
            and entry.get("name") == manifest.name
            for entry in saved.get("checks", [])
        ):
            raise ValueError("The server did not confirm the evidence link")
    return len(files)
