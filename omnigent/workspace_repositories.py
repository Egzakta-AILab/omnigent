"""Read nested checkouts declared by the workspace's ``repos.yaml``."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from omnigent.git_providers import ParsedRemote, resolve_pr_url, resolve_remote
from omnigent.workspace_fs import WorkspaceReaderError

_MANIFEST = "repos.yaml"
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_REPOSITORIES = 64
_BRANCH_LOOKUP_SECONDS = 8.0
_REPOSITORY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


@dataclass(frozen=True)
class _Repository:
    name: str
    url: str
    status: str
    description: str
    depends_on: list[str]


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
    if not isinstance(manifest, dict) or type(manifest.get("version")) is not int:
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


def _checkout_state(root: Path, name: str) -> Literal["ready", "missing", "invalid"]:
    path = root / name
    try:
        if path.is_symlink():
            return "invalid"
        if not path.exists():
            return "missing"
        git_marker = path / ".git"
        if not path.is_dir() or path.resolve() != path or git_marker.is_symlink():
            return "invalid"
        if not (git_marker.is_dir() or git_marker.is_file()):
            return "invalid"
        # The command also handles linked worktrees. Check the reported root so
        # Git discovery cannot mistake the parent for a broken child checkout.
        top_level = _git_output(path, "rev-parse", "--show-toplevel")
        return "ready" if top_level and Path(top_level).resolve() == path else "invalid"
    except (OSError, ValueError, RuntimeError):
        return "invalid"


def _git_output(checkout: Path, *args: str) -> str | None:
    # Git environment overrides from a caller's shell must not redirect this read
    # to a different checkout or inject a different remote config.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=checkout,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1,
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


def _branch_pr(checkout: Path, repo: _Repository, deadline: float) -> dict[str, Any]:
    branch = _git_output(checkout, "symbolic-ref", "--quiet", "--short", "HEAD")
    branch = branch if branch and len(branch) <= 512 else None
    result: dict[str, Any] = {
        "repository": repo.name,
        "branch": branch,
        "remote": None,
        "state": "unavailable",
        "pr": None,
    }
    expected = _github_selector(resolve_remote(repo.url))
    if expected is None:
        return result
    origin = _git_output(checkout, "remote", "get-url", "origin")
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


def read_workspace_repositories(root: Path, repository: object = None) -> dict[str, Any]:
    """List declared child checkouts or look up one checkout's current GitHub PR."""
    deadline = time.monotonic() + _BRANCH_LOOKUP_SECONDS
    if repository is not None and not _valid_name(repository):
        raise WorkspaceReaderError(400, "invalid_repository", "Invalid workspace repository name.")
    root = root.resolve()
    repos = _read_manifest(root)
    if repository is None:
        entries = repos or []
        with ThreadPoolExecutor(max_workers=8) as executor:
            checkout_states = list(
                executor.map(lambda repo: _checkout_state(root, repo.name), entries)
            )
        return {
            "manifest": _MANIFEST if repos is not None else None,
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
    if selected is None:
        raise WorkspaceReaderError(404, "unknown_repository", "Repository is not in repos.yaml.")
    if _checkout_state(root, selected.name) != "ready":
        raise WorkspaceReaderError(
            404, "checkout_unavailable", "Repository checkout is unavailable."
        )
    return _branch_pr(root / selected.name, selected, deadline)
