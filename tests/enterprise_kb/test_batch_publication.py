"""Multi-source text batches use SQL barriers, including real queue consumers."""

import asyncio
from types import SimpleNamespace

import pytest
from test_content_publication import environment as environment
from test_index_plan import SOURCE, Database, Queue
from test_index_plan import embedding as embedding
from test_publication import publish, view
from test_publication import runtime as runtime

from openviking.indexing.batch import (
    TextPublicationBatch,
    bind_text_batch,
    current_batch_id,
    text_publication_batch,
)
from openviking.indexing.enqueue import enqueue_text_plan
from openviking.indexing.publication import PublicationStore
from openviking.storage.collection_schemas import TextEmbeddingHandler
from openviking.storage.queuefs.embedding_msg import EmbeddingMsg
from openviking.storage.queuefs.process_result import ProcessResult

A = "viking://resources/manual/a.md"
B = "viking://resources/manual/b.md"


def member(store, batch, uri):
    generation = store.begin("acct", "ns", uri, "new", ["x"], batch=batch)
    store.seal(generation)
    return generation


def test_complete_early_chapter_waits_for_producer_and_all_chapters(tmp_path):
    store = PublicationStore(str(tmp_path / "db"))
    old = {uri: publish(store, uri) for uri in (A, B)}
    batch = store.begin_batch("acct", "ns")
    a = member(store, batch, A)
    assert not store.acknowledge(a, "x")
    assert view(store)[A]["generation"] == old[A]
    b = member(store, batch, B)
    assert not store.seal_batch(batch)
    assert view(store)[A]["generation"] == old[A]
    assert store.acknowledge(b, "x")
    assert {uri: view(store)[uri]["generation"] for uri in (A, B)} == {A: a, B: b}
    assert store.batch_state(batch)["state"] == "published"
    assert PublicationStore(store.path).batch_state(batch)["state"] == "published"


@pytest.mark.parametrize("failure", ["worker", "superseded", "edited"])
def test_any_member_invalidates_batch_and_keeps_other_old_chapters(tmp_path, failure):
    store = PublicationStore(str(tmp_path / "db"))
    old = {uri: publish(store, uri) for uri in (A, B)}
    batch = store.begin_batch("acct", "ns")
    a = member(store, batch, A)
    if failure == "edited":
        epoch = store.observe_draft("acct", B)
        b = store.begin(
            "acct",
            "ns",
            B,
            "new",
            ["x"],
            source_ref={"sha256": "a" * 64, "size": 5, "draft_epoch": epoch},
            batch=batch,
        )
        store.seal(b)
    else:
        b = member(store, batch, B)
    store.acknowledge(a, "x")
    store.seal_batch(batch)
    if failure == "worker":
        store.fail(b, "embedding timeout")
    elif failure == "superseded":
        store.begin("acct", "ns", B, "newer", ["y"])
    else:
        store.invalidate_draft("acct", B)
    assert not store.acknowledge(b, "x")
    assert store.batch_state(batch)["state"] == "failed"
    assert {uri: view(store)[uri]["generation"] for uri in (A, B)} == old
    assert store.state(a)["state"] == "failed"


def test_batch_rejects_duplicate_sources_and_scope_mismatch(tmp_path):
    store = PublicationStore(str(tmp_path / "db"))
    batch = store.begin_batch("acct", "ns")
    member(store, batch, A)
    with pytest.raises(ValueError, match="Duplicate"):
        member(store, batch, A)
    with pytest.raises(ValueError, match="account/namespace"):
        store.begin("other", "ns", B, "new", ["x"], batch=batch)
    store.seal_batch(batch)
    with pytest.raises(RuntimeError, match="no longer"):
        member(store, batch, B)


async def test_real_consumers_complete_during_producer_but_cannot_publish(runtime, embedding):
    queue = Queue()
    handler = TextEmbeddingHandler(Database())
    old = {uri: publish(runtime, uri, namespace="resources") for uri in (A, B)}
    generations = {}

    @text_publication_batch
    async def produce():
        for uri in (A, B):
            queue.messages.clear()
            msg = EmbeddingMsg(
                SOURCE,
                {
                    "uri": uri,
                    "account_id": "acct",
                    "context_type": "resource",
                    "level": 2,
                    "is_leaf": True,
                },
            )
            await enqueue_text_plan(msg, SOURCE, embedding, queue)
            generations[uri] = queue.messages[0].context_data["index_generation"]
            for child in queue.messages:
                result = await handler.on_dequeue({"data": child.to_json()})
                assert result.outcome.value == "success", result.error
            assert {u: view(runtime)[u]["generation"] for u in (A, B)} == old

    await produce()
    assert {u: view(runtime)[u]["generation"] for u in (A, B)} == generations


@pytest.mark.parametrize("failure", ["counter", "retry", "cancel"])
async def test_producer_failures_preserve_batch(runtime, embedding, failure):
    queue = Queue()
    counters = SimpleNamespace(failed_records=0)

    @text_publication_batch
    async def produce(*, counters):
        msg = EmbeddingMsg(SOURCE, {"uri": A, "account_id": "acct"})
        await enqueue_text_plan(msg, SOURCE, embedding, queue)
        if failure == "counter":
            counters.failed_records += 1
        elif failure == "retry":
            return ProcessResult.requeued()
        else:
            raise asyncio.CancelledError()

    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await produce(counters=counters)
    else:
        await produce(counters=counters)
    generation = queue.messages[0].context_data["index_generation"]
    assert runtime.state(generation)["state"] == "failed"
    assert not view(runtime)


async def test_shared_dag_worker_binds_each_executors_batch(runtime, monkeypatch):
    from openviking.storage.queuefs.semantic_dag import SemanticDagExecutor

    # Execute successive DAG nodes in the same worker context. Its inherited
    # batch must not leak to a later executor, even if the worker was reused.
    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_dag.get_viking_fs", lambda: SimpleNamespace()
    )
    outer = TextPublicationBatch()
    batches = [TextPublicationBatch(), TextPublicationBatch()]
    ids = []
    with bind_text_batch(outer):
        for batch in batches:
            with bind_text_batch(batch):
                executor = SemanticDagExecutor(
                    processor=None, context_type="resource", max_concurrent_llm=1, ctx=None
                )

            async def work(_):
                ids.append(current_batch_id(runtime, "acct", "ns"))

            executor._run_work_bound = work
            await executor._run_work(None)
        assert current_batch_id(runtime, "acct", "ns") not in ids
    assert len(set(ids)) == 2


@pytest.mark.parametrize("invalid_second", [False, True])
async def test_actual_uri_reindex_batches_two_source_files(
    environment, embedding, monkeypatch, invalid_second
):
    from unittest.mock import AsyncMock

    from openviking.service import reindex_executor as reindex
    from openviking.utils.embedding_utils import vectorize_file

    e = environment
    old = {}
    for uri in (A, B):
        await e.fs._async_agfs.write(e.fs._uri_to_path(uri, ctx=e.ctx), b"old chapter\n")
        e.queue.messages.clear()
        await vectorize_file(uri, {"name": "chapter.md"}, "viking://resources/manual", ctx=e.ctx)
        for msg in e.queue.messages:
            assert (await e.handler.on_dequeue({"data": msg.to_json()})).outcome.value == "success"
        old[uri] = "old chapter\n"
    e.queue.messages.clear()
    for uri in (A, B):
        source = SOURCE
        if uri == B and invalid_second:
            source = "| " + "oversized_header_" * 100 + " |\n| --- |\n| value |\n"
        await e.fs._async_agfs.write(e.fs._uri_to_path(uri, ctx=e.ctx), source.encode())
    monkeypatch.setattr(
        reindex, "get_openviking_config", lambda: SimpleNamespace(embedding=embedding)
    )
    executor = reindex.ReindexExecutor()
    executor._best_file_summary = AsyncMock(return_value="summary")
    executor._chunk_reindex_ingest_options = AsyncMock(return_value=None)
    executor._effective_file_vectorization_concurrency = lambda: 2
    counters = reindex._ReindexCounters()
    await executor._reindex_resource_vectors_from_entries(
        root_uri="viking://resources/manual",
        directories=[],
        files=[A, B],
        counters=counters,
        ctx=e.ctx,
    )
    assert counters.failed_records == int(invalid_second), counters.warnings
    assert {uri: await e.fs.read_file(uri, ctx=e.ctx) for uri in (A, B)} == old
    if invalid_second:
        generation = e.queue.messages[0].context_data["index_generation"]
        assert e.store.state(generation)["state"] == "failed"
        return
    first = [msg for msg in e.queue.messages if msg.context_data["uri"] == A]
    second = [msg for msg in e.queue.messages if msg.context_data["uri"] == B]
    for msg in first:
        assert (await e.handler.on_dequeue({"data": msg.to_json()})).outcome.value == "success"
    assert {uri: await e.fs.read_file(uri, ctx=e.ctx) for uri in (A, B)} == old
    for msg in second:
        assert (await e.handler.on_dequeue({"data": msg.to_json()})).outcome.value == "success"
    assert {uri: await e.fs.read_file(uri, ctx=e.ctx) for uri in (A, B)} == {A: SOURCE, B: SOURCE}
