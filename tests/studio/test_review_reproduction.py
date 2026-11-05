"""Check archive provenance and bounded capture without a Docker daemon."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

from omnigent.studio.reviews import reproduction
from omnigent.studio.reviews.reproduction import (
    ReproductionError,
    ReproductionSide,
    ReproductionSource,
    _capture,
    _export,
    _prepare_archive_repository,
    reproduce,
    verify_fix,
)


def test_export_is_bound_to_commit_and_excludes_dirty_files(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init", "--quiet", "--object-format=sha1")
    source = tmp_path / "source.txt"
    source.write_text("committed", encoding="utf-8")
    git("add", "source.txt")
    git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "fixture")
    commit = git("rev-parse", "HEAD")
    source.write_text("dirty", encoding="utf-8")
    (tmp_path / "untracked.txt").write_text("untracked", encoding="utf-8")
    (tmp_path / ".git" / "info" / "attributes").write_text(
        "source.txt export-ignore\n", encoding="utf-8"
    )
    nested = tmp_path / "nested"
    nested.mkdir()
    bare = tmp_path / "export.git"
    _prepare_archive_repository(nested, bare, object_format="sha1")
    with tempfile.TemporaryFile() as archive:
        exported = _export(bare, commit, archive)
        contents = archive.read()
        assert exported.size == len(contents)
        assert exported.digest == hashlib.sha256(contents).hexdigest()
        with tarfile.open(fileobj=io.BytesIO(contents)) as tree:
            assert tree.getnames() == ["source.txt"]
            file = tree.extractfile("source.txt")
            assert file is not None
            assert file.read() == b"committed"
    with tempfile.TemporaryFile() as archive, pytest.raises(ReproductionError, match="exported"):
        _export(bare, "b" * 40, archive)


def test_capture_bounds_output_and_hashes_discarded_bytes() -> None:
    output = io.BytesIO()
    capture = _capture(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 100_000)"],
        destination=output,
        timeout=5,
        limit=8,
        truncate=True,
    )
    assert capture.status == 0 and capture.size == 100_000
    assert capture.digest == hashlib.sha256(b"x" * 100_000).hexdigest()
    assert output.getvalue() == b"x" * 8


def test_capture_retains_partial_output_on_timeout() -> None:
    output = io.BytesIO()
    capture = _capture(
        [sys.executable, "-u", "-c", "import time; print('started'); time.sleep(30)"],
        destination=output,
        timeout=1,
        limit=32,
        truncate=True,
    )
    assert capture.status is None
    assert output.getvalue() == b"started\n"


def test_reproduce_requires_full_commit_ids_before_using_docker(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="full lowercase"):
        reproduce(
            repository=tmp_path,
            base="main",
            head="b" * 40,
            image="test-image",
            command=("echo", "approved"),
        )


def test_fix_runs_the_captured_head_and_ephemeral_candidate(tmp_path: Path, monkeypatch) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(repository), *args], text=True).strip()

    git("init", "--quiet", "--object-format=sha1")
    source_file = repository / "source.txt"
    source_file.write_text("before\n")
    (repository / "unrelated.txt").write_text("excluded\n")
    git("add", "source.txt", "unrelated.txt")
    git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "fixture")
    head = git("rev-parse", "HEAD")
    source_file.write_text("dirty checkout\n")
    original_status = git("status", "--porcelain")
    source = ReproductionSource(
        run_id="a" * 32,
        pr_url="https://github.com/org/repo/pull/1",
        patch_sha256="b" * 64,
        base_sha="c" * 40,
        head_sha=head,
    )
    patch = b"diff --git a/source.txt b/source.txt\n--- a/source.txt\n+++ b/source.txt\n"
    patch += b"@@ -1 +1 @@\n-before\n+fixed\n"
    observed = []

    def run_side(_docker, *, commit, tree, archive, image_id, command, timeout):
        assert command == ("check",) and timeout == 5 and image_id == "sha256:" + "d" * 64
        with tarfile.open(fileobj=tree) as tar:
            assert tar.getnames() == ["source.txt"]
            file = tar.extractfile("source.txt")
            assert file is not None
            observed.append(file.read())
        return ReproductionSide(
            commit=commit,
            archive_sha256=archive.digest,
            archive_bytes=archive.size,
            state="exited",
            exit_code=1 if commit == head else 0,
            elapsed_ms=1,
            output="",
            output_sha256=hashlib.sha256(b"").hexdigest(),
            output_bytes=0,
            output_truncated=False,
        )

    monkeypatch.setattr(reproduction, "_engine", lambda *_args: "sha256:" + "d" * 64 + " null")
    monkeypatch.setattr(reproduction, "_run_side", run_side)
    options = {
        "repository": repository,
        "source": source,
        "image": "fixture",
        "command": ("check",),
        "timeout": 5,
        "paths": ("source.txt",),
    }
    receipt = verify_fix(**options, patch=patch)
    repeated = verify_fix(**options, patch=patch)
    assert observed == [b"before\n", b"fixed\n"] * 2
    assert receipt.before.commit == head and receipt.after.commit != head
    assert receipt.after.commit == repeated.after.commit
    assert receipt.after.archive_sha256 == repeated.after.archive_sha256
    assert receipt.outcome == "fail_to_pass"
    assert receipt.proposed_patch_sha256 == hashlib.sha256(patch).hexdigest()
    assert receipt.source == source
    assert receipt.included_paths == ("source.txt",)
    with pytest.raises(ReproductionError, match="does not apply"):
        verify_fix(**options, patch=patch.replace(b"-before", b"-unexpected"))
    unsafe_patch = (
        b"diff --git a/../escaped b/../escaped\nnew file mode 100644\n"
        b"--- /dev/null\n+++ b/../escaped\n@@ -0,0 +1 @@\n+outside\n"
    )
    with pytest.raises(ReproductionError, match="does not apply"):
        verify_fix(**options, patch=unsafe_patch)
    assert not (tmp_path / "escaped").exists()
    assert len(observed) == 4
    assert source_file.read_text() == "dirty checkout\n"
    assert git("status", "--porcelain") == original_status and git("rev-parse", "HEAD") == head
    assert (
        subprocess.run(
            ["git", "-C", str(repository), "cat-file", "-e", receipt.after.commit],
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )


@pytest.mark.parametrize("lost_create", [False, True])
def test_container_cleanup_after_invocation_error_or_lost_create(monkeypatch, lost_create) -> None:
    calls = []

    def engine(_docker, *args):
        calls.append(args)
        if args[0] == "create" and lost_create:
            raise ReproductionError("Docker create failed")
        if args[0] == "inspect":
            return json.dumps({"Status": "exited", "ExitCode": 127, "OOMKilled": False})
        return ""

    def attach(_command, *, destination, **_kwargs):
        output = b"Omnigent reproduction started\ncheck: not found\n"
        destination.write(output)
        return reproduction._Capture(127, len(output), hashlib.sha256(output).hexdigest())

    monkeypatch.setattr(reproduction, "_engine", engine)
    monkeypatch.setattr(reproduction, "_capture", attach)
    options = {
        "commit": "a" * 40,
        "tree": io.BytesIO(),
        "archive": reproduction._Capture(0, 10_240, "a" * 64),
        "image_id": "sha256:" + "a" * 64,
        "command": ("check",),
        "timeout": 5,
    }
    if lost_create:
        with pytest.raises(ReproductionError, match="create failed"):
            reproduction._run_side(["docker"], **options)
    else:
        result = reproduction._run_side(["docker"], **options)
        assert result.state == "setup_error" and result.exit_code == 127
    create, cleanup = calls[0], calls[-1]
    assert create[0] == "create" and cleanup == ("rm", "--force", "--volumes", create[2])
