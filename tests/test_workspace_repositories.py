"""The workspace inventory and PR lookup stay scoped to declared Git checkouts."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnigent.runner import github_resource
from omnigent.workspace_fs import WorkspaceReaderError
from omnigent.workspace_repositories import read_workspace_repositories


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)


def _checkout(root: Path, name: str, origin: str | None = None) -> Path:
    path = root / name
    path.mkdir()
    _git(path, "init", "--initial-branch=local-feature")
    if origin:
        _git(path, "remote", "add", "origin", origin)
    return path


def _manifest(root: Path, repos: str) -> None:
    (root / "repos.yaml").write_text(f"version: 1\nrepos:\n{repos}", encoding="utf-8")


def _unexpected_gh(*_args: Any, **_kwargs: Any) -> Any:
    pytest.fail("a provider call was made")


def test_catalog_reads_only_declared_checkouts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(github_resource, "_gh", _unexpected_gh)
    _git(tmp_path, "init")
    _checkout(tmp_path, "KVARK")
    _checkout(tmp_path, "personal-checkout")
    (tmp_path / "ParentFallback" / ".git").mkdir(parents=True)
    (tmp_path / "Alias").symlink_to(tmp_path / "KVARK", target_is_directory=True)
    _manifest(
        tmp_path,
        "  KVARK:\n"
        "    url: https://github.com/Team/KVARK.git\n"
        "    description: Main repository\n"
        "    depends_on: [Services]\n"
        "  Services: {url: https://github.com/Team/Services.git}\n"
        "  ParentFallback: {url: https://github.com/Team/ParentFallback.git}\n"
        "  Alias: {url: https://github.com/Team/Alias.git}\n",
    )

    catalog = read_workspace_repositories(tmp_path)

    assert catalog["manifest"] == "repos.yaml"
    assert {row["name"]: row["checkout"] for row in catalog["repositories"]} == {
        "KVARK": "ready",
        "Services": "missing",
        "ParentFallback": "invalid",
        "Alias": "invalid",
    }
    assert catalog["repositories"][0] == {
        "name": "KVARK",
        "path": "KVARK",
        "status": "active",
        "description": "Main repository",
        "depends_on": ["Services"],
        "checkout": "ready",
    }
    for invalid in ("Services", "ParentFallback", "Alias", "personal-checkout"):
        with pytest.raises(WorkspaceReaderError) as raised:
            read_workspace_repositories(tmp_path, invalid)
        assert raised.value.status == 404


def test_no_manifest_does_not_scan_workspace(tmp_path: Path):
    _checkout(tmp_path, "KVARK")

    assert read_workspace_repositories(tmp_path) == {"manifest": None, "repositories": []}


@pytest.mark.parametrize(
    "repos",
    [
        "  ../KVARK: {url: https://github.com/Team/KVARK.git}\n",
        "  KVARK: {url: https://github.com/Team/KVARK.git, status: [active]}\n",
        "  KVARK: {url: https://github.com/Team/KVARK.git}\n"
        "  KVARK: {url: https://github.com/Other/KVARK.git}\n",
        "  KVARK: &clone {url: https://github.com/Team/KVARK.git}\n  Services: *clone\n",
        "#" + "x" * 65536,
    ],
)
def test_invalid_manifest_fails_closed(tmp_path: Path, repos: str):
    _manifest(tmp_path, repos)

    with pytest.raises(WorkspaceReaderError, match=r"Invalid workspace repos\.yaml") as raised:
        read_workspace_repositories(tmp_path)
    assert raised.value.status == 400


def test_manifest_symlink_is_rejected(tmp_path: Path):
    target = tmp_path / "other.yaml"
    target.write_text("version: 1\nrepos: {}\n", encoding="utf-8")
    (tmp_path / "repos.yaml").symlink_to(target)

    with pytest.raises(WorkspaceReaderError, match=r"Invalid workspace repos\.yaml"):
        read_workspace_repositories(tmp_path)


def test_lookup_uses_exact_local_branch_and_preserves_git_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _git(tmp_path, "init")
    checkout = _checkout(tmp_path, "KVARK", "git@github.com:Team/KVARK.git")
    _git(checkout, "config", "branch.local-feature.remote", "origin")
    _git(checkout, "config", "branch.local-feature.merge", "refs/heads/team-feature")
    _git(checkout, "config", "remote.origin.gh-resolved", "https://github.com/Unrelated/Parent")
    _manifest(tmp_path, "  KVARK: {url: https://github.com/team/kvark.git}\n")
    config = checkout / ".git" / "config"
    before = config.read_bytes()
    monkeypatch.setattr(github_resource.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(github_resource, "_account_token_for", lambda *_, **__: None)

    def gh(argv: list[str], *, cwd: str, token: str | None) -> tuple[int, str, str]:
        assert argv[:2] == ["pr", "list"]
        assert "--repo=github.com/Team/KVARK" in argv
        assert "--head=local-feature" in argv
        assert Path(cwd) == checkout
        assert token is None
        return 0, json.dumps([_pr(headRefName="local-feature")]), ""

    monkeypatch.setattr(github_resource, "_gh", gh)

    assert read_workspace_repositories(tmp_path, "KVARK") == {
        "repository": "KVARK",
        "branch": "local-feature",
        "remote": "github.com/Team/KVARK",
        "state": "found",
        "pr": {"url": "https://github.com/team/kvark/pull/3", "title": "Feature", "number": 3},
    }
    assert config.read_bytes() == before


@pytest.mark.parametrize(
    ("origin", "remote"),
    [
        ("https://github.com/OldTeam/KVARK.git", "github.com/OldTeam/KVARK"),
        ("https://github.com/Team/Another.git", "github.com/Team/Another"),
        ("https://gitlab.com/Team/KVARK.git", None),
        ("https://other.example.com/Team/KVARK.git", None),
    ],
)
def test_remote_mismatch_stops_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str, remote: str | None
):
    _checkout(tmp_path, "KVARK", origin)
    _manifest(tmp_path, "  KVARK: {url: https://github.com/Team/KVARK.git}\n")
    monkeypatch.setattr(github_resource, "read_workspace_branch_pr", _unexpected_gh)

    result = read_workspace_repositories(tmp_path, "KVARK")

    assert result["state"] == "unavailable"
    assert result["reason"] == "origin_mismatch"
    assert result["remote"] == remote
    assert result["expected_remote"] == "github.com/Team/KVARK"


def _pr(**fields: Any) -> dict[str, Any]:
    return {
        "url": "https://github.com/Team/KVARK/pull/3",
        "title": "Feature",
        "number": 3,
        "headRefName": "team-feature",
        "isCrossRepository": False,
        **fields,
    }


@pytest.mark.parametrize(
    ("response", "exit_code", "state"),
    [
        ([], 0, "no_pr"),
        ([], 1, "unavailable"),
        ([_pr(url="https://github.com/Other/KVARK/pull/3")], 0, "unavailable"),
        ([_pr(url="https://github.com/Team/Other/pull/3")], 0, "unavailable"),
        ([_pr(url="https://token@github.com/Team/KVARK/pull/3")], 0, "unavailable"),
        ([_pr(headRefName="wrong-feature")], 0, "unavailable"),
        ([_pr(isCrossRepository=True)], 0, "unavailable"),
        ([_pr(), _pr()], 0, "unavailable"),
    ],
)
def test_lookup_returns_no_link_on_empty_error_or_unverified_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: object, exit_code: int, state: str
):
    checkout = _checkout(tmp_path, "KVARK", "https://github.com/Team/KVARK.git")
    _git(checkout, "symbolic-ref", "HEAD", "refs/heads/team-feature")
    _manifest(tmp_path, "  KVARK: {url: https://github.com/Team/KVARK.git}\n")
    monkeypatch.setattr(github_resource.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(github_resource, "_account_token_for", lambda *_, **__: None)
    monkeypatch.setattr(
        github_resource, "_gh", lambda *_, **__: (exit_code, json.dumps(response), "")
    )

    result = read_workspace_repositories(tmp_path, "KVARK")

    assert result["state"] == state
    assert result["pr"] is None


def test_enterprise_host_lookup_does_not_use_public_github_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _checkout(tmp_path, "KVARK", "https://git.example.com/Team/KVARK.git")
    _manifest(tmp_path, "  KVARK: {url: https://git.example.com/Team/KVARK.git}\n")
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", "git.example.com")
    monkeypatch.setattr(github_resource.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(github_resource, "_account_token_for", _unexpected_gh)

    def gh(argv: list[str], *, cwd: str, token: str | None) -> tuple[int, str, str]:
        assert "--repo=git.example.com/Team/KVARK" in argv
        assert Path(cwd) == tmp_path / "KVARK"
        assert token is None
        return 0, "[]", ""

    monkeypatch.setattr(github_resource, "_gh", gh)

    assert read_workspace_repositories(tmp_path, "KVARK")["state"] == "no_pr"
