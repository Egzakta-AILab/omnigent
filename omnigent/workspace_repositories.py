"""Discover and classify Git checkouts within an explicit workspace."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time
from collections import deque
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import yaml

from omnigent.git_providers import ParsedRemote, host_of, resolve_pr_url, resolve_remote
from omnigent.workspace_fs import WorkspaceReaderError
from omnigent.workspace_settings import read_workspace_settings, replace_workspace_settings

_MANIFEST = "repos.yaml"
_SETTINGS = ".omnigent-repositories.json"
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_REPOSITORIES = 64
_DEFAULT_DEPTH = 2
_MAX_DEPTH = 5
_MAX_ENTRIES = 4096
_SCAN_SECONDS = 5.0
_BRANCH_LOOKUP_SECONDS = 8.0
_REPOSITORY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_ORIGIN_NAME = re.compile(r"[A-Za-z0-9.-]+/[A-Za-z0-9_./-]{1,510}")
_SKIP_DIRECTORIES = frozenset(
    {
        ".git",
        ".cache",
        ".next",
        ".pnpm",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "target",
        "venv",
    }
)

CheckoutState = Literal["ready", "missing", "invalid"]
RepositoryRole = Literal["active", "reference", "excluded"]


@dataclass(frozen=True)
class _Repository:
    name: str
    url: str
    status: str
    description: str
    depends_on: list[str]


@dataclass(frozen=True)
class _Assignment:
    path: str
    role: RepositoryRole
    origin: str | None


class _ManifestLoader(yaml.SafeLoader):
    def compose_node(self, parent: Any, index: Any) -> Any:
        # A small manifest can otherwise expand into an arbitrarily large graph.
        if self.check_event(yaml.AliasEvent):
            raise _invalid_manifest()
        return super().compose_node(parent, index)

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        result = super().construct_mapping(node, deep)
        if len(result) != len(node.value):
            raise _invalid_manifest()
        return result


def _invalid_manifest() -> WorkspaceReaderError:
    return WorkspaceReaderError(400, "invalid_manifest", "Invalid workspace repos.yaml manifest.")


def _valid_name(name: object) -> bool:
    return isinstance(name, str) and _REPOSITORY_NAME.fullmatch(name) is not None


def _read_manifest(root: Path) -> list[_Repository] | None:
    path = root / _MANIFEST
    try:
        # A workspace can contain special files. Open only the regular file at
        # the workspace root, without following a manifest symlink or blocking on a FIFO.
        if path.is_symlink():
            raise _invalid_manifest()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise _invalid_manifest()
            raw = stream.read(_MAX_MANIFEST_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _invalid_manifest() from exc
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise _invalid_manifest()

    try:
        manifest = yaml.load(raw, Loader=_ManifestLoader)
    except (yaml.YAMLError, RecursionError, ValueError) as exc:
        raise _invalid_manifest() from exc
    if not isinstance(manifest, dict) or "repos" not in manifest:
        return None
    if type(manifest.get("version")) is not int:
        raise _invalid_manifest()
    repos = manifest.get("repos")
    if manifest["version"] != 1 or not isinstance(repos, dict) or len(repos) > _MAX_REPOSITORIES:
        raise _invalid_manifest()

    result: list[_Repository] = []
    seen: set[str] = set()
    for name, entry in repos.items():
        if not isinstance(name, str) or not _valid_name(name) or not isinstance(entry, dict):
            raise _invalid_manifest()
        if name.casefold() in seen:
            raise _invalid_manifest()
        seen.add(name.casefold())
        url = entry.get("url")
        status = entry.get("status", "active")
        description = entry.get("description", "")
        depends_on = entry.get("depends_on", [])
        if (
            not isinstance(url, str)
            or not url.strip()
            or len(url) > 2048
            or not isinstance(status, str)
            or status not in ("active", "legacy", "experimental")
            or not isinstance(description, str)
            or len(description) > 512
            or not isinstance(depends_on, list)
            or len(depends_on) > _MAX_REPOSITORIES
            or any(not _valid_name(dependency) for dependency in depends_on)
        ):
            raise _invalid_manifest()
        result.append(_Repository(name, url, status, description, depends_on))
    return result


def is_workspace_repository_path(path: object) -> bool:
    """Accept the workspace root or a normalized relative checkout path."""
    return (
        isinstance(path, str)
        and 1 <= len(path) <= 512
        and path.isprintable()
        and "\\" not in path
        and ":" not in path
        and (
            path == "."
            or all(part.lower() not in {"", ".", "..", ".git"} for part in path.split("/"))
        )
    )


@contextmanager
def _open_workspace_directory(root: Path, name: str) -> Iterator[int]:
    """Open every component without following symlinks or entering another device."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    try:
        device = os.fstat(descriptor).st_dev
        for part in Path(name).parts:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            if os.fstat(descriptor).st_dev != device:
                raise OSError("Repository path crosses a workspace mount")
        yield descriptor
    finally:
        os.close(descriptor)


def _contained_directory(root: Path, name: str) -> CheckoutState:
    try:
        with _open_workspace_directory(root, name):
            return "ready"
    except FileNotFoundError:
        return "missing"
    except (OSError, ValueError, RuntimeError):
        return "invalid"


def _checkout_state(root: Path, name: str, *, deadline: float | None = None) -> CheckoutState:
    path = root / name
    try:
        with _open_workspace_directory(root, name) as directory:
            try:
                marker = os.stat(".git", dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return "invalid"
            if not (stat.S_ISDIR(marker.st_mode) or stat.S_ISREG(marker.st_mode)):
                return "invalid"
            if marker.st_dev != os.fstat(directory).st_dev:
                return "invalid"
        # The command also handles linked worktrees. Check the reported root so
        # Git discovery cannot mistake the parent for a broken child checkout.
        top_level = _git_output(path, "rev-parse", "--show-toplevel", deadline=deadline)
        return "ready" if top_level and Path(top_level).resolve() == path else "invalid"
    except FileNotFoundError:
        return "missing"
    except (OSError, ValueError, RuntimeError):
        return "invalid"


def _git_output(checkout: Path, *args: str, deadline: float | None = None) -> str | None:
    # Git environment overrides from a caller's shell must not redirect this read
    # to a different checkout or inject a different remote config.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    timeout = min(1.0, deadline - time.monotonic()) if deadline is not None else 1.0
    if timeout <= 0:
        return None
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=checkout,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def _github_selector(remote: ParsedRemote | None) -> str | None:
    if remote is None or remote.provider != "github":
        return None
    selector = f"{remote.host}/{remote.repository}"
    parsed = resolve_pr_url(f"https://{selector}/pull/1")
    if parsed and parsed.host == remote.host and parsed.repository == remote.repository.lower():
        return selector
    return None


def _local_branch(checkout: Path, deadline: float) -> str | None:
    branch = _git_output(checkout, "symbolic-ref", "--quiet", "--short", "HEAD", deadline=deadline)
    return branch if branch and len(branch) <= 512 and branch.isprintable() else None


def _valid_origin(value: object) -> bool:
    return (
        isinstance(value, str)
        and _ORIGIN_NAME.fullmatch(value) is not None
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _origin_label(raw: str | None) -> str | None:
    """Show only a network host and repository path, never remote credentials."""
    if not raw or len(raw) > 2048 or not raw.isprintable():
        return None
    host = host_of(raw)
    if host is None:
        return None
    path = urlsplit(raw).path if "://" in raw else raw.partition(":")[2]
    label = f"{host}/{path.strip('/').removesuffix('.git')}"
    return label if _valid_origin(label) else None


def _branch_pr(
    checkout: Path, repository: str, expected: str | None, deadline: float
) -> dict[str, Any]:
    branch = _local_branch(checkout, deadline)
    result: dict[str, Any] = {
        "repository": repository,
        "branch": branch,
        "remote": None,
        "state": "unavailable",
        "pr": None,
    }
    if expected is None:
        return result
    origin = _git_output(checkout, "remote", "get-url", "origin", deadline=deadline)
    actual = resolve_remote(origin) if origin else None
    actual_selector = _github_selector(actual)
    result["remote"] = actual_selector
    if actual is None or actual_selector is None or actual_selector.lower() != expected.lower():
        return {**result, "reason": "origin_mismatch", "expected_remote": expected}
    if branch is None:
        return result

    from omnigent.runner.github_resource import read_workspace_branch_pr

    state, pr = read_workspace_branch_pr(str(checkout), actual, branch, deadline=deadline)
    return {**result, "state": state, "pr": pr}


def _invalid_settings() -> WorkspaceReaderError:
    return WorkspaceReaderError(
        400, "invalid_repository_settings", "Invalid workspace repository settings."
    )


def _valid_depth(value: object) -> bool:
    return type(value) is int and 0 <= value <= _MAX_DEPTH


def _role(value: object) -> RepositoryRole:
    if value not in ("active", "reference", "excluded"):
        raise _invalid_settings()
    return cast("RepositoryRole", value)


def _parse_assignments(raw: bytes | None) -> tuple[int, list[_Assignment]]:
    if raw is None:
        return _DEFAULT_DEPTH, []
    try:
        config = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise _invalid_settings() from exc
    if (
        not isinstance(config, dict)
        or config.get("version") != 1
        or type(config.get("version")) is not int
        or not _valid_depth(config.get("depth"))
    ):
        raise _invalid_settings()
    entries = config.get("repositories")
    if not isinstance(entries, list) or len(entries) > _MAX_REPOSITORIES:
        raise _invalid_settings()
    paths: set[str] = set()
    assignments = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "role", "origin"}:
            raise _invalid_settings()
        path, origin = entry["path"], entry["origin"]
        role = _role(entry["role"])
        if (
            not isinstance(path, str)
            or not is_workspace_repository_path(path)
            or path.casefold() in paths
            or (origin is not None and (role != "active" or not _valid_origin(origin)))
        ):
            raise _invalid_settings()
        paths.add(path.casefold())
        assignments.append(_Assignment(path, role, origin))
    return config["depth"], assignments


def _discover_paths(
    root: Path, depth: int, excluded: set[str], deadline: float
) -> tuple[list[str], bool]:
    """Walk bounded directories, including nested checkouts under a Git root."""
    pending = deque([(".", 0)])
    candidates: list[str] = []
    entries_seen = 0
    device = root.stat().st_dev
    while pending:
        if time.monotonic() >= deadline:
            return candidates, True
        path, level = pending.popleft()
        try:
            with _open_workspace_directory(root, path) as directory:
                try:
                    marker = os.stat(".git", dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    marker = None
                if (
                    path.casefold() not in excluded
                    and marker
                    and marker.st_dev == device
                    and (stat.S_ISREG(marker.st_mode) or stat.S_ISDIR(marker.st_mode))
                ):
                    if len(candidates) >= _MAX_REPOSITORIES:
                        return candidates, True
                    candidates.append(path)
                if level >= depth:
                    continue
                with os.scandir(directory) as children:
                    for entry in children:
                        entries_seen += 1
                        if entries_seen > _MAX_ENTRIES or time.monotonic() >= deadline:
                            return candidates, True
                        if entry.name.casefold() in _SKIP_DIRECTORIES or not entry.is_dir(
                            follow_symlinks=False
                        ):
                            continue
                        child = (Path(path) / entry.name).as_posix()
                        if (
                            is_workspace_repository_path(child)
                            and entry.stat(follow_symlinks=False).st_dev == device
                        ):
                            pending.append((child, level + 1))
        except OSError:
            # Report partial results if the selected host denied a subtree.
            return candidates, True
    return candidates, False


def _repository_details(
    root: Path,
    assignment: _Assignment,
    declared: bool,
    deadline: float,
) -> dict[str, Any]:
    checkout = _checkout_state(root, assignment.path, deadline=deadline)
    path = root / assignment.path
    origin = branch = None
    if checkout == "ready":
        branch = _local_branch(path, deadline)
        origin = _origin_label(_git_output(path, "remote", "get-url", "origin", deadline=deadline))
    return {
        "path": assignment.path,
        "branch": branch,
        "origin": origin,
        "role": assignment.role,
        "declared": declared,
        "checkout": checkout,
        **({"expected_origin": assignment.origin} if assignment.role == "active" else {}),
    }


def read_repository_settings(root: Path, scan_depth: int | None = None) -> dict[str, Any]:
    """Read existing assignments, or discover candidates after an explicit scan."""
    if scan_depth is not None and not _valid_depth(scan_depth):
        raise _invalid_settings()
    root = root.resolve()
    raw, revision = read_workspace_settings(root, _SETTINGS, _MAX_MANIFEST_BYTES)
    depth, assignments = _parse_assignments(raw)
    manifest = _read_manifest(root) or []
    repositories = {
        repo.name.casefold(): (_Assignment(repo.name, "active", _origin_label(repo.url)), True)
        for repo in manifest
    }
    for assignment in assignments:
        repositories.setdefault(assignment.path.casefold(), (assignment, False))

    deadline = time.monotonic() + _SCAN_SECONDS
    truncated = False
    if scan_depth is not None:
        excluded = {name for name, (entry, _) in repositories.items() if entry.role == "excluded"}
        discovered, truncated = _discover_paths(root, scan_depth, excluded, deadline)
        available_slots = _MAX_REPOSITORIES - sum(
            1 for _, declared in repositories.values() if not declared
        )
        for path in sorted(discovered, key=str.casefold):
            key = path.casefold()
            if key in repositories:
                continue
            if not available_slots:
                truncated = True
                break
            repositories[key] = (_Assignment(path, "reference", None), False)
            available_slots -= 1

    with ThreadPoolExecutor(max_workers=8) as executor:
        rows = list(
            executor.map(
                lambda entry: _repository_details(root, entry[0], entry[1], deadline),
                repositories.values(),
            )
        )
    # A random directory with a bogus Git marker is not a discoverable checkout.
    saved = {entry.path.casefold() for entry in assignments}
    return {
        "revision": revision,
        "depth": depth if scan_depth is None else scan_depth,
        "repositories": [
            row
            for row in rows
            if row["declared"] or row["path"].casefold() in saved or row["checkout"] == "ready"
        ],
        "truncated": truncated or time.monotonic() >= deadline,
    }


def write_repository_settings(root: Path, params: dict[str, Any]) -> dict[str, Any]:
    """Save explicit assignments and pin an Active checkout's current origin."""
    revision = params.get("revision")
    depth = params.get("depth")
    requested = params.get("repositories")
    if (
        not isinstance(revision, str)
        or not _valid_depth(depth)
        or not isinstance(requested, list)
        or len(requested) > _MAX_REPOSITORIES
    ):
        raise _invalid_settings()
    root = root.resolve()

    def update(raw: bytes | None) -> bytes:
        _, assignments = _parse_assignments(raw)
        prior = {entry.path.casefold(): entry for entry in assignments}
        declared = {repo.name.casefold() for repo in _read_manifest(root) or []}
        seen: set[str] = set()
        entries = []
        deadline = time.monotonic() + _SCAN_SECONDS
        for row in requested:
            if not isinstance(row, dict) or set(row) - {"path", "role", "origin"}:
                raise _invalid_settings()
            path = row.get("path")
            role = _role(row.get("role"))
            origin_hint = row.get("origin")
            if (
                not isinstance(path, str)
                or not is_workspace_repository_path(path)
                or path.casefold() in seen
                or (origin_hint is not None and not _valid_origin(origin_hint))
            ):
                raise _invalid_settings()
            key = path.casefold()
            seen.add(key)
            if key in declared:
                if role != "active":
                    raise WorkspaceReaderError(
                        409, "declared_repository", "Change declared repositories in repos.yaml."
                    )
                continue
            previous = prior.get(key)
            if (
                previous
                and previous.path == path
                and previous.role == "active"
                and role == "active"
            ):
                origin = previous.origin
            else:
                origin = None
                if _contained_directory(root, path) == "invalid":
                    raise _invalid_settings()
                if role == "active":
                    if _checkout_state(root, path, deadline=deadline) != "ready":
                        raise WorkspaceReaderError(
                            409,
                            "checkout_unavailable",
                            "Only a valid Git checkout can be made Active.",
                        )
                    origin = _origin_label(
                        _git_output(root / path, "remote", "get-url", "origin", deadline=deadline)
                    )
                    if time.monotonic() >= deadline or ("origin" in row and origin != origin_hint):
                        raise WorkspaceReaderError(
                            409,
                            "checkout_changed",
                            "The checkout changed. Scan again before saving.",
                        )
            entries.append({"path": path, "role": role, "origin": origin})
        return (
            json.dumps({"version": 1, "depth": depth, "repositories": entries}, indent=2) + "\n"
        ).encode()

    replace_workspace_settings(root, _SETTINGS, _MAX_MANIFEST_BYTES, revision, update)
    return read_repository_settings(root)


def read_workspace_repositories(root: Path, repository: object = None) -> dict[str, Any]:
    """List declared and configured Active checkouts, or find one branch's PR."""
    deadline = time.monotonic() + _BRANCH_LOOKUP_SECONDS
    if repository is not None and not is_workspace_repository_path(repository):
        raise WorkspaceReaderError(400, "invalid_repository", "Invalid workspace repository name.")
    root = root.resolve()
    repos = _read_manifest(root)
    raw, _ = read_workspace_settings(root, _SETTINGS, _MAX_MANIFEST_BYTES)
    _, assignments = _parse_assignments(raw)
    declared = {entry.name.casefold() for entry in repos or ()}
    active = [
        entry
        for entry in assignments
        if entry.role == "active" and entry.path.casefold() not in declared
    ]
    if repository is None:
        entries = (repos or []) + [
            _Repository(entry.path, "", "active", "", []) for entry in active
        ]
        with ThreadPoolExecutor(max_workers=8) as executor:
            checkout_states = list(
                executor.map(
                    lambda repo: _checkout_state(root, repo.name, deadline=deadline), entries
                )
            )
        return {
            "manifest": _MANIFEST if repos is not None else None,
            **({"configured": True} if raw is not None else {}),
            "repositories": [
                {
                    "name": repo.name,
                    "path": repo.name,
                    "status": repo.status,
                    "description": repo.description,
                    "depends_on": repo.depends_on,
                    "checkout": checkout,
                }
                for repo, checkout in zip(entries, checkout_states, strict=True)
            ],
        }

    selected = next((repo for repo in repos or () if repo.name == repository), None)
    if selected is not None:
        path, url = selected.name, selected.url
    else:
        assignment = next((entry for entry in active if entry.path == repository), None)
        if assignment is None:
            raise WorkspaceReaderError(
                404, "unknown_repository", "Repository is not Active in this workspace."
            )
        path = assignment.path
        url = f"https://{assignment.origin}" if assignment.origin else ""
    if _checkout_state(root, path, deadline=deadline) != "ready":
        raise WorkspaceReaderError(
            404, "checkout_unavailable", "Repository checkout is unavailable."
        )
    return _branch_pr(root / path, path, _github_selector(resolve_remote(url)), deadline)
