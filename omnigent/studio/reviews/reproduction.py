"""Run an approved command on two exported Git revisions in disposable containers."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import selectors
import subprocess
import tarfile
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal

from pydantic import AwareDatetime, Field, RootModel, field_validator, model_validator

from omnigent.studio.reviews.evidence_files import (
    EVIDENCE_LIMITS,
    MAX_CAPTURED_FILES,
    CapturedEvidenceFile,
    evidence_type_for_name,
)
from omnigent.studio.reviews.models import FrozenModel, ReviewRoleDigest, Sha

_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_ARCHIVE_LIMIT = 128 * 1024 * 1024
_OUTPUT_LIMIT = 8 * 1024
REPRODUCTION_CHUNK_BYTES = 12 * 1024
MAX_REPRODUCTION_CHUNKS = 16
MAX_REPRODUCTION_BYTES = MAX_REPRODUCTION_CHUNKS * REPRODUCTION_CHUNK_BYTES
MAX_FIX_PATCH_BYTES = 128 * 1024
_ENGINE_LIMIT = 10
_START_MARKER = b"Omnigent reproduction started\n"
_COMMAND = """
seconds="$1"
shift
exec timeout -s KILL "$seconds" /bin/sh -c '
  tar -xf - -C /work 2>/dev/null || exit 125
  cd /work || exit 125
  printf "Omnigent reproduction started\\n"
  exec "$@" </dev/null
' reproduction "$@"
"""


class ReproductionError(RuntimeError):
    """The requested evidence could not be collected safely."""


class ReproductionSide(FrozenModel):
    commit: Sha
    archive_sha256: ReviewRoleDigest
    archive_bytes: int = Field(gt=0, le=_ARCHIVE_LIMIT)
    state: Literal["exited", "timed_out", "setup_error"]
    exit_code: int | None = Field(default=None, ge=0, le=255)
    oom_killed: bool = False
    elapsed_ms: int = Field(ge=0)
    output: str = Field(max_length=_OUTPUT_LIMIT)
    output_sha256: ReviewRoleDigest
    output_bytes: int = Field(ge=0, le=2**63 - 1)
    output_truncated: bool
    evidence: tuple[CapturedEvidenceFile, ...] = Field(
        default=(), max_length=MAX_CAPTURED_FILES, exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def valid_evidence(self) -> ReproductionSide:
        if self.evidence and (
            self.state != "exited"
            or self.oom_killed
            or len({item.name for item in self.evidence}) != len(self.evidence)
        ):
            raise ValueError("Evidence must identify distinct files from an exited check")
        return self


class ReproductionSource(FrozenModel):
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    pr_url: str
    patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_sha: Sha
    head_sha: Sha


class ReproductionReceipt(FrozenModel):
    schema_version: Literal["omnigent.studio.reproduction.v1"] = "omnigent.studio.reproduction.v1"
    observed_at: AwareDatetime
    runtime: Literal["docker-no-network"] = "docker-no-network"
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    command: tuple[str, ...] = Field(min_length=1, max_length=8192)
    timeout_seconds: int = Field(ge=1, le=300)
    source: ReproductionSource | None = None
    included_paths: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)
    base: ReproductionSide
    head: ReproductionSide

    @field_validator("included_paths")
    @classmethod
    def validate_included_paths(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        _validate_export_paths(paths)
        return paths

    @field_validator("command")
    @classmethod
    def validate_command(cls, command: tuple[str, ...]) -> tuple[str, ...]:
        if (
            not command
            or not command[0]
            or command[0].startswith("-")
            or sum(len(arg.encode()) + 1 for arg in command) > 8192
        ):
            raise ValueError("Provide a reproduction command of at most 8192 UTF-8 bytes")
        if any("\0" in arg for arg in command):
            raise ValueError("The reproduction command contains a null byte")
        return command


FixOutcome = Literal[
    "fail_to_pass", "pass_to_pass", "fail_to_fail", "pass_to_fail", "inconclusive"
]


def _fix_outcome(before: ReproductionSide, after: ReproductionSide) -> FixOutcome:
    if any(
        side.state != "exited" or side.oom_killed or side.exit_code is None
        for side in (before, after)
    ):
        return "inconclusive"
    if before.exit_code == 0:
        return "pass_to_pass" if after.exit_code == 0 else "pass_to_fail"
    return "fail_to_pass" if after.exit_code == 0 else "fail_to_fail"


class FixVerificationReceipt(FrozenModel):
    schema_version: Literal["omnigent.studio.fix-verification.v1"] = (
        "omnigent.studio.fix-verification.v1"
    )
    observed_at: AwareDatetime
    runtime: Literal["docker-no-network"] = "docker-no-network"
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source: ReproductionSource
    proposed_patch_sha256: ReviewRoleDigest
    included_paths: tuple[str, ...]
    command: tuple[str, ...]
    timeout_seconds: int = Field(ge=1, le=300)
    before: ReproductionSide
    after: ReproductionSide

    outcome: FixOutcome

    @model_validator(mode="after")
    def validate_comparison(self) -> FixVerificationReceipt:
        ReproductionReceipt.validate_command(self.command)
        _validate_export_paths(self.included_paths)
        if (
            self.before.commit != self.source.head_sha
            or self.after.commit == self.before.commit
            or len(self.after.commit) != len(self.before.commit)
            or self.outcome != _fix_outcome(self.before, self.after)
        ):
            raise ValueError("The fix results do not match the captured head and outcome")
        return self


class FixCheckEvidence(FixVerificationReceipt):
    patch: str = Field(min_length=1, max_length=MAX_FIX_PATCH_BYTES)

    @field_validator("patch")
    @classmethod
    def validate_patch(cls, patch: str) -> str:
        validate_fix_patch(patch.encode())
        return patch

    @model_validator(mode="after")
    def validate_patch_digest(self) -> FixCheckEvidence:
        if hashlib.sha256(self.patch.encode()).hexdigest() != self.proposed_patch_sha256:
            raise ValueError("The proposed patch differs from the patch used in the fix check")
        return self


ReviewCheckReceipt = ReproductionReceipt | FixCheckEvidence


class ReviewCheckUpload(RootModel[ReviewCheckReceipt]):
    pass


class AttachedReproduction(FrozenModel):
    digest: ReviewRoleDigest
    attached_at: int
    provenance: Literal["uploaded"] = "uploaded"
    candidate_run_id: str | None = None
    receipt: ReviewCheckReceipt


class SharedReproductionReceipt(FrozenModel):
    observed_at: AwareDatetime
    image_id: str
    timeout_seconds: int
    included_paths: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)
    base: ReproductionSide
    head: ReproductionSide


class SharedFixCheckEvidence(FrozenModel):
    observed_at: AwareDatetime
    image_id: str
    timeout_seconds: int
    before: ReproductionSide
    after: ReproductionSide
    outcome: FixOutcome
    included_paths: tuple[str, ...]
    patch: str


class SharedAttachedReproduction(FrozenModel):
    digest: ReviewRoleDigest
    attached_at: int
    provenance: Literal["uploaded"] = "uploaded"
    receipt: SharedReproductionReceipt | SharedFixCheckEvidence

    @classmethod
    def from_attachment(cls, entry: AttachedReproduction) -> SharedAttachedReproduction:
        receipt = entry.receipt
        if isinstance(receipt, FixCheckEvidence):
            shared = SharedFixCheckEvidence(
                observed_at=receipt.observed_at,
                image_id=receipt.image_id,
                timeout_seconds=receipt.timeout_seconds,
                before=receipt.before,
                after=receipt.after,
                outcome=receipt.outcome,
                included_paths=receipt.included_paths,
                patch=receipt.patch,
            )
            return cls(digest=entry.digest, attached_at=entry.attached_at, receipt=shared)
        return cls(
            digest=entry.digest,
            attached_at=entry.attached_at,
            receipt=SharedReproductionReceipt(
                observed_at=receipt.observed_at,
                image_id=receipt.image_id,
                timeout_seconds=receipt.timeout_seconds,
                included_paths=receipt.included_paths,
                base=receipt.base,
                head=receipt.head,
            ),
        )


@dataclass(frozen=True)
class _Capture:
    status: int | None
    size: int
    digest: str


def _capture(
    command: list[str],
    *,
    destination: BinaryIO,
    timeout: int,
    limit: int,
    truncate: bool,
    stdin: BinaryIO | None = None,
    combine_stderr: bool = False,
) -> _Capture:
    """Drain a subprocess with a deadline and bounded retained output."""
    deadline = time.monotonic() + timeout
    digest = hashlib.sha256()
    size = 0
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ALLOW_PROTOCOL": "",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    with subprocess.Popen(
        command,
        stdin=stdin if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if combine_stderr else subprocess.DEVNULL,
        env=env,
    ) as process:
        assert process.stdout is not None
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.select(max(0.0, deadline - time.monotonic())):
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    chunk = os.read(process.stdout.fileno(), 64 * 1024)
                    if not chunk:
                        return _Capture(
                            process.wait(timeout=max(0.0, deadline - time.monotonic())),
                            size,
                            digest.hexdigest(),
                        )
                    retained = max(0, limit - size)
                    if not truncate and len(chunk) > retained:
                        raise ReproductionError("Output exceeds the reproduction byte limit")
                    destination.write(chunk[:retained])
                    digest.update(chunk)
                    size += len(chunk)
            raise TimeoutError
        except (TimeoutError, subprocess.TimeoutExpired):
            process.kill()
            process.wait()
            return _Capture(None, size, digest.hexdigest())
        except BaseException:
            process.kill()
            process.wait()
            raise


def _metadata(command: list[str], error: str, *, stdin: BinaryIO | None = None) -> bytes:
    output = io.BytesIO()
    try:
        captured = _capture(
            command,
            destination=output,
            stdin=stdin,
            timeout=_ENGINE_LIMIT,
            limit=4096,
            truncate=False,
        )
    except OSError as exc:
        raise ReproductionError(error) from exc
    if captured.status != 0:
        raise ReproductionError(error)
    return output.getvalue().rstrip(b"\r\n")


def _engine(docker: list[str], *args: str) -> str:
    return _metadata([*docker, *args], f"Docker {args[0]} failed").decode("utf-8")


def _prepare_archive_repository(repository: Path, bare: Path, *, object_format: str) -> None:
    objects = _metadata(
        [
            "git",
            "-C",
            str(repository),
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "objects",
        ],
        "Could not resolve the checkout's Git objects",
    )
    object_path = Path(os.fsdecode(objects))
    if not object_path.is_absolute() or not object_path.is_dir() or b"\n" in objects:
        raise ReproductionError("The checkout's Git object directory is invalid")
    _metadata(
        ["git", "init", "--bare", "--template=", f"--object-format={object_format}", str(bare)],
        "Could not create an isolated Git export repository",
    )
    (bare / "objects" / "info" / "alternates").write_bytes(objects + b"\n")


def _export(bare: Path, commit: str, tree: BinaryIO, *, paths: tuple[str, ...] = ()) -> _Capture:
    git = ["git", "--no-replace-objects", "--literal-pathspecs", f"--git-dir={bare}"]
    try:
        export = _capture(
            [*git, "archive", "--format=tar", commit, "--", *paths],
            destination=tree,
            timeout=60,
            limit=_ARCHIVE_LIMIT,
            truncate=False,
        )
        if export.status != 0:
            raise ReproductionError(f"Commit {commit} could not be exported from the checkout")
        tree.seek(0)
        identified = _metadata(
            ["git", "get-tar-commit-id"], "The exported tree has no commit ID", stdin=tree
        )
        if identified != commit.encode("ascii"):
            raise ReproductionError("The exported tree does not match the requested commit")
    except OSError as exc:
        raise ReproductionError(f"Commit {commit} could not be exported") from exc
    tree.seek(0)
    return export


def _stage_fix(bare: Path, head: str, patch: bytes) -> str:
    """Apply a proposal to a private index and create an ephemeral commit for export."""
    git = ["git", "--no-replace-objects", f"--git-dir={bare}"]
    if _metadata([*git, "cat-file", "-t", head], "Captured head is unavailable") != b"commit":
        raise ReproductionError("The captured head does not identify a Git commit")
    _metadata([*git, "read-tree", head], "Could not initialize the proposed fix index")
    with tempfile.TemporaryFile() as input_file:
        input_file.write(patch)
        input_file.seek(0)
        _metadata(
            [*git, "apply", "--cached", "--whitespace=nowarn", "-"],
            "The proposed patch does not apply cleanly to the captured head",
            stdin=input_file,
        )
    tree = _metadata([*git, "write-tree"], "Could not write the proposed fix tree")
    head_tree = _metadata([*git, "rev-parse", f"{head}^{{tree}}"], "Could not read the head tree")
    if tree == head_tree:
        raise ReproductionError("The proposed patch does not change the captured head")
    # Fixed metadata makes identical proposals produce the same commit and archive.
    identity = b"Omnigent Studio <studio@example.invalid> 0 +0000"
    with tempfile.TemporaryFile() as commit:
        commit.write(
            b"tree "
            + tree
            + b"\nparent "
            + head.encode("ascii")
            + b"\nauthor "
            + identity
            + b"\ncommitter "
            + identity
            + b"\n\nStudio fix preview\n"
        )
        commit.seek(0)
        return _metadata(
            [*git, "hash-object", "-t", "commit", "-w", "--stdin"],
            "Could not create the proposed fix snapshot",
            stdin=commit,
        ).decode("ascii")


@dataclass(frozen=True)
class EvidenceCollection:
    names: tuple[str, ...]
    directory: Path

    def __post_init__(self) -> None:
        if not 1 <= len(self.names) <= MAX_CAPTURED_FILES or len(set(self.names)) != len(
            self.names
        ):
            raise ValueError(f"Select 1 to {MAX_CAPTURED_FILES} distinct evidence files")
        for name in self.names:
            evidence_type_for_name(name)


def _collect_evidence_file(
    docker: list[str], container: str, filename: str, destination: Path
) -> CapturedEvidenceFile | None:
    from omnigent.studio.reviews.artifacts import inspect_evidence

    media_type = evidence_type_for_name(filename)
    limit = EVIDENCE_LIMITS[media_type]
    output = io.BytesIO()
    try:
        copied = _capture(
            [*docker, "exec", container, "tar", "-cf", "-", "-C", "/evidence", filename],
            destination=output,
            timeout=_ENGINE_LIMIT,
            limit=limit + 64 * 1024,
            truncate=False,
        )
        if copied.status != 0:
            return None
        output.seek(0)
        with tarfile.open(fileobj=output, mode="r:") as archive:
            member = archive.next()
            if (
                member is None
                or member.name != filename
                or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE)
                or not 0 < member.size <= limit
            ):
                return None
            stream = archive.extractfile(member)
            if stream is None:
                return None
            content = stream.read(limit + 1)
            if len(content) != member.size or archive.next() is not None:
                return None
        inspect_evidence(media_type, content)
    except (ReproductionError, tarfile.TarError, ValueError):
        return None
    with (destination / filename).open("xb") as file:
        file.write(content)
    return CapturedEvidenceFile(
        name=filename,
        digest=hashlib.sha256(content).hexdigest(),
        media_type=media_type,
        byte_size=len(content),
    )


def _run_side(
    docker: list[str],
    *,
    commit: str,
    tree: BinaryIO,
    archive: _Capture,
    image_id: str,
    command: tuple[str, ...],
    timeout: int,
    collection: EvidenceCollection | None = None,
) -> ReproductionSide:
    name = f"omnigent-proof-{uuid.uuid4().hex}"
    created = False
    try:
        _engine(
            docker,
            "create",
            "--name",
            name,
            "--interactive",
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "128",
            "--memory",
            "1g",
            "--memory-swap",
            "1g",
            "--cpus",
            "2",
            "--user",
            "65532:65532",
            "--tmpfs",
            "/work:rw,nodev,nosuid,exec,size=256m,mode=1777",
            "--tmpfs",
            "/tmp:rw,nodev,nosuid,exec,size=128m,mode=1777",
            *(
                ["--tmpfs", "/evidence:rw,nodev,nosuid,noexec,size=32m,mode=1777"]
                if collection is not None
                else []
            ),
            "--env",
            "HOME=/tmp",
            "--log-driver",
            "none",
            "--no-healthcheck",
            "--entrypoint",
            "/bin/sh",
            image_id,
            "-c",
            *(
                ["while :; do sleep 60; done"]
                if collection is not None
                else [_COMMAND, "omnigent", str(timeout + _ENGINE_LIMIT), *command]
            ),
        )
        created = True
        if collection is not None:
            _engine(docker, "start", name)
            execution = [
                *docker,
                "exec",
                "--interactive",
                name,
                "/bin/sh",
                "-c",
                _COMMAND,
                "omnigent",
                str(timeout + _ENGINE_LIMIT),
                *command,
            ]
        else:
            execution = [*docker, "start", "--attach", "--interactive", name]
        started = time.monotonic()
        with tempfile.TemporaryFile() as output:
            observed = _capture(
                execution,
                stdin=tree,
                destination=output,
                timeout=timeout,
                limit=_OUTPUT_LIMIT,
                truncate=True,
                combine_stderr=True,
            )
            elapsed_ms = round((time.monotonic() - started) * 1000)
            output.seek(0)
            preview = output.read()
        state: dict[str, object] = {}
        exit_code = None
        if observed.status is not None:
            state = json.loads(_engine(docker, "inspect", "--format", "{{json .State}}", name))
            if collection is not None:
                if state.get("Status") != "running":
                    raise ReproductionError("The evidence container stopped before collection")
                exit_code = observed.status
            else:
                exit_code = state.get("ExitCode")
                if state.get("Status") != "exited" or type(exit_code) is not int:
                    raise ReproductionError("Docker did not confirm the reproduction exit")
        outcome = (
            "timed_out"
            if observed.status is None
            else "exited"
            if preview.startswith(_START_MARKER) and exit_code not in {126, 127}
            else "setup_error"
        )
        evidence = ()
        if collection is not None and outcome == "exited" and state.get("OOMKilled") is not True:
            collection.directory.mkdir(mode=0o700)
            evidence = tuple(
                result
                for filename in collection.names
                if (result := _collect_evidence_file(docker, name, filename, collection.directory))
                is not None
            )
        return ReproductionSide(
            commit=commit,
            archive_sha256=archive.digest,
            archive_bytes=archive.size,
            state=outcome,
            exit_code=exit_code,
            oom_killed=state.get("OOMKilled") is True,
            elapsed_ms=elapsed_ms,
            output=preview.decode("utf-8", errors="replace"),
            output_sha256=observed.digest,
            output_bytes=observed.size,
            output_truncated=observed.size > len(preview),
            evidence=evidence,
        )
    except (ReproductionError, KeyboardInterrupt) as exc:
        if not created:
            try:
                _engine(docker, "rm", "--force", "--volumes", name)
            except ReproductionError:
                raise ReproductionError(
                    f"Docker could not confirm creation or cleanup of {name}"
                ) from exc
        raise
    finally:
        if created:
            try:
                _engine(docker, "rm", "--force", "--volumes", name)
            except ReproductionError as exc:
                raise ReproductionError(f"Docker could not remove container {name}") from exc


def _compare_revisions(
    *,
    repository: Path,
    base: str,
    head: str,
    image: str,
    command: tuple[str, ...],
    socket: Path,
    timeout: int,
    proposed_patch: bytes | None = None,
    paths: tuple[str, ...] = (),
    collection: EvidenceCollection | None = None,
) -> tuple[str, ReproductionSide, ReproductionSide]:
    if not all(_SHA.fullmatch(commit) for commit in (base, head)):
        raise ValueError("Provide both full lowercase Git commit IDs")
    if len(base) != len(head):
        raise ValueError("Both commits must use the same Git object format")
    if not socket.is_absolute() or not repository.is_dir() or not 1 <= timeout <= 300:
        raise ValueError("Provide a local checkout, an absolute Docker socket, and 1–300 seconds")
    ReproductionReceipt.validate_command(command)
    if not image or len(image) > 256 or image.startswith("-"):
        raise ValueError("Provide an installed Docker image")
    with (
        tempfile.TemporaryDirectory(prefix="omnigent-proof-") as temporary,
        tempfile.TemporaryFile() as base_tree,
        tempfile.TemporaryFile() as head_tree,
    ):
        config = Path(temporary) / "docker"
        config.mkdir()
        bare = Path(temporary) / "export.git"
        _prepare_archive_repository(
            repository, bare, object_format="sha1" if len(base) == 40 else "sha256"
        )
        if proposed_patch is not None:
            head = _stage_fix(bare, base, proposed_patch)
        docker = ["docker", "--config", str(config), "--host", f"unix://{socket}"]
        metadata = _engine(
            docker, "image", "inspect", "--format", "{{.Id}} {{json .Config.Volumes}}", image
        )
        image_id, _, volumes = metadata.partition(" ")
        if _IMAGE_ID.fullmatch(image_id) is None:
            raise ReproductionError("Docker did not resolve an immutable local image ID")
        if volumes not in ("null", "{}"):
            raise ReproductionError("Choose an image without declared Docker volumes")
        base_archive = _export(bare, base, base_tree, paths=paths)
        head_archive = _export(bare, head, head_tree, paths=paths)
        before, after = (
            _run_side(
                docker,
                commit=commit,
                tree=tree,
                archive=archive,
                image_id=image_id,
                command=command,
                timeout=timeout,
                collection=(
                    EvidenceCollection(collection.names, collection.directory / side)
                    if collection is not None
                    else None
                ),
            )
            for side, commit, tree, archive in (
                ("base", base, base_tree, base_archive),
                ("head", head, head_tree, head_archive),
            )
        )
    return image_id, before, after


def reproduce(
    *,
    repository: Path,
    base: str,
    head: str,
    image: str,
    command: tuple[str, ...],
    socket: Path = Path("/var/run/docker.sock"),
    timeout: int = 60,
    source: ReproductionSource | None = None,
    collection: EvidenceCollection | None = None,
    paths: tuple[str, ...] = (),
) -> ReproductionReceipt:
    """Observe one command on two Git exports without mounting the host checkout."""
    if source is not None and (source.base_sha, source.head_sha) != (base, head):
        raise ValueError("The reproduction commits do not match the saved review run")
    _validate_export_paths(paths)
    image_id, before, after = _compare_revisions(
        repository=repository,
        base=base,
        head=head,
        image=image,
        command=command,
        socket=socket,
        timeout=timeout,
        collection=collection,
        paths=paths,
    )
    return ReproductionReceipt(
        observed_at=datetime.now(UTC),
        image_id=image_id,
        command=command,
        timeout_seconds=timeout,
        source=source,
        included_paths=paths,
        base=before,
        head=after,
    )


def validate_fix_patch(patch: bytes) -> None:
    if not 0 < len(patch) <= MAX_FIX_PATCH_BYTES or b"\0" in patch:
        raise ValueError("Provide a Git text patch of at most 128 KiB")
    try:
        text = patch.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("The proposed patch must be UTF-8 text") from exc
    if "GIT binary patch" in text.splitlines():
        raise ValueError("The proposed patch must contain text changes")


def _validate_export_paths(paths: tuple[str, ...]) -> None:
    if len(paths) > 32 or any(
        len(path.encode()) > 1024
        or path in ("", ".")
        or not path.isprintable()
        or PurePosixPath(path).is_absolute()
        or ".." in PurePosixPath(path).parts
        or str(PurePosixPath(path)) != path
        for path in paths
    ):
        raise ValueError("Provide up to 32 relative Git paths, without parent traversal")


def verify_fix(
    *,
    repository: Path,
    source: ReproductionSource,
    patch: bytes,
    image: str,
    command: tuple[str, ...],
    socket: Path = Path("/var/run/docker.sock"),
    timeout: int = 60,
    paths: tuple[str, ...] = (),
) -> FixVerificationReceipt:
    """Compare the captured head with the same head plus a proposed text patch."""
    validate_fix_patch(patch)
    _validate_export_paths(paths)
    image_id, before, after = _compare_revisions(
        repository=repository,
        base=source.head_sha,
        head=source.head_sha,
        image=image,
        command=command,
        socket=socket,
        timeout=timeout,
        proposed_patch=patch,
        paths=paths,
    )
    return FixVerificationReceipt(
        observed_at=datetime.now(UTC),
        image_id=image_id,
        source=source,
        proposed_patch_sha256=hashlib.sha256(patch).hexdigest(),
        included_paths=paths,
        command=command,
        timeout_seconds=timeout,
        before=before,
        after=after,
        outcome=_fix_outcome(before, after),
    )
