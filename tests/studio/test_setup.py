"""Setup contracts, with one owned HTTP/process journey and no provider calls."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from omnigent.studio import setup

_BASE = "https://studio.example.test/instance"
_HOST = "owned-host"
_WORKSPACE = "/workspace/project"
_SECRET = "private-test-value-that-must-not-be-exported"


@pytest.fixture
def responses(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(setup, "_headers", lambda *_: {"Authorization": f"Bearer {_SECRET}"})
    return {
        "health": {"status": "ok"},
        ".well-known/omnigent.json": {
            "manifest_version": 1,
            "server_version": "0.22.0.dev0",
            "auth": {"mode": "accounts"},
        },
        "v1/info": {
            "server_version": "0.22.0.dev0",
            "accounts_enabled": True,
            "single_user": False,
            "needs_setup": False,
        },
        f"v1/hosts/{_HOST}": {
            "host_id": _HOST,
            "status": "online",
            "configured_harnesses": dict.fromkeys(setup.HARNESSES, True),
        },
        f"v1/hosts/{_HOST}/mcp-servers": {
            "mcp_servers": [
                {"name": "linear", "harness": "codex", "transport": "http"},
                {"name": "linear", "harness": "claude", "transport": "http"},
            ],
        },
        **{
            f"v1/hosts/{_HOST}/harnesses/{harness}/startup": {
                "command_source": "config",
                "command": _SECRET,
                "args": [_SECRET],
                "environment": {"variables": {"TOKEN": _SECRET}},
            }
            for harness in setup.HARNESSES
        },
        **{
            f"v1/skills:{harness}": {
                "skills": [{"name": "review", "description": _SECRET}],
                "warnings": [],
            }
            for harness in setup.HARNESSES
        },
    }


def _collect(
    responses: dict[str, Any],
    requests: list[httpx.Request] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    def respond(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        assert request.url.host == "studio.example.test"
        assert request.url.path.startswith("/instance/")
        route = request.url.path.removeprefix("/instance/")
        if route == "v1/skills":
            route += f":{request.url.params['harness']}"
        response = responses[route]
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response)

    kwargs = {
        "mode": "server",
        "server_url": _BASE,
        "host_id": _HOST,
        "workspace": _WORKSPACE,
        "transport": httpx.MockTransport(respond),
        **overrides,
    }
    return setup.collect_setup(**kwargs)


def test_preflight_reads_exact_target_without_claiming_execution(
    responses: dict[str, Any],
) -> None:
    requests: list[httpx.Request] = []
    receipt = _collect(responses, requests, required_skills=["review"], required_mcps=["linear"])
    assert receipt["preflight_ready"] is True
    assert all(request.method == "GET" for request in requests)
    assert all(request.headers["authorization"] == f"Bearer {_SECRET}" for request in requests)
    for request in requests:
        if request.url.path.endswith("/v1/skills"):
            assert request.url.params["host_id"] == _HOST
            assert request.url.params["path"] == _WORKSPACE
    assert set(receipt["harnesses"]) == set(setup.HARNESSES)
    assert receipt["harnesses"]["codex-native"]["startup_source"] == "config"
    assert receipt["harnesses"]["claude-native"]["skills"] == ["review"]
    assert receipt["harnesses"]["codex-native"]["mcp_connectivity"] == "not_run"
    assert receipt["harnesses"]["codex-native"]["mcp_inventory_scope"] == "user"
    assert set(receipt["qualification"].values()) == {"not_run"}
    assert receipt["server"] == {
        "version": "0.22.0.dev0",
        "source_commit": None,
        "auth_mode": "accounts",
    }
    assert _SECRET not in json.dumps(receipt)


@pytest.mark.parametrize(
    "readiness",
    [
        None,
        {},
        {"claude-native": True},
        {"codex-native": False},
        {"codex-native": "needs-auth"},
        {"codex-native": "unexpected-value"},
    ],
)
def test_unknown_or_unavailable_selected_harness_blocks(
    responses: dict[str, Any], readiness: Any
) -> None:
    responses[f"v1/hosts/{_HOST}"]["configured_harnesses"] = readiness
    receipt = _collect(responses, harnesses=["codex-native"])
    assert receipt["preflight_ready"] is False
    assert receipt["harnesses"]["codex-native"]["reported_ready"] is not True


def test_unselected_harness_does_not_change_selected_readiness(responses: dict[str, Any]) -> None:
    responses[f"v1/hosts/{_HOST}"]["configured_harnesses"]["claude-native"] = "new-unknown-state"
    receipt = _collect(responses, harnesses=["codex-native"])
    assert receipt["preflight_ready"] is True
    assert set(receipt["harnesses"]) == {"codex-native"}


@pytest.mark.parametrize("status", [401, 403, 404])
def test_host_access_failure_does_not_probe_private_inventory(
    responses: dict[str, Any], status: int
) -> None:
    responses[f"v1/hosts/{_HOST}"] = httpx.Response(status, json={"detail": _SECRET})
    requests: list[httpx.Request] = []
    receipt = _collect(responses, requests)
    assert receipt["preflight_ready"] is False
    host_paths = [r.url.path for r in requests if "/hosts/" in r.url.path]
    assert host_paths == [f"/instance/v1/hosts/{_HOST}"]
    assert _SECRET not in json.dumps(receipt)


def test_offline_host_blocks_without_waiting_for_inventory(responses: dict[str, Any]) -> None:
    responses[f"v1/hosts/{_HOST}"]["status"] = "offline"
    requests: list[httpx.Request] = []
    receipt = _collect(responses, requests)
    assert receipt["preflight_ready"] is False
    assert not any(r.url.path.endswith("/mcp-servers") for r in requests)
    assert any(c["name"] == "host.online" and c["status"] == "fail" for c in receipt["checks"])


def test_incomplete_skills_preserve_warning_count_without_source_contents(
    responses: dict[str, Any],
) -> None:
    responses["v1/skills:codex-native"]["warnings"] = [_SECRET]
    receipt = _collect(responses)
    assert receipt["preflight_ready"] is False
    assert receipt["harnesses"]["codex-native"]["skill_warning_count"] == 1
    assert _SECRET not in json.dumps(receipt)


def test_required_skills_and_mcps_are_checked_per_harness(responses: dict[str, Any]) -> None:
    responses["v1/skills:codex-native"]["skills"] = []
    responses[f"v1/hosts/{_HOST}/mcp-servers"]["mcp_servers"] = [
        {"name": "linear", "harness": "claude"}
    ]
    receipt = _collect(responses, required_skills=["review"], required_mcps=["linear"])
    assert receipt["preflight_ready"] is False
    assert receipt["harnesses"]["codex-native"]["missing_required_skills"] == ["review"]
    assert receipt["harnesses"]["codex-native"]["missing_required_mcps"] == ["linear"]
    assert receipt["harnesses"]["claude-native"]["missing_required_skills"] == []
    assert receipt["harnesses"]["claude-native"]["missing_required_mcps"] == []


def test_unsupported_mcp_inventory_is_unknown_not_empty(responses: dict[str, Any]) -> None:
    responses[f"v1/hosts/{_HOST}/mcp-servers"] = httpx.Response(501, json={"detail": _SECRET})
    receipt = _collect(responses)
    assert receipt["preflight_ready"] is False
    assert receipt["harnesses"]["codex-native"]["mcp_servers"] is None
    assert _SECRET not in json.dumps(receipt)


@pytest.mark.parametrize(
    ("route", "invalid"),
    [
        ("health", []),
        (".well-known/omnigent.json", {"manifest_version": 0, "auth": {"mode": _SECRET}}),
        ("v1/info", {"server_version": _SECRET, "single_user": "false"}),
        (f"v1/hosts/{_HOST}", {"host_id": "another-host", "status": "online"}),
        ("v1/skills:codex-native", {"skills": [{}], "warnings": []}),
        ("v1/skills:codex-native", {"skills": [], "warnings": None}),
        (f"v1/hosts/{_HOST}/mcp-servers", {"mcp_servers": [{}]}),
        (f"v1/hosts/{_HOST}/harnesses/codex-native/startup", {"command_source": _SECRET}),
    ],
)
def test_invalid_required_contracts_fail_closed(
    responses: dict[str, Any], route: str, invalid: Any
) -> None:
    responses[route] = invalid
    receipt = _collect(responses)
    assert receipt["preflight_ready"] is False
    assert _SECRET not in json.dumps(receipt)


@pytest.mark.parametrize("auth_mode", ["accounts", "oidc", "header", "custom"])
def test_protected_server_modes_are_accepted(responses: dict[str, Any], auth_mode: str) -> None:
    responses[".well-known/omnigent.json"]["auth"]["mode"] = auth_mode
    responses["v1/info"]["accounts_enabled"] = auth_mode == "accounts"
    receipt = _collect(responses)
    assert receipt["preflight_ready"] is True
    assert receipt["server"]["auth_mode"] == auth_mode


def test_auth_discovery_disagreement_blocks(responses: dict[str, Any]) -> None:
    responses["v1/info"]["accounts_enabled"] = False
    assert _collect(responses)["preflight_ready"] is False


def test_invalid_version_metadata_is_not_exported(responses: dict[str, Any]) -> None:
    responses["v1/info"]["server_version"] = _SECRET
    receipt = _collect(responses)
    assert receipt["preflight_ready"] is False
    assert receipt["server"]["version"] is None
    assert _SECRET not in json.dumps(receipt)


@pytest.mark.parametrize("single_user", [True, False])
def test_server_mode_rejects_single_user_or_unprotected_auth(
    responses: dict[str, Any], single_user: bool
) -> None:
    responses["v1/info"]["single_user"] = single_user
    responses[".well-known/omnigent.json"]["auth"]["mode"] = "none"
    assert _collect(responses)["preflight_ready"] is False


def test_initial_account_setup_is_blocked(responses: dict[str, Any]) -> None:
    responses["v1/info"]["needs_setup"] = True
    assert _collect(responses)["preflight_ready"] is False


def test_server_commit_is_observed_separately_from_local_checkout(
    responses: dict[str, Any],
) -> None:
    responses["v1/info"]["source_commit"] = "a" * 40
    receipt = _collect(responses)
    assert receipt["server"]["source_commit"] == "a" * 40
    assert "commit" in receipt["verifier"]["checkout"]
    assert receipt["qualification"]["server_source_identity"] == "not_run"


def test_redirect_does_not_send_credentials_to_another_server(responses: dict[str, Any]) -> None:
    responses["health"] = httpx.Response(
        302, headers={"location": f"https://other.example/{_SECRET}"}
    )
    requests: list[httpx.Request] = []
    receipt = _collect(responses, requests)
    assert receipt["preflight_ready"] is False
    assert {request.url.host for request in requests} == {"studio.example.test"}
    assert _SECRET not in json.dumps(receipt)


def test_transport_exception_contents_are_not_exported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup, "_headers", lambda *_: {})

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(_SECRET, request=request)

    receipt = setup.collect_setup(
        mode="server",
        server_url=_BASE,
        host_id=_HOST,
        workspace=_WORKSPACE,
        transport=httpx.MockTransport(fail),
    )
    assert receipt["preflight_ready"] is False
    assert _SECRET not in json.dumps(receipt)
    assert all(c["status"] == "unknown" for c in receipt["checks"])


def test_credential_backend_failure_is_secret_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_: Any) -> dict[str, str]:
        raise RuntimeError(_SECRET)

    monkeypatch.setattr(setup, "_headers", fail)
    receipt = setup.collect_setup(
        mode="server", server_url=_BASE, host_id=_HOST, workspace=_WORKSPACE
    )
    assert receipt["preflight_ready"] is False
    assert receipt["checks"] == [
        {"name": "credentials", "status": "unknown", "reason": "credential_resolution_failed"}
    ]
    assert _SECRET not in json.dumps(receipt)


@pytest.mark.parametrize(
    "url",
    [
        "studio.example.test",
        "file:///tmp/local",
        "https://user:password@studio.example.test",
        "https://studio.example.test?token=private",
        "https://studio.example.test#private",
        "https://[::1",
        "https://studio.example.test:99999",
        " https://studio.example.test",
        "https://studio.example.test\n",
        "http://studio.example.test",
    ],
)
def test_invalid_credential_destinations_are_rejected_without_echoing(url: str) -> None:
    with pytest.raises(ValueError) as caught:
        setup.validate_server_url(url, "server")
    assert url not in str(caught.value)


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:6767", "http://localhost:6767/prefix/", "http://[::1]:6767"]
)
def test_loopback_urls_allow_local_http(url: str) -> None:
    assert setup.validate_server_url(url, "local") == url.rstrip("/")


def test_local_mode_cannot_claim_a_remote_target() -> None:
    with pytest.raises(ValueError, match="local mode requires loopback"):
        setup.validate_server_url(_BASE, "local")


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), 31])
def test_request_deadlines_are_bounded(timeout: float) -> None:
    with pytest.raises(ValueError, match="Timeout"):
        setup.collect_setup(
            mode="server", server_url=_BASE, host_id=_HOST, workspace=_WORKSPACE, timeout=timeout
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"workspace": "relative/project"},
        {"workspace": "/project\n"},
        {"host_id": "../../another-host"},
        {"harnesses": []},
        {"harnesses": ["codex"]},
    ],
)
def test_invalid_execution_scope_is_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        setup.collect_setup(
            **{
                "mode": "server",
                "server_url": _BASE,
                "host_id": _HOST,
                "workspace": _WORKSPACE,
                **overrides,
            }
        )


def test_cli_writes_receipt_and_returns_nonzero_for_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    expected = {
        "preflight_ready": False,
        "checks": [{"name": "host", "status": "unknown", "reason": "offline"}],
    }
    captured: list[dict[str, Any]] = []

    def collect(**kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return copy.deepcopy(expected)

    monkeypatch.setattr(setup, "collect_setup", collect)
    output = tmp_path / "nested" / "receipts" / "receipt.json"
    code = setup.main(
        [
            "setup",
            "--mode",
            "server",
            "--server",
            _BASE,
            "--host-id",
            _HOST,
            "--workspace",
            _WORKSPACE,
            "--harness",
            "both",
            "--output",
            str(output),
            "--require-skill",
            "review",
            "--require-mcp",
            "linear",
            "--json",
        ]
    )
    assert code == 1
    assert json.loads(output.read_text()) == expected
    assert json.loads(capsys.readouterr().out) == expected
    assert tuple(captured[0]["harnesses"]) == setup.HARNESSES
    assert captured[0]["required_skills"] == ["review"]
    assert captured[0]["required_mcps"] == ["linear"]


def test_cli_output_failure_is_nonzero_and_secret_safe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        setup, "collect_setup", lambda **_: {"preflight_ready": True, "checks": []}
    )
    parent_file = tmp_path / _SECRET
    parent_file.write_text("not a directory")
    with pytest.raises(SystemExit) as caught:
        setup.main(
            [
                "setup",
                "--mode",
                "server",
                "--server",
                _BASE,
                "--host-id",
                _HOST,
                "--workspace",
                _WORKSPACE,
                "--output",
                str(parent_file / "receipt.json"),
            ]
        )
    assert caught.value.code == 2
    error = capsys.readouterr().err
    assert "Could not write the setup receipt" in error
    assert _SECRET not in error


def test_cli_human_summary_distinguishes_preflight_from_qualification(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        setup, "collect_setup", lambda **_: {"preflight_ready": True, "checks": []}
    )
    assert (
        setup.main(
            [
                "setup",
                "--mode",
                "server",
                "--server",
                _BASE,
                "--host-id",
                _HOST,
                "--workspace",
                _WORKSPACE,
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    assert "Preflight ready" in text
    assert "not qualified" in text


def test_script_process_writes_receipt_over_real_http(
    responses: dict[str, Any], tmp_path: Path
) -> None:
    """Cross the script/import/CLI/HTTP/file boundary once against an owned stub.

    The input matrix stays in MockTransport tests. This proves command wiring
    and real transport only; the scripted host is not native host qualification.
    """
    received: list[tuple[str, str, dict[str, list[str]], str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def answer(self) -> None:
            parsed = urlsplit(self.path)
            params = parse_qs(parsed.query)
            received.append((self.command, parsed.path, params, self.headers.get("Authorization")))
            route = parsed.path.removeprefix("/instance/")
            if route == "v1/skills":
                route += f":{params.get('harness', [''])[0]}"
            accepted = self.command == "GET" and route in responses
            payload = json.dumps(responses[route] if accepted else {"detail": _SECRET}).encode()
            self.send_response(200 if accepted else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = answer

        def log_message(self, *_: object) -> None:
            pass

    output = tmp_path / "receipts" / "setup.json"
    child_env = {
        **os.environ,
        "OMNIGENT_CONFIG_HOME": str(tmp_path / "config"),
        "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
        "OMNIGENT_REMOTE_AUTH_TOKEN": _SECRET,
        "HTTP_PROXY": "http://127.0.0.1:1",
        "HTTPS_PROXY": "http://127.0.0.1:1",
        "NO_PROXY": "",
    }
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}/instance"
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(setup._CHECKOUT / "scripts" / "verify_studio.py"),
                    "setup",
                    "--mode",
                    "server",
                    "--server",
                    base,
                    "--host-id",
                    _HOST,
                    "--workspace",
                    _WORKSPACE,
                    "--harness",
                    "both",
                    "--require-skill",
                    "review",
                    "--require-mcp",
                    "linear",
                    "--output",
                    str(output),
                    "--json",
                ],
                cwd=tmp_path,
                env=child_env,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
    assert not thread.is_alive()
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(output.read_text())
    assert json.loads(result.stdout) == receipt
    assert receipt["preflight_ready"] is True
    assert receipt["target"] == {"server_url": base, "host_id": _HOST, "workspace": _WORKSPACE}
    assert set(receipt["harnesses"]) == set(setup.HARNESSES)
    assert set(receipt["qualification"].values()) == {"not_run"}
    assert _SECRET not in result.stdout + result.stderr + output.read_text()
    expected = Counter(
        [
            ("GET", "/instance/health"),
            ("GET", "/instance/.well-known/omnigent.json"),
            ("GET", "/instance/v1/info"),
            ("GET", f"/instance/v1/hosts/{_HOST}"),
            ("GET", f"/instance/v1/hosts/{_HOST}/mcp-servers"),
            *[
                ("GET", f"/instance/v1/hosts/{_HOST}/harnesses/{harness}/startup")
                for harness in setup.HARNESSES
            ],
            ("GET", "/instance/v1/skills"),
            ("GET", "/instance/v1/skills"),
        ]
    )
    assert Counter((method, path) for method, path, _, _ in received) == expected
    assert {auth for _, _, _, auth in received} == {f"Bearer {_SECRET}"}
    skill_queries = [params for _, path, params, _ in received if path == "/instance/v1/skills"]
    assert {query["harness"][0] for query in skill_queries} == set(setup.HARNESSES)
    assert all(
        query["host_id"] == [_HOST] and query["path"] == [_WORKSPACE] for query in skill_queries
    )
