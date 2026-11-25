"""Studio v1 extension request and response contracts."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import UUID4, BaseModel, ConfigDict, Field, StringConstraints

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
Harness = Literal["claude-native", "codex-native", "cursor-native", "pi-native", "opencode-native"]


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Page(Params):
    limit: int = Field(default=50, ge=1, le=100)
    after: str | None = Field(default=None, max_length=128)


class TaskKey(Params):
    linear_workspace: UUID
    linear_issue_id: UUID


class TaskRef(Params):
    task_id: UUID


class Acceptance(TaskRef):
    acceptance_check: Text


class PullRequest(TaskRef):
    repository: Annotated[str, StringConstraints(pattern=r"^[\w.-]+/[\w.-]+$", max_length=200)]
    number: int = Field(ge=1, le=2147483647)
    head_sha: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{40}$")]


class SessionLink(TaskRef):
    session_id: UUID


class Handoff(TaskRef):
    schema_version: Literal["studio-handoff@1"]
    request_id: UUID
    recipient: Name
    question: Text
    evidence: list[Text] = Field(default_factory=list, max_length=12)


class Response(Params):
    request_id: UUID
    decision: Literal["accept", "not_me", "away", "reply"]
    message: Text | None = None


class LinearComment(TaskRef):
    request_id: UUID4
    body: Text


class ConfirmedComment(LinearComment):
    confirmation_token: Annotated[str, StringConstraints(min_length=32, max_length=128)]


class LinearIssueChange(TaskRef):
    request_id: UUID4
    change: Literal["status", "assignee"]
    value: UUID


class ConfirmedIssueChange(LinearIssueChange):
    confirmation_token: Annotated[str, StringConstraints(min_length=32, max_length=128)]


class LeaseClaim(TaskRef):
    idempotency_key: UUID
    harness: Harness


class LeaseRef(Params):
    idempotency_key: UUID


class FinishRound(LeaseRef):
    round: int = Field(ge=1, le=3)
    passed: bool = Field(strict=True)


class Seat(Params):
    slot_id: Name
    harness: Literal["claude-native", "codex-native"]
    encrypted_profile_ref: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    enabled: bool = Field(strict=True)
    weight: int = Field(default=1, ge=1, le=100)
