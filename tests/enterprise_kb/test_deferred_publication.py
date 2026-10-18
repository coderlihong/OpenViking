"""Publication work runs outside the writer's transaction.

Every publish used to run its member walk, manifest merge and the json/gzip
encode of a ~35MB snapshot inside ``BEGIN IMMEDIATE``: one publish held the
single writer for ~0.7s and every other writer queued behind it, which is what
made the vector phase crawl. The publish is now prepared in a read-only pass
and applied by a short write transaction once the receipt transaction has
committed.
"""

import sqlite3
import threading
import time

from test_publication import view

from openviking.indexing.publication import PublicationStore

ACCOUNT = "acct"
NAMESPACE = "ns"
A = "viking://resources/manual/a.md"


def member(store, batch, uri):
    generation = store.begin(ACCOUNT, NAMESPACE, uri, "new", ["x"], batch=batch)
    store.seal(generation)
    return generation


def test_prepare_does_not_hold_the_writer_lock(tmp_path, monkeypatch):
    """A second writer commits while a publication is being prepared."""
    store = PublicationStore(str(tmp_path / "db"))
    batch = store.begin_batch(ACCOUNT, NAMESPACE)
    a = member(store, batch, A)
    assert not store.acknowledge(a, "x")

    real_collect = PublicationStore._collect_publication

    def slow_collect(self, db, batch=None, generation=None):
        # Stands in for the manifest merge plus gzip of a ~35MB snapshot.
        time.sleep(0.6)
        return real_collect(self, db, batch=batch, generation=generation)

    monkeypatch.setattr(PublicationStore, "_collect_publication", slow_collect)
    worker = threading.Thread(target=store.seal_batch, args=(batch,), daemon=True)
    worker.start()
    time.sleep(0.2)  # let the worker enter the read-only preparation
    started = time.perf_counter()
    store.begin_batch("other", "ns")
    elapsed = time.perf_counter() - started
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert elapsed < 0.3, f"writer waited {elapsed:.3f}s behind a preparing publisher"
    assert view(store)[A]["generation"] == a


def test_a_failed_publish_is_retried_by_the_sweep(tmp_path, monkeypatch):
    """A publication that fails after the commit is retried, not lost."""
    store = PublicationStore(str(tmp_path / "db"))
    batch = store.begin_batch(ACCOUNT, NAMESPACE)
    a = member(store, batch, A)
    assert not store.acknowledge(a, "x")

    real_apply = PublicationStore._apply_publication
    calls = {"n": 0}

    def flaky_apply(self, db, plan):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("simulated publish failure")
        return real_apply(self, db, plan)

    monkeypatch.setattr(PublicationStore, "_apply_publication", flaky_apply)
    # The seal is answered by the deferred publish, so a failed apply is False.
    assert not store.seal_batch(batch)
    assert calls["n"] == 1
    assert store.batch_state(batch)["state"] == "queued"
    assert view(store) == {}

    monkeypatch.setattr(PublicationStore, "_apply_publication", real_apply)
    # The periodic sweep republishes a queued batch whose publish never landed.
    store.recover_stranded_batches()
    assert store.batch_state(batch)["state"] == "published"
    assert view(store)[A]["generation"] == a
