"""Choose a bounded PR sample. The regular CI workflows still provide full coverage."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import PurePosixPath

MAX_PER_SUITE = 4
VERSION_TEST = "tests/test_version.py"
WEB_SMOKE = "web/src/lib/capabilities.test.ts"
RELEASE_TEST = "tests/deploy/test_studio_release.py"
RELEASE_FILES = {
    ".github/workflows/studio-build.yml",
    "deploy/docker/Dockerfile",
    "deploy/docker/Dockerfile.dockerignore",
    "deploy/docker/studio_release.py",
}
SLOW_DIRS = {"e2e", "e2e_ui", "e2e_live", "browser_ui", "integration", "benchmarks"}
SAFE_PATH = re.compile(r"[a-zA-Z0-9_./@+-]+")


def test_suite(path: str) -> str | None:
    parts = PurePosixPath(path).parts
    if not SAFE_PATH.fullmatch(path) or ".." in parts:
        return None
    if path.startswith("web/src/") and path.endswith((".test.ts", ".test.tsx")):
        return "web"
    if path.startswith("tests/") and re.fullmatch(r"test_.+\.py", parts[-1]):
        if SLOW_DIRS.isdisjoint(parts):
            return "python"
    return None


def selection(changed: list[str], regular_files: set[str]) -> dict:
    # Changed test files first, source/test pairs second, then domain samples.
    priority: dict[str, int] = {}
    generic = False
    sampled = False
    for path in sorted(set(changed)) or [""]:
        source = PurePosixPath(path)
        if test_suite(path) and path in regular_files:
            priority[path] = 0
            continue
        pairs: list[str] = []
        if path.startswith("omnigent/") and source.suffix == ".py":
            mirror = source.relative_to("omnigent").with_name(f"test_{source.name}")
            pairs.append(f"tests/{mirror}")
        elif path.startswith("web/src/") and source.suffix in {".ts", ".tsx"}:
            pairs = [str(source.with_suffix(ext)) for ext in (".test.ts", ".test.tsx")]
        exact = [p for p in pairs if p in regular_files and test_suite(p)]
        if exact:
            for target in exact:
                priority[target] = min(priority.get(target, 1), 1)
            continue
        sampled = True
        if path in RELEASE_FILES or path == RELEASE_TEST:
            target = RELEASE_TEST
        elif path.startswith("omnigent/db/migrations/"):
            target = "tests/db/test_migration_studio_dispatch.py"
        elif any(part.startswith(("auth", "permission")) for part in source.parts) or (
            "permission" in source.stem
        ):
            target = (
                "web/src/lib/permissionsApi.test.ts"
                if path.startswith("web/")
                else "tests/entities/test_permission.py"
            )
        elif path.startswith(("omnigent/stores/", "omnigent/db/")):
            target = "tests/stores/test_session_journal_store.py"
        elif path.startswith(("omnigent/studio/", "omnigent/extensions/")):
            target = "tests/extensions/test_studio_rpc.py"
        elif path.startswith("omnigent/") and source.suffix == ".py":
            target = "tests/server/test_host_registry.py"
        elif path.startswith("web/"):
            target = WEB_SMOKE
        else:
            target = VERSION_TEST
            generic = True
        if target not in regular_files:
            target = VERSION_TEST
            generic = True
        if target not in regular_files:
            raise ValueError(f"Missing PR signal fallback: {target}")
        priority.setdefault(target, 2)
    ranked = sorted(priority, key=lambda path: (priority[path], path))
    output = {}
    for suite in ("python", "web"):
        tests = [path for path in ranked if test_suite(path) == suite]
        sampled |= len(tests) > MAX_PER_SUITE
        output[suite] = tests[:MAX_PER_SUITE]
    return {**output, "smoke": sampled, "generic_fallback": generic}


def main(base: str, head: str) -> None:
    if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (base, head)):
        raise ValueError("Expected full Git commit SHAs")

    def records(*args: str) -> list[str]:
        data = subprocess.check_output(["git", *args])
        return [os.fsdecode(entry) for entry in data.split(b"\0") if entry]

    changed = records("diff", "--no-renames", "--name-only", "-z", base, head, "--")
    entries = records("ls-tree", "-r", "-z", head, "--", "tests", "web/src")
    regular_files = {entry.split("\t", 1)[1] for entry in entries if entry.startswith("100")}
    print(json.dumps(selection(changed, regular_files)))


if __name__ == "__main__":
    main(*sys.argv[1:])
