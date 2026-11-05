"""Read portable review roles from an explicitly configured workspace."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from omnigent.workspace_fs import WorkspaceReaderError

_MANIFEST = "review-profiles.yaml"
_MAX_MANIFEST_BYTES = 32 * 1024
_MAX_PROFILE_BYTES = 8 * 1024
_MAX_PROFILES = 32
_PROFILE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def _invalid_profiles() -> WorkspaceReaderError:
    return WorkspaceReaderError(
        400,
        "invalid_review_profiles",
        "Check workspace review-profiles.yaml and its Markdown files.",
    )


class _ManifestLoader(yaml.SafeLoader):
    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise _invalid_profiles()
        return super().compose_node(parent, index)

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        mapping = super().construct_mapping(node, deep)
        if len(mapping) != len(node.value):
            raise _invalid_profiles()
        return mapping


@contextmanager
def _open_directory(path: str | Path, parent: int | None = None) -> Iterator[int]:
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        yield directory
    finally:
        os.close(directory)


def _read_regular_file(directory: int, parts: list[str], limit: int) -> str:
    if len(parts) > 1:
        with _open_directory(parts[0], directory) as child:
            return _read_regular_file(child, parts[1:], limit)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(parts[0], flags, dir_fd=directory), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise _invalid_profiles()
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise _invalid_profiles()
    return raw.decode("utf-8-sig")


def _parse_manifest(raw: str) -> dict[str, dict[str, str]]:
    manifest = yaml.load(raw, Loader=_ManifestLoader)
    if not isinstance(manifest, dict) or type(manifest.get("version")) is not int:
        raise _invalid_profiles()
    profiles = manifest.get("profiles")
    if manifest["version"] != 1 or not isinstance(profiles, dict) or len(profiles) > _MAX_PROFILES:
        raise _invalid_profiles()
    result: dict[str, dict[str, str]] = {}
    for profile_id, entry in profiles.items():
        if (
            not isinstance(profile_id, str)
            or not _PROFILE_ID.fullmatch(profile_id)
            or not isinstance(entry, dict)
        ):
            raise _invalid_profiles()
        if entry.keys() - {"label", "description", "file"}:
            raise _invalid_profiles()
        label = entry.get("label")
        description = entry.get("description", "")
        file = entry.get("file")
        if (
            not isinstance(label, str)
            or not label.strip()
            or not label.isprintable()
            or len(label) > 128
            or not isinstance(description, str)
            or (description and not description.isprintable())
            or len(description) > 512
            or not isinstance(file, str)
            or not file.endswith(".md")
            or len(file) > 512
            or not file.isprintable()
            or "\\" in file
            or ":" in file
        ):
            raise _invalid_profiles()
        parts = file.split("/")
        if len(parts) > 16 or any(part in {"", ".", "..", ".git"} for part in parts):
            raise _invalid_profiles()
        result[profile_id] = {
            "label": label.strip(),
            "description": description.strip(),
            "file": file,
        }
    return result


def _instructions(raw: str) -> str:
    lines = raw.splitlines()
    if lines and lines[0] == "---":
        end = next(
            (index for index, line in enumerate(lines[1:], 1) if line in ("---", "...")), None
        )
        if end is None:
            raise _invalid_profiles()
        lines = lines[end + 1 :]
    body = "\n".join(lines).strip()
    if not body or "\x00" in body:
        raise _invalid_profiles()
    return body


def _read_profile(directory: int, profile_id: str, entry: dict[str, str]) -> dict[str, str]:
    instructions = _instructions(
        _read_regular_file(directory, entry["file"].split("/"), _MAX_PROFILE_BYTES)
    )
    metadata = {"id": profile_id, "label": entry["label"], "description": entry["description"]}
    fingerprint = json.dumps(
        {**metadata, "file": entry["file"], "instructions": instructions}, sort_keys=True
    )
    return {
        **metadata,
        "digest": hashlib.sha256(fingerprint.encode()).hexdigest(),
        "instructions": instructions,
    }


def read_review_profiles(root: Path, profile_id: object = None) -> dict[str, Any]:
    """List workspace role metadata or load one declared role's instructions."""
    if profile_id is not None and (
        not isinstance(profile_id, str) or not _PROFILE_ID.fullmatch(profile_id)
    ):
        raise WorkspaceReaderError(400, "invalid_review_profile", "Invalid review profile ID.")
    try:
        with _open_directory(root.resolve(strict=True)) as directory:
            try:
                raw = _read_regular_file(directory, [_MANIFEST], _MAX_MANIFEST_BYTES)
            except FileNotFoundError:
                raw = None
            profiles = _parse_manifest(raw) if raw is not None else {}
            if isinstance(profile_id, str):
                if profile_id not in profiles:
                    raise WorkspaceReaderError(
                        404,
                        "review_profile_missing",
                        "Review profile is not in review-profiles.yaml.",
                    )
                return _read_profile(directory, profile_id, profiles[profile_id])

            catalog = []
            for name, entry in profiles.items():
                metadata = _read_profile(directory, name, entry)
                metadata.pop("instructions")
                catalog.append(metadata)
            return {"profiles": catalog}
    except (OSError, RuntimeError, ValueError, yaml.YAMLError) as exc:
        raise _invalid_profiles() from exc
