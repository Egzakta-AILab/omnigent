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
    reproduction = commands.add_parser("reproduce", help="compare two Git revisions in Docker")
    reproduction.add_argument("--repository", type=Path, required=True)
    reproduction.add_argument("--base", help="full base commit ID")
    reproduction.add_argument("--head", help="full head commit ID")
    reproduction.add_argument("--run-id", help="read both commits from a saved review run")
    reproduction.add_argument("--mode", choices=("local", "server"))
    reproduction.add_argument("--server", help="trusted instance URL for --run-id")
    reproduction.add_argument("--pr", help="expected pull request URL for --run-id")
    reproduction.add_argument("--image", required=True, help="locally installed test image")
    reproduction.add_argument("--socket", type=Path, default=Path("/var/run/docker.sock"))
    reproduction.add_argument("--timeout", type=int, default=60, help="seconds per revision")
    reproduction.add_argument("--output", type=Path, required=True, help="new private JSON file")
    reproduction.add_argument("argv", nargs=argparse.REMAINDER, help="command after --")
    args = parser.parse_args(argv)
    if args.command == "reproduce":
        from omnigent.studio.reviews.reproduction import ReproductionError, reproduce
        from omnigent.studio.verify_review_run import read_reproduction_source

        command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        if args.output.exists():
            parser.error("Choose a new file for the reproduction receipt")
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
            result = reproduce(
                repository=args.repository.expanduser().resolve(),
                base=base,
                head=head,
                image=args.image,
                command=tuple(command),
                socket=args.socket.expanduser(),
                timeout=args.timeout,
                source=source,
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
        for label, side in (("Base", result.base), ("Head", result.head)):
            print(
                f"{label} {side.commit[:8]}: {side.state}, "
                f"exit {side.exit_code}, {side.elapsed_ms} ms"
            )
        return 0 if result.base.state == result.head.state == "exited" else 1
    if args.command == "contracts":
        from omnigent.studio.contracts import main as contracts_main

        return contracts_main(output=args.output, json_output=args.json)
    if args.command in ("review", "review-run"):
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
            else:
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
