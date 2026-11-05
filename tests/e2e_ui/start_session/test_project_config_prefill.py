"""E2E: the home composer prefills from a project's stored ``config``.

Visiting ``/?project=<name>`` seeds the new-session composer from that project's
stored defaults (``web/src/shell/projectPrefill.ts`` +
``web/src/shell/NewChatDialog.tsx``): host, working directory, and agent all
come from ``config``, silently falling back to the generic defaults for any
field the config leaves unset. This replaced the old newest-session inference —
stored config is now the single project-driven prefill source.

This drives the real chain: the composer resolves the project NAME → id via
``GET /v1/sessions/projects``, fetches ``GET /v1/projects/{id}`` for its config,
and seeds the host / workspace / agent, which then ride along to the create
``POST /v1/sessions``.

Heavy ``page.route`` stubbing mirrors ``test_start_session`` and is required for
the same reason: the e2e_ui harness's tunneled runner registers no *host* and
the host filesystem endpoint has nothing to browse, so ``/v1/hosts``,
``/v1/agents``, the project config, and the create ``POST`` are faked (the POST
handler *captures the body* — the thing under test — and returns a real seeded
session id so post-send navigation lands somewhere real). Default precedence
and worktree request variants are covered in
``web/src/shell/NewChatDialog.projectPrefill.test.tsx``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from playwright.async_api import Route, async_playwright, expect

from tests._helpers.async_thread import run_in_fresh_loop as _run_in_fresh_loop
from tests._helpers.picker_routes import OWN_AGENTS
from tests._helpers.polling import poll_until_async
from tests.e2e_ui.start_session.helpers import stub_empty_host_picker_data

_HOST_ID = "host_e2e_cfg"
_PROJECT_ID = "proj_e2e_cfg"
_PROJECT_NAME = "ConfiguredProject"
_CONFIG_WORKSPACE = "/work/configured-repo"
# Bare create endpoint (POST captured); NOT the /{id}/... sub-routes.
_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")
# One project config endpoint: /v1/projects/<id> (not the bare list).
_PROJECT_CFG_RE = re.compile(r"/v1/projects/[^/?]+")


def _hosts_body() -> str:
    return json.dumps(
        {"hosts": [{"host_id": _HOST_ID, "name": "e2e-host", "owner": "e2e", "status": "online"}]}
    )


def _agents_body() -> str:
    """Two agents: the default-ranked Claude Code and a second the config pins."""
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_claude_e2e",
                    "name": "claude-native-ui",
                    "display_name": "Claude Code",
                    "description": "Anthropic's coding agent",
                    "harness": None,
                    "skills": [],
                },
                {
                    "id": "ag_pinned_e2e",
                    "name": "polly",
                    "display_name": "Polly",
                    "description": "Multi-agent coding",
                    "harness": "claude-sdk",
                    "skills": [],
                },
            ]
        }
    )


def _projects_list_body() -> str:
    """``GET /v1/sessions/projects`` — the composer resolves the ?project= name
    to this id. Returns a bare ``ProjectSummary[]`` (``{id, name}``)."""
    return json.dumps([{"id": _PROJECT_ID, "name": _PROJECT_NAME}])


def _project_config_body() -> str:
    """``GET /v1/projects/{id}`` — the stored defaults the composer seeds from."""
    return json.dumps(
        {
            "id": _PROJECT_ID,
            "name": _PROJECT_NAME,
            "config": {
                "host_id": _HOST_ID,
                "workspace": _CONFIG_WORKSPACE,
                "agent_id": "ag_pinned_e2e",
            },
        }
    )


@pytest.mark.parametrize(
    ("mobile", "remembered"),
    [(False, False), (True, False), (False, True)],
    ids=["desktop", "mobile", "remembered-workspace"],
)
def test_composer_prefills_from_project_config(
    seeded_session: tuple[str, str], tmp_path: Path, mobile: bool, remembered: bool
) -> None:
    """A project visit can browse and launch with saved or remembered settings.

    The pinned agent (``ag_pinned_e2e``) and workspace (``/work/configured-repo``)
    come from ``config`` — NOT from the default-ranked Claude Code or a recent
    workspace — and ride along to ``POST /v1/sessions``.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_prefill(base_url, session_id, tmp_path, mobile, remembered))


async def _drive_prefill(
    base_url: str, session_id: str, tmp_path: Path, mobile: bool, remembered: bool
) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 540} if mobile else None)
        try:
            if remembered:
                last_start = {
                    _PROJECT_ID: {
                        "agentId": "ag_pinned_e2e",
                        "hostId": _HOST_ID,
                        "workspace": _CONFIG_WORKSPACE,
                        "configSignature": "[]",
                    }
                }
                await page.add_init_script(
                    "localStorage.setItem('omnigent:project-launch-defaults', "
                    f"JSON.stringify({json.dumps(last_start)}));"
                )
            create_bodies: list[dict[str, Any]] = []
            workspace_reads: list[str] = []

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_hosts_body()
                )

            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_agents_body()
                )

            async def handle_projects_list(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_projects_list_body()
                )

            async def handle_project_config(route: Route) -> None:
                project = json.loads(_project_config_body())
                if remembered:
                    project["config"] = {}
                await route.fulfill(json=project)

            async def handle_filesystem(route: Route) -> None:
                path = urlsplit(route.request.url).path
                workspace_reads.append(path)
                data = (
                    [
                        {
                            "name": "README.md",
                            "path": f"{_CONFIG_WORKSPACE}/README.md",
                            "type": "file",
                        }
                    ]
                    if path.endswith(_CONFIG_WORKSPACE)
                    else []
                )
                await route.fulfill(json={"object": "list", "data": data, "has_more": False})

            async def handle_events(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
                )

            async def handle_sessions(route: Route) -> None:
                if route.request.method == "POST":
                    create_bodies.append(route.request.post_data_json)
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"id": session_id}),
                    )
                else:
                    await route.continue_()

            # Neutralize the agent-discovery scan so only the stubbed catalog
            # feeds the picker (a leftover native agent would rank ahead).
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps({"data": []})
                )

            await page.route("**/v1/hosts", handle_hosts)
            await stub_empty_host_picker_data(page, _HOST_ID)
            await page.route(
                re.compile(rf"/v1/hosts/{_HOST_ID}/filesystem(?:/|\?|$)"), handle_filesystem
            )
            await page.route("**/v1/agents", handle_agents)
            await page.route("**/v1/sessions/projects", handle_projects_list)
            await page.route(_PROJECT_CFG_RE, handle_project_config)
            await page.route("**/v1/sessions/*/events", handle_events)
            await page.route(_SESSIONS_RE, handle_sessions)
            await page.route(
                re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"), handle_agent_scan
            )
            await page.route(OWN_AGENTS, lambda route: route.fulfill(json={"data": []}))

            await page.goto(f"{base_url}/?project={_PROJECT_NAME}")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # The agent picker reflects the project's saved or remembered agent.
            await expect(page.get_by_test_id("new-chat-landing-agent-select")).to_contain_text(
                "Polly", timeout=15_000
            )

            composer = page.get_by_test_id("new-chat-landing-input")
            await composer.fill("start here")
            await page.get_by_role("button", name="Open project workspace", exact=True).click(
                timeout=5_000
            )
            workspace_picker = page.get_by_test_id("workspace-picker")
            await expect(workspace_picker.get_by_text("README.md", exact=True)).to_be_visible()
            await expect(page.get_by_test_id("workspace-picker-select")).to_have_count(0)
            await expect(page.get_by_test_id("workspace-picker-new-folder")).to_have_count(0)
            assert f"/v1/hosts/{_HOST_ID}/filesystem{_CONFIG_WORKSPACE}" in workspace_reads
            assert create_bodies == []
            await page.screenshot(path=str(tmp_path / "project-workspace-before-send.png"))
            await page.get_by_test_id("workspace-picker-close").click()
            await expect(workspace_picker).to_be_hidden()

            await expect(composer).to_have_text("start here")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await poll_until_async(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["host_id"] == _HOST_ID, body
            assert body["project_id"] == _PROJECT_ID, body
            if remembered:
                assert body["agent_id"] == "ag_pinned_e2e", body
                assert body["workspace"] == _CONFIG_WORKSPACE, body
            else:
                # Untouched server defaults are filled by the server.
                assert "agent_id" not in body, body
                assert "workspace" not in body, body
        finally:
            await browser.close()


# The sandbox sentinel a project can store as its default host (mirrors
# SANDBOX_HOST_CHOICE in web/src/lib/hostPreferences.ts).
_SANDBOX_CHOICE = "__sandbox__"


def _managed_info_body() -> str:
    """``GET /v1/info`` for a managed deployment that offers a sandbox."""
    return json.dumps(
        {
            "accounts_enabled": False,
            "login_url": None,
            "needs_setup": False,
            "databricks_features": True,
            "managed_sandboxes_enabled": True,
            "sandbox_provider": "lakebox",
            "server_version": "0.0.0-e2e",
            "smart_routing_enabled": False,
        }
    )


def _sandbox_config_body() -> str:
    """``GET /v1/projects/{id}`` whose stored default host is the sandbox."""
    return json.dumps(
        {
            "id": _PROJECT_ID,
            "name": _PROJECT_NAME,
            "config": {"host_id": _SANDBOX_CHOICE},
        }
    )


def test_composer_prefills_sandbox_default_from_project_config(
    seeded_session: tuple[str, str],
) -> None:
    """A project whose stored default host is the sandbox selects the sandbox.

    Regression: the settings dialog offers (and persists) the sandbox sentinel as
    a default host, but prefill only seeded real online hosts, so the sandbox
    default was silently dropped and the composer fell back to a connected host.
    With the sandbox handled in prefill, a ``?project=`` visit selects the
    sandbox — the create posts ``host_type: "managed"`` (no ``host_id``).
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_sandbox_prefill(base_url, session_id))


async def _drive_sandbox_prefill(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []

            async def handle_info(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_managed_info_body()
                )

            async def handle_sandbox_models(route: Route) -> None:
                await route.fulfill(
                    json={
                        "configured": False,
                        "models": [],
                        "configuration_revision": None,
                        "provider_label": None,
                        "default_model": None,
                        "status": "unconfigured",
                    }
                )

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_hosts_body()
                )

            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_agents_body()
                )

            async def handle_projects_list(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_projects_list_body()
                )

            async def handle_project_config(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_sandbox_config_body()
                )

            async def handle_events(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
                )

            async def handle_sessions(route: Route) -> None:
                if route.request.method == "POST":
                    create_bodies.append(route.request.post_data_json)
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"id": session_id}),
                    )
                else:
                    await route.continue_()

            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps({"data": []})
                )

            await page.route("**/v1/info", handle_info)
            await page.route(
                "**/v1/sandbox-providers/*/harnesses/*/model-options*", handle_sandbox_models
            )
            await page.route("**/v1/hosts", handle_hosts)
            await page.route("**/v1/agents", handle_agents)
            await page.route("**/v1/sessions/projects", handle_projects_list)
            await page.route(_PROJECT_CFG_RE, handle_project_config)
            await page.route("**/v1/sessions/*/events", handle_events)
            await page.route(_SESSIONS_RE, handle_sessions)
            await page.route(
                re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"), handle_agent_scan
            )
            await page.route(OWN_AGENTS, lambda route: route.fulfill(json={"data": []}))

            await page.goto(f"{base_url}/?project={_PROJECT_NAME}")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # The host chip shows the sandbox — proof the stored sandbox default
            # was honored rather than dropped for a connected host.
            await expect(page.get_by_test_id("new-chat-landing-host-chip")).to_have_attribute(
                "aria-label", re.compile(re.escape("Sandbox")), timeout=15_000
            )

            await page.get_by_test_id("new-chat-landing-input").fill("start here")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await poll_until_async(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            # A sandbox create is managed (server provisions the host); no host_id.
            assert body.get("host_type") == "managed", body
            assert "host_id" not in body or body["host_id"] is None, body
        finally:
            await browser.close()


def test_composer_files_new_session_into_project(seeded_session: tuple[str, str]) -> None:
    """A ``?project=`` visit files the new session at create time (born filed).

    The sidebar's per-project "new session" pencil lands here with the project
    pre-scoped. On Send the session must be *born filed*: the create
    ``POST /v1/sessions`` carries the legacy ``omni_project`` label so the
    sidebar groups the new row under its project from its first appearance —
    instead of briefly showing it under the ungrouped "Sessions" section until
    the follow-up ``project_id`` move (``moveConversationToProject``) catches up
    in the search-indexed session list. The sidebar, the ``?project=`` folder
    query, and the project list all dual-read membership from this label OR the
    first-class ``project_id`` the move then sets, so there is no ungrouped
    window either way.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_born_filed(base_url, session_id))


async def _drive_born_filed(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_hosts_body()
                )

            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_agents_body()
                )

            async def handle_projects_list(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_projects_list_body()
                )

            async def handle_project_config(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_project_config_body()
                )

            async def handle_events(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
                )

            async def handle_sessions(route: Route) -> None:
                if route.request.method == "POST":
                    create_bodies.append(route.request.post_data_json)
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"id": session_id}),
                    )
                else:
                    await route.continue_()

            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps({"data": []})
                )

            await page.route("**/v1/hosts", handle_hosts)
            await stub_empty_host_picker_data(page, _HOST_ID)
            await page.route("**/v1/agents", handle_agents)
            await page.route("**/v1/sessions/projects", handle_projects_list)
            await page.route(_PROJECT_CFG_RE, handle_project_config)
            await page.route("**/v1/sessions/*/events", handle_events)
            await page.route(_SESSIONS_RE, handle_sessions)
            await page.route(
                re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"), handle_agent_scan
            )
            await page.route(OWN_AGENTS, lambda route: route.fulfill(json={"data": []}))

            # The per-project pencil destination: the composer lands pre-scoped
            # to this project (no interaction needed to file into it).
            await page.goto(f"{base_url}/?project={_PROJECT_NAME}")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            await page.get_by_test_id("new-chat-landing-input").fill("write the docs")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await poll_until_async(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["host_id"] == _HOST_ID, body
            # Born filed, first-class: the create names the project so the
            # server files it at insert. No legacy omni_project label and no
            # follow-up move — the row is a member from its first appearance,
            # with no window where it exists unfiled.
            assert body["project_id"] == _PROJECT_ID, body
            assert (body.get("labels") or {}).get("omni_project") is None, body
        finally:
            await browser.close()
