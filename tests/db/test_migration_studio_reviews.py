"""The review ledger upgrades and downgrades without changing existing data."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.db_models import (
    SqlStudioFindingArtifact,
    SqlStudioInlineDraft,
    SqlStudioReviewCollaborator,
    SqlStudioReviewComment,
    SqlStudioReviewEvent,
    SqlStudioReviewOperation,
    SqlStudioReviewPass,
    SqlStudioReviewPatchChunk,
    SqlStudioReviewQuestion,
    SqlStudioReviewReceiptChunk,
    SqlStudioReviewRun,
)
from omnigent.db.utils import _build_alembic_config

_PREVIOUS = "d0a1f879a01d"
_REVISION = "e6a2b4c8d013"
_PROJECT_ORIGIN = "5f9ca09e07cd"
_RUNS = "8f4b6c20a115"
_RUN_REPORTS = "3792ec30b1de"
_INLINE_DRAFTS = "4bf0aa1e9173"
_INLINE_DELIVERY = "7a2594cfe931"
_FINDING_ARTIFACTS = "ba9152e8d96f"
_HOSTED_CHECKS = "b51ac8e4d231"
_PUBLISHERS = "2ad8b1357c64"
_TABLES = {"studio_review_passes", "studio_review_operations", "studio_review_events"}


def _migrate(uri: str, engine: sa.Engine, step: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        getattr(command, step)(config, revision)


def test_upgrade_and_downgrade_preserve_existing_database(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'migration.db'}"
    engine = sa.create_engine(uri)
    try:
        _migrate(uri, engine, "upgrade", _PREVIOUS)
        assert not _TABLES.intersection(sa.inspect(engine).get_table_names())
        with engine.begin() as connection:
            connection.execute(
                sa.text("CREATE TABLE legacy_guard (value VARCHAR(32) PRIMARY KEY)")
            )
            connection.execute(sa.text("INSERT INTO legacy_guard VALUES ('preserved')"))
        _migrate(uri, engine, "upgrade", _REVISION)
        assert set(sa.inspect(engine).get_table_names()) >= _TABLES
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO studio_review_passes "
                    "(workspace_id,id,owner_scope,source_session_id,target_json,profile_json,"
                    "state,dispatch_status,created_at,updated_at,version) "
                    "VALUES (0,:id,:owner,:source,:target,:profile,1,1,1,1,1)"
                ),
                {
                    "id": b"r" * 16,
                    "owner": b"o" * 32,
                    "source": b"s" * 16,
                    "target": b"{}",
                    "profile": b"{}",
                },
            )
            assert (
                connection.execute(sa.text("SELECT value FROM legacy_guard")).scalar_one()
                == "preserved"
            )
        _migrate(uri, engine, "upgrade", _PROJECT_ORIGIN)
        _migrate(uri, engine, "upgrade", _RUN_REPORTS)
        assert "studio_inline_drafts" not in sa.inspect(engine).get_table_names()
        _migrate(uri, engine, "upgrade", _INLINE_DRAFTS)
        _migrate(uri, engine, "upgrade", _HOSTED_CHECKS)
        _migrate(uri, engine, "upgrade", _PUBLISHERS)
        for model in (
            SqlStudioReviewPass,
            SqlStudioReviewOperation,
            SqlStudioReviewEvent,
            SqlStudioReviewRun,
            SqlStudioReviewPatchChunk,
            SqlStudioReviewReceiptChunk,
            SqlStudioReviewQuestion,
            SqlStudioReviewCollaborator,
            SqlStudioReviewComment,
            SqlStudioInlineDraft,
            SqlStudioFindingArtifact,
        ):
            inspected = sa.inspect(engine)
            actual = {c["name"] for c in inspected.get_columns(model.__tablename__)}
            assert actual == set(model.__table__.c.keys())
            assert inspected.get_foreign_keys(model.__tablename__) == []
        with engine.connect() as connection:
            assert connection.execute(
                sa.text("SELECT source_session_id, source_project_id FROM studio_review_passes")
            ).one() == (b"s" * 16, None)
        grants = SqlStudioReviewCollaborator.__table__
        with engine.begin() as connection:
            connection.execute(
                grants.insert().values(
                    workspace_id=0,
                    run_id="a" * 32,
                    user_key=b"p" * 32,
                    user_id="publisher",
                    owner_user_id="owner",
                )
            )
            assert connection.scalar(sa.select(grants.c.can_publish)) is False
            connection.execute(grants.update().values(can_publish=True))
        with pytest.raises(RuntimeError, match="review publishers are assigned"):
            _migrate(uri, engine, "downgrade", _HOSTED_CHECKS)
        with engine.begin() as connection:
            connection.execute(grants.delete())
        questions = SqlStudioReviewQuestion.__table__
        with engine.begin() as connection:
            connection.execute(
                questions.insert().values(
                    workspace_id=0,
                    run_id="a" * 32,
                    id="b" * 32,
                    actor_scope=b"a" * 32,
                    intent_hash=b"b" * 32,
                    input_json="{}",
                    state="accepted",
                    created_at=1,
                    updated_at=1,
                    check_job_json="{}",
                )
            )
        with pytest.raises(RuntimeError, match="sandbox check dispatches exist"):
            _migrate(uri, engine, "downgrade", _FINDING_ARTIFACTS)
        with engine.begin() as connection:
            connection.execute(questions.delete())
        artifacts = SqlStudioFindingArtifact.__table__
        with engine.begin() as connection:
            connection.execute(
                artifacts.insert().values(
                    workspace_id=0,
                    run_id="a" * 32,
                    finding_key=b"f" * 32,
                    digest=b"d" * 32,
                    attached_at=1,
                    metadata_json="{}",
                )
            )
        with pytest.raises(RuntimeError, match="finding artifacts exist"):
            _migrate(uri, engine, "downgrade", _INLINE_DELIVERY)
        with engine.begin() as connection:
            connection.execute(artifacts.delete())
        _migrate(uri, engine, "downgrade", _INLINE_DELIVERY)
        assert artifacts.name not in sa.inspect(engine).get_table_names()
        drafts = SqlStudioInlineDraft.__table__
        with engine.begin() as connection:
            connection.execute(
                drafts.insert().values(
                    workspace_id=0,
                    run_id="a" * 32,
                    id="b" * 32,
                    actor_scope=b"x" * 32,
                    intent_hash=b"y" * 32,
                    thread_json="{}",
                    anchor_json="{}",
                    body="draft",
                    version=1,
                    discarded=False,
                    created_at=1,
                    updated_at=1,
                )
            )
        with engine.begin() as connection:
            assert (
                connection.execute(sa.select(drafts.c.publication_state)).scalar_one() == "draft"
            )
            connection.execute(drafts.update().values(publication_state="posted"))
        with pytest.raises(RuntimeError, match="inline publication records"):
            _migrate(uri, engine, "downgrade", _INLINE_DRAFTS)
        with engine.begin() as connection:
            connection.execute(drafts.update().values(publication_state="draft"))
        with pytest.raises(RuntimeError, match="saved inline drafts"):
            _migrate(uri, engine, "downgrade", _RUN_REPORTS)
        with engine.begin() as connection:
            connection.execute(drafts.update().values(discarded=True, body=""))
        _migrate(uri, engine, "downgrade", _PROJECT_ORIGIN)
        assert "studio_inline_drafts" not in sa.inspect(engine).get_table_names()
        assert "studio_review_runs" not in sa.inspect(engine).get_table_names()
        _migrate(uri, engine, "downgrade", _REVISION)
        with engine.connect() as connection:
            assert (
                connection.execute(
                    sa.text("SELECT source_session_id FROM studio_review_passes")
                ).scalar_one()
                == b"s" * 16
            )
        _migrate(uri, engine, "downgrade", _PREVIOUS)
        assert not _TABLES.intersection(sa.inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert (
                connection.execute(sa.text("SELECT value FROM legacy_guard")).scalar_one()
                == "preserved"
            )
        _migrate(uri, engine, "upgrade", _REVISION)
        with engine.connect() as connection:
            assert (
                connection.execute(
                    sa.text("SELECT count(*) FROM studio_review_passes")
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()


def test_migration_keeps_owner_and_start_uniqueness_in_the_database(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'constraints.db'}"
    engine = sa.create_engine(uri)
    try:
        _migrate(uri, engine, "upgrade", _REVISION)
        constraints = sa.inspect(engine).get_unique_constraints("studio_review_operations")
        keys = {tuple(c["column_names"]) for c in constraints}
        assert ("workspace_id", "owner_scope", "action", "idempotency_key") in keys
        assert ("workspace_id", "review_id", "start_once") in keys
        assert sa.inspect(engine).get_pk_constraint("studio_review_events")[
            "constrained_columns"
        ] == ["workspace_id", "review_id", "sequence"]
    finally:
        engine.dispose()
