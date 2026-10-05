"""Actual L0/L1 worker writes obey the shared text publication barrier."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding
from test_publication import view

from openviking.indexing.batch import text_publication_batch
from openviking.storage.vector_ids import publication_member_id, vector_record_id_for_data
from openviking.utils.embedding_utils import vectorize_directory_meta, vectorize_file

ROOT = "viking://resources/manual"
URI = ROOT + "/chapter.md"


async def finish(e, messages):
    for msg in messages:
        result = await e.handler.on_dequeue({"data": msg.to_json()})
        assert result.outcome.value == "success", result.error


async def directory(e, label, **kwargs):
    e.queue.messages.clear()
    for suffix, body, include in (
        (".abstract.md", label + " abstract", kwargs.get("include_abstract", True)),
        (".overview.md", label + " overview", kwargs.get("include_overview", True)),
    ):
        if include:
            await e.fs.write(ROOT + "/" + suffix, body, ctx=e.ctx)
    await vectorize_directory_meta(
        ROOT, label + " abstract", label + " overview", ctx=e.ctx, **kwargs
    )
    return list(e.queue.messages)


async def test_directory_levels_and_text_wait_for_all_then_publish(environment):
    e = environment
    await finish(e, await directory(e, "old"))
    old = view(e.store)[ROOT]["generation"]
    prior_ids = set(e.handler._vikingdb.records)

    @text_publication_batch
    async def produce():
        semantic = await directory(e, "new")
        await finish(e, semantic)
        assert view(e.store)[ROOT]["generation"] == old
        await e.fs._async_agfs.write(e.fs._uri_to_path(URI, ctx=e.ctx), b"new chapter text")
        e.queue.messages.clear()
        await vectorize_file(URI, {"name": "chapter.md"}, ROOT, ctx=e.ctx)
        return semantic, list(e.queue.messages)

    semantic, children = await produce()
    assert view(e.store)[ROOT]["generation"] == old
    await finish(e, children)
    current = view(e.store)
    assert current[ROOT]["generation"] == semantic[0].context_data["index_generation"]
    assert current[URI]["generation"] == children[0].context_data["index_generation"]
    assert await e.fs.read_file(URI, ctx=e.ctx) == "new chapter text"
    assert prior_ids.issubset(e.handler._vikingdb.records)
    assert all(vector_record_id_for_data(m.context_data) not in prior_ids for m in semantic)
    assert len({vector_record_id_for_data(m.context_data) for m in semantic}) == 2
    assert [publication_member_id(m.context_data) for m in semantic] == ["semantic:0", "semantic:1"]


async def test_only_one_directory_receipt_never_publishes(environment):
    e = environment
    old = await directory(e, "old")
    await finish(e, old)
    old_generation = view(e.store)[ROOT]["generation"]
    new = await directory(e, "new")
    await finish(e, new[:1])
    assert view(e.store)[ROOT]["generation"] == old_generation
    await e.handler.on_cancelled({"data": new[1].to_json()})
    assert e.store.state(new[0].context_data["index_generation"])["state"] == "failed"
    assert view(e.store)[ROOT]["generation"] == old_generation


async def test_partial_level_update_rebuilds_other_existing_level(environment, monkeypatch):
    e = environment
    fs = e.fs
    monkeypatch.setattr("openviking.utils.embedding_utils.get_viking_fs", lambda: fs)
    fs.exists = AsyncMock(return_value=True)
    await fs._async_agfs.write(
        fs._uri_to_path(ROOT + "/.overview.md", ctx=e.ctx), b"retained overview"
    )
    messages = await directory(e, "new", include_overview=False)
    assert [m.context_data["level"] for m in messages] == [0, 1]
    assert messages[1].context_data["abstract"] == "retained overview"
    await finish(e, messages)
    assert view(e.store)[ROOT]["generation"] == messages[1].context_data["index_generation"]


async def test_actual_uri_reindex_includes_directory_generation(
    environment, embedding, monkeypatch
):
    from openviking.service import reindex_executor as reindex

    e = environment
    await e.fs._async_agfs.write(e.fs._uri_to_path(URI, ctx=e.ctx), b"chapter")
    monkeypatch.setattr(
        reindex, "get_openviking_config", lambda: SimpleNamespace(embedding=embedding)
    )
    executor = reindex.ReindexExecutor()
    await e.fs.write(ROOT + "/.abstract.md", "abstract", ctx=e.ctx)
    await e.fs.write(ROOT + "/.overview.md", "overview", ctx=e.ctx)
    executor._read_directory_abstract = AsyncMock(return_value="abstract")
    executor._read_directory_overview = AsyncMock(return_value="overview")
    executor._best_file_summary = AsyncMock(return_value="summary")
    executor._chunk_reindex_ingest_options = AsyncMock(return_value=None)
    executor._effective_file_vectorization_concurrency = lambda: 1
    counters = reindex._ReindexCounters()
    await executor._reindex_resource_vectors_from_entries(
        root_uri=ROOT, directories=[ROOT], files=[URI], counters=counters, ctx=e.ctx
    )
    assert counters.failed_records == 0, counters.warnings
    assert counters.rebuilt_records == 3
    messages = e.queue.messages
    assert {m.context_data["level"] for m in messages} == {0, 1, 2}
    await finish(e, messages[:2])
    assert not view(e.store)
    await finish(e, messages[2:])
    assert set(view(e.store)) == {ROOT, URI}


@pytest.mark.parametrize("level", [2, 3])
def test_nonchunk_directory_member_rejects_invalid_level(level):
    with pytest.raises(ValueError, match="directory"):
        publication_member_id({"level": level})


@pytest.mark.parametrize("outcome", ["failed", "requeued"])
async def test_reindex_cannot_continue_after_semantic_failure(environment, monkeypatch, outcome):
    from openviking.service import reindex_executor as reindex
    from openviking.storage.queuefs.process_result import ProcessResult
    from openviking_cli.exceptions import OpenVikingError

    result = ProcessResult.failed("VLM failed") if outcome == "failed" else ProcessResult.requeued()
    processor = SimpleNamespace(on_dequeue=AsyncMock(return_value=result))
    monkeypatch.setattr(reindex, "SemanticProcessor", lambda **kwargs: processor)
    monkeypatch.setattr(
        reindex,
        "get_openviking_config",
        lambda: SimpleNamespace(vlm=SimpleNamespace(max_concurrent=1)),
    )
    with pytest.raises(OpenVikingError):
        await reindex.ReindexExecutor()._run_semantic_processor(
            uri=ROOT, context_type="resource", ctx=environment.ctx
        )


async def test_managed_dag_does_not_swallow_directory_generation_error(environment, monkeypatch):
    from openviking.storage.queuefs.semantic_dag import DirNode, SemanticDagExecutor

    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_dag.get_viking_fs", lambda: environment.fs
    )
    processor = SimpleNamespace(
        _generate_overview=AsyncMock(side_effect=RuntimeError("VLM unavailable"))
    )
    executor = SemanticDagExecutor(processor, "resource", 1, environment.ctx)
    executor._nodes[ROOT] = DirNode(
        uri=ROOT,
        children_dirs=[],
        file_paths=[],
        file_index={},
        child_index={},
        file_summaries=[],
        children_abstracts=[],
        pending=0,
    )
    executor._finalize_file_summaries = lambda node: []
    executor._finalize_children_abstracts = AsyncMock(return_value=[])
    with pytest.raises(RuntimeError, match="VLM unavailable"):
        await executor._overview_task(ROOT)


async def test_single_marker_reindex_does_not_bypass_directory_generation(environment, monkeypatch):
    from openviking.core.context import ContextLevel
    from openviking.service import reindex_executor as reindex

    e = environment
    e.fs._async_agfs.pathlock_acquire_exact = AsyncMock(return_value={"lock": "marker"})
    e.fs._async_agfs.pathlock_release = AsyncMock()
    monkeypatch.setattr(reindex, "get_viking_fs", lambda: e.fs)
    executor = reindex.ReindexExecutor()
    await e.fs.write(ROOT + "/.abstract.md", "abstract", ctx=e.ctx)
    await e.fs.write(ROOT + "/.overview.md", "overview", ctx=e.ctx)
    executor._read_directory_abstract = AsyncMock(return_value="abstract")
    executor._read_directory_overview = AsyncMock(return_value="overview")
    executor._upsert_context = AsyncMock()
    await executor.reindex_directory_marker(dir_uri=ROOT, level=ContextLevel.ABSTRACT, ctx=e.ctx)
    executor._upsert_context.assert_not_awaited()
    assert [m.context_data["level"] for m in e.queue.messages] == [0, 1]
    assert all(m.context_data["index_generation"] for m in e.queue.messages)
    await finish(e, e.queue.messages)
    assert ROOT in view(e.store)
    e.fs._async_agfs.pathlock_release.assert_awaited_once()
