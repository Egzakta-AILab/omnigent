"""Durable intake and single-attempt dispatch state for Studio PR webhooks."""

from __future__ import annotations

import os
import sqlite3
import stat
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

_LEASE_SECONDS = 360
_MAX_PENDING = 200


class InboxFull(RuntimeError):
    """The receiver cannot durably admit another delivery right now."""


@dataclass(frozen=True)
class Delivery:
    key: str
    route: str
    policy: str
    payload: str
    state: str
    run_id: str | None
    attempts: int
    lease: str


class WebhookInbox:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            mode = os.fstat(descriptor).st_mode
            if not stat.S_ISREG(mode) or mode & 0o077:
                raise ValueError("Use a private regular file for the webhook inbox")
        finally:
            os.close(descriptor)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS deliveries (
                    key TEXT PRIMARY KEY, route TEXT NOT NULL, policy TEXT NOT NULL,
                    payload TEXT NOT NULL, state TEXT NOT NULL, run_id TEXT, detail TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, lease TEXT,
                    available_at REAL NOT NULL, updated_at REAL NOT NULL
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS available_deliveries "
                "ON deliveries(state, available_at)"
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=3)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def enqueue(self, key: str, route: str, policy: str, payload: str) -> bool:
        now = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM deliveries WHERE key = ?", (key,)).fetchone():
                return False
            pending = connection.execute(
                "SELECT count(*) FROM deliveries "
                "WHERE state IN ('queued','capturing','starting','drafting')"
            ).fetchone()[0]
            if pending >= _MAX_PENDING:
                raise InboxFull
            connection.execute(
                """INSERT INTO deliveries
                   (key, route, policy, payload, state, available_at, updated_at)
                   VALUES (?, ?, ?, ?, 'queued', ?, ?)""",
                (key, route, policy, payload, now, now),
            )
        return True

    def claim(self) -> Delivery | None:
        now = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT key, route, policy, payload, state, run_id, attempts
                   FROM deliveries WHERE state IN ('queued','capturing','starting','drafting')
                   AND available_at <= ? ORDER BY available_at, key LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            state = row["state"] if row["state"] in ("starting", "drafting") else "capturing"
            attempts = row["attempts"] + int(state != "starting")
            lease = uuid.uuid4().hex
            connection.execute(
                """UPDATE deliveries SET state = ?, attempts = ?, lease = ?,
                   available_at = ?, updated_at = ? WHERE key = ?""",
                (state, attempts, lease, now + _LEASE_SECONDS, now, row["key"]),
            )
            return Delivery(
                row["key"],
                row["route"],
                row["policy"],
                row["payload"],
                state,
                row["run_id"],
                attempts,
                lease,
            )

    def mark_starting(self, job: Delivery, run_id: str) -> bool:
        now = time.time()
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE deliveries SET state = 'starting', run_id = ?,
                   available_at = ?, updated_at = ?
                   WHERE key = ? AND lease = ? AND state = 'capturing' AND available_at > ?""",
                (run_id, now + _LEASE_SECONDS, now, job.key, job.lease, now),
            )
            return cursor.rowcount == 1

    def finish(self, job: Delivery, state: str, detail: str, *, delay: float = 0) -> bool:
        now = time.time()
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE deliveries SET state = ?, detail = ?, lease = NULL,
                   available_at = ?, updated_at = ? WHERE key = ? AND lease = ?""",
                (state, detail, now + delay, now, job.key, job.lease),
            )
            return cursor.rowcount == 1

    def recent(self) -> list[dict[str, object]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT key, route, state, run_id, detail, attempts, updated_at
                   FROM deliveries ORDER BY updated_at DESC, key LIMIT 50"""
            )
            return [dict(row) for row in rows]
