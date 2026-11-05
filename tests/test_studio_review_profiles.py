from pathlib import Path

import pytest

from omnigent.studio.reviews.profiles import read_review_profiles
from omnigent.workspace_fs import WorkspaceReaderError


def _manifest(root: Path, role_file: str = "roles/security.md") -> None:
    (root / "review-profiles.yaml").write_text(
        "version: 1\nprofiles:\n  security:\n    label: Security\n"
        f"    description: Authentication and permissions\n    file: '{role_file}'\n"
    )


def test_catalog_exposes_metadata_and_fingerprints_only(tmp_path: Path) -> None:
    _manifest(tmp_path)
    role_file = tmp_path / "roles" / "security.md"
    role_file.parent.mkdir()
    role_file.write_text("---\nmodel: private-model\ntools: Bash\n---\n\nReview access guards.\n")

    profile = read_review_profiles(tmp_path, "security")
    assert profile == {
        "id": "security",
        "label": "Security",
        "description": "Authentication and permissions",
        "digest": profile["digest"],
        "instructions": "Review access guards.",
    }
    assert len(bytes.fromhex(profile["digest"])) == 32
    assert read_review_profiles(tmp_path) == {
        "profiles": [{name: value for name, value in profile.items() if name != "instructions"}]
    }

    role_file.write_text("Review SSRF risks.\n")
    assert read_review_profiles(tmp_path, "security")["digest"] != profile["digest"]


def test_missing_manifest_does_not_scan_workspace(tmp_path: Path) -> None:
    (tmp_path / "security.md").write_text("No automatic discovery.")
    assert read_review_profiles(tmp_path) == {"profiles": []}
    with pytest.raises(WorkspaceReaderError) as failure:
        read_review_profiles(tmp_path, "security")
    assert failure.value.status == 404


@pytest.mark.parametrize(
    "path", ["../secret.md", "/tmp/secret.md", "./security.md", "credentials.yml"]
)
def test_rejects_undeclared_path_shapes(tmp_path: Path, path: str) -> None:
    _manifest(tmp_path, path)
    with pytest.raises(WorkspaceReaderError, match="Check workspace") as failure:
        read_review_profiles(tmp_path)
    assert failure.value.status == 400


@pytest.mark.parametrize("symlink_at", ["manifest", "directory", "file"])
def test_does_not_follow_symlinks(tmp_path: Path, symlink_at: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _manifest(workspace)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "security.md").write_text("Other workspace data")
    if symlink_at == "manifest":
        _manifest(outside)
        manifest = workspace / "review-profiles.yaml"
        manifest.unlink()
        manifest.symlink_to(outside / manifest.name)
    elif symlink_at == "directory":
        (workspace / "roles").symlink_to(outside, target_is_directory=True)
    else:
        (workspace / "roles").mkdir()
        (workspace / "roles" / "security.md").symlink_to(outside / "security.md")
    with pytest.raises(WorkspaceReaderError) as failure:
        read_review_profiles(workspace)
    assert failure.value.status == 400


@pytest.mark.parametrize("body", ["", "---\nmodel: private-model", "a" * (8192 + 1)])
def test_empty_malformed_or_oversize_role_fails_closed(tmp_path: Path, body: str) -> None:
    _manifest(tmp_path, "security.md")
    (tmp_path / "security.md").write_text(body)
    with pytest.raises(WorkspaceReaderError) as failure:
        read_review_profiles(tmp_path, "security")
    assert failure.value.status == 400


@pytest.mark.parametrize(
    "manifest",
    [
        "version: 1\nversion: 1\nprofiles: {}\n",
        "version: 1\nprofiles: {security: &entry {}, other: *entry}\n",
        "version: 2\nprofiles: {}\n",
        "version: true\nprofiles: {}\n",
    ],
)
def test_manifest_rejects_ambiguous_or_unsupported_configuration(
    tmp_path: Path, manifest: str
) -> None:
    (tmp_path / "review-profiles.yaml").write_text(manifest)
    with pytest.raises(WorkspaceReaderError) as failure:
        read_review_profiles(tmp_path)
    assert failure.value.status == 400
