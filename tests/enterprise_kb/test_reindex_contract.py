"""KB-020: D2 reindex complete contract (F9/F11).

Covers TC-9.1, TC-9.3, TC-9.4, TC-11.1, TC-11.2, TC-11.3, TC-11.5, TC-11.6:

- reindex of an existing Markdown makes zero parser calls, never rewrites the
  original source/directory, and does not re-split chapters by reading ``_toc.json``;
- enterprise dry_run returns a plan with ZERO writes for both build modes;
- the two effective-config modes are distinct: ``vectors_only`` keeps the
  published L0/L1, ``semantic_and_vectors`` regenerates directory L0/L1 and
  refreshes affected ancestors;
- a joint image record with no current valid summary is SEMANTICS_REQUIRED and
  is never degraded to a text-only summary vector;
- business writers/admins may reindex an authorized resource domain without
  disguising themselves as ROOT, while a plain USER stays in its private space.

Fixtures reuse the real SQLite registry, disk AGFS and VikingFS reads from the
sibling publication tests; model/vector DB are test doubles. Full product
acceptance remains NOT_RUN.
"""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding
from test_publication import view
from test_semantic_publication import finish

from openviking.core.context import ContextLevel
from openviking.server.identity import RequestContext, Role
from openviking.service import reindex_executor as reindex
from openviking_cli.exceptions import PermissionDeniedError
from openviking_cli.session.user_id import UserIdentifier

ROOT = "viking://resources/manual"
URI = ROOT + "/chapter.md"
IMAGE = ROOT + "/images/a.png"


# --------------------------------------------------------------------------- #
# TC-11.5: enterprise dry_run is a zero-write preview for both build modes
# --------------------------------------------------------------------------- #


def _preview_env(e, embedding, monkeypatch):
    monkeypatch.setattr(reindex, "get_viking_fs", lambda: e.fs)
    monkeypatch.setattr(
        reindex, "get_openviking_config", lambda: SimpleNamespace(embedding=embedding)
    )
    # A preview must not require an embedding queue at all.
    monkeypatch.setattr(
        reindex,
        "get_service",
        lambda: SimpleNamespace(
            viking_fs=e.fs, vikingdb_manager=SimpleNamespace(has_queue_manager=False)
        ),
    )
    executor = reindex.ReindexExecutor()
    executor._effective_file_vectorization_concurrency = lambda: 1
    return executor


async def test_dry_run_preview_writes_nothing_vectors_only(environment, embedding, monkeypatch):
    e = environment
    await e.fs.write(URI, "# 章节\n\n正文。" * 40, ctx=e.ctx)
    executor = _preview_env(e, embedding, monkeypatch)
    e.queue.messages.clear()

    result = await executor._run(
        uri=URI,
        object_type="resource",
        mode="vectors_only",
        dry_run=True,
        ctx=e.ctx,
    )

    assert result["status"] == "preview"
    assert result["dry_run"] is True
    assert result["planned_chunks"] >= 1
    # ZERO writes: no queue work and no publication generation.
    assert e.queue.messages == []
    assert view(e.store) == {}
    assert result["effective_config"]["write_directory_levels"] is False


async def test_dry_run_preview_counts_directory_levels_semantic_mode(
    environment, embedding, monkeypatch
):
    e = environment
    await e.fs.write(URI, "# 章节\n\n正文。" * 40, ctx=e.ctx)
    executor = _preview_env(e, embedding, monkeypatch)
    # The disk fixture has no tree_directory transport; the preview walks the
    # tree through the same helper the real build uses.
    executor._tree_all = AsyncMock(
        return_value=[{"uri": URI, "isDir": False}]
    )
    e.queue.messages.clear()

    result = await executor._run(
        uri=ROOT,
        object_type="resource",
        mode="semantic_and_vectors",
        dry_run=True,
        ctx=e.ctx,
    )

    assert result["status"] == "preview"
    # ROOT plus its own L0/L1 pair are planned but not written.
    assert result["planned_semantic_levels"] == 2
    assert result["planned_chunks"] >= 1
    assert e.queue.messages == []
    assert view(e.store) == {}
    assert result["effective_config"]["write_directory_levels"] is True


async def test_dry_run_does_not_rewrite_original_markdown(environment, embedding, monkeypatch):
    e = environment
    body = "# 章节\n\n原始正文不会被改写。" * 20
    await e.fs.write(URI, body, ctx=e.ctx)
    executor = _preview_env(e, embedding, monkeypatch)

    await executor._run(
        uri=URI, object_type="resource", mode="vectors_only", dry_run=True, ctx=e.ctx
    )

    assert await e.fs.read_file(URI, ctx=e.ctx) == body


async def test_legacy_prune_orphans_dry_run_contract_preserved(monkeypatch):
    """The legacy prune_orphans dry_run preview must still be accepted."""
    executor = reindex.ReindexExecutor()
    seen = {}

    async def fake_run(**kwargs):
        seen.update(kwargs)
        return {"status": "completed"}

    class FakeTracker:
        async def has_running(self, *args, **kwargs):
            return False

    monkeypatch.setattr(executor, "_run", fake_run)
    monkeypatch.setattr(
        reindex, "get_task_tracker", lambda: FakeTracker()
    )
    ctx = RequestContext(user=UserIdentifier("test", "alice"), role=Role.ROOT)
    result = await executor.execute(
        uri="viking://resources/demo",
        mode="prune_orphans",
        wait=True,
        dry_run=True,
        ctx=ctx,
    )
    assert result == {"status": "completed"}
    assert seen["dry_run"] is True


# --------------------------------------------------------------------------- #
# TC-9.1 / TC-9.3 / TC-11.1: parser-free, non-destructive, D1-equivalent plan
# --------------------------------------------------------------------------- #


async def test_reindex_markdown_zero_parser_calls_and_no_toc_resplit(
    environment, embedding, monkeypatch
):
    e = environment
    source = "# 维修说明\n\n" + "".join(f"{i} 检查液压回路。\n\n" for i in range(60))
    await e.fs.write(URI, source, ctx=e.ctx)
    # A stale TOC sidecar exists but must never be read to re-split chapters.
    await e.fs.write(ROOT + "/_toc.json", '{"bookmarks": []}', ctx=e.ctx)

    parse_calls: list[tuple] = []

    class GuardedParser:
        async def parse(self, *args, **kwargs):
            parse_calls.append(args)
            raise AssertionError("reindex of existing Markdown must not call a parser")

        async def submit(self, *args, **kwargs):
            parse_calls.append(args)
            raise AssertionError("reindex of existing Markdown must not call a parser")

    monkeypatch.setattr("openviking.parse.parser_router.ParserRouter.parse", GuardedParser.parse)
    monkeypatch.setattr(reindex, "get_viking_fs", lambda: e.fs)
    monkeypatch.setattr(
        reindex, "get_openviking_config", lambda: SimpleNamespace(embedding=embedding)
    )
    monkeypatch.setattr(
        reindex,
        "get_service",
        lambda: SimpleNamespace(
            vikingdb_manager=SimpleNamespace(fetch_by_uri=AsyncMock(return_value=None))
        ),
    )
    executor = reindex.ReindexExecutor()
    executor._effective_file_vectorization_concurrency = lambda: 1
    executor._best_file_summary = AsyncMock(return_value="summary")
    executor._chunk_reindex_ingest_options = AsyncMock(return_value=None)
    counters = reindex._ReindexCounters()
    e.queue.messages.clear()

    read_uris: list[str] = []
    original_read = e.fs.read_file

    async def tracking_read(uri, **kwargs):
        read_uris.append(uri)
        return await original_read(uri, **kwargs)

    monkeypatch.setattr(e.fs, "read_file", tracking_read)

    await executor._reindex_resource_vectors_from_entries(
        root_uri=URI, directories=[], files=[URI], counters=counters, ctx=e.ctx
    )

    assert counters.failed_records == 0, counters.warnings
    assert parse_calls == []
    assert not any(u.endswith("_toc.json") for u in read_uris)
    assert await e.fs.read_file(URI, ctx=e.ctx) == source
    assert len(e.queue.messages) > 1
    assert {m.context_data["uri"] for m in e.queue.messages} == {URI}
    assert {m.context_data["source_uri"] for m in e.queue.messages} == {URI}


# --------------------------------------------------------------------------- #
# TC-11.2: the two effective-config modes are distinct
# --------------------------------------------------------------------------- #


def test_effective_mode_config_distinguishes_two_modes():
    vectors = reindex.ReindexExecutor.effective_mode_config("vectors_only")
    semantic = reindex.ReindexExecutor.effective_mode_config("semantic_and_vectors")
    assert vectors.write_directory_levels is False
    assert vectors.regenerate_image_summaries is False
    assert vectors.writes_semantics is False
    assert semantic.write_directory_levels is True
    assert semantic.regenerate_image_summaries is True
    assert semantic.writes_semantics is True
    # Neither mode may degrade a joint image record to a text-only vector.
    assert vectors.requires_image_semantics is True
    assert semantic.requires_image_semantics is True


async def test_vectors_only_does_not_touch_published_directory_levels(
    environment, embedding, monkeypatch
):
    from test_semantic_publication import directory

    e = environment
    await finish(e, await directory(e, "old"))
    assert await e.fs.abstract(ROOT, ctx=e.ctx) == "old abstract"
    await e.fs.write(URI, "# 新章节\n\n正文。" * 30, ctx=e.ctx)

    monkeypatch.setattr(reindex, "get_viking_fs", lambda: e.fs)
    monkeypatch.setattr(
        reindex, "get_openviking_config", lambda: SimpleNamespace(embedding=embedding)
    )
    monkeypatch.setattr(
        reindex,
        "get_service",
        lambda: SimpleNamespace(
            vikingdb_manager=SimpleNamespace(fetch_by_uri=AsyncMock(return_value=None))
        ),
    )
    executor = reindex.ReindexExecutor()
    executor._effective_file_vectorization_concurrency = lambda: 1
    executor._best_file_summary = AsyncMock(return_value="summary")
    executor._chunk_reindex_ingest_options = AsyncMock(return_value=None)
    executor._run_semantic_processor = AsyncMock(
        side_effect=AssertionError("vectors_only must not regenerate semantics")
    )
    run = reindex._ReindexRunContext(
        ctx=e.ctx, counters=reindex._ReindexCounters(), lock=None, ingest_options=None
    )
    e.queue.messages.clear()

    await executor._reindex_resource(uri=URI, mode="vectors_only", run=run)

    executor._run_semantic_processor.assert_not_awaited()
    # Published directory L0/L1 remain readable and unchanged.
    assert await e.fs.abstract(ROOT, ctx=e.ctx) == "old abstract"
    assert await e.fs.overview(ROOT, ctx=e.ctx) == "old overview"


# --------------------------------------------------------------------------- #
# TC-9.4 / TC-11.6: ancestor semantics propagate bottom-up, never cross-domain
# --------------------------------------------------------------------------- #


def test_ancestor_directories_stay_inside_domain():
    assert reindex.ReindexExecutor._ancestor_directories("viking://resources/a.md") == []
    assert reindex.ReindexExecutor._ancestor_directories(
        "viking://resources/raw/manual/ch1.md"
    ) == ["viking://resources/raw/manual", "viking://resources/raw"]
    assert reindex.ReindexExecutor._ancestor_directories(
        "viking://user/bob/resources/x.md"
    ) == ["viking://user/bob/resources"]


def test_ancestor_directories_include_registered_root_without_crossing_it():
    assert reindex.ReindexExecutor._ancestor_directories(
        "viking://resources/shared/tenant-a/manual/chapter.md",
        namespace_root="viking://resources/shared/tenant-a",
    ) == ["viking://resources/shared/tenant-a/manual", "viking://resources/shared/tenant-a"]
    assert reindex.ReindexExecutor._ancestor_directories(
        "viking://resources/shared/tenant-a", namespace_root="viking://resources/shared/tenant-a"
    ) == []
    with pytest.raises(reindex.InvalidArgumentError):
        reindex.ReindexExecutor._ancestor_directories(
            "viking://resources/shared/tenant-ab/chapter.md",
            namespace_root="viking://resources/shared/tenant-a",
        )


async def test_registered_namespace_refresh_never_aggregates_sibling_domain(environment, monkeypatch):
    e = environment
    root = "viking://resources/shared/tenant-a"
    sibling = "viking://resources/shared/tenant-b"
    e.store.namespace_register(e.ctx.account_id, "tenant-a", root)
    e.store.namespace_register(e.ctx.account_id, "tenant-b", sibling)
    await e.fs.write(root + "/manual/chapter.md", "authorized source", ctx=e.ctx)
    await e.fs.write(sibling + "/private.md", "sibling secret", ctx=e.ctx)
    monkeypatch.setattr(reindex, "get_viking_fs", lambda: e.fs)
    monkeypatch.setattr(reindex, "configured_store", lambda: e.store)
    executor = reindex.ReindexExecutor()
    executor._run_semantic_processor = AsyncMock(return_value={})
    run = reindex._ReindexRunContext(ctx=e.ctx, counters=reindex._ReindexCounters(),
                                    lock=None, ingest_options=None)
    await executor._refresh_ancestor_semantics(uri=root + "/manual/chapter.md", run=run)
    calls = [call.kwargs for call in executor._run_semantic_processor.await_args_list]
    assert [call["uri"] for call in calls] == [root + "/manual", root]
    assert all(call["recursive"] is False and call["propagate_to_parent"] is False for call in calls)
    assert not any(call["uri"] == "viking://resources/shared" for call in calls)


async def test_automatic_semantic_bubbling_stops_at_registered_root(environment, monkeypatch):
    from openviking.storage.queuefs import semantic_processor as semantic
    from openviking.storage.queuefs.semantic_msg import SemanticMsg
    e = environment
    root = "viking://resources/shared/tenant-a"
    e.store.namespace_register(e.ctx.account_id, "tenant-a", root)
    # A root message must never even inspect or plan its parent. Test the real
    # namespace registry; only the forbidden operation is replaced by a trap.
    monkeypatch.setattr(semantic, "configured_store", lambda: e.store)
    plan = AsyncMock(side_effect=AssertionError("cross-domain parent inspected"))
    monkeypatch.setattr(semantic, "plan_abstract_overview_refresh", plan)
    msg = SemanticMsg(uri=root, context_type="resource", account_id=e.ctx.account_id,
                      generation_trigger="reindex", propagate_to_parent=True)
    await semantic.SemanticProcessor()._enqueue_parent_refresh(msg, root, l0_body_changed=True)
    plan.assert_not_awaited()


async def test_semantic_mode_refreshes_ancestors_bottom_up(environment, monkeypatch):
    e = environment
    await e.fs.write("viking://resources/raw/manual/ch1.md", "正文", ctx=e.ctx)
    await e.fs.write("viking://resources/raw/manual/other.md", "正文", ctx=e.ctx)

    monkeypatch.setattr(reindex, "get_viking_fs", lambda: e.fs)
    executor = reindex.ReindexExecutor()
    calls: list[str] = []

    async def fake_semantic(**kwargs):
        calls.append(kwargs["uri"])
        return {}

    executor._run_semantic_processor = fake_semantic
    executor._reindex_resource_vectors = AsyncMock()
    run = reindex._ReindexRunContext(
        ctx=e.ctx, counters=reindex._ReindexCounters(), lock=None, ingest_options=None
    )

    await executor._reindex_resource(
        uri="viking://resources/raw/manual/ch1.md",
        mode="semantic_and_vectors",
        run=run,
        recursive=False,
    )

    # Deepest ancestor first, then upward; the domain root is never rebuilt.
    assert calls == [
        "viking://resources/raw/manual/ch1.md",
        "viking://resources/raw/manual",
        "viking://resources/raw",
    ]
    assert "viking://resources" not in calls


async def test_recursive_semantic_mode_does_not_double_refresh_ancestors(
    environment, monkeypatch
):
    e = environment
    await e.fs.write("viking://resources/raw/manual/ch1.md", "正文", ctx=e.ctx)

    monkeypatch.setattr(reindex, "get_viking_fs", lambda: e.fs)
    executor = reindex.ReindexExecutor()
    calls: list[tuple[str, bool]] = []

    async def fake_semantic(**kwargs):
        calls.append((kwargs["uri"], kwargs.get("recursive", True)))
        return {}

    executor._run_semantic_processor = fake_semantic
    executor._reindex_resource_vectors = AsyncMock()
    run = reindex._ReindexRunContext(
        ctx=e.ctx, counters=reindex._ReindexCounters(), lock=None, ingest_options=None
    )

    await executor._reindex_resource(
        uri="viking://resources/raw/manual",
        mode="semantic_and_vectors",
        run=run,
        recursive=True,
    )

    # Recursive mode lets the processor walk upward itself; the executor must
    # not add an extra ancestor pass.
    assert calls == [("viking://resources/raw/manual", True)]


async def test_semantic_reindex_does_not_normalize_figure_citations(
    environment, monkeypatch
):
    """Citation normalization is its own stage: reindex must not rewrite the
    markdown it is about to archive. The vector phase reads the published
    bytes as they stand, so a reindex that normalized citations would be a
    hidden write stage instead of pure compute."""
    e = environment
    await e.fs.write("viking://resources/raw/manual/ch1.md", "正文", ctx=e.ctx)

    monkeypatch.setattr(reindex, "get_viking_fs", lambda: e.fs)
    executor = reindex.ReindexExecutor()
    events: list[tuple] = []

    async def fake_rewrite(uri, ctx=None, lease_ref=None):
        events.append(("normalize", uri, lease_ref))
        return {"files_processed": 1, "references_rewritten": 2}

    async def fake_semantic(**kwargs):
        events.append(("semantic", kwargs["uri"]))
        return {}

    monkeypatch.setattr(
        "openviking.parse.image_rewrite.rewrite_image_uris", fake_rewrite
    )
    executor._run_semantic_processor = fake_semantic
    executor._reindex_resource_vectors = AsyncMock()
    lease = {"id": "tree-lease"}
    run = reindex._ReindexRunContext(
        ctx=e.ctx, counters=reindex._ReindexCounters(), lock=lease, ingest_options=None
    )

    await executor._reindex_resource(
        uri="viking://resources/raw/manual",
        mode="semantic_and_vectors",
        run=run,
    )

    assert [event[0] for event in events] == ["semantic"]


async def test_semantic_reindex_without_a_lease_skips_normalization(
    environment, monkeypatch
):
    e = environment
    monkeypatch.setattr(reindex, "get_viking_fs", lambda: e.fs)
    executor = reindex.ReindexExecutor()
    called: list = []

    async def fake_rewrite(*args, **kwargs):
        called.append((args, kwargs))
        return {}

    monkeypatch.setattr(
        "openviking.parse.image_rewrite.rewrite_image_uris", fake_rewrite
    )
    executor._run_semantic_processor = AsyncMock(return_value={})
    executor._reindex_resource_vectors = AsyncMock()
    counters = reindex._ReindexCounters()
    run = reindex._ReindexRunContext(
        ctx=e.ctx, counters=counters, lock=None, ingest_options=None
    )

    await executor._reindex_resource(
        uri="viking://resources/raw/manual",
        mode="semantic_and_vectors",
        run=run,
    )

    assert called == []
    assert counters.warnings == []


# --------------------------------------------------------------------------- #
# TC-11.3: namespace semantic refresh must forward joint image summaries
# --------------------------------------------------------------------------- #


async def test_namespace_refresh_forwards_image_summaries_to_vector_phase(
    environment, embedding, monkeypatch
):
    """The namespace ``semantic_and_vectors`` path must not drop the per-image
    VLM summaries produced by the semantic processor. Dropping them leaves every
    image with no current summary, so the vector phase counts them unsupported
    and never writes the joint image record (F5/F9, TC-11.3)."""
    e = environment
    image_uri = "viking://resources/raw/manual/images/a.png"
    images_dir = "viking://resources/raw/manual/images"
    await e.fs.write(image_uri, b"original image", ctx=e.ctx)

    monkeypatch.setattr(reindex, "get_viking_fs", lambda: e.fs)
    executor = reindex.ReindexExecutor()
    details = {
        "name": "a.png",
        "summary": "阀芯检修图",
        "image_occurrences": [{"summary": "A"}],
    }

    async def fake_semantic(**kwargs):
        return {"image_summaries": {image_uri: details}}

    executor._run_semantic_processor = fake_semantic
    captured: dict = {}

    async def fake_vectors_from_entries(**kwargs):
        captured.update(kwargs)

    executor._reindex_resource_vectors_from_entries = fake_vectors_from_entries
    run = reindex._ReindexRunContext(
        ctx=e.ctx, counters=reindex._ReindexCounters(), lock=None, ingest_options=None
    )

    directories, files, image_summaries = await executor._refresh_namespace_resource_semantics(
        target_root="viking://resources",
        directories=["viking://resources/raw", images_dir],
        files=[image_uri],
        run=run,
    )

    assert image_summaries == {image_uri: details}
    assert image_uri in files


# --------------------------------------------------------------------------- #
# TC-11.3: joint image records are never degraded; SEMANTICS_REQUIRED otherwise
# --------------------------------------------------------------------------- #


def _image_entries_env(e, embedding, monkeypatch):
    monkeypatch.setattr(reindex, "get_viking_fs", lambda: e.fs)
    monkeypatch.setattr(
        reindex, "get_openviking_config", lambda: SimpleNamespace(embedding=embedding)
    )
    monkeypatch.setattr(
        reindex,
        "get_service",
        lambda: SimpleNamespace(
            vikingdb_manager=SimpleNamespace(fetch_by_uri=AsyncMock(return_value=None))
        ),
    )
    executor = reindex.ReindexExecutor()
    executor._effective_file_vectorization_concurrency = lambda: 1
    executor._best_file_summary = AsyncMock(return_value="aggregate")
    executor._chunk_reindex_ingest_options = AsyncMock(return_value=None)
    executor._upsert_context = AsyncMock(
        side_effect=AssertionError("text-only path forbidden for images")
    )
    return executor


async def test_joint_image_without_valid_summary_is_skipped(
    environment, embedding, monkeypatch
):
    e = environment
    await e.fs.write(IMAGE, b"original image", ctx=e.ctx)
    executor = _image_entries_env(e, embedding, monkeypatch)

    async def missing_cache(*args, **kwargs):
        raise ValueError("SEMANTICS_REQUIRED: published image has no occurrence cache")

    monkeypatch.setattr(
        "openviking.indexing.image_cache.load_image_semantics", missing_cache
    )
    counters = reindex._ReindexCounters()
    e.queue.messages.clear()

    await executor._reindex_resource_vectors_from_entries(
        root_uri=IMAGE, directories=[], files=[IMAGE], counters=counters, ctx=e.ctx
    )

    # A captionless image is a skip (unsupported), never an error path.
    assert counters.unsupported_records == 1
    assert counters.failed_records == 0
    assert e.queue.messages == []
    executor._upsert_context.assert_not_awaited()


async def test_joint_image_with_fresh_summary_keeps_joint_input(
    environment, embedding, monkeypatch
):
    from test_image_context import JointEmbedder

    e = environment
    model = JointEmbedder()
    monkeypatch.setattr(type(embedding), "get_embedder", lambda self: model)
    e.handler._embedder = model
    await e.fs.write(IMAGE, b"original image", ctx=e.ctx)
    executor = _image_entries_env(e, embedding, monkeypatch)
    details = {"name": "a.png", "summary": "阀芯检修图", "image_occurrences": [{"summary": "A"}]}
    counters = reindex._ReindexCounters()
    e.queue.messages.clear()

    await executor._reindex_resource_vectors_from_entries(
        root_uri=IMAGE,
        directories=[],
        files=[IMAGE],
        counters=counters,
        ctx=e.ctx,
        image_summaries={IMAGE: details},
    )

    assert counters.failed_records == 0, counters.warnings
    message = e.queue.messages[0]
    assert message.context_data["_require_joint_image_embedding"] is True
    assert {part["type"] for part in message.message} == {"text", "image_url"}
    executor._upsert_context.assert_not_awaited()


# --------------------------------------------------------------------------- #
# TC-9.3 / TC-14.4: business writer authorization for reindex
# --------------------------------------------------------------------------- #


def test_authorized_business_writer_can_reindex_resource_domain():
    authorize = reindex.ReindexExecutor._authorize_reindex_target
    admin = RequestContext(user=UserIdentifier("acct", "writer"), role=Role.ADMIN)
    root = RequestContext(user=UserIdentifier("acct", "root"), role=Role.ROOT)
    user = RequestContext(user=UserIdentifier("acct", "bob"), role=Role.USER)

    # Admin/ROOT may rebuild a shared knowledge domain without disguising as ROOT.
    authorize("viking://resources/raw/manual", admin)
    authorize("viking://resources/raw/manual", root)

    # A plain USER is limited to its own private namespace.
    with pytest.raises(PermissionDeniedError):
        authorize("viking://resources/raw/manual", user)
    authorize("viking://user/bob/resources/x.md", user)
    with pytest.raises(PermissionDeniedError):
        authorize("viking://user/alice/resources/x.md", user)


def test_reindex_authorization_rejects_hidden_actor_peer_view():
    authorize = reindex.ReindexExecutor._authorize_reindex_target
    ctx = RequestContext(
        user=UserIdentifier("acct", "bob"), role=Role.ADMIN, actor_peer_id="peer-a"
    )
    with pytest.raises(PermissionDeniedError):
        authorize("viking://user/bob/peers/peer-b/resources", ctx)


async def test_execute_rejects_unauthorized_user_resource_reindex(monkeypatch):
    executor = reindex.ReindexExecutor()
    ctx = RequestContext(user=UserIdentifier("acct", "bob"), role=Role.USER)
    with pytest.raises(PermissionDeniedError):
        await executor.execute(
            uri="viking://resources/raw/manual",
            mode="vectors_only",
            wait=True,
            ctx=ctx,
        )


async def test_execute_allows_admin_resource_reindex(monkeypatch):
    executor = reindex.ReindexExecutor()
    seen = {}

    async def fake_run(**kwargs):
        seen.update(kwargs)
        return {"status": "completed", "dry_run": kwargs["dry_run"]}

    class FakeTracker:
        async def has_running(self, *args, **kwargs):
            return False

    monkeypatch.setattr(executor, "_run", fake_run)
    monkeypatch.setattr(reindex, "get_task_tracker", lambda: FakeTracker())
    ctx = RequestContext(user=UserIdentifier("acct", "writer"), role=Role.ADMIN)

    result = await executor.execute(
        uri="viking://resources/raw/manual",
        mode="semantic_and_vectors",
        wait=True,
        dry_run=True,
        ctx=ctx,
    )

    assert result["status"] == "completed"
    assert seen["dry_run"] is True
    assert seen["mode"] == "semantic_and_vectors"


# --------------------------------------------------------------------------- #
# TC-11.1: result counters surface the skip verdict without a business code
# --------------------------------------------------------------------------- #


async def test_run_result_counts_unsupported_without_error_code(monkeypatch):
    executor = reindex.ReindexExecutor()

    async def fake_reindex_resource(*, uri, mode, run, recursive=True):
        run.counters.unsupported_records += 1

    monkeypatch.setattr(executor, "_reindex_resource", fake_reindex_resource)

    class FakeAGFS:
        async def pathlock_acquire_exact(self, path):
            return {"id": "lease"}

        async def pathlock_acquire_tree(self, path):
            return {"id": "lease"}

        async def pathlock_as_borrowed(self, lease):
            return lease

        async def pathlock_release(self, lease):
            return None

    class FakeFS:
        _async_agfs = FakeAGFS()

        def _uri_to_path(self, uri, ctx):
            return "/local/acct/resources/demo.md"

        async def stat(self, uri, ctx, skip_count=False):
            return {"isDir": False}

    monkeypatch.setattr(
        reindex,
        "get_service",
        lambda: SimpleNamespace(
            viking_fs=FakeFS(), vikingdb_manager=SimpleNamespace(has_queue_manager=True)
        ),
    )
    ctx = RequestContext(user=UserIdentifier("acct", "writer"), role=Role.ADMIN)
    result = await executor._run(
        uri="viking://resources/demo.md",
        object_type="resource",
        mode="vectors_only",
        ctx=ctx,
    )
    assert result["status"] == "completed"
    assert result["unsupported_records"] == 1
    assert "code" not in result


# --------------------------------------------------------------------------- #
# Task stage observability: stage transitions land in the task tracker
# --------------------------------------------------------------------------- #


async def test_resource_reindex_reports_semantic_and_vector_stages(monkeypatch):
    e = None  # no environment needed; everything below is stubbed

    class FakeAGFS:
        async def pathlock_acquire_tree(self, path):
            return {"id": "lease"}

        async def pathlock_acquire_exact(self, path):
            return {"id": "lease"}

        async def pathlock_as_borrowed(self, lease):
            return lease

        async def pathlock_release(self, lease):
            return None

    class FakeFS:
        _async_agfs = FakeAGFS()

        def _uri_to_path(self, uri, ctx):
            return "/local/acct/resources/manual"

        async def stat(self, uri, ctx, skip_count=False):
            return {"isDir": True}

        async def exists(self, uri, ctx):
            return True

    stages: list[str] = []

    class FakeTracker:
        async def update_stage(self, task_id, stage, account_id=None, user_id=None, meta=None):
            stages.append(stage)

    from openviking.service import task_work_index as twi

    monkeypatch.setattr(reindex, "get_service", lambda: SimpleNamespace(
        viking_fs=FakeFS(), vikingdb_manager=SimpleNamespace(has_queue_manager=True)
    ))
    monkeypatch.setattr(reindex, "get_task_tracker", lambda: FakeTracker())
    executor = reindex.ReindexExecutor()
    executor._effective_file_vectorization_concurrency = lambda: 1
    executor._run_semantic_processor = AsyncMock(return_value={})
    executor._reindex_resource_vectors = AsyncMock()

    ctx = RequestContext(user=UserIdentifier("acct", "writer"), role=Role.ADMIN)
    with twi.bind_task_context("task-stage-1", "acct", "writer"):
        await executor._reindex_resource(
            uri="viking://resources/manual",
            mode="semantic_and_vectors",
            run=reindex._ReindexRunContext(
                ctx=ctx, counters=reindex._ReindexCounters(), lock=None, ingest_options=None
            ),
        )

    # Citation normalization is its own stage and must not appear here;
    # the semantic stage precedes the vector stage, which runs last.
    assert stages[0].startswith("semantic:")
    assert stages[-1].startswith("vectors:")


async def test_stage_update_without_task_context_is_noop(monkeypatch):
    from openviking.service import task_work_index as twi

    called = []

    class FakeTracker:
        async def update_stage(self, task_id, stage, **kwargs):
            called.append((task_id, stage))

    monkeypatch.setattr(reindex, "get_task_tracker", lambda: FakeTracker())
    executor = reindex.ReindexExecutor()
    with twi.detach_task_context():
        await executor._update_stage("semantic:x")
    assert called == []


# --------------------------------------------------------------------------- #
# Reuse guards: retrying a failed reindex must not re-embed unchanged content.
# Live, that is how one file held two full generations (24 old + 21 new rows)
# and a tree grew dozens of stale generations per URI.
# --------------------------------------------------------------------------- #


def _records_manager(records):
    """A vikingdb_manager double whose filter reads publication-written records.

    The reuse guards scan with And(Eq(account_id), Eq(uri), Eq(level)) and
    project named output fields -- the same shape the real engine serves for
    the scan the guard issues.
    """
    from openviking.storage.expr import And, Eq

    def _eqs(expr):
        if isinstance(expr, Eq):
            return {expr.field: expr.value}
        if isinstance(expr, And):
            merged = {}
            for child in expr.conds:
                merged.update(_eqs(child))
            return merged
        raise AssertionError(f"guard sent an unexpected filter node: {expr!r}")

    async def fake_filter(*, filter, limit, output_fields, ctx, **kwargs):
        wanted = _eqs(filter)
        rows = []
        for record in records.values():
            if all(record.get(field) == value for field, value in wanted.items()):
                rows.append({field: record.get(field) for field in output_fields})
        return rows[:limit]

    return SimpleNamespace(vikingdb_manager=SimpleNamespace(filter=fake_filter))


def _guard_executor(e, embedding, monkeypatch, records=None):
    monkeypatch.setattr(reindex, "get_viking_fs", lambda: e.fs)
    monkeypatch.setattr(
        reindex, "get_openviking_config", lambda: SimpleNamespace(embedding=embedding)
    )
    monkeypatch.setattr(
        reindex,
        "get_service",
        lambda: _records_manager(e.handler._vikingdb.records if records is None else records),
    )
    executor = reindex.ReindexExecutor()
    executor._effective_file_vectorization_concurrency = lambda: 1
    return executor


async def test_directory_reindex_reuses_unchanged_semantics(
    environment, embedding, monkeypatch
):
    from test_semantic_publication import directory

    e = environment
    await finish(e, await directory(e, "stable"))
    published_generation = view(e.store)[ROOT]["generation"]

    executor = _guard_executor(e, embedding, monkeypatch)
    counters = reindex._ReindexCounters()
    e.queue.messages.clear()

    await executor._reindex_resource_vectors_from_entries(
        root_uri=ROOT, directories=[ROOT], files=[], counters=counters, ctx=e.ctx
    )

    assert counters.failed_records == 0, counters.warnings
    assert counters.reused_records == 2
    assert counters.rebuilt_records == 0
    # The sidecar texts already live under the published generation: a retry
    # opens no second generation and spends no embedding call.
    assert e.queue.messages == []
    assert view(e.store)[ROOT]["generation"] == published_generation


async def test_directory_reindex_rebuilds_when_a_sidecar_changes(
    environment, embedding, monkeypatch
):
    from test_semantic_publication import directory

    e = environment
    await finish(e, await directory(e, "stable"))
    await e.fs.write(ROOT + "/.overview.md", "changed overview", ctx=e.ctx)

    executor = _guard_executor(e, embedding, monkeypatch)
    counters = reindex._ReindexCounters()

    await executor._reindex_resource_vectors_from_entries(
        root_uri=ROOT, directories=[ROOT], files=[], counters=counters, ctx=e.ctx
    )

    assert counters.failed_records == 0, counters.warnings
    # The semantic plan is one barrier generation: a changed level republishes
    # both, and only the fully unchanged case skips.
    assert counters.reused_records == 0
    assert counters.rebuilt_records == 2
    assert {m.context_data["level"] for m in e.queue.messages} == {0, 1}


async def test_matching_record_respects_publication_visibility(monkeypatch):
    ctx = RequestContext(user=UserIdentifier("acct", "user"), role=Role.USER)
    executor = reindex.ReindexExecutor()
    row = {
        "account_id": "acct",
        "uri": URI,
        "level": 0,
        "index_generation": "gen-1",
        "content_revision": "rev-1",
        "chunk_text": "body",
    }

    async def run(records, *, generation, revision="rev-1", expected=None):
        monkeypatch.setattr(reindex, "get_service", lambda: _records_manager(records))
        return await executor._matching_record_exists(
            uri=URI,
            level=0,
            content_revision=revision,
            live_generation=generation,
            ctx=ctx,
            expected_texts=expected,
        )

    # The live generation's row with a matching revision is reusable.
    assert await run({"r": dict(row)}, generation="gen-1")
    # A row of some other generation is invisible to every query: rebuild.
    assert not await run({"r": dict(row)}, generation="gen-2")
    # A revision mismatch is a rebuild even under the live generation.
    assert not await run({"r": dict(row)}, generation="gen-1", revision="rev-2")
    # Legacy rows (no generation) are visible only where the manifest does not
    # publish the URI at all -- the imported-store shape.
    legacy = dict(row, index_generation=None)
    assert await run({"r": legacy}, generation=None)
    assert not await run({"r": legacy}, generation="gen-1")
    # Managed rows under an unpublished URI are invisible too.
    assert not await run({"r": dict(row)}, generation=None)
    # Caption pinning: the stored chunk_text multiset must match exactly, so an
    # image whose captions changed over an unchanged asset is rebuilt.
    pair = {
        "a": dict(row, chunk_text="A"),
        "b": dict(row, chunk_text="B"),
    }
    assert await run(pair, generation="gen-1", expected=["B", "A"])
    assert not await run(pair, generation="gen-1", expected=["A"])


async def test_published_content_unchanged_falls_back_to_legacy_records(
    environment, embedding, monkeypatch
):
    e = environment
    source = "# 章节\n\n稳定的正文。\n" * 10
    await e.fs.write(URI, source, ctx=e.ctx)
    executor = _guard_executor(
        e,
        embedding,
        monkeypatch,
        records={
            "legacy": {
                "account_id": e.ctx.account_id,
                "uri": URI,
                "level": 2,
                "index_generation": None,
                "content_revision": hashlib.sha256(source.encode()).hexdigest(),
            }
        },
    )
    # No publication row for this URI (an imported store): the chunk-row
    # lookup returns nothing and the guard falls back to the record scan.
    assert e.store.published_chunks(e.ctx.account_id, URI) is None
    assert await executor._published_content_unchanged(URI, ctx=e.ctx, manifest={})
    # A changed file must not be mistaken for the published revision.
    await e.fs.write(URI, "# 章节\n\n改过的正文。\n" * 10, ctx=e.ctx)
    assert not await executor._published_content_unchanged(URI, ctx=e.ctx, manifest={})
