"""Bounded media file identities shared by the sandbox and evidence store."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from omnigent.studio.reviews.models import FrozenModel, ReviewRoleDigest

EvidenceMediaType = Literal[
    "image/png", "image/jpeg", "video/mp4", "video/webm", "application/json"
]
EVIDENCE_LIMITS: dict[EvidenceMediaType, int] = {
    "image/png": 4 * 1024 * 1024,
    "image/jpeg": 4 * 1024 * 1024,
    "video/mp4": 16 * 1024 * 1024,
    "video/webm": 16 * 1024 * 1024,
    "application/json": 64 * 1024,
}
_EXTENSIONS: dict[str, EvidenceMediaType] = {
    ".png": "image/png",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".json": "application/json",
}
MAX_CAPTURED_FILES = 2
EvidenceSide = Literal["base", "head"]
EvidenceFilename = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}$")]


def evidence_type_for_name(name: str) -> EvidenceMediaType:
    if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}", name):
        kind = _EXTENSIONS.get(PurePosixPath(name).suffix.lower())
        if kind is not None:
            return kind
    raise ValueError("Use a PNG, JPEG, MP4, WebM, or JSON filename without directories")


class CapturedEvidenceFile(FrozenModel):
    name: EvidenceFilename
    digest: ReviewRoleDigest
    media_type: EvidenceMediaType
    byte_size: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_file(self) -> CapturedEvidenceFile:
        if self.media_type != evidence_type_for_name(self.name):
            raise ValueError("Evidence media type differs from the filename")
        if self.byte_size > EVIDENCE_LIMITS[self.media_type]:
            raise ValueError("Evidence file exceeds its size limit")
        return self
