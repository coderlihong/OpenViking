"""A batch whose producer died must still publish the work it finished.

``seal_batch`` is the producer's last act.  When the process dies first the
batch stays ``building`` forever even though every member already holds its
receipts, and ``_publish_batch_if_complete`` refuses to publish anything but a
``queued`` batch -- so the chapters it contains stay invisible to retrieval.
``recover_stranded_batches`` seals exactly those batches, and only those.
"""

import sqlite3

from test_publication import publish, view

from openviking.indexing.publication import PublicationStore

A = "viking://resources/manual/a.md"
B = "viking://resources/manual/b.md"


def member(store, batch, uri):
    generation = store.begin("acct", "ns", uri, "new", ["x"], batch=batch)
    store.seal(generation)
    return generation


def test_stranded_batch_with_every_receipt_publishes_its_members(tmp_path):
    store = PublicationStore(str(tmp_path / "db"))
    old = {uri: publish(store, uri) for uri in (A, B)}
    batch = store.begin_batch("acct", "ns")
    a = member(store, batch, A)
    b = member(store, batch, B)
    assert not store.acknowledge(a, "x")
    assert not store.acknowledge(b, "x")
    assert store.batch_state(batch)["remaining"] == 0
    # The producer died right before seal_batch: complete, but still building.
    assert store.batch_state(batch)["state"] == "building"
    assert view(store)[A]["generation"] == old[A]
    assert store.recover_stranded_batches() == 1
    assert {uri: view(store)[uri]["generation"] for uri in (A, B)} == {A: a, B: b}
    assert store.batch_state(batch)["state"] == "published"
    assert store.recover_stranded_batches() == 0


def test_stranded_batch_with_an_outstanding_receipt_is_left_alone(tmp_path):
    store = PublicationStore(str(tmp_path / "db"))
    old = {uri: publish(store, uri) for uri in (A, B)}
    batch = store.begin_batch("acct", "ns")
    member(store, batch, A)
    b = member(store, batch, B)
    assert not store.acknowledge(b, "x")
    assert store.batch_state(batch)["remaining"] == 1
    assert store.recover_stranded_batches() == 0
    assert store.batch_state(batch)["state"] == "building"
    assert view(store)[A]["generation"] == old[A]


def test_stranded_batch_with_an_unsealed_member_is_left_alone(tmp_path):
    store = PublicationStore(str(tmp_path / "db"))
    batch = store.begin_batch("acct", "ns")
    a = store.begin("acct", "ns", A, "new", ["x"], batch=batch)
    assert not store.acknowledge(a, "x")
    assert store.recover_stranded_batches() == 0
    assert store.batch_state(batch)["state"] == "building"
    assert A not in view(store)


def test_sealed_batches_are_not_reopened(tmp_path):
    store = PublicationStore(str(tmp_path / "db"))
    batch = store.begin_batch("acct", "ns")
    a = member(store, batch, A)
    store.acknowledge(a, "x")
    assert store.seal_batch(batch)
    assert store.recover_stranded_batches() == 0
    assert view(store)[A]["generation"] == a


def test_legacy_null_remaining_batch_keeps_the_old_seal_path(tmp_path):
    store = PublicationStore(str(tmp_path / "db"))
    batch = store.begin_batch("acct", "ns")
    a = member(store, batch, A)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE batches SET remaining=NULL WHERE id=?", (batch,))
    # No counter to trust: the batch must not be treated as stranded.
    assert store.recover_stranded_batches() == 0
    assert store.acknowledge(a, "x") is False
    assert store.seal_batch(batch)
    assert view(store)[A]["generation"] == a


def test_a_batch_that_is_still_growing_is_left_to_its_producer(tmp_path):
    store = PublicationStore(str(tmp_path / "db"))
    batch = store.begin_batch("acct", "ns")
    a = member(store, batch, A)
    assert not store.acknowledge(a, "x")
    assert store.batch_state(batch)["remaining"] == 0
    # Produced a moment ago: the periodic sweep must not race a live producer.
    assert store.recover_stranded_batches(idle_seconds=60.0) == 0
    assert store.batch_state(batch)["state"] == "building"
    assert store.recover_stranded_batches(idle_seconds=0.0) == 1


def test_stale_building_member_is_reaped_so_the_batch_can_settle(tmp_path):
    """A member whose producer died must not trap its whole batch forever."""
    import time

    store = PublicationStore(str(tmp_path / "db"))
    publish(store, A)
    batch = store.begin_batch("acct", "ns")
    stuck = store.begin("acct", "ns", B, "new", ["x"], batch=batch)
    ready = member(store, batch, A)
    assert not store.acknowledge(ready, "x")
    assert store.batch_state(batch)["remaining"] == 1
    assert store.state(stuck)["state"] == "building"
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE builds SET created=? WHERE generation IN (?, ?)",
            (time.time() - 3 * 3600, stuck, ready),
        )
    assert store.recover_stranded_batches() == 1
    assert store.state(stuck)["state"] == "failed"
    assert view(store)[A]["generation"] == ready
    assert store.batch_state(batch)["state"] == "published"


def test_a_young_building_member_is_never_reaped(tmp_path):
    store = PublicationStore(str(tmp_path / "db"))
    batch = store.begin_batch("acct", "ns")
    stuck = store.begin("acct", "ns", B, "new", ["x"], batch=batch)
    ready = member(store, batch, A)
    assert not store.acknowledge(ready, "x")
    assert store.recover_stranded_batches() == 0
    assert store.state(stuck)["state"] == "building"
    assert store.batch_state(batch)["state"] == "building"


def test_an_old_member_in_a_still_growing_batch_is_left_to_its_producer(tmp_path):
    """A live producer keeps adding members; only idle batches are reaped."""
    import time

    store = PublicationStore(str(tmp_path / "db"))
    batch = store.begin_batch("acct", "ns")
    stuck = store.begin("acct", "ns", B, "new", ["x"], batch=batch)
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE builds SET created=? WHERE generation=?",
            (time.time() - 3 * 3600, stuck),
        )
    member(store, batch, A)
    assert store.recover_stranded_batches() == 0
    assert store.state(stuck)["state"] == "building"
    assert store.batch_state(batch)["state"] == "building"
