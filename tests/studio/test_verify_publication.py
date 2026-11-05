"""Only the expected confirmed inline GitHub comment satisfies read-back."""

import httpx
import pytest

from omnigent.studio import setup, verify_publication
from omnigent.studio.reviews.discussion import InlinePublicationPreview


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "draft",
        "unknown",
        "version",
        "commit",
        "line",
        "range",
        "side",
        "author",
        "other_pr",
    ],
)
def test_publication_verifier_checks_the_original_anchor(monkeypatch, fault):
    run_id, draft_id = "1" * 32, "2" * 32
    pr_url = "https://github.com/example/repo/pull/17"
    comment_url = f"{pr_url}#discussion_r42"
    anchor = {"kind": "diff", "path": "file", "side": "head", "start": 3, "end": 4}
    draft = {
        "id": draft_id,
        "anchor": anchor,
        "thread": anchor,
        "body": "A private draft.",
        "created_at": 1,
        "updated_at": 1,
        "version": 1,
    }
    preview = InlinePublicationPreview.model_validate(
        {
            "run_id": run_id,
            "provider": "github",
            "pr_url": pr_url,
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "patch_sha256": "3" * 64,
            "digest": "4" * 64,
            "drafts": [draft],
        }
    )
    saved = {
        **draft,
        "version": 2 if fault == "version" else 1,
        "publication_state": fault if fault in {"draft", "unknown"} else "posted",
        "provider_url": None if fault in {"draft", "unknown"} else comment_url,
    }
    target = {
        "source_project_id": "5" * 32,
        "repository_path": ".",
        "host_id": "6" * 32,
        "workspace": "/repo",
        "pr_url": pr_url,
        "repository": "example/repo",
        "pr_number": 17,
        "base_sha": preview.base_sha,
        "head_sha": preview.head_sha,
    }
    endpoint = f"/v1/studio/review-runs/{run_id}"
    responses = {
        endpoint: {
            "id": run_id,
            "target": target,
            "reviewers": [],
            "created_at": 1,
            "patch_sha256": preview.patch_sha256,
        },
        f"{endpoint}/discussion": {"publication_drafts": [saved]},
    }
    readbacks = []

    def readback(pr, comment_id):
        readbacks.append((pr.url, comment_id))
        return {
            "id": 42,
            "html_url": comment_url,
            "pull_request_url": "https://api.github.com/repos/example/repo/pulls/"
            + ("99" if fault == "other_pr" else "17"),
            "subject_type": "line",
            "path": "file",
            "side": "LEFT" if fault == "side" else "RIGHT",
            "start_side": "RIGHT",
            "original_commit_id": "c" * 40 if fault == "commit" else preview.head_sha,
            "original_start_line": 2 if fault == "range" else 3,
            "original_line": 5 if fault == "line" else 4,
            "line": 500,
            "body": draft["body"],
            "user": {"login": "different" if fault == "author" else "Reviewer"},
        }

    def route(request):
        assert request.method == "GET"
        return httpx.Response(200, json=responses[request.url.path])

    monkeypatch.setattr(setup, "_headers", lambda *_: {})
    monkeypatch.setattr(verify_publication, "_github_comment", readback)
    receipt = verify_publication.collect_publication(
        mode="local",
        server_url="http://127.0.0.1:1234",
        preview=preview,
        draft_id=draft_id,
        author="reviewer",
        transport=httpx.MockTransport(route),
    )
    assert receipt["publication_ready"] == (fault == "none"), receipt
    assert readbacks == ([] if fault in {"draft", "unknown", "version"} else [(pr_url, 42)])
    assert draft["body"] not in str(receipt)
