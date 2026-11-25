from dataclasses import replace
from importlib.metadata import Distribution
from uuid import uuid4

import pytest
from fastapi import HTTPException

from omnigent.extensions import ExtensionManifest, ExtensionPermission, ExtensionPluginState
from omnigent.extensions.studio_rpc import (
    StudioRpcContext,
    invoke_studio_handler,
    load_studio_handlers,
)
from omnigent.stores.studio_tasks import StudioTaskStore
from omnigent.studio.rpc import TaskKey


def test_authoritative_package_binding_and_candidate_authorization(tmp_path, monkeypatch, db_uri):
    package = tmp_path / "egzakta_release-1.dist-info"
    package.mkdir()
    (package / "METADATA").write_text("Name: egzakta-release\nVersion: 1.0\n")
    (package / "entry_points.txt").write_text(
        "[omnigent.studio_rpc.v1]\negzakta.release = release_fixture:Handler\n"
    )
    (tmp_path / "release_fixture.py").write_text("""
from omnigent.stores.studio_tasks import StudioTaskStore
from omnigent.studio.rpc import Acceptance
class Handler:
    def can_sign_off(self, candidate_id, context):
        return context.actor == 'qa' and candidate_id == 'candidate'
    def invoke(self, method, params, context):
        store = StudioTaskStore(context.storage_location)
        update = Acceptance(
            task_id=params['task_id'], acceptance_check='Accepted by '+context.actor
        )
        return store.change_task(context.actor, update)
""")
    monkeypatch.syspath_prepend(str(tmp_path))
    dist = Distribution.at(package)
    manifest = ExtensionManifest(
        id="egzakta.release",
        display_name="Release",
        distribution="egzakta-release",
        version="1",
        requires_omnigent=">=0.23",
        extension_api=1,
        permissions=frozenset({ExtensionPermission.STUDIO_RELEASES}),
    )

    def discover(item):
        return load_studio_handlers(ExtensionPluginState((item,), {}), dist.entry_points)

    assert not discover(replace(manifest, distribution="different"))
    handlers = discover(manifest)
    user = StudioRpcContext("dev", 0, db_uri, None)
    task = StudioTaskStore(db_uri).task_for_issue(
        "dev", TaskKey(linear_workspace=uuid4(), linear_issue_id=uuid4())
    )
    params = {"candidate_id": "candidate", "task_id": task["task_id"], "actor": "qa"}
    with pytest.raises(HTTPException, match="owner"):
        invoke_studio_handler(handlers, "releases", "signOff", params, user)
    with pytest.raises(HTTPException, match="candidate ID"):
        invoke_studio_handler(handlers, "releases", "signOff", {}, user)
    assert (
        invoke_studio_handler(handlers, "releases", "signOff", params, replace(user, actor="qa"))[
            "acceptance_check"
        ]
        == "Accepted by qa"
    )
    assert (
        StudioTaskStore(db_uri).task_for_issue(
            "dev",
            TaskKey.model_validate({k: task[k] for k in ("linear_workspace", "linear_issue_id")}),
        )["acceptance_check"]
        == "Accepted by qa"
    )
