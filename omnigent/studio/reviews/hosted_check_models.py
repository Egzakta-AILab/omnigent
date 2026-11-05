"""Wire contracts for an operator-approved check on a connected host."""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator

from omnigent.studio.reviews.check_proposal import ProposedCheck
from omnigent.studio.reviews.evidence_files import EvidenceFilename, EvidenceSide
from omnigent.studio.reviews.models import FrozenModel, ReviewRoleDigest, require_uuid
from omnigent.studio.reviews.reproduction import ReproductionSource

CHECK_CHUNK_BYTES = 512 * 1024
CHECK_JOB_SECONDS = 540
CHECK_HOST_PREFIX = "studio_project_pull_request_check_"
ImageId = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
CheckState = Literal["running", "attached", "partial", "failed", "unconfirmed"]


def proposal_digest(proposal: ProposedCheck) -> str:
    return hashlib.sha256(proposal.model_dump_json().encode()).hexdigest()


class ApproveHostedCheck(FrozenModel):
    proposal_digest: ReviewRoleDigest
    image_id: ImageId
    confirm: Literal[True]

    @field_validator("confirm", mode="before")
    @classmethod
    def require_confirmation(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Confirm the sandbox check")
        return value


class HostedCheckJob(FrozenModel):
    id: str
    state: CheckState
    image_id: ImageId
    started_at: int
    receipt_digest: ReviewRoleDigest | None = None
    files_attached: int = 0


class HostedCheckLookup(FrozenModel):
    job_id: str

    @field_validator("job_id")
    @classmethod
    def validate_job(cls, value: str) -> str:
        return require_uuid(value)


class HostedCheckStart(HostedCheckLookup):
    repository: str
    image_id: ImageId
    proposal_id: str
    proposal: ProposedCheck
    source: ReproductionSource

    @field_validator("proposal_id")
    @classmethod
    def validate_proposal(cls, value: str) -> str:
        return require_uuid(value)


class HostedCheckFile(HostedCheckLookup):
    side: EvidenceSide
    filename: EvidenceFilename
    offset: int = Field(strict=True, ge=0, lt=16 * 1024 * 1024)
