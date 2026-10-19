# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Query-view leases must live off the publication writer queue.

Retrieval and publication must not share one write queue: the pin/heartbeat/
release of every find used to be three writes into publication.db's
single-writer queue, so finds queued behind any background writer (measured:
finds stretching to 18s and leases lapsing into ``QueryLeaseExpired``). They
now belong to a dedicated lease store: the publication database must see none
of them, and the retention/GC planners must still honour the pins.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager

import pytest

from openviking.indexing.manifest_codec import encode_manifest
from openviking.indexing.publication import PublicationStore, QueryLeaseExpired

LEASE_STORE_SUFFIX = ".query_leases.sqlite3"


@pytest.fixture
def store(tmp_path):
    return PublicationStore(str(tmp_path / "pub.db"))


@contextmanager
def _connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def _record_db_calls(store):
    """Shadow the bound ``db()`` so tests can assert on transaction modes."""
    calls: list[dict] = []
    original = store.db

    @contextmanager
    def spy(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        with original(*args, **kwargs) as db:
            yield db

    store.db = spy
    return calls


def _readonly_only(calls):
    return bool(calls) and all(call["kwargs"].get("readonly", False) for call in calls)


def _seed_snapshot(store, snapshot_id="s1", created=None):
    blob = encode_manifest({"viking://resources/raw/a/1.md": {"generation": "g1"}})
    with store.db() as db:
        db.execute(
            "INSERT INTO snapshots VALUES (?,?,?,?,?)",
            (snapshot_id, "a", "n", blob, created if created is not None else time.time()),
        )
    return blob


def test_query_lease_writes_never_touch_the_publication_writer(store):
    calls = _record_db_calls(store)
    lease, manifest = store.capture("a", ttl=120)
    store.renew(lease, ttl=120)
    with _connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0
    with _connect(store.path + LEASE_STORE_SUFFIX) as db:
        row = db.execute("SELECT snapshots, expires FROM leases WHERE id=?", (lease,)).fetchone()
        assert row is not None
        assert json.loads(row["snapshots"]) == []
        assert row["expires"] > time.time()
    store.release(lease)
    with _connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0
    with _connect(store.path + LEASE_STORE_SUFFIX) as db:
        assert db.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0, "release must drop the pin"
    assert manifest == {}
    assert _readonly_only(calls), "query lease paths must not open a publication write transaction"


def test_capture_pins_exactly_the_manifest_it_serves(store):
    _seed_snapshot(store)
    with store.db() as db:
        db.execute("INSERT INTO heads VALUES (?,?,?)", ("a", "n", "s1"))
    lease, manifest = store.capture("a", ttl=120)
    assert manifest == {"viking://resources/raw/a/1.md": {"generation": "g1"}}
    held = store._query_leases.held(lease)
    assert held is not None
    assert json.loads(held["snapshots"]) == ["s1"]


def test_expired_lease_renewal_is_refused(store):
    lease, _ = store.capture("a", ttl=0.05)
    time.sleep(0.06)
    with pytest.raises(QueryLeaseExpired):
        store.renew(lease, ttl=120)


def test_capture_never_serves_a_snapshot_that_vanished_mid_pin(store, monkeypatch):
    _seed_snapshot(store)
    with store.db() as db:
        db.execute("INSERT INTO heads VALUES (?,?,?)", ("a", "n", "s1"))
    original_insert = store._query_leases.insert
    sabotages = []

    def sabotage(snapshot_ids, ttl, busy_timeout_ms=None):
        # Simulate the retention sweep winning the race between the read and
        # the pin: the pinned view would name content the store no longer has.
        sabotages.append(list(snapshot_ids))
        with store.db() as db:
            db.executemany("DELETE FROM snapshots WHERE id=?", [(sid,) for sid in snapshot_ids])
        return original_insert(snapshot_ids, ttl, busy_timeout_ms=busy_timeout_ms)

    monkeypatch.setattr(store._query_leases, "insert", sabotage)
    lease, manifest = store.capture("a", ttl=120)
    assert sabotages[0] == ["s1"], "the verify step must catch the vanished snapshot and re-read"
    assert manifest == {}, "capture must not serve a manifest whose snapshot vanished mid-pin"
    assert json.loads(store._query_leases.held(lease)["snapshots"]) == [], "the retry pins only surviving heads"


def test_retired_candidates_still_honour_lease_store_pins(store):
    _seed_snapshot(store)
    with store.db() as db:
        db.execute(
            "INSERT INTO builds VALUES (?,?,?,?,?,?,?,?,?)",
            ("g1", "a", "n", "viking://resources/raw/a/1.md", None, None, "published", time.time() - 7200, None),
        )
    assert any(r["generation"] == "g1" for r in store.retired_candidates(grace_seconds=3600))
    store._query_leases.insert(["s1"], ttl=120)
    assert not any(r["generation"] == "g1" for r in store.retired_candidates(grace_seconds=3600))


def test_prune_snapshots_keeps_store_pinned_snapshots(store):
    _seed_snapshot(store, created=time.time() - 7200)
    with store.db() as db:
        db.execute("INSERT INTO heads VALUES (?,?,?)", ("a", "n", "anchor"))
        db.execute(
            "INSERT INTO snapshots VALUES (?,?,?,?,?)", ("anchor", "a", "n", encode_manifest({}), time.time())
        )
    lease = store._query_leases.insert(["s1"], ttl=120)
    assert store.prune_snapshots(older_than_seconds=3600.0, limit=64) == 0
    store._query_leases.release(lease)
    assert store.prune_snapshots(older_than_seconds=3600.0, limit=64) == 1


def test_legacy_publication_lease_rows_still_protect(store):
    _seed_snapshot(store)
    with store.db() as db:
        db.execute(
            "INSERT INTO builds VALUES (?,?,?,?,?,?,?,?,?)",
            ("g1", "a", "n", "viking://resources/raw/a/1.md", None, None, "published", time.time() - 7200, None),
        )
        db.execute("INSERT INTO leases VALUES (?,?,?)", ("legacy", json.dumps(["s1"]), time.time() + 60))
    assert not any(r["generation"] == "g1" for r in store.retired_candidates(grace_seconds=3600))
    with store.db() as db:
        db.execute("UPDATE leases SET expires=? WHERE id=?", (time.time() - 1, "legacy"))
    assert any(r["generation"] == "g1" for r in store.retired_candidates(grace_seconds=3600))
