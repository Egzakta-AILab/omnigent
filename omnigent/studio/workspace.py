"""Session-free pull request browsing on the selected workspace host."""

import re
from pathlib import Path
from typing import Any

from omnigent.runner import github_resource
from omnigent.runner.session_prs import PullRequestRef
from omnigent.workspace_fs import WorkspaceReaderError
from omnigent.workspace_repositories import resolve_workspace_github_repository

PULL_REQUEST_OPS = frozenset(
    {
        "studio_project_pull_requests",
        "studio_project_pull_request",
        "studio_project_pull_request_patch",
    }
)
_FULL_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_PATCH_PREVIEW_CHARS = 64 * 1024


def read_project_pull_requests(root: Path, op: str, params: dict[str, Any]) -> dict[str, Any]:
    """Serve list, preview, and revision-checked patch reads without session state."""
    checkout, remote, review_available = resolve_workspace_github_repository(
        root, params.get("repository")
    )
    if (
        params.get("require_active") is True or params.get("capture") is True
    ) and not review_available:
        raise WorkspaceReaderError(
            409, "reference_repository", "Mark this repository Active to run a review."
        )
    if op == "studio_project_pull_requests":
        return {
            "pull_requests": github_resource.list_workspace_pull_requests(str(checkout), remote)
        }

    url = params.get("pr_url")
    if not isinstance(url, str) or not 1 <= len(url) <= 2048:
        raise ValueError("Choose a pull request URL.")
    reference = PullRequestRef.from_url(url)
    if (
        reference.provider != remote.provider
        or reference.host != remote.host
        or reference.repository.casefold() != remote.repository.casefold()
    ):
        raise WorkspaceReaderError(
            400, "pr_repository_mismatch", "This pull request belongs to another repository."
        )
    if op == "studio_project_pull_request":
        result = github_resource.read_workspace_pull_request(
            str(checkout), reference, include_files=params.get("include_files") is True
        )
        return {**result, "review_available": review_available}

    base_sha, head_sha = params.get("base_sha"), params.get("head_sha")
    if not (
        isinstance(base_sha, str)
        and _FULL_SHA.fullmatch(base_sha)
        and isinstance(head_sha, str)
        and _FULL_SHA.fullmatch(head_sha)
    ):
        raise ValueError("Reload the pull request to view its patch.")
    result = github_resource.read_workspace_pull_request_patch(
        str(checkout), reference, head_sha=head_sha, base_sha=base_sha
    )
    if params.get("capture") is True:
        return result
    patch = result["patch"]
    return {"patch": patch[:_PATCH_PREVIEW_CHARS], "truncated": len(patch) > _PATCH_PREVIEW_CHARS}
