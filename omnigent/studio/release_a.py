"""Read-only Release A matrix and retained operational evidence gate."""

from __future__ import annotations

import hashlib
import ipaddress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omnigent.runner.session_prs import PullRequestRef
from omnigent.studio import setup
from omnigent.studio.reviews.discussion import InlinePublicationPreview, ReviewQuestion
from omnigent.studio.reviews.models import ReviewRoleDigest, ReviewRun, Sha, require_uuid
from omnigent.studio.verify_lifecycle import collect_lifecycle
from omnigent.studio.verify_publication import collect_publication
from omnigent.studio.verify_review import collect_review
from omnigent.studio.verify_review_run import _matches_pr_url, collect_review_run

Route = Literal["local:codex", "local:claude", "server:codex", "server:claude"]
Mode = Literal["local", "server"]
ROUTES: tuple[Route, ...] = ("local:codex", "local:claude", "server:codex", "server:claude")
_MAX_JSON_BYTES = 256 * 1024
_MAX_ARTIFACT_BYTES = 128 * 1024 * 1024


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Observation(_Input):
    """An operator-reviewed recording or log, bound to the evaluated run."""

    observed_by: Annotated[str, Field(min_length=1, max_length=128)]
    checkout: Sha
    run_id: str
    artifact: Annotated[str, Field(min_length=1, max_length=1024)]
    sha256: ReviewRoleDigest


class ProviderHeadObservation(Observation):
    pr: str
    previous_head: Sha
    new_head: Sha


class Lane(_Input):
    server: str
    host_id: str
    workspace: str
    run_id: str
    review_id: str
    finding_key: ReviewRoleDigest
    question_id: str
    candidate_id: str
    base_sha: Sha
    head_sha: Sha
    skills: list[str] = Field(default_factory=list, max_length=32)
    mcp: list[str] = Field(default_factory=list, max_length=32)
    workflow: Observation | None = None
    disconnect: Observation | None = None
    restart: Observation | None = None
    desktop_browser: Observation | None = None


class SecondRepository(_Input):
    lane: Route
    run_id: str
    pr: str
    base_sha: Sha
    head_sha: Sha


class Publication(_Input):
    lane: Route
    preview: str
    draft_id: str
    author: str


class ReleaseEvidence(_Input):
    checkout: Sha | None = None
    routes: dict[Route, Lane] = Field(default_factory=dict)
    second_repository: SecondRepository | None = None
    provider_head_advanced: ProviderHeadObservation | None = None
    publication: Publication | None = None


def read_evidence(path: Path) -> ReleaseEvidence:
    try:
        with path.open("rb") as file:
            raw = file.read(_MAX_JSON_BYTES + 1)
    except OSError:
        raise ValueError("Could not read the release evidence manifest") from None
    if len(raw) > _MAX_JSON_BYTES:
        raise ValueError("The release evidence manifest is too large")
    try:
        return ReleaseEvidence.model_validate_json(raw)
    except ValidationError:
        raise ValueError(
            "The release evidence manifest does not match the expected schema"
        ) from None


def _artifact(root: Path, reference: str) -> Path:
    path = Path(reference)
    if path.is_absolute():
        raise ValueError("Evidence paths must be relative to the manifest directory")
    artifact = (root / path).resolve()
    if not artifact.is_relative_to(root) or not artifact.is_file():
        raise ValueError("Evidence artifact is missing or outside the manifest directory")
    return artifact


def _observation(
    observation: Observation | None, root: Path, checkout: str | None, run_ids: set[str]
) -> str:
    if observation is None:
        return "missing"
    if (
        observation.checkout != checkout
        or observation.run_id not in run_ids
        or not observation.observed_by.strip()
    ):
        return "scope_mismatch"
    try:
        path = _artifact(root, observation.artifact)
        if not 0 < path.stat().st_size <= _MAX_ARTIFACT_BYTES:
            return "invalid_artifact"
        with path.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
    except (OSError, ValueError):
        return "invalid_artifact"
    return "operator_record_verified" if digest == observation.sha256 else "digest_mismatch"


def _saved_run(
    mode: Mode,
    lane: Lane,
    run_id: str,
    *,
    question_id: str | None = None,
    transport: httpx.BaseTransport | None,
) -> tuple[ReviewRun | None, ReviewQuestion | None]:
    try:
        server = setup.validate_server_url(lane.server, mode)
        run_id = require_uuid(run_id)
        headers = setup._headers(server, None)
    except Exception:  # noqa: BLE001 — credential errors must not expose values
        return None, None
    with httpx.Client(
        base_url=f"{server}/",
        headers=headers,
        timeout=5,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    ) as client:
        probe = setup._Probe(client, [])
        path = f"v1/studio/review-runs/{run_id}"
        try:
            run = ReviewRun.model_validate(probe.get("run", path), strict=True)
            question = None
            if question_id is not None:
                question_id = require_uuid(question_id)
                question = ReviewQuestion.model_validate(
                    probe.get("question", f"{path}/discussion/questions/{question_id}"),
                    strict=True,
                )
        except (ValidationError, ValueError):
            return None, None
    return run, question


def _route_mode(name: Route) -> Mode:
    return "server" if name.startswith("server:") else "local"


def _validate_lane_url(server: str, mode: Mode) -> None:
    setup.validate_server_url(server, mode)
    if mode == "local":
        return
    url = urlsplit(server)
    host = (url.hostname or "").rstrip(".").lower()
    try:
        address = ipaddress.ip_address(host)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        local = address.is_loopback or address.is_unspecified
    except ValueError:
        local = host == "localhost" or host.endswith(".localhost")
    if url.scheme != "https" or local:
        raise ValueError("A server lane requires a non-loopback HTTPS instance")


def _lane(
    name: Route,
    lane: Lane,
    pr: str,
    checkout: str | None,
    root: Path,
    transport: httpx.BaseTransport | None,
) -> dict[str, Any]:
    mode = _route_mode(name)
    _validate_lane_url(lane.server, mode)
    harness = "codex-native" if name.endswith(":codex") else "claude-native"
    run_id = require_uuid(lane.run_id)
    review_id = require_uuid(lane.review_id)
    question_id = require_uuid(lane.question_id)
    preflight = setup.collect_setup(
        mode=mode,
        server_url=lane.server,
        host_id=lane.host_id,
        workspace=lane.workspace,
        harnesses=[harness],
        required_skills=lane.skills,
        required_mcps=lane.mcp,
        transport=transport,
    )
    run, question = _saved_run(mode, lane, run_id, question_id=question_id, transport=transport)
    bound = bool(
        run is not None
        and run.id == run_id
        and _matches_pr_url(run.target.pr_url, pr)
        and (run.target.base_sha, run.target.head_sha) == (lane.base_sha, lane.head_sha)
        and (run.target.host_id, run.target.workspace) == (lane.host_id, lane.workspace)
        and any(
            member.id == review_id and member.profile.harness == harness
            for member in run.selected_reviewers()
        )
        and question is not None
        and question.id == question_id
        and question.harness == harness
    )
    review = collect_review(
        mode=mode,
        server_url=lane.server,
        review_id=review_id,
        harness=harness,
        pr_url=run.target.pr_url if bound and run is not None else pr,
        transport=transport,
    )
    lifecycle = collect_lifecycle(
        mode=mode,
        server_url=lane.server,
        run_id=run_id,
        pr_url=pr,
        finding_key=lane.finding_key,
        question_id=question_id,
        candidate_id=lane.candidate_id,
        transport=transport,
    )
    observations = {
        key: _observation(getattr(lane, key), root, checkout, {run_id})
        for key in ("workflow", "disconnect", "restart", "desktop_browser")
    }
    source_matches = checkout is not None and preflight["server"]["source_commit"] == checkout
    api_ready = bool(
        preflight["preflight_ready"]
        and source_matches
        and bound
        and review["review_ready"]
        and lifecycle["lifecycle_ready"]
    )
    return {
        "status": "verified"
        if api_ready
        and all(result == "operator_record_verified" for result in observations.values())
        else "incomplete",
        "api_ready": api_ready,
        "source_matches": source_matches,
        "run_and_harness_bound": bound,
        "setup": preflight,
        "review": review,
        "lifecycle": lifecycle,
        "operational_evidence": observations,
    }


def _second_repository(
    evidence: ReleaseEvidence,
    pr: PullRequestRef,
    checkout_path: str,
    transport: httpx.BaseTransport | None,
) -> str:
    second = evidence.second_repository
    if second is None or (lane := evidence.routes.get(second.lane)) is None:
        return "missing"
    ref = PullRequestRef.from_url(second.pr)
    if (ref.provider, ref.host.casefold(), ref.repository.casefold()) == (
        pr.provider,
        pr.host.casefold(),
        pr.repository.casefold(),
    ):
        return "same_repository"
    mode = _route_mode(second.lane)
    _validate_lane_url(lane.server, mode)
    run, _ = _saved_run(mode, lane, second.run_id, transport=transport)
    original, _ = _saved_run(mode, lane, lane.run_id, transport=transport)
    if (
        run is None
        or original is None
        or not _matches_pr_url(run.target.pr_url, ref.url)
        or run.target.source_project_id == original.target.source_project_id
        or run.target.host_id != lane.host_id
        or PurePosixPath(run.target.workspace, run.target.repository_path or ".")
        != PurePosixPath(checkout_path)
    ):
        return "scope_mismatch"
    receipt = collect_review_run(
        mode=mode,
        server_url=lane.server,
        run_id=second.run_id,
        pr_url=run.target.pr_url,
        base_sha=second.base_sha,
        head_sha=second.head_sha,
        transport=transport,
    )
    return "verified" if receipt["run_ready"] else "unconfirmed"


def _publication(
    evidence: ReleaseEvidence, root: Path, transport: httpx.BaseTransport | None
) -> str:
    item = evidence.publication
    if item is None or (lane := evidence.routes.get(item.lane)) is None:
        return "missing"
    with _artifact(root, item.preview).open("rb") as file:
        raw = file.read(_MAX_JSON_BYTES + 1)
    if len(raw) > _MAX_JSON_BYTES:
        return "invalid_preview"
    preview = InlinePublicationPreview.model_validate_json(raw)
    mode = _route_mode(item.lane)
    _validate_lane_url(lane.server, mode)
    result = collect_publication(
        mode=mode,
        server_url=lane.server,
        preview=preview,
        draft_id=item.draft_id,
        author=item.author,
        transport=transport,
    )
    return "verified" if result["publication_ready"] else "unconfirmed"


def _head_transition(evidence: ReleaseEvidence, pr: PullRequestRef, root: Path) -> str:
    observation = evidence.provider_head_advanced
    if observation is None:
        return "missing"
    try:
        if PullRequestRef.from_url(observation.pr) != pr:
            return "scope_mismatch"
    except ValueError:
        return "scope_mismatch"
    if observation.previous_head == observation.new_head or not any(
        lane.run_id == observation.run_id and lane.head_sha == observation.previous_head
        for lane in evidence.routes.values()
    ):
        return "scope_mismatch"
    return _observation(observation, root, evidence.checkout, {observation.run_id})


def collect_release_a(
    *,
    pr_url: str,
    second_repo: str,
    manifest: Path | None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Verify saved state by API and distinguish recorded operator observations."""
    pr = PullRequestRef.from_url(pr_url)
    if not PurePosixPath(second_repo).is_absolute():
        raise ValueError("Provide the absolute checkout path for the second repository")
    root = manifest.resolve().parent if manifest is not None else Path.cwd()
    evidence = read_evidence(manifest) if manifest is not None else ReleaseEvidence()
    routes: dict[str, Any] = {}
    for name in ROUTES:
        lane = evidence.routes.get(name)
        if lane is None:
            routes[name] = {"status": "missing"}
            continue
        try:
            routes[name] = _lane(name, lane, pr.url, evidence.checkout, root, transport)
        except (OSError, ValueError):
            routes[name] = {"status": "invalid"}

    checks = {"provider_head_advanced": _head_transition(evidence, pr, root)}
    for key, collect in (
        ("second_repository", lambda: _second_repository(evidence, pr, second_repo, transport)),
        ("live_inline_publication", lambda: _publication(evidence, root, transport)),
    ):
        try:
            checks[key] = collect()
        except (OSError, ValueError):
            checks[key] = "invalid"
    ready = all(route["status"] == "verified" for route in routes.values()) and all(
        result in ("verified", "operator_record_verified") for result in checks.values()
    )
    return {
        "schema": "omnigent.studio.release-a.v1",
        "observed_at": datetime.now(UTC).isoformat(),
        "checkout": evidence.checkout,
        "pr": pr.url,
        "routes": routes,
        "cross_route_checks": checks,
        "release_ready": ready,
    }
