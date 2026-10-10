"""Image vectors/assets use the same SQL barriers as text and navigation."""

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_content_publication import environment as environment
from test_image_context import JointEmbedder
from test_index_plan import embedding as embedding

from openviking.indexing.batch import TextPublicationBatch, bind_text_batch
from openviking.indexing.content import blob_path, draft_sources
from openviking.indexing.publication import query_snapshot
from openviking.utils.embedding_utils import vectorize_directory_meta, vectorize_file

ROOT = "viking://resources/manual"
IMAGE = ROOT + "/images/a.png"
TEXT = ROOT + "/chapter.md"


@pytest.fixture
def image_env(environment, embedding, monkeypatch):
    e = environment
    model = JointEmbedder()
    monkeypatch.setattr(type(embedding), "get_embedder", lambda self: model)
    e.handler._embedder = model
    return e


async def stage(e, asset=b"original", summary="old image"):
    await e.fs.write(IMAGE, asset, ctx=e.ctx)
    count = len(e.queue.messages)
    assert await vectorize_file(
        IMAGE, {"name": "a.png", "summary": summary}, ROOT + "/images", ctx=e.ctx
    )
    return e.queue.messages[count:]


async def consume(e, messages):
    for msg in messages:
        result = await e.handler.on_dequeue({"data": msg.to_json()})
        assert result.outcome.value == "success", result.error


def manifest(e):
    lease, current = e.store.capture("acct")
    e.store.release(lease)
    return current


async def test_asset_and_vector_generation_publish_together(image_env):
    e = image_env
    first = await stage(e)
    await consume(e, first)
    old = manifest(e)[IMAGE]["generation"]
    newer = await stage(e, b"new asset", "new image")
    assert newer[0].context_data["index_generation"] != old
    assert manifest(e)[IMAGE]["generation"] == old
    assert await e.fs.read_file_bytes(IMAGE, ctx=e.ctx) == b"original"
    with draft_sources():
        assert await e.fs.read_file_bytes(IMAGE, ctx=e.ctx) == b"new asset"
    assert base64.b64decode(newer[0].message[1]["image_url"]["url"].split(",", 1)[1]) == b"new asset"
    await consume(e, newer)
    assert await e.fs.read_file_bytes(IMAGE, ctx=e.ctx) == b"new asset"
    assert len(e.handler._vikingdb.records) == 2
    assert {r["content"] for r in e.handler._vikingdb.records.values()} == {"old image", "new image"}


async def test_text_navigation_and_image_wait_for_all_receipts(image_env):
    e = image_env
    await consume(e, await stage(e))
    old = manifest(e)[IMAGE]["generation"]
    e.queue.messages.clear()
    batch = TextPublicationBatch()
    with bind_text_batch(batch):
        images = await stage(e, b"next", "next summary")
        await e.fs.write(TEXT, "# chapter\n\nnew text", ctx=e.ctx)
        await vectorize_file(TEXT, {"name": "chapter.md", "summary": "text"}, ROOT, ctx=e.ctx)
        await e.fs.write(ROOT + "/.abstract.md", "new abstract", ctx=e.ctx)
        await e.fs.write(ROOT + "/.overview.md", "new overview", ctx=e.ctx)
        await vectorize_directory_meta(ROOT, "new abstract", "new overview", ctx=e.ctx)
        await consume(e, images)
        assert manifest(e)[IMAGE]["generation"] == old
        batch.finish(True)
    remaining = [m for m in e.queue.messages if m not in images]
    await consume(e, remaining[:-1])
    assert manifest(e)[IMAGE]["generation"] == old
    await consume(e, remaining[-1:])
    current = manifest(e)
    assert current[IMAGE]["generation"] == images[0].context_data["index_generation"]
    assert {ROOT, TEXT, IMAGE} <= set(current)
    assert e.store.batch_state(batch.id)["state"] == "published"


@pytest.mark.parametrize("failure", ["edited", "worker", "newer"])
async def test_failed_image_keeps_old_asset_while_text_publishes(image_env, failure):
    e = image_env
    await consume(e, await stage(e))
    old = manifest(e)[IMAGE]["generation"]
    e.queue.messages.clear()
    batch = TextPublicationBatch()
    with bind_text_batch(batch):
        messages = await stage(e, b"draft", "draft summary")
        await e.fs.write(TEXT, "body awaiting image", ctx=e.ctx)
        await vectorize_file(TEXT, {"name": "chapter.md", "summary": "text"}, ROOT, ctx=e.ctx)
        batch.finish(True)
    await consume(e, [m for m in e.queue.messages if m not in messages])
    generation = messages[0].context_data["index_generation"]
    if failure == "edited":
        await e.fs.write(IMAGE, b"concurrent edit", ctx=e.ctx)
        result = await e.handler.on_dequeue({"data": messages[0].to_json()})
        assert result.outcome.value == "failed"
    elif failure == "worker":
        e.handler._embedder = SimpleNamespace(supports_multimodal=False)
        result = await e.handler.on_dequeue({"data": messages[0].to_json()})
        assert result.outcome.value == "failed"
    else:
        await stage(e, b"newer attempt", "newer summary")
        result = await e.handler.on_dequeue({"data": messages[0].to_json()})
        assert result.outcome.value == "failed"
    # The failed image keeps its old asset and generation; the healthy text
    # member of the same batch still publishes.
    assert manifest(e)[IMAGE]["generation"] == old
    assert TEXT in manifest(e)
    assert e.store.batch_state(batch.id)["state"] == "published"
    assert e.store.state(generation)["state"] == "failed"


async def test_held_query_reads_old_asset_and_gc_protects_it(image_env):
    e = image_env
    await consume(e, await stage(e))
    old_ref = manifest(e)[IMAGE]["content"]
    @query_snapshot
    async def held(*, ctx):
        newer = await stage(e, b"new", "new summary")
        await consume(e, newer)
        assert await e.fs.read_file_bytes(IMAGE, ctx=e.ctx) == b"original"
        assert ("acct", old_ref["sha256"]) in e.store.protected_content_revisions()
    await held(ctx=e.ctx)
    assert await e.fs.read_file_bytes(IMAGE, ctx=e.ctx) == b"new"
    assert ("acct", old_ref["sha256"]) not in e.store.protected_content_revisions()


async def test_corrupt_asset_does_not_fall_back_to_working_copy(image_env):
    e = image_env
    await consume(e, await stage(e))
    ref = manifest(e)[IMAGE]["content"]
    await e.fs._async_agfs.write(blob_path(e.fs, e.ctx, ref["sha256"]), b"corrupt")
    with pytest.raises(RuntimeError, match="corrupted"):
        await e.fs.read_file_bytes(IMAGE, ctx=e.ctx)


async def test_rejected_image_enqueue_aborts_batch(image_env):
    e = image_env
    await consume(e, await stage(e))
    old = manifest(e)[IMAGE]["generation"]
    batch = TextPublicationBatch()
    e.queue.enqueue = AsyncMock(return_value=None)
    with bind_text_batch(batch), pytest.raises(RuntimeError, match="enqueue failed"):
        await stage(e, b"new", "new summary")
    assert e.store.batch_state(batch.id)["state"] == "failed"
    assert manifest(e)[IMAGE]["generation"] == old


async def test_occurrences_have_distinct_joint_vectors_and_one_asset_barrier(image_env):
    import hashlib
    from openviking.retrieve.hierarchical_retriever import HierarchicalRetriever
    e = image_env
    await e.fs.write(IMAGE, b"shared image", ctx=e.ctx)
    occurrences = [
        {"summary": "阀芯用途", "context_uri": TEXT, "context_hash": "A", "context": "chapter A", "asset_sha256": hashlib.sha256(b"shared image").hexdigest()},
        {"summary": "电路用途", "context_uri": ROOT + "/other.md", "context_hash": "B", "context": "chapter B", "asset_sha256": hashlib.sha256(b"shared image").hexdigest()},
    ]
    await vectorize_file(IMAGE, {"name": "a.png", "summary": "combined", "image_occurrences": occurrences}, ROOT + "/images", ctx=e.ctx)
    messages = e.queue.messages
    assert len(messages) == 2
    assert len({m.context_data["chunk_id"] for m in messages}) == 2
    assert len({m.context_data["index_generation"] for m in messages}) == 1
    assert [m.message[0]["text"] for m in messages] == ["阀芯用途", "电路用途"]
    await consume(e, messages[:1])
    assert IMAGE not in manifest(e)
    await consume(e, messages[1:])
    rows = list(e.handler._vikingdb.records.values())
    assert {r["content"] for r in rows} == {"阀芯用途", "电路用途"}
    assert {r["image_context_uri"] for r in rows} == {TEXT, ROOT + "/other.md"}
    retriever = HierarchicalRetriever.__new__(HierarchicalRetriever)
    retriever.hotness_alpha = 0
    found = await retriever._convert_to_matched_contexts(rows, e.ctx)
    assert len(found) == 1 and found[0].uri == IMAGE
    assert len(found[0].matches) == 2
    assert {m["image_context_uri"] for m in found[0].matches} == {TEXT, ROOT + "/other.md"}


async def test_image_edit_after_vlm_is_rejected_before_queue(image_env):
    import hashlib
    e = image_env
    await e.fs.write(IMAGE, b"new image", ctx=e.ctx)
    summary = {"name": "a.png", "summary": "old semantics", "image_occurrences": [{"summary": "old semantics", "asset_sha256": hashlib.sha256(b"old image").hexdigest()}]}
    with pytest.raises(ValueError, match="changed after"):
        await vectorize_file(IMAGE, summary, ROOT + "/images", ctx=e.ctx)
    assert not e.queue.messages


async def test_one_occurrence_failure_does_not_publish_other_occurrences(image_env):
    e = image_env
    await consume(e, await stage(e))
    old = manifest(e)[IMAGE]["generation"]
    await e.fs.write(IMAGE, b"next", ctx=e.ctx)
    await vectorize_file(IMAGE, {"name": "a.png", "summary": "combined", "image_occurrences": [{"summary": "A"}, {"summary": "B"}]}, ROOT + "/images", ctx=e.ctx)
    messages = e.queue.messages[-2:]
    await consume(e, messages[:1])
    e.handler._embedder = SimpleNamespace(supports_multimodal=False)
    result = await e.handler.on_dequeue({"data": messages[1].to_json()})
    assert result.outcome.value == "failed"
    assert manifest(e)[IMAGE]["generation"] == old
    assert await e.fs.read_file_bytes(IMAGE, ctx=e.ctx) == b"original"


async def test_uri_semantic_handoff_and_shared_occurrence_plan(image_env, embedding, monkeypatch):
    from openviking.service import reindex_executor as reindex
    e = image_env
    await e.fs.write(IMAGE, b"asset", ctx=e.ctx)
    details = {"name": "a.png", "summary": "aggregate", "image_occurrences": [{"summary": "A", "context_uri": TEXT, "context_hash": "A"}, {"summary": "B", "context_uri": TEXT, "context_hash": "B"}]}
    await vectorize_file(IMAGE, details, ROOT + "/images", ctx=e.ctx)
    first = e.queue.messages[:]
    await consume(e, first)
    monkeypatch.setattr(reindex, "get_viking_fs", lambda: e.fs)
    monkeypatch.setattr(reindex, "get_openviking_config", lambda: SimpleNamespace(embedding=embedding))
    executor = reindex.ReindexExecutor()
    executor._effective_file_vectorization_concurrency = lambda: 1
    executor._best_file_summary = AsyncMock(return_value="aggregate")
    executor._chunk_reindex_ingest_options = AsyncMock(return_value=None)
    e.queue.messages.clear()
    counts = reindex._ReindexCounters()
    await executor._reindex_resource_vectors_from_entries(root_uri=ROOT, directories=[], files=[IMAGE], counters=counts, ctx=e.ctx, image_summaries={IMAGE: details})
    assert not counts.failed_records, counts.warnings
    assert {m.context_data["chunk_id"] for m in e.queue.messages} == {m.context_data["chunk_id"] for m in first}
    assert e.queue.messages[0].context_data["index_generation"] != first[0].context_data["index_generation"]
    executor._run_semantic_processor = AsyncMock(return_value={"image_summaries": {IMAGE: details}})
    executor._reindex_resource_vectors = AsyncMock()
    run = SimpleNamespace(counters=counts, ctx=e.ctx, lock=None, ingest_options=None)
    await executor._reindex_resource(uri=ROOT, mode="semantic_and_vectors", run=run)
    assert executor._reindex_resource_vectors.await_args.kwargs["image_summaries"] == {IMAGE: details}


async def test_vectors_only_reuses_published_occurrences(image_env, embedding, monkeypatch):
    from openviking.service import reindex_executor as reindex
    e = image_env
    await consume(e, await stage(e))
    old = manifest(e)[IMAGE]["generation"]
    monkeypatch.setattr(reindex, "get_viking_fs", lambda: e.fs)
    monkeypatch.setattr(reindex, "get_openviking_config", lambda: SimpleNamespace(embedding=embedding))
    executor = reindex.ReindexExecutor()
    executor._effective_file_vectorization_concurrency = lambda: 1
    executor._best_file_summary = AsyncMock(return_value="aggregate")
    executor._chunk_reindex_ingest_options = AsyncMock(return_value=None)
    e.fs.ls = AsyncMock(return_value=[])
    counts = reindex._ReindexCounters()
    e.queue.messages.clear()
    await executor._reindex_resource_vectors_from_entries(root_uri=ROOT, directories=[], files=[IMAGE], counters=counts, ctx=e.ctx)
    assert counts.failed_records == 0, counts.warnings
    assert len(e.queue.messages) == 1
    assert e.queue.messages[0].context_data["abstract"] == "old image"
    assert manifest(e)[IMAGE]["generation"] == old


async def test_incremental_semantic_image_rebuild_keeps_occurrences(image_env, monkeypatch):
    from openviking.storage.queuefs import semantic_dag as dag
    e = image_env
    monkeypatch.setattr(dag, "get_viking_fs", lambda: e.fs)
    summary = {"name": "a.png", "summary": "A + B", "image_occurrences": [{"summary": "A"}, {"summary": "B"}]}
    processor = SimpleNamespace(_generate_single_file_summary=AsyncMock(return_value=summary))
    executor = dag.SemanticDagExecutor(processor, "resource", 1, e.ctx, incremental_update=True, skip_vectorization=True)
    executor._check_file_content_changed = AsyncMock(return_value=False)
    executor._read_existing_summary = AsyncMock(return_value={"name": "a.png", "summary": "legacy aggregate"})
    executor._on_file_done = AsyncMock()
    await executor._file_summary_task(ROOT + "/images", IMAGE)
    assert executor.image_summaries == {IMAGE: summary}
    processor._generate_single_file_summary.assert_awaited_once()
    executor._read_existing_summary.assert_not_awaited()
