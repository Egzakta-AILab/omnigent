"""Bot review policy stays tied to verified revisions as the PR changes."""

import base64
import json

import httpx
import pytest
from fastapi import HTTPException

from omnigent.studio.reviews.product_pack import GitHubCapture, Trigger

BASE = "1" * 40
HEAD = "2" * 40
CATALOG = "3" * 40
DEPLOYED = "4" * 40
APP = "Egzakta-AILab/KVARK"
META = "Egzakta-AILab/egzakta-org"
SERVICES = "Egzakta-AILab/Services"
LENSES = ["backend", "frontend", "ai-quality", "cicd", "security"]


class Fixture:
    def __init__(self):
        self.pull_reads = 0
        self.files = [{"filename": "frontend/picker.ts", "previous_filename": "backend/auth.py"}]
        self.current_head = HEAD
        self.last_head = HEAD
        self.draft = False
        self.labels = []
        self.capture_files = []
        profiles = {lens: {"file": f"profiles/{lens}.md"} for lens in LENSES}
        policy = {
            "schema_version": "studio-product-pack@1",
            "repository": APP,
            "review_instructions": "REVIEW.md",
            "lenses": {
                "repository": META,
                "revision": CATALOG,
                "catalog": "catalog.yaml",
                "profiles": LENSES,
            },
            "path_map": {
                "backend": ["backend/**"],
                "frontend": ["frontend/**"],
                "ai-quality": [],
                "cicd": [".github/**"],
                "security": ["**/auth.py"],
            },
            "protected_paths": ["**/auth.py"],
            "auto_lane": {
                "max_changed_lines": 150,
                "max_changed_files": 5,
                "max_components": 1,
                "allowed_severities": ["note"],
                "require_verified": True,
                "require_green_checks": True,
                "require_linear_issue": True,
                "allow_protected_paths": False,
            },
            "context": [
                {
                    "repository": SERVICES,
                    "access": "read_only",
                    "revision_source": "deployed_sha",
                    "files": ["charts/app.yaml"],
                }
            ],
        }
        self.contents = {
            (APP, "studio.yaml", BASE): json.dumps(policy),
            (APP, "REVIEW.md", BASE): "Instructions from the accepted base.",
            (META, "catalog.yaml", CATALOG): json.dumps({"profiles": profiles}),
            (SERVICES, "charts/app.yaml", DEPLOYED): "image: fixture",
            **{(META, p["file"], CATALOG): lens for lens, p in profiles.items()},
        }

    def serve(self, req):
        assert req.url.host == "api.github.test"
        assert req.method == "GET"
        if req.url.path == f"/repos/{APP}/pulls/7":
            self.pull_reads += 1
            return httpx.Response(
                200,
                json={
                    "number": 7,
                    "state": "open",
                    "draft": self.draft,
                    "labels": self.labels,
                    "base": {"repo": {"full_name": APP}, "ref": "release", "sha": BASE},
                    "head": {
                        "repo": {"full_name": "fixture/contribution"},
                        "sha": self.current_head if self.pull_reads == 1 else self.last_head,
                        "ref": "main",
                    },
                    "changed_files": len(self.files),
                    "user": {"login": "author"},
                    "requested_reviewers": [],
                },
            )
        if req.url.path == f"/repos/{APP}/compare/{BASE}...{HEAD}":
            return httpx.Response(200, json={"files": self.files})
        repo, path = req.url.path.removeprefix("/repos/").split("/contents/", 1)
        key = repo, path, req.url.params["ref"]
        self.capture_files.append(key)
        payload = self.contents[key].encode()
        return httpx.Response(
            200,
            json={
                "type": "file",
                "path": path,
                "encoding": "base64",
                "size": len(payload),
                "content": base64.b64encode(payload).decode(),
            },
        )

    def capture(self):
        trigger = Trigger(
            schema_version="studio-review-trigger@1",
            repository=APP,
            pull_number=7,
            base_ref="release",
            base_sha=BASE,
            head_sha=HEAD,
            mode="full",
        )
        with httpx.Client(
            base_url="https://api.github.test", transport=httpx.MockTransport(self.serve)
        ) as client:
            return GitHubCapture(client).capture(trigger, DEPLOYED)


def test_rename_uses_old_and_new_paths_and_only_pinned_policy_sources():
    fixture = Fixture()
    result = fixture.capture()
    assert result["selected_lenses"] == ["backend", "frontend", "security"]
    assert result["protected_paths"] == ["backend/auth.py"]
    assert result["profiles"] == {name: name for name in result["selected_lenses"]}
    assert result["instructions"] == fixture.contents[APP, "REVIEW.md", BASE]
    assert result["services"] == {"charts/app.yaml": "image: fixture"}
    assert fixture.pull_reads == 2
    assert all(
        sha == {APP: BASE, META: CATALOG, SERVICES: DEPLOYED}[repo]
        for repo, _, sha in fixture.capture_files
    )


@pytest.mark.parametrize("stale_at_start", [True, False])
def test_stale_head_is_rejected_even_when_it_changes_during_capture(stale_at_start):
    fixture = Fixture()
    if stale_at_start:
        fixture.current_head = "9" * 40
    else:
        fixture.last_head = "9" * 40
    with pytest.raises(HTTPException) as exc:
        fixture.capture()
    assert exc.value.status_code == 409
    if stale_at_start:
        assert not fixture.capture_files


def test_draft_requires_live_opt_in():
    fixture = Fixture()
    fixture.draft = True
    with pytest.raises(HTTPException) as exc:
        fixture.capture()
    assert exc.value.status_code == 409 and not fixture.capture_files
    fixture.labels = [{"name": "studio:review"}]
    assert fixture.capture()["selected_lenses"] == ["backend", "frontend", "security"]


def test_queue_serializes_repositories_and_rejects_late_results(db_uri):
    from omnigent.db.db_models import workspace_scope
    from omnigent.stores.studio_review_queue import ReviewQueue

    now = [10]
    queue = ReviewQueue(db_uri, clock=lambda: now[0])
    revision = Trigger(
        schema_version="studio-review-trigger@1",
        repository=APP,
        pull_number=7,
        base_ref="main",
        base_sha=BASE,
        head_sha=HEAD,
        mode="delta",
    )
    fixture = Fixture().capture()
    first = queue.enqueue("bot", revision, fixture, {}, [])
    now[0] += 1
    next_rev = revision.model_copy(update={"head_sha": "9" * 40})
    second = queue.enqueue("bot", next_rev, fixture, {}, [])
    assert (
        queue.enqueue("bot", revision.model_copy(update={"mode": "full"}), fixture, {}, [])["mode"]
        == "full"
    )
    assert queue.enqueue("bot", revision, fixture, {}, [])["mode"] == "full"
    claimed = queue.claim(APP)
    assert claimed and claimed[0] == first["review_id"].replace("-", "")
    assert queue.claim(APP) is None
    now[0] += 1201
    claimed = queue.claim(APP)
    assert claimed and claimed[0] == second["review_id"].replace("-", "")
    queue.finish(first["review_id"], {"lane": "auto"})
    assert (
        queue.rpc("viewer", "getWalkthrough", {"review_id": first["review_id"]})["state"]
        == "unknown"
    )
    with workspace_scope(13):
        assert not queue.rpc("other", "listQueue", {})["items"]


@pytest.mark.parametrize(
    ("severity", "status", "manual", "lane", "route"),
    [
        ("note", "verified", False, "auto", "human_review"),
        ("note", "verified", True, "vibe_check", "vibe_check"),
        ("blocks_merge", "check_failed", False, "human_review", "fail_check"),
        ("should_fix", "unverified", False, "human_review", "human_review"),
        ("should_fix", "env_blocked", False, "human_review", "environment_owner"),
    ],
)
def test_revision_evidence_routes_by_base_policy(severity, status, manual, lane, route):
    from omnigent.studio.reviews.automated import LensResult, freeze

    c = Fixture().capture()
    path = "backend/product.py"
    c.update(
        repository=APP,
        base_sha=BASE,
        head_sha=HEAD,
        base_ref="main",
        head_ref="feature",
        protected_paths=[],
        labels=[],
        bot_fix=False,
        checks_green=True,
        component_count=1,
        linear_linked=True,
        files=[
            {
                "filename": path,
                "status": "added",
                "patch": "@@ -0,0 +1 @@\n+return 1",
                "additions": 1,
                "deletions": 0,
            }
        ],
    )
    c["policy"]["lanes"] = {
        "otherwise": "human_review",
        "unverified": "human_review",
        "env_blocked": "environment_owner",
        "needs_manual_trial": "vibe_check",
        "blocks_merge": "fail_check",
        "should_fix": "human_review",
    }
    cite = {"path": path, "line": 1, "text": "Observed the changed return value."}
    envelope = LensResult.model_validate(
        {
            "review_id": "lens",
            "base_sha": BASE,
            "head_sha": HEAD,
            "needs_manual_trial": manual,
            "proofs": [
                {
                    **cite,
                    "severity": severity,
                    "status": status,
                    "command": "git show",
                    "observed": "return 1",
                }
            ],
            "walkthrough": [
                {
                    **cite,
                    "layer": "summary",
                    "services_path": "charts/app.yaml",
                    "services_line": 1,
                }
            ],
        }
    )
    result = freeze(c, {"backend": envelope}, False)
    assert (result["status"], result["lane"], result["route"]) == (status, lane, route)
    assert result["walkthrough"][0]["services_url"].endswith(f"/{DEPLOYED}/charts/app.yaml#L1")
    assert len(result["shadow"]["raw"]) == 1
    if lane == "auto":
        c["component_count"] = None
        assert freeze(c, {"backend": envelope}, False)["lane"] == "human_review"
