"""Media and structured measurements attached to a captured review finding."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, FiniteFloat, StringConstraints, model_validator

from omnigent.studio.reviews.evidence_files import (
    EVIDENCE_LIMITS,
    EvidenceFilename,
    EvidenceMediaType,
    EvidenceSide,
)
from omnigent.studio.reviews.models import FrozenModel, ReviewRoleDigest, Sha
from omnigent.studio.reviews.reproduction import ReproductionSource


class EvidenceStorageError(RuntimeError):
    """Evidence bytes could not be stored or retrieved."""


MetricLabel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class EvidenceMeasurement(FrozenModel):
    label: MetricLabel
    unit: MetricLabel
    baseline: FiniteFloat | None = None
    observed: FiniteFloat


class EvidenceMeasurements(FrozenModel):
    method: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    metrics: list[EvidenceMeasurement] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def unique_metric_labels(self) -> EvidenceMeasurements:
        if len({metric.label for metric in self.metrics}) != len(self.metrics):
            raise ValueError("Use a distinct label for each measurement")
        return self


class EvidenceCheckSelection(FrozenModel):
    receipt_digest: ReviewRoleDigest
    side: EvidenceSide
    name: EvidenceFilename


class EvidenceCheck(EvidenceCheckSelection):
    commit: Sha


class FindingArtifact(FrozenModel):
    digest: ReviewRoleDigest
    source: ReproductionSource
    media_type: EvidenceMediaType
    byte_size: int
    attached_at: int
    provenance: Literal["uploaded"] = "uploaded"
    measurements: EvidenceMeasurements | None = None
    checks: tuple[EvidenceCheck, ...] = Field(default=(), max_length=8)


def inspect_evidence(media_type: EvidenceMediaType, content: bytes) -> EvidenceMeasurements | None:
    if not 0 < len(content) <= EVIDENCE_LIMITS[media_type]:
        raise ValueError("The evidence file is empty or exceeds its size limit")
    if media_type == "application/json":
        return EvidenceMeasurements.model_validate_json(content, strict=True)
    signatures = {
        "image/png": b"\x89PNG\r\n\x1a\n",
        "image/jpeg": b"\xff\xd8\xff",
        "video/webm": b"\x1a\x45\xdf\xa3",
    }
    matches = (
        content[4:8] == b"ftyp"
        if media_type == "video/mp4"
        else content.startswith(signatures[media_type])
    )
    if not matches:
        raise ValueError("The evidence file does not match its declared format")
    return None
