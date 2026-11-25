"""Studio setup, verification, and reproduction commands."""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import math
import os
import re
import subprocess
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import httpx
from packaging.version import InvalidVersion, Version

from omnigent.diagnostics import redact_url
from omnigent.harness_availability import (
    is_harness_availability,
    reported_harness_availability,
)
from omnigent.version import VERSION

HARNESSES = ("codex-native", "claude-native")
_CHECKOUT = Path(__file__).resolve().parents[2]
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def validate_server_url(server_url: str, mode: str) -> str:
    """Validate the explicit credential destination without echoing rejected input."""
    try:
        if any(ord(char) < 33 or ord(char) == 127 for char in server_url):
            raise ValueError
        parsed = urlsplit(server_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "?" in server_url
            or "#" in server_url
        ):
            raise ValueError
        # Accessing port validates malformed and out-of-range ports.
        _ = parsed.port
        httpx.URL(server_url)
        hostname = parsed.hostname.lower()
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = hostname == "localhost"
        if mode == "local" and not loopback:
            raise ValueError
        if parsed.scheme == "http" and not loopback:
            raise ValueError
    except (ValueError, httpx.InvalidURL):
        raise ValueError(
            "Use an explicit HTTPS server URL, or HTTP on loopback; local mode requires "
            "loopback. User information, queries, fragments and whitespace are not allowed."
        ) from None
    return server_url.rstrip("/")


def _headers(server_url: str, host_id: str | None) -> dict[str, str]:
    from omnigent.chat import _remote_headers

    return _remote_headers(server_url=server_url, host_id=host_id)


def _checkout_identity() -> dict[str, str | bool | None]:
    """Read this verifier's source identity separately from the remote server."""

    def git(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(_CHECKOUT), *args],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain")
    return {
        "commit": commit if commit and _SHA.fullmatch(commit) else None,
        "dirty": bool(status) if status is not None else None,
    }


class _Probe:
    def __init__(self, client: httpx.Client, checks: list[dict[str, Any]]) -> None:
        self.client = client
        self.checks = checks

    def check(
        self,
        name: str,
        status: Literal["pass", "fail", "unknown"],
        reason: str,
    ) -> None:
        self.checks.append({"name": name, "status": status, "reason": reason})

    def get(
        self, name: str, path: str, *, params: dict[str, str] | None = None
    ) -> dict[str, Any] | None:
        try:
            response = self.client.get(path, params=params)
        except httpx.HTTPError:
            self.check(name, "unknown", "request_failed")
            return None
        if response.status_code != 200:
            self.check(name, "fail", f"http_{response.status_code}")
            return None
        try:
            data = response.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            self.check(name, "unknown", "invalid_response")
            return None
        return data


def _server_checks(probe: _Probe, mode: str) -> dict[str, Any]:
    health = probe.get("server.health", "health")
    if health is not None:
        probe.check(
            "server.health", "pass" if health.get("status") == "ok" else "fail", "liveness_only"
        )
    manifest = probe.get("server.manifest", ".well-known/omnigent.json")
    auth_mode = None
    if manifest is not None:
        auth = manifest.get("auth")
        reported_mode = auth.get("mode") if isinstance(auth, dict) else None
        auth_mode = (
            reported_mode
            if reported_mode in ("accounts", "oidc", "header", "custom", "none")
            else None
        )
        valid = (
            type(manifest.get("manifest_version")) is int
            and manifest["manifest_version"] >= 1
            and auth_mode is not None
        )
        probe.check("server.manifest", "pass" if valid else "unknown", "capability_discovery")
    info = probe.get("server.info", "v1/info")
    observed: dict[str, Any] = {"version": None, "source_commit": None, "auth_mode": None}
    if info is None:
        return observed
    reported_version = info.get("server_version")
    version = None
    if isinstance(reported_version, str) and len(reported_version) <= 80:
        with contextlib.suppress(InvalidVersion):
            version = str(Version(reported_version))
    valid = (
        version is not None
        and type(info.get("single_user")) is bool
        and type(info.get("accounts_enabled")) is bool
        and type(info.get("needs_setup")) is bool
        and info.get("accounts_enabled") == (auth_mode == "accounts")
    )
    if not valid:
        probe.check("server.info", "unknown", "invalid_capability_response")
        return observed
    observed["version"] = version
    observed["auth_mode"] = "single_user" if info["single_user"] else auth_mode
    commit = info.get("source_commit")
    if isinstance(commit, str) and _SHA.fullmatch(commit):
        observed["source_commit"] = commit
    probe.check("server.info", "pass", "observed_from_server")
    if mode == "server":
        protected = not info["single_user"] and auth_mode in (
            "accounts",
            "oidc",
            "header",
            "custom",
        )
        probe.check("server.authentication", "pass" if protected else "fail", "shared_server_auth")
    if info["needs_setup"]:
        probe.check("server.account_setup", "fail", "initial_account_setup_required")
    return observed


def _harness_checks(
    probe: _Probe,
    *,
    harness: str,
    host_path: str,
    host_id: str,
    workspace: str,
    host: dict[str, Any] | None,
    mcps: list[dict[str, Any]] | None,
    required_skills: Sequence[str],
    required_mcps: Sequence[str],
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "reported_ready": None,
        "startup_source": None,
        "skills": None,
        "skill_warning_count": None,
        "mcp_servers": None,
        "mcp_inventory_scope": "user",
        "mcp_connectivity": "not_run",
    }
    if host is None or host.get("status") != "online":
        probe.check(f"{harness}.target", "unknown", "online_owned_host_required")
        return summary
    readiness = host.get("configured_harnesses")
    if isinstance(readiness, dict) and (
        harness not in readiness or is_harness_availability(readiness[harness])
    ):
        available, reason = reported_harness_availability(harness, readiness)
        summary["reported_ready"] = available
        probe.check(
            f"{harness}.readiness",
            "pass" if available is True else "fail" if available is False else "unknown",
            reason or ("host_report_only" if available is True else "readiness_unreported"),
        )
    else:
        probe.check(f"{harness}.readiness", "unknown", "readiness_unreported_or_invalid")
    startup = probe.get(f"{harness}.startup", f"{host_path}/harnesses/{harness}/startup")
    if startup is not None:
        source = startup.get("command_source")
        valid_source = source in ("env", "config", "default")
        summary["startup_source"] = source if valid_source else None
        probe.check(
            f"{harness}.startup", "pass" if valid_source else "unknown", "source_label_only"
        )
    skills = probe.get(
        f"{harness}.skills",
        "v1/skills",
        params={"host_id": host_id, "harness": harness, "path": workspace},
    )
    if skills is not None:
        entries, warnings = skills.get("skills"), skills.get("warnings")
        valid_skills = (
            isinstance(entries, list)
            and all(isinstance(s, dict) and isinstance(s.get("name"), str) for s in entries)
            and isinstance(warnings, list)
            and all(isinstance(w, str) for w in warnings)
        )
        if valid_skills:
            names = sorted({skill["name"] for skill in entries})
            summary["skills"], summary["skill_warning_count"] = names, len(warnings)
            missing = sorted(set(required_skills) - set(names))
            summary["missing_required_skills"] = missing
            probe.check(
                f"{harness}.skills",
                "fail" if warnings or missing else "pass",
                "incomplete_inventory"
                if warnings
                else "required_skill_missing"
                if missing
                else "inventory_only",
            )
        else:
            probe.check(f"{harness}.skills", "unknown", "invalid_inventory")
    if mcps is not None:
        family = harness.removesuffix("-native")
        names = sorted({item["name"] for item in mcps if item["harness"] == family})
        summary["mcp_servers"] = names
        missing = sorted(set(required_mcps) - set(names))
        summary["missing_required_mcps"] = missing
        probe.check(
            f"{harness}.mcps",
            "fail" if missing else "pass",
            "required_mcp_missing" if missing else "inventory_only",
        )
    return summary


def collect_setup(
    *,
    mode: str,
    server_url: str,
    host_id: str,
    workspace: str,
    harnesses: Sequence[str] = HARNESSES,
    required_skills: Sequence[str] = (),
    required_mcps: Sequence[str] = (),
    timeout: float = 5.0,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Collect preflight signals; real agent, PR and reconnect checks remain separate.

    ``transport`` is injectable for deterministic HTTP contract tests. Only GET
    requests are made; the caller names the credential destination explicitly.
    """
    if mode not in ("local", "server"):
        raise ValueError("Mode must be local or server.")
    server_url = validate_server_url(server_url, mode)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", host_id):
        raise ValueError("Provide a valid explicit host ID.")
    if not PurePosixPath(workspace).is_absolute() or any(ord(c) < 32 for c in workspace):
        raise ValueError("Provide an absolute workspace path on the selected host.")
    if not harnesses or any(h not in HARNESSES for h in harnesses):
        raise ValueError("Select codex-native, claude-native, or both.")
    if not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError("Timeout must be greater than zero and at most 30 seconds.")
    checks: list[dict[str, Any]] = []
    receipt: dict[str, Any] = {
        "schema": "omnigent.studio.setup.v1",
        "observed_at": datetime.now(UTC).isoformat(),
        "mode": mode,
        "target": {
            "server_url": redact_url(server_url),
            "host_id": host_id,
            "workspace": workspace,
        },
        "verifier": {"version": VERSION, "checkout": _checkout_identity()},
        "server": {"version": None, "source_commit": None, "auth_mode": None},
        "checks": checks,
        "harnesses": {},
        "preflight_ready": False,
        "qualification": {
            "agent_turn": "not_run",
            "continuation": "not_run",
            "reconnect": "not_run",
            "executor_pr_access": "not_run",
            "server_source_identity": "not_run",
            "execution_host_placement": "not_run",
        },
    }
    try:
        headers = _headers(server_url, host_id)
    except Exception:  # noqa: BLE001 — credential backends must not expose exception contents
        checks.append(
            {"name": "credentials", "status": "unknown", "reason": "credential_resolution_failed"}
        )
        return receipt
    with httpx.Client(
        base_url=f"{server_url}/",
        headers=headers,
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    ) as client:
        probe = _Probe(client, checks)
        receipt["server"] = _server_checks(probe, mode)
        host_path = f"v1/hosts/{quote(host_id, safe='')}"
        host = probe.get("host.access", host_path)
        if host is not None:
            valid_host = host.get("host_id") == host_id and host.get("status") in (
                "online",
                "offline",
            )
            probe.check(
                "host.access", "pass" if valid_host else "unknown", "selected_host_response"
            )
            if not valid_host:
                host = None
            else:
                probe.check(
                    "host.online",
                    "pass" if host["status"] == "online" else "fail",
                    "host_liveness",
                )
        mcps = None
        if host is not None and host.get("status") == "online":
            inventory = probe.get("host.mcp_inventory", f"{host_path}/mcp-servers")
            if inventory is not None:
                entries = inventory.get("mcp_servers")
                if isinstance(entries, list) and all(
                    isinstance(item, dict)
                    and isinstance(item.get("name"), str)
                    and isinstance(item.get("harness"), str)
                    for item in entries
                ):
                    mcps = entries
                    probe.check("host.mcp_inventory", "pass", "inventory_only")
                else:
                    probe.check("host.mcp_inventory", "unknown", "invalid_inventory")
        for harness in dict.fromkeys(harnesses):
            receipt["harnesses"][harness] = _harness_checks(
                probe,
                harness=harness,
                host_path=host_path,
                host_id=host_id,
                workspace=workspace,
                host=host,
                mcps=mcps,
                required_skills=required_skills,
                required_mcps=required_mcps,
            )
    receipt["preflight_ready"] = bool(checks) and all(c["status"] == "pass" for c in checks)
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    """Run setup checks and return nonzero for failed or unknown required signals."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="read-only preflight; does not run an agent")
    setup.add_argument("--mode", choices=("local", "server"), required=True)
    setup.add_argument("--server", required=True, help="explicit trusted instance URL")
    setup.add_argument("--host-id", required=True)
    setup.add_argument("--workspace", required=True, help="absolute path on that host")
    setup.add_argument("--harness", choices=(*HARNESSES, "both"), action="append")
    setup.add_argument("--require-skill", action="append", default=[])
    setup.add_argument("--require-mcp", action="append", default=[])
    setup.add_argument("--timeout", type=float, default=5.0)
    setup.add_argument("--output", type=Path, help="write a JSON receipt")
    setup.add_argument("--json", action="store_true", help="print the JSON receipt")
    contracts = commands.add_parser("contracts", help="run deterministic local A2 contracts")
    contracts.add_argument("--output", type=Path, help="write a JSON receipt copy")
    contracts.add_argument("--json", action="store_true", help="print the JSON receipt")
    creator = commands.add_parser("creator-contracts", help="inspect the review panel contract")
    creator.add_argument("--manifest", type=Path, help="validate a review.panel manifest")
    creator_preview = commands.add_parser(
        "creator-preview", help="verify a scoped Creator preview"
    )
    creator_preview.add_argument("--mode", choices=("local", "server"), required=True)
    creator_preview.add_argument("--server", required=True, help="explicit trusted instance URL")
    creator_preview.add_argument("--review", required=True, help="saved review run ID")
    creator_preview.add_argument(
        "--definition", type=Path, help="JSON definition, or use the template"
    )
    creator_release = commands.add_parser("release-b", help="read back a Creator installation")
    creator_release.add_argument("--mode", choices=("local", "server"), required=True)
    creator_release.add_argument("--server", required=True, help="explicit trusted instance URL")
    creator_release.add_argument("--review", required=True, help="saved review run ID")
    creator_release.add_argument("--package", type=Path, required=True, help="JSON definition")
    creator_release.add_argument("--expect", choices=("enabled", "disabled"), default="enabled")
    creator_release.add_argument("--version", type=int, help="expected selected version")
    release_a = commands.add_parser("release-a", help="verify Release A acceptance evidence")
    release_a.add_argument(
        "--matrix", default="local:codex,local:claude,server:codex,server:claude"
    )
    release_a.add_argument("--pr", help="representative pull request URL")
    release_a.add_argument("--second-repo", help="absolute checkout path on its host")
    release_a.add_argument("--manifest", type=Path, help="explicit instance and retained evidence")
    release_a.add_argument(
        "--schema", action="store_true", help="print the evidence manifest schema"
    )
    release_a.add_argument("--output", type=Path, help="write a JSON receipt copy")
    review = commands.add_parser("review", help="verify a saved native review report")
    review.add_argument("--mode", choices=("local", "server"), required=True)
    review.add_argument("--server", required=True, help="explicit trusted instance URL")
    review.add_argument("--review-id", required=True, help="ID of an existing review")
    review.add_argument("--harness", choices=HARNESSES, required=True)
    review.add_argument("--pr", required=True, help="expected GitHub pull request URL")
    review.add_argument("--output", type=Path, help="write a JSON receipt")
    review.add_argument("--json", action="store_true", help="print the JSON receipt")
    run = commands.add_parser("review-run", help="verify a saved PR review run and its citations")
    run.add_argument("--mode", choices=("local", "server"), required=True)
    run.add_argument("--server", required=True, help="explicit trusted instance URL")
    run.add_argument("--run-id", required=True)
    run.add_argument("--pr", required=True, help="expected pull request URL")
    run.add_argument("--base", required=True, help="expected full base SHA")
    run.add_argument("--head", required=True, help="expected full head SHA")
    run.add_argument("--minimum-reviewers", type=int, default=1)
    run.add_argument("--output", type=Path, help="write a JSON receipt")
    run.add_argument("--json", action="store_true", help="print the JSON receipt")
    lifecycle = commands.add_parser("lifecycle", help="verify a saved proof and candidate lineage")
    lifecycle.add_argument("--mode", choices=("local", "server"), required=True)
    lifecycle.add_argument("--server", required=True, help="explicit trusted instance URL")
    lifecycle.add_argument("--pr", required=True, help="expected pull request URL")
    lifecycle.add_argument("--run-id", required=True)
    lifecycle.add_argument("--finding", required=True, help="saved finding key")
    lifecycle.add_argument("--question", required=True, help="saved proof-backed question ID")
    lifecycle.add_argument("--candidate", required=True, help="saved candidate review run ID")
    lifecycle.add_argument("--output", type=Path, help="write a JSON receipt")
    lifecycle.add_argument("--json", action="store_true", help="print the JSON receipt")
    publication = commands.add_parser(
        "publication", help="read back a previously confirmed GitHub inline comment"
    )
    publication.add_argument("--mode", choices=("local", "server"), required=True)
    publication.add_argument("--server", required=True, help="explicit trusted instance URL")
    publication.add_argument("--preview", type=Path, required=True, help="retained preview JSON")
    publication.add_argument("--draft", required=True, help="one selected draft ID")
    publication.add_argument("--author", required=True, help="expected GitHub login")
    publication.add_argument("--output", type=Path, help="write a JSON receipt")
    publication.add_argument("--json", action="store_true", help="print the JSON receipt")
    reproduction = commands.add_parser("reproduce", help="compare two Git revisions in Docker")
    reproduction.add_argument("--repository", type=Path, required=True)
    reproduction.add_argument("--base", help="full base commit ID")
    reproduction.add_argument("--head", help="full head commit ID")
    reproduction.add_argument("--run-id", help="read both commits from a saved review run")
    reproduction.add_argument(
        "--proposal-id", help="use the exact command from a saved agent proposal"
    )
    reproduction.add_argument("--mode", choices=("local", "server"))
    reproduction.add_argument("--server", help="trusted instance URL for --run-id")
    reproduction.add_argument("--pr", help="expected pull request URL for --run-id")
    reproduction.add_argument(
        "--fix-patch", type=Path, help="compare a proposed patch with the saved head"
    )
    reproduction.add_argument(
        "--include-path",
        action="append",
        default=[],
        help="limit both snapshots to a Git path",
    )
    reproduction.add_argument("--image", required=True, help="locally installed test image")
    reproduction.add_argument("--socket", type=Path, default=Path("/var/run/docker.sock"))
    reproduction.add_argument("--timeout", type=int, default=60, help="seconds per revision")
    reproduction.add_argument("--output", type=Path, required=True, help="new private JSON file")
    reproduction.add_argument(
        "--capture-evidence",
        action="append",
        default=[],
        metavar="FILENAME",
        help="collect a named file from /evidence on each revision, up to two names",
    )
    reproduction.add_argument("argv", nargs=argparse.REMAINDER, help="command after --")
    proof = commands.add_parser("attach-proof", help="attach a saved check and its evidence")
    proof.add_argument("--mode", choices=("local", "server"), required=True)
    proof.add_argument("--server", required=True)
    proof.add_argument("--finding", required=True, help="captured finding key")
    proof.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "release-a":
        from omnigent.studio.release_a import ROUTES, ReleaseEvidence, collect_release_a

        if args.schema:
            print(json.dumps(ReleaseEvidence.model_json_schema(), indent=2))
            return 0
        if not args.pr or not args.second_repo:
            parser.error("Provide --pr and --second-repo to verify Release A")
        if sorted(args.matrix.split(",")) != sorted(ROUTES):
            parser.error("Release A requires all four local and server routes exactly once")
        try:
            receipt = collect_release_a(
                pr_url=args.pr, second_repo=args.second_repo, manifest=args.manifest
            )
            serialized = json.dumps(receipt, indent=2) + "\n"
            if args.output:
                args.output.write_text(serialized, encoding="utf-8")
        except ValueError as exc:
            parser.error(str(exc))
        except OSError:
            parser.error("Could not write the release receipt")
        print(serialized, end="")
        return 0 if receipt["release_ready"] else 1
    if args.command == "creator-contracts":
        from pydantic import ValidationError

        from omnigent.extensions.review_panel import ReviewPanelContribution, inspect_review_panel
        from omnigent.studio.creator_panel import creator_panel_contract

        catalog = inspect_review_panel()
        catalog["generated_panel"] = creator_panel_contract().model_dump(mode="json")
        if args.manifest is not None:
            try:
                manifest = ReviewPanelContribution.model_validate_json(args.manifest.read_bytes())
            except (OSError, ValidationError):
                parser.error("Invalid review.panel manifest. Check the installed manifest schema.")
            catalog["validated_manifest"] = manifest.model_dump(mode="json")
        print(json.dumps(catalog, indent=2))
        return 0
    if args.command == "creator-preview":
        from omnigent.studio.verify_creator import verify_creator_preview

        try:
            preview = verify_creator_preview(
                mode=args.mode,
                server_url=args.server,
                run_id=args.review,
                definition=args.definition,
            )
        except ValueError as exc:
            parser.error(str(exc))
        print(preview.model_dump_json(indent=2))
        return 0
    if args.command == "release-b":
        from omnigent.studio.verify_creator import verify_creator_installation

        try:
            installation = verify_creator_installation(
                mode=args.mode,
                server_url=args.server,
                run_id=args.review,
                package=args.package,
                expect=args.expect,
                version=args.version,
            )
        except ValueError as exc:
            parser.error(str(exc))
        print(installation.model_dump_json(indent=2))
        return 0
    if args.command == "attach-proof":
        from omnigent.studio.attach_proof import attach_proof

        try:
            server = validate_server_url(args.server, args.mode)
            try:
                headers = _headers(server, None)
            except Exception:  # noqa: BLE001 — credential errors may contain private values
                parser.exit(2, "Could not load the instance credentials.\n")
            with httpx.Client(
                base_url=f"{server}/",
                headers=headers,
                timeout=60,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                count = attach_proof(
                    client, receipt_path=args.receipt.expanduser(), finding_key=args.finding
                )
        except httpx.HTTPStatusError as exc:
            parser.exit(
                2,
                f"Could not attach all evidence (HTTP {exc.response.status_code}). "
                "Retry with the same receipt.\n",
            )
        except httpx.HTTPError:
            parser.exit(2, "Could not reach the evidence API. Retry with the same receipt.\n")
        except (OSError, ValueError) as exc:
            parser.exit(2, f"Could not attach evidence: {exc}\n")
        print(f"Attached the check receipt and {count} file links.")
        return 0
    if args.command == "reproduce":
        from omnigent.studio.attach_proof import evidence_directory
        from omnigent.studio.reviews.reproduction import (
            MAX_FIX_PATCH_BYTES,
            EvidenceCollection,
            FixVerificationReceipt,
            ReproductionError,
            reproduce,
            verify_fix,
        )
        from omnigent.studio.verify_review_run import read_check_proposal, read_reproduction_source

        command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        if args.output.exists():
            parser.error("Choose a new file for the reproduction receipt")
        if args.capture_evidence and args.fix_patch is not None:
            parser.error("--capture-evidence currently supports base/head reproductions")
        if args.proposal_id and (
            not args.run_id
            or args.fix_patch is not None
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", args.image)
        ):
            parser.error(
                "A sandbox proposal needs a saved review and an immutable Docker image ID"
            )
        try:
            source = None
            if any((args.run_id, args.mode, args.server, args.pr)):
                if not all((args.run_id, args.mode, args.server, args.pr)):
                    parser.error("Use --run-id, --mode, --server and --pr together")
                if args.base or args.head:
                    parser.error("Use a saved review run or specify --base and --head")
                source = read_reproduction_source(
                    mode=args.mode, server_url=args.server, run_id=args.run_id, pr_url=args.pr
                )
                base, head = source.base_sha, source.head_sha
            else:
                if not args.base or not args.head:
                    parser.error("Use a saved review run or specify --base and --head")
                base, head = args.base, args.head
            if args.proposal_id:
                proposal = read_check_proposal(
                    mode=args.mode,
                    server_url=args.server,
                    run_id=args.run_id,
                    proposal_id=args.proposal_id,
                )
                if not proposal.matches_intent(
                    command=tuple(command),
                    paths=tuple(args.include_path),
                    timeout=args.timeout,
                    evidence_files=tuple(args.capture_evidence),
                ):
                    raise ReproductionError("The command differs from the saved check proposal")
            options: dict[str, Any] = {
                "repository": args.repository.expanduser().resolve(),
                "image": args.image,
                "command": tuple(command),
                "socket": args.socket.expanduser(),
                "timeout": args.timeout,
            }
            if args.fix_patch is not None:
                if source is None:
                    parser.error("--fix-patch requires a saved review run")
                with args.fix_patch.expanduser().open("rb") as patch_file:
                    patch = patch_file.read(MAX_FIX_PATCH_BYTES + 1)
                result = verify_fix(
                    source=source, patch=patch, paths=tuple(args.include_path), **options
                )
            else:
                collection = None
                if args.capture_evidence:
                    collection = EvidenceCollection(
                        tuple(args.capture_evidence), evidence_directory(args.output)
                    )
                    collection.directory.mkdir(mode=0o700, parents=True)
                result = reproduce(
                    base=base,
                    head=head,
                    source=source,
                    collection=collection,
                    paths=tuple(args.include_path),
                    proposal_id=args.proposal_id,
                    **options,
                )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=args.output.parent, prefix=".reproduction-"
            ) as receipt_file:
                receipt_file.write(result.model_dump_json(indent=2) + "\n")
                receipt_file.flush()
                os.link(receipt_file.name, args.output)
        except (OSError, ReproductionError, ValueError) as exc:
            parser.exit(2, f"Reproduction failed: {exc}\n")
        if isinstance(result, FixVerificationReceipt):
            sides = (("Captured head", result.before), ("Proposed snapshot", result.after))
        else:
            sides = (("Base", result.base), ("Head", result.head))
        for label, side in sides:
            print(
                f"{label} {side.commit[:8]}: {side.state}, "
                f"exit {side.exit_code}, {side.elapsed_ms} ms"
            )
        if isinstance(result, FixVerificationReceipt):
            print(f"Comparison: {result.outcome.replace('_', ' ')}")
            return 0 if result.outcome == "fail_to_pass" else 1
        complete = result.base.state == result.head.state == "exited"
        for label, side in sides:
            missing = set(args.capture_evidence) - {item.name for item in side.evidence}
            if missing:
                print(f"{label} evidence unavailable: {', '.join(sorted(missing))}")
                complete = False
        return 0 if complete else 1
    if args.command == "contracts":
        from omnigent.studio.contracts import main as contracts_main

        return contracts_main(output=args.output, json_output=args.json)
    if args.command in ("review", "review-run", "lifecycle", "publication"):
        try:
            if args.command == "review":
                from omnigent.studio.verify_review import collect_review

                receipt = collect_review(
                    mode=args.mode,
                    server_url=args.server,
                    review_id=args.review_id,
                    harness=args.harness,
                    pr_url=args.pr,
                )
                ready_key, label = "review_ready", "Saved review"
            elif args.command == "review-run":
                from omnigent.studio.verify_review_run import collect_review_run

                receipt = collect_review_run(
                    mode=args.mode,
                    server_url=args.server,
                    run_id=args.run_id,
                    pr_url=args.pr,
                    base_sha=args.base,
                    head_sha=args.head,
                    minimum_reviewers=args.minimum_reviewers,
                )
                ready_key, label = "run_ready", "Saved review run"
            elif args.command == "lifecycle":
                from omnigent.studio.verify_lifecycle import collect_lifecycle

                receipt = collect_lifecycle(
                    mode=args.mode,
                    server_url=args.server,
                    run_id=args.run_id,
                    pr_url=args.pr,
                    finding_key=args.finding,
                    question_id=args.question,
                    candidate_id=args.candidate,
                )
                ready_key, label = "lifecycle_ready", "Saved review lifecycle"
            else:
                from omnigent.studio.reviews.discussion import InlinePublicationPreview
                from omnigent.studio.verify_publication import collect_publication

                try:
                    with args.preview.open("rb") as file:
                        raw_preview = file.read(128 * 1024 + 1)
                    if len(raw_preview) > 128 * 1024:
                        raise ValueError
                    preview = InlinePublicationPreview.model_validate_json(raw_preview)
                except (OSError, ValueError):
                    parser.error("Provide a valid retained publication preview JSON file")
                receipt = collect_publication(
                    mode=args.mode,
                    server_url=args.server,
                    preview=preview,
                    draft_id=args.draft,
                    author=args.author,
                )
                ready_key, label = "publication_ready", "GitHub publication"
        except ValueError as exc:
            parser.error(str(exc))
        serialized = json.dumps(receipt, indent=2) + "\n"
        if args.output:
            try:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(serialized, encoding="utf-8")
            except OSError:
                parser.exit(2, "Could not write the review receipt.\n")
        if args.json:
            print(serialized, end="")
        else:
            print(f"{label} ready" if receipt[ready_key] else f"{label} unconfirmed")
            for check in receipt["checks"]:
                print(f"  {check['status']:7} {check['name']}: {check['reason']}")
        return 0 if receipt[ready_key] else 1
    harnesses = [
        h
        for selected in (args.harness or ["both"])
        for h in (HARNESSES if selected == "both" else (selected,))
    ]
    try:
        receipt = collect_setup(
            mode=args.mode,
            server_url=args.server,
            host_id=args.host_id,
            workspace=args.workspace,
            harnesses=harnesses,
            required_skills=args.require_skill,
            required_mcps=args.require_mcp,
            timeout=args.timeout,
        )
    except ValueError as exc:
        parser.error(str(exc))
    serialized = json.dumps(receipt, indent=2) + "\n"
    if args.output:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized, encoding="utf-8")
        except OSError:
            parser.exit(2, "Could not write the setup receipt.\n")
    if args.json:
        print(serialized, end="")
    else:
        print("Preflight ready" if receipt["preflight_ready"] else "Preflight blocked")
        for check in receipt["checks"]:
            print(f"  {check['status']:7} {check['name']}: {check['reason']}")
        print(
            "Agent execution, reconnect, PR access and server source identity are not qualified."
        )
        if args.output:
            print(f"Receipt: {args.output}")
    return 0 if receipt["preflight_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
