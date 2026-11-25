from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

import omnigent.db


def test_downgrade_preserves_existing_leases_and_seats(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'studio.db'}"
    config = Config()
    config.set_main_option(
        "script_location", str(Path(omnigent.db.__file__).parent / "migrations")
    )
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "673928a5bf01")
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO studio_agent_leases VALUES "
            "(1, zeroblob(16), zeroblob(16), 'agent', NULL, 'codex-native', 'active', 1, 100)"
        )
        conn.exec_driver_sql(
            "INSERT INTO studio_seat_slots VALUES (1, 'slot', 'codex-native', 'profile', 1, 2, 0)"
        )

    command.upgrade(config, "85b30e459c20")
    with engine.begin() as conn:
        conn.exec_driver_sql("UPDATE studio_agent_leases SET session_id = zeroblob(16)")
    command.downgrade(config, "71208e0fd432")
    with pytest.raises(RuntimeError, match="Export existing Studio dispatch evidence"):
        command.downgrade(config, "673928a5bf01")
    with engine.begin() as conn:
        assert conn.exec_driver_sql(
            "SELECT session_id FROM studio_agent_leases"
        ).scalar_one() == bytes(16)
        conn.exec_driver_sql("UPDATE studio_agent_leases SET session_id = NULL")
    command.downgrade(config, "673928a5bf01")

    inspector = sa.inspect(engine)
    assert not {"studio_dispatches", "studio_api_budgets"} & set(inspector.get_table_names())
    assert [c["name"] for c in inspector.get_check_constraints("studio_agent_leases")] == [
        "ck_studio_lease_round"
    ]
    assert inspector.get_pk_constraint("studio_seat_slots")["constrained_columns"] == [
        "workspace_id",
        "slot_id",
    ]
    with engine.connect() as conn:
        assert tuple(conn.exec_driver_sql("SELECT * FROM studio_agent_leases").one()) == (
            1,
            bytes(16),
            bytes(16),
            "agent",
            None,
            "codex-native",
            "active",
            1,
            100,
        )
        assert tuple(conn.exec_driver_sql("SELECT * FROM studio_seat_slots").one()) == (
            1,
            "slot",
            "codex-native",
            "profile",
            1,
            2,
            0,
        )
    engine.dispose()
