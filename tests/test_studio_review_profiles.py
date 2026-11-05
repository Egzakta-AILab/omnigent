from pathlib import Path

import pytest

from omnigent.studio.reviews.profiles import (
    read_review_profile_editor,
    read_review_profiles,
    write_review_profile_editor,
)
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


def test_managed_profile_round_trip_and_conflict(tmp_path: Path) -> None:
    _manifest(tmp_path, "security.md")
    linked_file = tmp_path / "security.md"
    linked_text = "Review access checks."
    linked_file.write_text(linked_text)
    initial = read_review_profile_editor(tmp_path)
    linked = initial["profiles"][0]
    assert linked["source"] == "security.md"
    assert linked["managed"] is False

    draft = {
        "id": "security-custom",
        "label": "Security custom",
        "description": linked["description"],
        "instructions": linked["instructions"],
        "archived": False,
        "defaults": {"harness": "codex-native", "model": "model-test", "reasoning_effort": "high"},
    }
    with pytest.raises(WorkspaceReaderError) as linked_edit:
        write_review_profile_editor(
            tmp_path, {"revision": initial["revision"], "profile": {**draft, "id": "security"}}
        )
    assert linked_edit.value.status == 409
    assert read_review_profile_editor(tmp_path) == initial

    created = write_review_profile_editor(
        tmp_path, {"revision": initial["revision"], "profile": draft}
    )
    assert created["revision"] != initial["revision"]
    assert created["profiles"][1]["managed"] is True
    assert read_review_profile_editor(tmp_path) == created

    role = read_review_profiles(tmp_path, "security-custom")
    assert role["defaults"] == draft["defaults"]
    assert role["instructions"] == linked_text
    assert "instructions" not in read_review_profiles(tmp_path)["profiles"][1]

    with pytest.raises(WorkspaceReaderError) as stale:
        write_review_profile_editor(
            tmp_path, {"revision": initial["revision"], "profile": {**draft, "label": "Stale"}}
        )
    assert stale.value.status == 409
    assert read_review_profile_editor(tmp_path) == created

    updated = write_review_profile_editor(
        tmp_path,
        {
            "revision": created["revision"],
            "profile": {**draft, "defaults": {**draft["defaults"], "reasoning_effort": "low"}},
        },
    )
    assert read_review_profiles(tmp_path, "security-custom")["digest"] != role["digest"]

    archived = write_review_profile_editor(
        tmp_path, {"revision": updated["revision"], "profile": {**draft, "archived": True}}
    )
    assert archived["profiles"][1]["archived"] is True
    assert [entry["id"] for entry in read_review_profiles(tmp_path)["profiles"]] == ["security"]
    with pytest.raises(WorkspaceReaderError) as missing:
        read_review_profiles(tmp_path, "security-custom")
    assert missing.value.status == 404

    write_review_profile_editor(tmp_path, {"revision": archived["revision"], "profile": draft})
    assert read_review_profiles(tmp_path, "security-custom")["digest"] == role["digest"]
    assert linked_file.read_text() == linked_text


@pytest.mark.parametrize(
    "invalid",
    [
        {"label": "Invalid\nlabel"},
        {"instructions": " "},
        {"instructions": "😀" * 2049},
        {"defaults": {"model": "--flag"}},
        {"defaults": {"reasoning_effort": "extreme"}},
        {"managed": True},
    ],
)
def test_invalid_managed_profile_never_writes(tmp_path: Path, invalid: dict) -> None:
    catalog = read_review_profile_editor(tmp_path)
    draft = {"id": "profile", "label": "Profile", "instructions": "Review", "archived": False}
    with pytest.raises(WorkspaceReaderError) as failure:
        write_review_profile_editor(
            tmp_path, {"revision": catalog["revision"], "profile": {**draft, **invalid}}
        )
    assert failure.value.status == 400
    assert read_review_profile_editor(tmp_path) == catalog
