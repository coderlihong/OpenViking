"""Private sidecar versions and exact public URI/semantic reads share snapshots."""

import hashlib
from types import SimpleNamespace

import pytest
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding
from test_semantic_publication import ROOT, directory, finish

from openviking.indexing.content import blob_path, draft_sources
from openviking.indexing.publication import query_snapshot
from openviking.utils.embedding_utils import vectorize_directory_meta


async def test_sidecar_and_navigation_reads_wait_for_both_receipts(environment):
    e = environment
    await finish(e, await directory(e, "old"))
    messages = await directory(e, "new")
    assert await e.fs.read_file(ROOT + "/.abstract.md", ctx=e.ctx) == "old abstract"
    assert await e.fs.abstract(ROOT, ctx=e.ctx) == "old abstract"
    assert await e.fs.overview(ROOT, ctx=e.ctx) == "old overview"
    with draft_sources():
        assert await e.fs.abstract(ROOT, ctx=e.ctx) == "new abstract"
    await finish(e, messages[:1])
    assert await e.fs.overview(ROOT, ctx=e.ctx) == "old overview"
    await finish(e, messages[1:])
    assert await e.fs.abstract(ROOT, ctx=e.ctx) == "new abstract"
    assert await e.fs.overview(ROOT, ctx=e.ctx) == "new overview"
    assert await e.fs.read_file_bytes(ROOT + "/.overview.md", ctx=e.ctx) == b"new overview"


async def test_query_reads_fixed_semantics_across_publication(environment):
    e = environment
    await finish(e, await directory(e, "old"))
    messages = await directory(e, "new")
    reader = SimpleNamespace()

    @query_snapshot
    async def query(self, ctx):
        before = await e.fs.abstract(ROOT, ctx=ctx)
        await finish(e, messages)
        after = await e.fs.overview(ROOT, ctx=ctx)
        return before, after

    assert await query(reader, e.ctx) == ("old abstract", "old overview")
    assert await e.fs.overview(ROOT, ctx=e.ctx) == "new overview"


async def test_edit_during_build_aborts_directory_but_keeps_old_readable(environment):
    e = environment
    await finish(e, await directory(e, "old"))
    messages = await directory(e, "new")
    await finish(e, messages[:1])
    await e.fs.write(ROOT + "/.overview.md", "edited overview", ctx=e.ctx)
    result = await e.handler.on_dequeue({"data": messages[1].to_json()})
    assert result.outcome.value == "failed"
    assert await e.fs.overview(ROOT, ctx=e.ctx) == "old overview"
    e.queue.messages.clear()
    await vectorize_directory_meta(ROOT, "new abstract", "edited overview", ctx=e.ctx)
    await finish(e, e.queue.messages)
    assert await e.fs.overview(ROOT, ctx=e.ctx) == "edited overview"


async def test_sidecar_changed_before_begin_is_rejected(environment, monkeypatch):
    from openviking.indexing import semantic

    e = environment
    original = semantic.enqueue_semantic_plan

    async def change(*args, **kwargs):
        await e.fs.write(ROOT + "/.abstract.md", "changed after archive", ctx=e.ctx)
        return await original(*args, **kwargs)

    monkeypatch.setattr(semantic, "enqueue_semantic_plan", change)
    with pytest.raises(RuntimeError, match="changed while preparing"):
        await directory(e, "new")
    assert not e.queue.messages


async def test_archive_requires_body_matching_vector_input(environment):
    e = environment
    await e.fs.write(ROOT + "/.abstract.md", "actual abstract", ctx=e.ctx)
    await e.fs.write(ROOT + "/.overview.md", "overview", ctx=e.ctx)
    with pytest.raises(RuntimeError, match="does not match"):
        await vectorize_directory_meta(ROOT, "wrong abstract", "overview", ctx=e.ctx)
    assert not e.queue.messages


async def test_corrupt_sidecar_asset_fails_without_working_body_fallback(environment):
    e = environment
    await finish(e, await directory(e, "old"))
    with e.store.source_reference("acct", ROOT + "/.abstract.md") as ref:
        await e.fs._async_agfs.write(blob_path(e.fs, e.ctx, ref["sha256"]), b"corrupt")
    with pytest.raises(RuntimeError, match="corrupted"):
        await e.fs.abstract(ROOT, ctx=e.ctx)


async def test_gc_roots_include_sidecar_active_reader_and_pending_assets(environment):
    e = environment
    await finish(e, await directory(e, "old"))
    lease, _ = e.store.capture("acct")
    messages = await directory(e, "new")
    old = ("acct", hashlib.sha256(b"old overview").hexdigest())
    new = ("acct", hashlib.sha256(b"new overview").hexdigest())
    assert {old, new} <= e.store.protected_content_revisions()
    await finish(e, messages)
    assert old in e.store.protected_content_revisions()
    e.store.release(lease)
    assert old not in e.store.protected_content_revisions()
    assert new in e.store.protected_content_revisions()


async def test_uri_reindex_reads_latest_working_sidecars(environment, monkeypatch):
    from openviking.service import reindex_executor as reindex

    e = environment
    await finish(e, await directory(e, "old"))
    await e.fs.write(ROOT + "/.abstract.md", "latest abstract", ctx=e.ctx)
    await e.fs.write(ROOT + "/.overview.md", "latest overview", ctx=e.ctx)
    monkeypatch.setattr(reindex, "get_viking_fs", lambda: e.fs)
    executor = reindex.ReindexExecutor()
    executor._effective_file_vectorization_concurrency = lambda: 1
    e.queue.messages.clear()
    counters = reindex._ReindexCounters()
    await executor._reindex_resource_vectors_from_entries(
        root_uri=ROOT, directories=[ROOT], files=[], counters=counters, ctx=e.ctx
    )
    assert counters.failed_records == 0, counters.warnings
    assert [m.context_data["abstract"] for m in e.queue.messages] == [
        "latest abstract",
        "latest overview",
    ]
    assert await e.fs.abstract(ROOT, ctx=e.ctx) == "old abstract"
    await finish(e, e.queue.messages)
    assert await e.fs.overview(ROOT, ctx=e.ctx) == "latest overview"


async def test_direct_directory_index_joins_semantics_and_text(environment, monkeypatch):
    from unittest.mock import AsyncMock

    from openviking.utils.embedding_utils import index_resource

    e = environment
    await finish(e, await directory(e, "old"))
    await e.fs.write(ROOT + "/.abstract.md", "latest abstract", ctx=e.ctx)
    await e.fs.write(ROOT + "/.overview.md", "latest overview", ctx=e.ctx)
    await e.fs.write(ROOT + "/chapter.md", "chapter body", ctx=e.ctx)
    e.fs.exists = AsyncMock(return_value=True)
    e.fs.ls = AsyncMock(return_value=[{"name": "chapter.md", "uri": ROOT + "/chapter.md"}])
    e.queue.messages.clear()
    await index_resource(ROOT, e.ctx)
    assert {m.context_data["level"] for m in e.queue.messages} == {0, 1, 2}
    await finish(e, e.queue.messages[:2])
    assert await e.fs.abstract(ROOT, ctx=e.ctx) == "old abstract"
    await finish(e, e.queue.messages[2:])
    assert await e.fs.abstract(ROOT, ctx=e.ctx) == "latest abstract"


async def test_sidecar_metadata_writer_reads_working_version(environment):
    from openviking.core.context import ContextLevel
    from openviking.storage.abstract_overview import (
        _read_existing_document,
        render_abstract_overview,
    )

    e = environment
    await finish(e, await directory(e, "old"))
    raw = render_abstract_overview(
        ContextLevel.ABSTRACT,
        ROOT,
        "working body",
        {"source": {"kind": "file", "uri": "viking://resources/latest.md"}},
    )
    await e.fs.write(ROOT + "/.abstract.md", raw, ctx=e.ctx)
    assert await e.fs.abstract(ROOT, ctx=e.ctx) == "old abstract"
    existing = await _read_existing_document(e.fs, ROOT + "/.abstract.md", e.ctx)
    assert existing.body == "working body\n"
    assert existing.metadata["source"]["uri"] == "viking://resources/latest.md"


async def test_unpublished_new_level_cannot_leak_into_old_directory_view(environment):
    from openviking_cli.exceptions import NotFoundError

    e = environment
    await finish(e, await directory(e, "old", include_overview=False))
    await e.fs.write(ROOT + "/.overview.md", "not published overview", ctx=e.ctx)
    with pytest.raises(NotFoundError):
        await e.fs.read_file(ROOT + "/.overview.md", ctx=e.ctx)
    assert "not published overview" not in await e.fs.overview(ROOT, ctx=e.ctx)


async def test_old_directory_without_sidecar_references_requires_rebuild(environment):
    e = environment
    await e.fs.write(ROOT + "/.abstract.md", "working abstract", ctx=e.ctx)
    generation = e.store.begin("acct", "resources", ROOT, "legacy-plan", ["semantic:0"])
    e.store.seal(generation)
    e.store.acknowledge(generation, "semantic:0")
    with pytest.raises(RuntimeError, match="rebuild"):
        await e.fs.abstract(ROOT, ctx=e.ctx)
    with draft_sources():
        assert await e.fs.abstract(ROOT, ctx=e.ctx) == "working abstract"
