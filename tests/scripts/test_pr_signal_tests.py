"""Exercise PR signal selection without changing the regular CI selection."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / ".github/scripts/ci/pr_signal_tests.py"
spec = importlib.util.spec_from_file_location("pr_signal_tests", SCRIPT)
signal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(signal)


def test_mixed_changes_select_pairs_and_fallbacks():
    paths = {
        "tests/runner/test_helpers.py",
        "tests/db/test_migration_studio_dispatch.py",
        "tests/stores/test_session_journal_store.py",
        "tests/entities/test_permission.py",
        "web/src/lib/widget.test.tsx",
    }
    result = signal.selection(
        [
            "omnigent/runner/helpers.py",
            "omnigent/stores/new_store.py",
            "omnigent/server/auth.py",
            "omnigent/db/migrations/versions/abc.py",
            "web/src/lib/widget.ts",
        ],
        paths,
    )
    assert set(result["python"] + result["web"]) == paths
    assert result["smoke"] and not result["generic_fallback"]


@pytest.mark.parametrize(
    ("path", "suite"),
    [
        ("omnigent/runner/new_module.py", "tests/server/test_host_registry.py"),
        ("omnigent/studio/new_feature.py", "tests/extensions/test_studio_rpc.py"),
        ("omnigent/extensions/new_extension.py", "tests/extensions/test_studio_rpc.py"),
    ],
)
def test_unpaired_backend_sources_use_runtime_or_studio_smokes(path, suite):
    result = signal.selection([path], {suite, signal.VERSION_TEST})
    assert result == {"python": [suite], "web": [], "smoke": True, "generic_fallback": False}


def test_changed_tests_precede_pairs_and_selection_is_bounded():
    changed = [f"tests/test_example_{n}.py" for n in range(6)] + ["omnigent/alpha.py"]
    files = {*changed, "tests/test_alpha.py"}
    sample = signal.selection(changed, files)
    assert sample == signal.selection(changed[::-1], files)
    assert sample["python"] == sorted(changed[:-1])[: signal.MAX_PER_SUITE]
    assert sample["smoke"] and not sample["generic_fallback"]


@pytest.mark.parametrize(
    "path", ["docs/readme.md", "tests/e2e/test_real.py", "tests/test_;bad.py"]
)
def test_unknown_or_unsuitable_paths_use_explicit_smoke(path):
    result = signal.selection([path], {path, signal.VERSION_TEST})
    assert result == {
        "python": [signal.VERSION_TEST],
        "web": [],
        "smoke": True,
        "generic_fallback": True,
    }


def test_release_only_and_unknown_web_file_use_relevant_smokes():
    result = signal.selection(
        [*signal.RELEASE_FILES, "web/tsconfig.json"], {signal.RELEASE_TEST, signal.WEB_SMOKE}
    )
    assert result["python"] == [signal.RELEASE_TEST]
    assert result["web"] == [signal.WEB_SMOKE]
    assert result["smoke"] and not result["generic_fallback"]


def test_git_selection_ignores_removed_and_symlinked_test_files(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    git("config", "user.name", "CI")
    git("config", "user.email", "ci@example.test")
    tests = tmp_path / "tests"
    tests.mkdir()
    for name in ("test_removed.py", "test_version.py"):
        (tests / name).touch()
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (tests / "test_removed.py").unlink()
    (tests / "test_link.py").symlink_to("test_version.py")
    git("add", "-A")
    git("commit", "-qm", "change")
    result = json.loads(
        subprocess.check_output(
            [sys.executable, SCRIPT, base, git("rev-parse", "HEAD")],
            cwd=tmp_path,
        )
    )
    assert result["python"] == [signal.VERSION_TEST]
    assert result["generic_fallback"]
