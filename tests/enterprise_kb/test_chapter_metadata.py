# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Parser control records must not become retrieval knowledge."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from openviking.server.identity import RequestContext, Role
from openviking.service.reindex_executor import ReindexExecutor
from openviking.storage.queuefs.semantic_dag import SemanticDagExecutor
from openviking.utils.resource_processor import ResourceProcessor
from openviking_cli.session.user_id import UserIdentifier
from tests.storage.test_semantic_dag_skip_files import _FakeProcessor, _FakeVikingFS


async def test_semantic_pipeline_never_summarizes_or_embeds_chapter_metadata(monkeypatch):
    root = "viking://resources/manual"
    names = ["_toc.json", "_manifest.json", "toc.md", "index.md", "ordinary.json"]
    fs = _FakeVikingFS({root: [{"name": name, "isDir": False} for name in names]})
    monkeypatch.setattr("openviking.storage.queuefs.semantic_dag.get_viking_fs", lambda: fs)
    processor = _FakeProcessor()
    ctx = RequestContext(user=UserIdentifier("a", "u"), role=Role.USER)
    await SemanticDagExecutor(
        processor=processor, context_type="resource", max_concurrent_llm=2, ctx=ctx
    ).run(root)
    expected = {root + "/index.md", root + "/ordinary.json"}
    assert set(processor.summarized_files) == expected
    assert set(processor.vectorized_files) == expected


async def test_vectors_only_pipeline_skips_chapter_metadata(monkeypatch):
    root = "viking://resources/manual"
    fs = SimpleNamespace(
        tree=AsyncMock(
            return_value=[
                {"uri": root + "/" + name, "name": name, "isDir": False}
                for name in ["_toc.json", "_manifest.json", "toc.md", ".abstract.md", "chapter.md"]
            ]
        )
    )
    monkeypatch.setattr("openviking.utils.resource_processor.get_viking_fs", lambda: fs)
    monkeypatch.setattr(
        "openviking.utils.resource_processor.get_openviking_config",
        lambda: SimpleNamespace(
            queue_workers=SimpleNamespace(
                add_resource=SimpleNamespace(file_vectorization_concurrency=2)
            )
        ),
    )
    vectorize = AsyncMock()
    monkeypatch.setattr("openviking.utils.resource_processor.vectorize_file", vectorize)
    processor = ResourceProcessor(SimpleNamespace(get_embedder=lambda: None))
    ctx = RequestContext(user=UserIdentifier("a", "u"), role=Role.USER)
    await processor._vectorize_resource_files(root, ctx=ctx)
    assert vectorize.await_count == 1
    assert vectorize.await_args.kwargs["file_path"] == root + "/chapter.md"


def test_reindex_preserves_metadata_without_using_it_as_text():
    executor = ReindexExecutor()
    assert executor._is_hidden_meta_file("viking://resources/manual/_toc.json")
    assert executor._is_hidden_meta_file("viking://resources/manual/_manifest.json")
    # The navigation index must not become retrieval knowledge: a flat list of
    # every chapter title would outrank the chapters it points at.
    assert executor._is_hidden_meta_file("viking://resources/manual/toc.md")
    assert not executor._is_hidden_meta_file("viking://resources/manual/ordinary.json")


async def test_explicit_reindex_of_control_file_does_not_embed_metadata(monkeypatch):
    from openviking.service.reindex_executor import _ReindexCounters

    fs = SimpleNamespace(
        exists=AsyncMock(return_value=True), stat=AsyncMock(return_value={"isDir": False})
    )
    monkeypatch.setattr("openviking.service.reindex_executor.get_viking_fs", lambda: fs)
    executor = ReindexExecutor()
    rebuild = AsyncMock()
    monkeypatch.setattr(executor, "_reindex_resource_vectors_from_entries", rebuild)
    ctx = RequestContext(user=UserIdentifier("a", "u"), role=Role.USER)
    await executor._reindex_resource_vectors(
        uri="viking://resources/manual/_manifest.json", counters=_ReindexCounters(), ctx=ctx
    )
    assert rebuild.await_args.kwargs["files"] == []
