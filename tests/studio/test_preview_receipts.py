import hashlib
import io
import json
import time
import zipfile

import httpx
import pytest

from omnigent.studio.preview_receipts import verified_preview


@pytest.mark.parametrize(
    "change,eligible",
    [
        ({}, True),
        ({"checked_out_sha": "b" * 40}, False),
        ({"build_id": "old"}, False),
        ({"url": "https://vpn.invalid:20000"}, False),
        ({"branch": "fork"}, False),
    ],
)
def test_readback_requires_trusted_run_and_exact_build(monkeypatch, change, eligible):
    client_type = httpx.Client
    repository = "acme/repo"
    head = "a" * 40
    identity = {"build_id": "current", "workspace_id": "workspace", "template_version": "version"}
    receipt = {
        **identity,
        "repository": repository,
        "requested_sha": head,
        "checked_out_sha": head,
        "pr_number": 7,
        "url": "https://vpn.invalid:32101",
        **{k: v for k, v in change.items() if k != "branch"},
    }
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as file:
        file.writestr("receipt.json", json.dumps(receipt))

    def github(req):
        suffix = req.url.path.removeprefix("/repos/acme/repo")
        if not suffix:
            return httpx.Response(200, json={"default_branch": "main"})
        if suffix == "/actions/artifacts":
            return httpx.Response(
                200,
                json={
                    "artifacts": [
                        {
                            "id": 9,
                            "name": "studio-preview-7-" + head,
                            "expired": False,
                            "digest": "sha256:" + hashlib.sha256(blob.getvalue()).hexdigest(),
                            "workflow_run": {"id": 4},
                        }
                    ]
                },
            )
        if suffix == "/actions/runs/4":
            return httpx.Response(
                200,
                json={
                    "event": "workflow_dispatch",
                    "conclusion": "success",
                    "head_branch": change.get("branch", "main"),
                    "path": ".github/workflows/coder-pr-workspace.yml",
                    "head_repository": {"full_name": repository},
                },
            )
        assert suffix == "/actions/artifacts/9/zip"
        return httpx.Response(
            302, headers={"Location": "https://evidence.blob.core.windows.net/download"}
        )

    def download(req):
        assert "Authorization" not in req.headers
        return httpx.Response(200, content=blob.getvalue())

    monkeypatch.setattr(
        httpx, "Client", lambda **kw: client_type(transport=httpx.MockTransport(download), **kw)
    )
    with client_type(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(github),
        headers={"Authorization": "Bearer test"},
    ) as client:
        actual = verified_preview(
            client, repository, 7, head, identity, "vpn.invalid", time.monotonic() + 5
        )
    assert (actual == receipt) if eligible else (actual is None)
