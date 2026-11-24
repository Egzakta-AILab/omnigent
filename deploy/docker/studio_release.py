"""Validate the release commit and private destination before a Studio image build."""

import argparse
import json
import re
import subprocess
from collections import Counter
from collections.abc import Callable
from pathlib import Path

EXTENSIONS = "Egzakta-AILab/studio-extensions"
PACKAGE = "omnigent-studio"
DISTRIBUTIONS = {
    "omnigent",
    "omnigent-client",
    "omnigent-ui-sdk",
    "studio-rpc-contract",
    "egzakta-coder",
    "egzakta-work",
    "egzakta-review",
    "egzakta-fleet",
    "egzakta-comms",
    "egzakta-release",
}
SHA = re.compile(r"[0-9a-f]{40}")


def verify_pin(tag: str, commit: str, read: Callable[[str], dict]) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,126}", tag) or not SHA.fullmatch(commit):
        raise ValueError("Supply an exact release tag and a full lowercase commit SHA.")
    metadata = read(f"releases/tags/{tag}")
    if (
        metadata["tag_name"] != tag
        or metadata["draft"] is not False
        or not metadata.get("published_at")
    ):
        raise ValueError("A published extension release is required.")
    ref = read(f"git/ref/tags/{tag}")
    if ref["ref"] != f"refs/tags/{tag}":
        raise ValueError("Expected the exact release tag ref.")
    target = ref["object"]
    seen = set()
    while target["type"] == "tag":
        sha = target["sha"]
        if not SHA.fullmatch(sha) or sha in seen or len(seen) >= 16:
            raise ValueError("Invalid annotated tag chain.")
        seen.add(sha)
        target = read(f"git/tags/{sha}")["object"]
    if target["type"] != "commit" or target["sha"] != commit:
        raise ValueError("The release tag no longer resolves to the pinned commit.")


def verify_registry(metadata: dict) -> None:
    if (
        metadata.get("visibility") != "private"
        or metadata.get("name") != PACKAGE
        or metadata.get("package_type") != "container"
    ):
        raise ValueError("An existing private Studio container package is required.")


def verify_wheels(directory: Path) -> None:
    counts = Counter(w.name.split("-", 1)[0].replace("_", "-") for w in directory.glob("*.whl"))
    if counts != dict.fromkeys(DISTRIBUTIONS, 1):
        raise ValueError(
            "The Studio wheelhouse must contain one wheel for every required distribution."
        )


def api(path: str) -> dict:
    return json.loads(subprocess.check_output(["gh", "api", path], text=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    pin = subs.add_parser("pin")
    pin.add_argument("tag")
    pin.add_argument("commit")
    subs.add_parser("registry")
    wheels = subs.add_parser("wheels")
    wheels.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "pin":
        verify_pin(args.tag, args.commit, lambda path: api(f"repos/{EXTENSIONS}/{path}"))
    elif args.command == "registry":
        verify_registry(api(f"orgs/Egzakta-AILab/packages/container/{PACKAGE}"))
    else:
        verify_wheels(args.directory)


if __name__ == "__main__":
    main()
