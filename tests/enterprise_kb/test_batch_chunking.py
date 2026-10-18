"""A recursive producer must not hold an entire tree in one publication batch.

Observed in production: a tree reindex bound 4,137+ members to a single batch
that stayed ``building`` for hours, so no published row appeared, thousands of
builds queued behind the barrier, and a restart discarded all of the work
because the abandoned batch never sealed.

These tests pin the rollover contract: a filled chunk is sealed by the producer
itself, the next member continues in a fresh batch, and chunks publish on their
own receipts without waiting for the producer to return.
"""

import time

import pytest

from openviking.indexing.batch import (
    TextPublicationBatch,
    begin_batched,
    bind_text_batch,
    publication_chunk_members,
    publication_chunk_seconds,
)
from openviking.indexing.publication import PublicationStore

A = "viking://resources/manual/a.md"
B = "viking://resources/manual/b.md"


def make_member(store, batch, uri):
    generation = store.begin("acct", "ns", uri, "new", ["x"], batch=batch)
    store.seal(generation)
    return generation


def test_chunk_size_defaults_and_env_overrides(monkeypatch):
    monkeypatch.delenv("OPENVIKING_PUBLICATION_CHUNK_MEMBERS", raising=False)
    assert publication_chunk_members() == 1500
    monkeypatch.setenv("OPENVIKING_PUBLICATION_CHUNK_MEMBERS", "7")
    assert publication_chunk_members() == 7
    monkeypatch.setenv("OPENVIKING_PUBLICATION_CHUNK_MEMBERS", "not-a-number")
    assert publication_chunk_members() == 1500
    monkeypatch.setenv("OPENVIKING_PUBLICATION_CHUNK_MEMBERS", "-3")
    assert publication_chunk_members() == 0
    monkeypatch.setenv("OPENVIKING_PUBLICATION_CHUNK_MEMBERS", "  ")
    assert publication_chunk_members() == 1500


def test_filled_chunk_is_sealed_and_members_continue_in_a_new_batch(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENVIKING_PUBLICATION_CHUNK_MEMBERS", "2")
    store = PublicationStore(str(tmp_path / "db"))
    batch = TextPublicationBatch()
    batches, members = [], []
    for index in range(5):
        batch_id = batch.join(store, "acct", "ns")
        batches.append(batch_id)
        members.append(make_member(store, batch_id, f"viking://resources/manual/{index}.md"))
    # Two members per batch: the third join seals the first chunk and opens a second.
    assert batches[0] == batches[1]
    assert batches[2] == batches[3]
    assert batches[0] != batches[2] != batches[4]
    assert len(set(batches)) == 3
    assert store.batch_state(batches[0])["state"] == "queued"
    assert store.batch_state(batches[2])["state"] == "queued"
    assert store.batch_state(batches[4])["state"] == "building"
    # The receipts alone complete each sealed chunk; the producer is still running.
    for generation in members:
        store.acknowledge(generation, "x")
    assert store.batch_state(batches[0])["state"] == "published"
    assert store.batch_state(batches[2])["state"] == "published"
    assert store.batch_state(batches[4])["state"] == "building"
    # The final, still-open chunk publishes once the producer seals it.
    batch.finish(True)
    assert store.batch_state(batches[4])["state"] == "published"


def test_an_interrupted_producer_loses_only_its_open_chunk(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENVIKING_PUBLICATION_CHUNK_MEMBERS", "2")
    store = PublicationStore(str(tmp_path / "db"))
    batch = TextPublicationBatch()
    batches, members = [], []
    for index in range(5):
        batch_id = batch.join(store, "acct", "ns")
        batches.append(batch_id)
        members.append(make_member(store, batch_id, f"viking://resources/manual/{index}.md"))
    for generation in members:
        store.acknowledge(generation, "x")
    # Simulate the producer dying before finish(): the open chunk is abandoned.
    store.fail_batch(batches[4], "producer interrupted")
    assert store.batch_state(batches[0])["state"] == "published"
    assert store.batch_state(batches[2])["state"] == "published"
    assert store.batch_state(batches[4])["state"] == "failed"
    assert store.state(members[0])["state"] == "published"
    assert store.state(members[4])["state"] in {"failed", "superseded"}


def test_rollover_can_be_disabled_for_a_single_legacy_batch(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENVIKING_PUBLICATION_CHUNK_MEMBERS", "0")
    store = PublicationStore(str(tmp_path / "db"))
    batch = TextPublicationBatch()
    batches = []
    for index in range(5):
        batch_id = batch.join(store, "acct", "ns")
        batches.append(batch_id)
        make_member(store, batch_id, f"viking://resources/manual/{index}.md")
    assert len(set(batches)) == 1
    assert store.batch_state(batches[0])["state"] == "building"
    batch.finish(True)
    assert store.batch_state(batches[0])["state"] == "queued"
    assert not store.seal_batch(batches[0])


def test_a_sealed_producer_batch_cannot_accept_more_members(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENVIKING_PUBLICATION_CHUNK_MEMBERS", "2")
    store = PublicationStore(str(tmp_path / "db"))
    batch = TextPublicationBatch()
    for index in range(2):
        make_member(store, batch.join(store, "acct", "ns"), f"viking://resources/manual/{index}.md")
    batch.finish(True)
    with pytest.raises(RuntimeError):
        batch.join(store, "acct", "ns")


def test_partial_chunk_is_flushed_once_it_ages_out(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENVIKING_PUBLICATION_CHUNK_MEMBERS", "0")
    monkeypatch.setenv("OPENVIKING_PUBLICATION_CHUNK_SECONDS", "0.05")
    assert publication_chunk_seconds() == 0.05
    store = PublicationStore(str(tmp_path / "db"))
    batch = TextPublicationBatch()
    first = batch.join(store, "acct", "ns")
    make_member(store, first, A)
    assert store.batch_state(first)["state"] == "building"
    time.sleep(0.08)
    second = batch.join(store, "acct", "ns")
    assert second != first
    assert store.batch_state(first)["state"] == "queued"


def test_chunk_seconds_defaults_to_five_minutes(monkeypatch):
    monkeypatch.delenv("OPENVIKING_PUBLICATION_CHUNK_SECONDS", raising=False)
    assert publication_chunk_seconds() == 300.0
    monkeypatch.setenv("OPENVIKING_PUBLICATION_CHUNK_SECONDS", "0")
    assert publication_chunk_seconds() == 0.0
    monkeypatch.setenv("OPENVIKING_PUBLICATION_CHUNK_SECONDS", "junk")
    assert publication_chunk_seconds() == 300.0


def test_concurrent_joins_share_one_batch_id(tmp_path):
    """Producers join from worker threads; a racing member used to get ``None``.

    ``asyncio.to_thread`` reaches ``join`` from several threads at once. The
    first caller creates the batch id inside a slow SQLite write, and a second
    caller that observed ``registry`` already set but ``id`` still ``None``
    joined with ``None``: that member left the batch, so it published on its own
    while its siblings were still building -- the atomicity the batch exists to
    provide. Observed as an intermittent early publish in
    ``test_actual_uri_reindex_batches_two_source_files``.
    """
    import threading

    store = PublicationStore(str(tmp_path / "db"))
    batch = TextPublicationBatch()
    real_begin = store.begin_batch

    def slow_begin(account, namespace):
        time.sleep(0.05)
        return real_begin(account, namespace)

    store.begin_batch = slow_begin
    joined = []

    def worker():
        joined.append(batch.join(store, "acct", "ns"))

    first = threading.Thread(target=worker)
    first.start()
    time.sleep(0.01)
    second = threading.Thread(target=worker)
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)

    assert len(joined) == 2
    assert None not in joined, "a racing member joined with no batch"
    assert len(set(joined)) == 1
    assert batch.members == 2


def test_member_outliving_a_sealed_chunk_publishes_in_a_fresh_batch(tmp_path):
    """A producer in flight when its chunk seals must not lose its member.

    Observed live: 20 "Failed chunk reindex ... Publication batch no longer
    accepts members" records on one image, whose batch had already rolled over.
    """
    store = PublicationStore(str(tmp_path / "db"))
    batch = TextPublicationBatch()
    with bind_text_batch(batch):
        first = begin_batched(store, "acct", "ns", A, "new", ["x"])
        sealed = batch.id
        store.seal_batch(sealed)  # the chunk rolled over while we were working
        second = begin_batched(store, "acct", "ns", B, "new", ["y"])

    assert second != first
    assert batch.id != sealed, "the member must continue in a fresh chunk"
    assert store.batch_state(sealed)["state"] != "building"
    assert store.batch_state(batch.id)["state"] == "building"

    # The late member really is a member of the fresh chunk: sealing it leaves
    # the chunk waiting for its receipts like any other sealed member.
    store.seal(second)
    assert store.state(second)["state"] == "queued"
    assert store.batch_state(batch.id)["state"] == "building"

