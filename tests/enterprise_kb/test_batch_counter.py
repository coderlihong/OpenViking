"""The batch publication counter replaces the per-receipt whole-batch scan.

Every receipt used to walk all members of its batch -- several queries each --
inside the write transaction, which is what capped concurrent batch builds.
``batches.remaining`` now tracks outstanding members instead, so these tests
pin the accounting: exactly one decrement per member, publication only at zero,
and the original scan path for batches that predate the column.
"""

import sqlite3

import pytest

from openviking.indexing.publication import PublicationStore

A = "viking://resources/manual/a.md"
B = "viking://resources/manual/b.md"


def member(store, batch, uri):
    generation = store.begin("acct", "ns", uri, "new", ["x"], batch=batch)
    store.seal(generation)
    return generation


def remaining(store, batch):
    return store.batch_state(batch)["remaining"]


def test_counter_publishes_only_when_the_last_member_became_ready(tmp_path):
    store = PublicationStore(str(tmp_path / "db"))
    batch = store.begin_batch("acct", "ns")
    a = member(store, batch, A)
    b = member(store, batch, B)
    assert remaining(store, batch) == 2
    # Nothing has a receipt yet, so sealing the batch publishes nothing.
    assert not store.seal_batch(batch)
    assert not store.acknowledge(a, "x")
    assert remaining(store, batch) == 1
    assert store.batch_state(batch)["state"] == "queued"
    assert store.acknowledge(b, "x")
    assert remaining(store, batch) == 0
    assert store.batch_state(batch)["state"] == "published"
    assert view_of(store, a) == "published"
    assert view_of(store, b) == "published"


def test_duplicate_receipt_and_duplicate_seal_do_not_double_count(tmp_path):
    store = PublicationStore(str(tmp_path / "db"))
    batch = store.begin_batch("acct", "ns")
    a = member(store, batch, A)
    b = member(store, batch, B)
    store.seal_batch(batch)
    assert not store.acknowledge(a, "x")
    assert remaining(store, batch) == 1
    # The same child is acknowledged again: the receipt is already there, so the
    # member must not be counted down a second time.
    assert not store.acknowledge(a, "x")
    assert remaining(store, batch) == 1
    # A build only leaves 'building' once, so a second seal is not a decrement.
    assert not store.seal(a)
    assert remaining(store, batch) == 1
    assert store.acknowledge(b, "x")
    assert store.batch_state(batch)["state"] == "published"
    with pytest.raises(ValueError, match="Unexpected child"):
        store.acknowledge(b, "not-declared")


def test_receipts_arriving_before_the_seal_are_counted_at_the_seal(tmp_path):
    """A member that completes while still 'building' is counted down by seal."""
    store = PublicationStore(str(tmp_path / "db"))
    batch = store.begin_batch("acct", "ns")
    a = store.begin("acct", "ns", A, "new", ["x"], batch=batch)
    b = member(store, batch, B)
    # a collects its only receipt before it is sealed.
    assert not store.acknowledge(a, "x")
    assert remaining(store, batch) == 2
    store.seal(a)
    assert remaining(store, batch) == 1
    store.seal_batch(batch)
    assert store.acknowledge(b, "x")
    assert store.batch_state(batch)["state"] == "published"


def test_superseded_member_leaves_the_counter_exact(tmp_path):
    store = PublicationStore(str(tmp_path / "db"))
    batch = store.begin_batch("acct", "ns")
    stale = member(store, batch, A)
    assert remaining(store, batch) == 1
    # A rebuild of the same chapter inside the same batch replaces the member
    # that was already enqueued rather than being refused. The replaced member
    # never became ready, so it owes the batch a decrement and the rebuild then
    # takes its place: the batch is no further from publishing than before.
    rebuilt = member(store, batch, A)
    assert store.state(stale)["state"] == "superseded"
    assert remaining(store, batch) == 1
    b = member(store, batch, B)
    assert remaining(store, batch) == 2
    assert not store.seal_batch(batch)
    # The first receipt of two publishes nothing, but it does settle its member.
    assert not store.acknowledge(rebuilt, "x")
    assert remaining(store, batch) == 1
    assert store.acknowledge(b, "x")
    assert store.batch_state(batch)["state"] == "published"
    assert view_of(store, rebuilt) == "published"
    assert view_of(store, stale) == "superseded"


def test_last_member_retired_while_still_building_fails_the_batch(tmp_path):
    """Retiring the last live member aborts the batch at once.

    The producer may still be enqueueing the rest of the batch, and a rejected
    enqueue relies on the abort to stop the remaining members from being built,
    so this must not wait for ``seal_batch``.
    """
    store = PublicationStore(str(tmp_path / "db"))
    batch = store.begin_batch("acct", "ns")
    a = member(store, batch, A)
    store.fail(a, "embedding timeout")
    assert remaining(store, batch) == 0
    assert store.batch_state(batch)["state"] == "failed"
    # The failed batch no longer accepts members.
    with pytest.raises(RuntimeError, match="no longer accepts"):
        store.begin("acct", "ns", B, "new", ["x"], batch=batch)


def test_failed_member_is_counted_down_and_the_rest_still_publish(tmp_path):
    store = PublicationStore(str(tmp_path / "db"))
    batch = store.begin_batch("acct", "ns")
    a = member(store, batch, A)
    b = member(store, batch, B)
    assert remaining(store, batch) == 2
    store.fail(b, "embedding timeout")
    assert remaining(store, batch) == 1
    # Retiring an already retired member must not count it down again.
    store.fail(b, "embedding timeout")
    assert remaining(store, batch) == 1
    store.seal_batch(batch)
    assert store.acknowledge(a, "x")
    assert store.batch_state(batch)["state"] == "published"


def test_last_live_member_retired_fails_the_batch(tmp_path):
    store = PublicationStore(str(tmp_path / "db"))
    batch = store.begin_batch("acct", "ns")
    a = member(store, batch, A)
    store.seal_batch(batch)
    store.fail(a, "embedding timeout")
    assert store.batch_state(batch)["state"] == "failed"


def test_legacy_batch_without_a_counter_keeps_the_scan_path(tmp_path):
    """A batch written before the column existed is NULL and still publishes."""
    store = PublicationStore(str(tmp_path / "db"))
    batch = store.begin_batch("acct", "ns")
    with store.db() as db:
        db.execute("UPDATE batches SET remaining=NULL WHERE id=?", (batch,))
    # The member added afterwards stays NULL too (NULL+1 is NULL), exactly like
    # a registry that never learned about the counter.
    a = member(store, batch, A)
    assert remaining(store, batch) is None
    assert not store.seal_batch(batch)
    assert store.acknowledge(a, "x")
    assert store.batch_state(batch)["state"] == "published"
    assert remaining(store, batch) is None


def test_registry_created_without_the_column_is_migrated_in_place(tmp_path):
    """A registry built before the counter existed gains the column on startup.

    ``CREATE TABLE IF NOT EXISTS`` cannot retrofit a column onto an existing
    database, so ``init`` adds it the way the OAuth store does. Rows written
    before the migration keep NULL and stay on the original whole-batch scan.
    """
    path = str(tmp_path / "db")
    legacy = sqlite3.connect(path)
    legacy.executescript(
        "CREATE TABLE batches (id TEXT PRIMARY KEY, account TEXT, namespace TEXT, "
        "state TEXT, error TEXT);"
    )
    legacy.execute("INSERT INTO batches VALUES ('old','acct','ns','building',NULL)")
    legacy.commit()
    legacy.close()

    store = PublicationStore(path)
    raw = sqlite3.connect(path)
    assert "remaining" in {row[1] for row in raw.execute("PRAGMA table_info(batches)")}
    assert raw.execute("SELECT remaining FROM batches WHERE id='old'").fetchone()[0] is None
    raw.close()

    # The legacy batch still builds and publishes through the scan path.
    a = member(store, "old", A)
    assert remaining(store, "old") is None
    assert not store.seal_batch("old")
    assert store.acknowledge(a, "x")
    assert store.batch_state("old")["state"] == "published"


def test_counter_survives_reopening_the_store(tmp_path):
    store = PublicationStore(str(tmp_path / "db"))
    batch = store.begin_batch("acct", "ns")
    member(store, batch, A)
    assert remaining(PublicationStore(store.path), batch) == 1


def view_of(store, generation):
    return store.state(generation)["state"]
