"""Creator panel compilation validates data and identifies immutable definitions."""

import json

import httpx
import pytest
from pydantic import ValidationError

from omnigent.studio.creator_panel import (
    PANEL_BYTES,
    QA_PANEL,
    compile_creator_panel,
    creator_panel_contract,
)
from omnigent.studio.verify_creator import verify_creator_preview

_REVIEW = {"run_id": "saved-run", "base_sha": "a" * 40, "head_sha": "b" * 40}


def test_compile_digests_canonical_definition_and_binds_revision() -> None:
    template = creator_panel_contract().template
    definition = template.model_dump(mode="json")
    raw = json.dumps(definition, sort_keys=True, indent=2).encode()
    result = compile_creator_panel(raw, **_REVIEW)
    assert result.panel == template
    assert (result.run_id, result.base_sha, result.head_sha) == tuple(_REVIEW.values())
    assert (
        result.digest
        == compile_creator_panel(template.model_dump_json().encode(), **_REVIEW).digest
    )
    assert (
        result.digest
        != compile_creator_panel(
            json.dumps(definition | {"group_by": "directory"}).encode(), **_REVIEW
        ).digest
    )


@pytest.mark.parametrize(
    "forged",
    [
        {"proof_action": "publish"},
        {"source": "javascript:alert(1)"},
        {"run_id": "foreign"},
        {"contract_version": 1},
        {"group_by": ["file"]},
    ],
)
def test_compile_rejects_executable_fields_and_unsupported_values(
    forged: dict[str, object],
) -> None:
    raw = json.dumps(QA_PANEL.model_dump(mode="json") | forged).encode()
    with pytest.raises(ValidationError):
        compile_creator_panel(raw, **_REVIEW)


def test_compile_bounds_source_before_decoding() -> None:
    with pytest.raises(ValueError, match="8 KiB"):
        compile_creator_panel(b" " * (PANEL_BYTES + 1), **_REVIEW)


def test_verifier_rejects_a_different_preview_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = "1" * 32
    monkeypatch.setattr("omnigent.studio.setup._headers", lambda *_: {})

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/studio/review-runs/{run_id}/creator-preview"
        if request.method == "GET":
            return httpx.Response(200, json=creator_panel_contract().model_dump(mode="json"))
        preview = compile_creator_panel(request.content, **_REVIEW)
        return httpx.Response(200, json=preview.model_dump(mode="json"))

    with pytest.raises(ValueError, match="different review or definition"):
        verify_creator_preview(
            mode="local",
            server_url="http://127.0.0.1:6767",
            run_id=run_id,
            definition=None,
            transport=httpx.MockTransport(respond),
        )
