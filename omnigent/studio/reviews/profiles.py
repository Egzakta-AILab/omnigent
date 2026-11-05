"""Read linked and manage editable review roles on their workspace host."""

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
from pydantic import ValidationError

from omnigent.studio.reviews.models import (
    MAX_REVIEW_INSTRUCTION_BYTES,
    ReviewProfileDraft,
    ReviewProfileEditorEntry,
    ReviewProfileEditorWrite,
)
from omnigent.workspace_fs import WorkspaceReaderError
from omnigent.workspace_settings import read_workspace_settings, replace_workspace_settings

_MANIFEST = "review-profiles.yaml"
_MANAGED = ".omnigent-review-profiles.json"
_MAX_MANIFEST_BYTES = 32 * 1024
_MAX_MANAGED_BYTES = 512 * 1024
_MAX_PROFILES = 32
_PROFILE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def _invalid_profiles() -> WorkspaceReaderError:
    return WorkspaceReaderError(
        400,
        "invalid_review_profiles",
        "Check workspace review-profiles.yaml, its Markdown files, and the managed catalog.",
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
        _read_regular_file(directory, entry["file"].split("/"), MAX_REVIEW_INSTRUCTION_BYTES)
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


def _read_linked_manifest(directory: int) -> dict[str, dict[str, str]]:
    try:
        raw = _read_regular_file(directory, [_MANIFEST], _MAX_MANIFEST_BYTES)
    except FileNotFoundError:
        return {}
    return _parse_manifest(raw)


def _parse_managed(raw: bytes | None) -> dict[str, ReviewProfileDraft]:
    if raw is None:
        return {}
    catalog = json.loads(raw)
    if (
        not isinstance(catalog, dict)
        or set(catalog) != {"version", "profiles"}
        or type(catalog["version"]) is not int
        or catalog["version"] != 1
        or not isinstance(catalog["profiles"], list)
        or len(catalog["profiles"]) > _MAX_PROFILES
    ):
        raise _invalid_profiles()
    profiles = [ReviewProfileDraft.model_validate(entry) for entry in catalog["profiles"]]
    by_id = {profile.id: profile for profile in profiles}
    if len(by_id) != len(profiles):
        raise _invalid_profiles()
    return by_id


def _managed_role(profile: ReviewProfileDraft) -> dict[str, Any]:
    role = profile.model_dump(exclude={"archived"}, exclude_none=True)
    fingerprint = json.dumps({**role, "file": _MANAGED}, sort_keys=True)
    return {**role, "digest": hashlib.sha256(fingerprint.encode()).hexdigest()}


def read_review_profiles(root: Path, profile_id: object = None) -> dict[str, Any]:
    """List workspace role metadata or load one declared role's instructions."""
    if profile_id is not None and (
        not isinstance(profile_id, str) or not _PROFILE_ID.fullmatch(profile_id)
    ):
        raise WorkspaceReaderError(400, "invalid_review_profile", "Invalid review profile ID.")
    try:
        root = root.resolve(strict=True)
        raw, _ = read_workspace_settings(root, _MANAGED, _MAX_MANAGED_BYTES)
        managed = _parse_managed(raw)
        with _open_directory(root) as directory:
            linked = _read_linked_manifest(directory)
            if managed.keys() & linked.keys():
                raise _invalid_profiles()
            if isinstance(profile_id, str):
                if profile_id in linked:
                    return _read_profile(directory, profile_id, linked[profile_id])
                if profile_id in managed and not managed[profile_id].archived:
                    return _managed_role(managed[profile_id])
                raise WorkspaceReaderError(
                    404, "review_profile_missing", "Review profile is unavailable."
                )

            roles = [_read_profile(directory, name, entry) for name, entry in linked.items()]
            roles.extend(
                _managed_role(profile) for profile in managed.values() if not profile.archived
            )
            for role in roles:
                role.pop("instructions")
            return {"profiles": roles}
    except (OSError, RuntimeError, ValueError, yaml.YAMLError) as exc:
        raise _invalid_profiles() from exc


def _linked_editor_profiles(root: Path) -> list[ReviewProfileEditorEntry]:
    with _open_directory(root) as directory:
        linked = _read_linked_manifest(directory)
        profiles = []
        for name, entry in linked.items():
            profile = _read_profile(directory, name, entry)
            profile.pop("digest")
            profiles.append(
                ReviewProfileEditorEntry.model_validate(
                    {**profile, "source": entry["file"], "managed": False, "archived": False}
                )
            )
        return profiles


def read_review_profile_editor(root: Path) -> dict[str, Any]:
    """Return editable drafts and read-only linked sources for the workspace owner."""
    try:
        root = root.resolve(strict=True)
        raw, revision = read_workspace_settings(root, _MANAGED, _MAX_MANAGED_BYTES)
        managed = _parse_managed(raw)
        profiles = _linked_editor_profiles(root)
        if managed.keys() & {entry.id for entry in profiles}:
            raise _invalid_profiles()
        profiles.extend(
            ReviewProfileEditorEntry(**profile.model_dump(), source=_MANAGED, managed=True)
            for profile in managed.values()
        )
        return {
            "revision": revision,
            "profiles": [entry.model_dump(exclude_none=True) for entry in profiles],
        }
    except (OSError, RuntimeError, ValueError, yaml.YAMLError) as exc:
        raise _invalid_profiles() from exc


def write_review_profile_editor(root: Path, params: dict[str, Any]) -> dict[str, Any]:
    """Save one managed profile against the version loaded by the editor."""
    try:
        request = ReviewProfileEditorWrite.model_validate(params)
    except ValidationError as exc:
        raise WorkspaceReaderError(
            400, "invalid_review_profile", "Invalid review profile draft."
        ) from exc

    def update(raw: bytes | None) -> bytes:
        managed = _parse_managed(raw)
        linked_ids = {profile.id for profile in _linked_editor_profiles(root)}
        if request.profile.id in linked_ids:
            raise WorkspaceReaderError(
                409,
                "linked_review_profile",
                "Linked profiles are read-only. Duplicate this profile.",
            )
        if managed.keys() & linked_ids:
            raise _invalid_profiles()
        managed[request.profile.id] = request.profile
        if len(managed) > _MAX_PROFILES:
            raise WorkspaceReaderError(
                400, "profile_limit", "A workspace supports up to 32 managed profiles."
            )
        return (
            json.dumps(
                {
                    "version": 1,
                    "profiles": [
                        entry.model_dump(exclude_none=True) for entry in managed.values()
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")

    try:
        root = root.resolve(strict=True)
        replace_workspace_settings(root, _MANAGED, _MAX_MANAGED_BYTES, request.revision, update)
        return read_review_profile_editor(root)
    except (OSError, RuntimeError, ValueError, yaml.YAMLError) as exc:
        raise _invalid_profiles() from exc
