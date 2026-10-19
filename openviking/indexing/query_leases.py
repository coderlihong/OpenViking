# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Query-view leases in their own tiny store, off the publication writer queue.

Every find/read used to pay three writes into publication.db's single-writer
queue: the pin (``INSERT INTO leases``), the heartbeat renewal (``UPDATE``) and
the release (``DELETE``). Under any background writer those queued behind
``BEGIN IMMEDIATE`` -- measured as finds stretching from ~2s to 18s and query
leases lapsing into ``QueryLeaseExpired`` while a maintenance claim waited 16s
for the writer. Retrieval and publication must not share one write queue.

The lease rows only need to outlive their query (TTL) and stay visible to the
retention/GC planners, so they live in a dedicated WAL database next to
publication.db with the same row shape (id, snapshots JSON, expires). Query
paths no longer touch the publication writer at all; ``prune_snapshots`` and
the GC planners merge this store's live rows, and ``capture`` re-verifies its
pinned snapshots afterwards, which restores the pin/read atomicity the
single-writer design used to guarantee for free.

The store is safe to delete while no server holds it: rows are TTL-bounded
advisory pins, and a restarted process re-pins whatever it reads.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from uuid import uuid4


class QueryLeaseStore:
    """One row shape, per-thread connections, TTL-expiry semantics.

    Contention here is query-heartbeat-vs-query-heartbeat on a database small
    enough to sit entirely in the page cache, so the default busy timeout is
    generous without ever being observable next to publication.db waits.
    """

    def __init__(self, path: str, busy_timeout_ms: int = 5000):
        self.path = str(path)
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        db = getattr(self._local, "conn", None)
        if db is None:
            db = sqlite3.connect(self.path, timeout=max(1.0, self._busy_timeout_ms / 1000.0))
            db.row_factory = sqlite3.Row
            # WAL + NORMAL: same pairing as publication.db, but on a file whose
            # whole write set is a handful of rows per query.
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
            db.execute(
                "CREATE TABLE IF NOT EXISTS leases ("
                "id TEXT PRIMARY KEY, snapshots TEXT NOT NULL, expires REAL NOT NULL)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS leases_expiry ON leases(expires)")
            db.commit()
            self._local.conn = db
        return db

    def _with_timeout(self, db: sqlite3.Connection, busy_timeout_ms: int | None):
        """Temporarily lower the busy timeout for event-loop callers."""
        if busy_timeout_ms is None:
            return None
        db.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")

        def _restore():
            db.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")

        return _restore

    def insert(self, snapshot_ids, ttl, busy_timeout_ms: int | None = None) -> str:
        """Pin ``snapshot_ids`` for ``ttl`` seconds; sweep expired rows on the way."""
        now = time.time()
        lease = uuid4().hex
        db = self._conn()
        restore = self._with_timeout(db, busy_timeout_ms)
        try:
            with db:
                db.execute("DELETE FROM leases WHERE expires<?", (now,))
                db.execute(
                    "INSERT INTO leases VALUES (?,?,?)",
                    (lease, json.dumps(list(snapshot_ids)), now + ttl),
                )
        finally:
            if restore is not None:
                restore()
        return lease

    def renew(self, lease, ttl, busy_timeout_ms: int | None = None) -> bool:
        """Extend a still-live lease; False means it lapsed (caller must retry)."""
        now = time.time()
        db = self._conn()
        restore = self._with_timeout(db, busy_timeout_ms)
        try:
            with db:
                changed = db.execute(
                    "UPDATE leases SET expires=? WHERE id=? AND expires>?",
                    (now + ttl, lease, now),
                ).rowcount
        finally:
            if restore is not None:
                restore()
        return bool(changed)

    def release(self, lease) -> None:
        db = self._conn()
        with db:
            db.execute("DELETE FROM leases WHERE id=?", (lease,))

    def held(self, lease) -> dict | None:
        """The live lease row (``snapshots``/``expires``) or None once lapsed."""
        row = self._conn().execute(
            "SELECT snapshots, expires FROM leases WHERE id=? AND expires>?",
            (lease, time.time()),
        ).fetchone()
        return dict(row) if row is not None else None

    def live_snapshot_ids(self) -> set:
        """Every snapshot id pinned by an unexpired lease, for GC planners."""
        rows = self._conn().execute(
            "SELECT snapshots FROM leases WHERE expires>?", (time.time(),)
        ).fetchall()
        pinned: set = set()
        for row in rows:
            try:
                pinned.update(json.loads(row["snapshots"]))
            except (TypeError, ValueError):
                continue
        return pinned
