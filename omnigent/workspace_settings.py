"""Bounded, revision-checked access to workspace-owned settings files."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from omnigent.workspace_fs import WorkspaceReaderError


@contextmanager
def _directory(root: Path) -> Iterator[int]:
    descriptor = os.open(root.resolve(strict=True), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _read(directory: int, filename: str, limit: int) -> tuple[bytes | None, str]:
    try:
        descriptor = os.open(
            filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
    except FileNotFoundError:
        return None, "missing"
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise WorkspaceReaderError(400, "invalid_settings", "Invalid workspace settings file.")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise WorkspaceReaderError(
            400, "invalid_settings", "Workspace settings file is too large."
        )
    return data, hashlib.sha256(data).hexdigest()


def read_workspace_settings(root: Path, filename: str, limit: int) -> tuple[bytes | None, str]:
    """Read a fixed settings filename at the workspace root."""
    if os.name == "nt":
        # Keep manifest-only repository discovery available on legacy hosts.
        if not os.path.lexists(root / filename):
            return None, "missing"
        raise WorkspaceReaderError(
            400, "unsupported_host", "Workspace settings require a macOS or Linux host."
        )
    try:
        with _directory(root) as directory:
            return _read(directory, filename, limit)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceReaderError(
            400, "invalid_settings", "Could not read workspace settings."
        ) from exc


def replace_workspace_settings(
    root: Path,
    filename: str,
    limit: int,
    revision: str,
    update: Callable[[bytes | None], bytes],
) -> None:
    """Apply a validated update to a fixed settings file if its revision still matches."""
    import fcntl

    def conflict() -> WorkspaceReaderError:
        return WorkspaceReaderError(
            409, "stale_settings", "Workspace settings changed. Reload before saving."
        )

    try:
        with _directory(root) as directory:
            try:
                fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise conflict() from exc
            data, current = _read(directory, filename, limit)
            if current != revision:
                raise conflict()
            replacement = update(data)
            if len(replacement) > limit:
                raise WorkspaceReaderError(
                    400, "invalid_settings", "Workspace settings file is too large."
                )
            temporary = f"{filename}.{secrets.token_hex(8)}.tmp"
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=directory
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(replacement)
                    stream.flush()
                    os.fsync(stream.fileno())
                if _read(directory, filename, limit)[1] != revision:
                    raise conflict()
                os.replace(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceReaderError(
            400, "settings_write_failed", "Could not save workspace settings."
        ) from exc
