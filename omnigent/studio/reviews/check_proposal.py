"""A draft sandbox check authored by a reviewer."""

from __future__ import annotations

from pydantic import Field, model_validator

from omnigent.studio.reviews.evidence_files import (
    MAX_CAPTURED_FILES,
    EvidenceFilename,
    evidence_type_for_name,
)
from omnigent.studio.reviews.models import FrozenModel
from omnigent.studio.reviews.reproduction import ReproductionReceipt, validate_export_paths


class ProposedCheck(FrozenModel):
    command: tuple[str, ...] = Field(min_length=1, max_length=32)
    paths: tuple[str, ...] = Field(min_length=1, max_length=8)
    timeout_seconds: int = Field(strict=True, ge=1, le=120)
    evidence_files: tuple[EvidenceFilename, ...] = Field(max_length=MAX_CAPTURED_FILES)

    @model_validator(mode="after")
    def validate_check(self) -> ProposedCheck:
        ReproductionReceipt.validate_command(self.command)
        validate_export_paths(self.paths)
        if sum(len(arg.encode()) + 1 for arg in self.command) > 2048 or any(
            arg and not arg.isprintable() for arg in self.command
        ):
            raise ValueError("Use printable check arguments within 2048 UTF-8 bytes")
        if len(set(self.paths)) != len(self.paths) or len(set(self.evidence_files)) != len(
            self.evidence_files
        ):
            raise ValueError("Use each path and evidence filename once")
        for name in self.evidence_files:
            evidence_type_for_name(name)
        return self

    def matches_intent(
        self,
        *,
        command: tuple[str, ...],
        paths: tuple[str, ...],
        timeout: int,
        evidence_files: tuple[str, ...],
    ) -> bool:
        return (self.command, self.paths, self.timeout_seconds, self.evidence_files) == (
            command,
            paths,
            timeout,
            evidence_files,
        )

    def matches(self, receipt: ReproductionReceipt) -> bool:
        return self.matches_intent(
            command=receipt.command,
            paths=receipt.included_paths,
            timeout=receipt.timeout_seconds,
            evidence_files=receipt.requested_evidence,
        )
