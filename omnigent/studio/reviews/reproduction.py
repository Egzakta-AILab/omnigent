"""Run an approved command on two exported Git revisions in disposable containers."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import selectors
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Literal

from pydantic import AwareDatetime, Field, field_validator

from omnigent.studio.reviews.models import FrozenModel, ReviewRoleDigest, Sha

_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_ARCHIVE_LIMIT = 128 * 1024 * 1024
_OUTPUT_LIMIT = 8 * 1024
REPRODUCTION_CHUNK_BYTES = 12 * 1024
MAX_REPRODUCTION_CHUNKS = 16
MAX_REPRODUCTION_BYTES = MAX_REPRODUCTION_CHUNKS * REPRODUCTION_CHUNK_BYTES
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
    base: ReproductionSide
    head: ReproductionSide

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


class AttachedReproduction(FrozenModel):
    digest: ReviewRoleDigest
    attached_at: int
    provenance: Literal["uploaded"] = "uploaded"
    receipt: ReproductionReceipt


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


def _export(bare: Path, commit: str, tree: BinaryIO) -> _Capture:
    git = ["git", "--no-replace-objects", f"--git-dir={bare}"]
    try:
        export = _capture(
            [*git, "archive", "--format=tar", commit],
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


def _run_side(
    docker: list[str],
    *,
    commit: str,
    tree: BinaryIO,
    archive: _Capture,
    image_id: str,
    command: tuple[str, ...],
    timeout: int,
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
            "--env",
            "HOME=/tmp",
            "--log-driver",
            "none",
            "--no-healthcheck",
            "--entrypoint",
            "/bin/sh",
            image_id,
            "-c",
            _COMMAND,
            "omnigent",
            str(timeout + _ENGINE_LIMIT),
            *command,
        )
        created = True
        started = time.monotonic()
        with tempfile.TemporaryFile() as output:
            observed = _capture(
                [*docker, "start", "--attach", "--interactive", name],
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
            exit_code = state.get("ExitCode")
            if state.get("Status") != "exited" or type(exit_code) is not int:
                raise ReproductionError("Docker did not confirm the reproduction exit")
        return ReproductionSide(
            commit=commit,
            archive_sha256=archive.digest,
            archive_bytes=archive.size,
            state=(
                "timed_out"
                if observed.status is None
                else "exited"
                if preview.startswith(_START_MARKER) and exit_code not in {126, 127}
                else "setup_error"
            ),
            exit_code=exit_code,
            oom_killed=state.get("OOMKilled") is True,
            elapsed_ms=elapsed_ms,
            output=preview.decode("utf-8", errors="replace"),
            output_sha256=observed.digest,
            output_bytes=observed.size,
            output_truncated=observed.size > len(preview),
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
) -> ReproductionReceipt:
    """Observe one command on two Git exports without mounting the host checkout."""
    if not all(_SHA.fullmatch(commit) for commit in (base, head)):
        raise ValueError("Provide both full lowercase Git commit IDs")
    if len(base) != len(head):
        raise ValueError("Both commits must use the same Git object format")
    if source is not None and (source.base_sha, source.head_sha) != (base, head):
        raise ValueError("The reproduction commits do not match the saved review run")
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
        docker = ["docker", "--config", str(config), "--host", f"unix://{socket}"]
        metadata = _engine(
            docker, "image", "inspect", "--format", "{{.Id}} {{json .Config.Volumes}}", image
        )
        image_id, _, volumes = metadata.partition(" ")
        if _IMAGE_ID.fullmatch(image_id) is None:
            raise ReproductionError("Docker did not resolve an immutable local image ID")
        if volumes not in ("null", "{}"):
            raise ReproductionError("Choose an image without declared Docker volumes")
        bare = Path(temporary) / "export.git"
        _prepare_archive_repository(
            repository, bare, object_format="sha1" if len(base) == 40 else "sha256"
        )
        base_archive = _export(bare, base, base_tree)
        head_archive = _export(bare, head, head_tree)
        sides = [
            _run_side(
                docker,
                commit=commit,
                tree=tree,
                archive=archive,
                image_id=image_id,
                command=command,
                timeout=timeout,
            )
            for commit, tree, archive in (
                (base, base_tree, base_archive),
                (head, head_tree, head_archive),
            )
        ]
    return ReproductionReceipt(
        observed_at=datetime.now(UTC),
        image_id=image_id,
        command=command,
        timeout_seconds=timeout,
        source=source,
        base=sides[0],
        head=sides[1],
    )
