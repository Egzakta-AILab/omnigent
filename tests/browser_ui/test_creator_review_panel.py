"""Exercise the Creator panel through the built workspace and real message port."""

from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract, chat_session_handle
from tests.browser_ui.conftest import BrowserContract

_PANEL_SCRIPT = """
window.addEventListener("message", (event) => {
  if (event.source !== parent || event.data.type !== "init") return;
  const identity = globalThis.__OMNIGENT_EXTENSION__;
  const port = event.ports[0];
  const send = (type, payload = {}) =>
    port.postMessage({ ...identity, source: "omnigent-extension", type, ...payload });
  const request = (requestId, method, params) => send("request", { requestId, method, params });
  const status = document.createElement("p");
  const root = document.querySelector("#root");
  root.append(status);
  port.onmessage = ({ data }) => {
    if (data.requestId === "findings") {
      const finding = data.result.findings[0];
      status.textContent = finding.path;
      request("receipts", "review.read", { findingKey: finding.key });
    }
    if (data.requestId === "receipts") {
      const target = {
        findingKey: data.result.findingKey,
        receiptDigest: data.result.proofs[0].digest,
      };
      for (const [label, action] of [
        ["Show failed check", "review.openProof"], ["Use this receipt", "review.attachProof"],
      ]) {
        const button = document.createElement("button");
        button.textContent = label;
        button.onclick = () => request(label, action, target);
        root.append(button);
      }
      request("denied", "sessions.listPage", {});
    }
    if (data.requestId === "denied") {
      status.textContent += " · " + data.error.code;
    }
  };
  send("ready");
  request("findings", "review.read", {});
}, { once: true });
"""


@pytest.fixture
def review_shell(page: Page, browser_contract: BrowserContract) -> Iterator[ChatSessionContract]:
    yield from chat_session_handle(page, browser_contract)


def test_panel_opens_the_native_receipt_and_prefills_without_submitting(
    page: Page, review_shell: ChatSessionContract
) -> None:
    finding = "a" * 64
    receipt = "b" * 64
    bundle = "d" * 64
    base = "1" * 40
    head = "2" * 40
    review_path = "/v1/studio/review-runs/creator-fixture"
    asset = f"/v1/extensions/example.qa/assets/{bundle}/script"
    contract = review_shell.contract
    contract.json(
        "/v1/extensions",
        {
            "object": "list",
            "data": [
                {
                    "id": "example.qa",
                    "status": "enabled",
                    "pages": [],
                    "primary_navigation": [],
                    "permissions": ["sessions.read"],
                    "browser": {"digest": bundle, "script_url": asset},
                    "review_panel": {
                        "slot": "review.panel",
                        "contract_version": "1",
                        "view": "qa",
                        "title": "QA checks",
                        "capabilities": ["review.read", "review.openProof", "review.attachProof"],
                    },
                }
            ],
        },
    )
    contract.response(asset, status=200, body=_PANEL_SCRIPT)
    contract.json(
        f"{review_path}/discussion",
        {
            "id": "creator-fixture",
            "project_name": "Creator test",
            "repository": "example/workspace",
            "pr_url": "https://example.test/pr/1",
            "pr_number": 1,
            "base_sha": base,
            "head_sha": head,
            "patch": "",
            "reports": [],
            "findings": [
                {
                    "key": finding,
                    "finding": {
                        "title": "Check failed",
                        "path": "src/check.py",
                        "side": "head",
                        "line": 2,
                        "severity": "high",
                        "detail": "Captured check failure.",
                    },
                }
            ],
            "comments": [],
            "can_ask": True,
            "can_comment": True,
            "collaborators": None,
        },
    )
    side = {
        "commit": base,
        "state": "exited",
        "exit_code": 0,
        "elapsed_ms": 42,
        "oom_killed": False,
        "output": "",
        "output_truncated": False,
    }
    contract.json(
        f"{review_path}/findings/{finding}/reproductions",
        [
            {
                "digest": receipt,
                "attached_at": 1,
                "provenance": "uploaded",
                "receipt": {
                    "base": side,
                    "head": {**side, "commit": head, "exit_code": 1},
                    "image_id": "sandbox:fixture",
                    "observed_at": "2026-11-05T13:00:00Z",
                },
            }
        ],
    )
    page.goto(f"{review_shell.base_url}/reviews/creator-fixture/discussion")
    page.get_by_text("QA checks", exact=True).click()
    panel = page.frame_locator('iframe[title="QA checks"]')
    expect(panel.get_by_text("src/check.py · MethodNotFound")).to_be_visible()
    panel.get_by_role("button", name="Show failed check").click()
    article = page.get_by_role("article").filter(has_text="sandbox:fixture")
    expect(article).to_be_focused()
    panel.get_by_role("button", name="Use this receipt").click()
    expect(page.get_by_label("Include check receipt")).to_have_value(receipt)
    # The contract's default-deny route also catches any attempt to send the question.
