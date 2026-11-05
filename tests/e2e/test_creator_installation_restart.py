"""Read back real Creator installations across isolated Omnigent server restarts."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import update

from omnigent.db.db_models import SqlStudioCreatorPackage
from omnigent.db.utils import get_or_create_engine
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.stores.studio_review_store import StudioReviewStore
from omnigent.studio.creator_panel import QA_PANEL, panel_capabilities, panel_digest
from omnigent.studio.reviews.models import ReviewProfile, ReviewTarget
from omnigent.studio.verify_creator import verify_creator_installation
from tests._helpers.live_server import isolated_local_server


def test_creator_install_update_restart_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server_path = tmp_path / "server"
    server_path.mkdir()
    db_uri = f"sqlite:///{server_path / 'chat.db'}"
    project_store = SqlAlchemyProjectStore(db_uri)
    reviews = StudioReviewStore(db_uri)
    project = project_store.create(uuid4().hex, "Creator", RESERVED_USER_LOCAL)
    other_project = project_store.create(uuid4().hex, "Another project", RESERVED_USER_LOCAL)

    def capture(project_id: str) -> str:
        key = uuid4().hex
        return reviews.capture_run(
            owner_user_id=RESERVED_USER_LOCAL,
            idempotency_key=key,
            request_hash=key * 2,
            target=ReviewTarget(
                source_project_id=project_id,
                host_id=uuid4().hex,
                workspace="/workspace/repository",
                repository_path=".",
                pr_url="https://github.com/example/project/pull/1",
                repository="example/project",
                pr_number=1,
                base_sha="a" * 40,
                head_sha="b" * 40,
            ),
            profiles=[
                ReviewProfile(
                    agent_id=uuid4().hex, agent_version="1", harness="codex-native", skill="review"
                )
            ],
            patch=(
                "diff --git a/file b/file\n--- a/file\n+++ b/file\n@@ -1 +1 @@\n-before\n+after\n"
            ),
        ).id

    run = capture(project.id)
    same_project_run = capture(project.id)
    foreign_run = capture(other_project.id)
    path = f"/v1/studio/review-runs/{run}/creator-installation"
    first_package = tmp_path / "qa-v1.json"
    first_package.write_text(QA_PANEL.model_dump_json(), encoding="utf-8")
    v2 = QA_PANEL.model_copy(update={"title": "QA by folder", "group_by": "directory"})
    second_package = tmp_path / "qa-v2.json"
    second_package.write_text(v2.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr("omnigent.studio.setup._headers", lambda *_: {})
    env = {"OMNIGENT_STUDIO_CREATOR_PILOT": "1"}

    with (
        isolated_local_server(server_path, env=env) as url,
        httpx.Client(base_url=url, trust_env=False) as client,
    ):
        first = (
            client.put(
                path,
                json={
                    "expected_revision": 0,
                    "panel": QA_PANEL.model_dump(mode="json"),
                    "digest": panel_digest(QA_PANEL),
                    "approved_capabilities": panel_capabilities(QA_PANEL),
                },
            )
            .raise_for_status()
            .json()
        )
        assert first["selected_version"] == 1
        update_intent = {
            "expected_revision": first["revision"],
            "panel": v2.model_dump(mode="json"),
            "digest": panel_digest(v2),
        }
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = [
                pool.submit(client.put, update_path, json=update_intent)
                for update_path in (
                    path,
                    f"/v1/studio/review-runs/{same_project_run}/creator-installation",
                )
            ]
            responses = sorted(
                (future.result() for future in outcomes), key=lambda r: r.status_code
            )
        assert [response.status_code for response in responses] == [200, 409]
        second = responses[0].json()
        assert second["selected_version"] == 2

    with (
        isolated_local_server(server_path, env=env) as url,
        httpx.Client(base_url=url, trust_env=False) as client,
    ):
        reopened = verify_creator_installation(
            mode="local",
            server_url=url,
            run_id=run,
            package=second_package,
            expect="enabled",
            version=2,
        )
        assert [entry.version for entry in reopened.history] == [2, 1]
        assert (
            (
                client.get(f"/v1/studio/review-runs/{same_project_run}/creator-installation")
                .raise_for_status()
                .json()["selected_version"]
            )
            == 2
        )
        assert (
            (
                client.get(f"/v1/studio/review-runs/{foreign_run}/creator-installation")
                .raise_for_status()
                .json()["history"]
            )
            == []
        )
        with get_or_create_engine(db_uri).begin() as connection:
            connection.execute(
                update(SqlStudioCreatorPackage)
                .where(SqlStudioCreatorPackage.version == 2)
                .values(definition_json='{"contract_version":"99"}')
            )
        unavailable = client.get(path).raise_for_status().json()
        assert unavailable["enabled"] and unavailable["active_panel"] is None
        assert unavailable["history"][0]["capabilities"] is None
        assert client.get(f"/v1/studio/review-runs/{run}/discussion").status_code == 200
        rolled_back = (
            client.patch(
                path,
                json={
                    "expected_revision": unavailable["revision"],
                    "version": 1,
                    "approved_capabilities": panel_capabilities(QA_PANEL),
                },
            )
            .raise_for_status()
            .json()
        )
        assert rolled_back["active_panel"] == QA_PANEL.model_dump(mode="json")
        client.patch(
            path, json={"expected_revision": rolled_back["revision"], "version": None}
        ).raise_for_status()

    with isolated_local_server(server_path, env=env) as url:
        disabled = verify_creator_installation(
            mode="local",
            server_url=url,
            run_id=run,
            package=first_package,
            expect="disabled",
            version=1,
        )
        assert disabled.active_panel is None
        assert [entry.version for entry in disabled.history] == [2, 1]
