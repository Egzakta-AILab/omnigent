"""The review ledger upgrades and downgrades without changing existing data."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.db_models import (
    SqlStudioReviewEvent,
    SqlStudioReviewOperation,
    SqlStudioReviewPass,
    SqlStudioReviewRun,
)
from omnigent.db.utils import _build_alembic_config

_PREVIOUS = "d0a1f879a01d"
_REVISION = "e6a2b4c8d013"
_PROJECT_ORIGIN = "5f9ca09e07cd"
_RUNS = "8f4b6c20a115"
_RUN_REPORTS = "bd90f34ca312"
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
        for model in (
            SqlStudioReviewPass,
            SqlStudioReviewOperation,
            SqlStudioReviewEvent,
            SqlStudioReviewRun,
        ):
            inspected = sa.inspect(engine)
            actual = {c["name"] for c in inspected.get_columns(model.__tablename__)}
            assert actual == set(model.__table__.c.keys())
            assert inspected.get_foreign_keys(model.__tablename__) == []
        with engine.connect() as connection:
            assert connection.execute(
                sa.text("SELECT source_session_id, source_project_id FROM studio_review_passes")
            ).one() == (b"s" * 16, None)
        _migrate(uri, engine, "downgrade", _PROJECT_ORIGIN)
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
