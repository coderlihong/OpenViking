"""On-disk source revisions, actual VikingFS reads and queued publication."""

import contextvars
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_index_plan import Database, Embedder, Queue
from test_index_plan import embedding as embedding

from openviking.indexing.content import archive_source, blob_path, draft_sources
from openviking.indexing.publication import configured_store, query_snapshot
from openviking.server.identity import RequestContext, Role
from openviking.storage.collection_schemas import TextEmbeddingHandler
from openviking.storage.viking_fs import VikingFS
from openviking.utils import embedding_utils
from openviking_cli.exceptions import NotFoundError, PermissionDeniedError
from openviking_cli.session.user_id import UserIdentifier

URI = "viking://resources/manual.md"


class DiskAGFS:
    """Disk substitute for AGFS transport; no encryption claim from this fixture."""

    def __init__(self, root):
        self.root = root

    def path(self, path):
        result = self.root / path.lstrip("/")
        assert result.is_relative_to(self.root)
        return result

    async def stat(self, path):
        target = self.path(path)
        if not target.exists():
            raise FileNotFoundError(path)
        return {"isDir": target.is_dir(), "size": target.stat().st_size}

    async def read(self, path, offset=0, size=-1):
        raw = self.path(path).read_bytes()
        return raw[offset:] if size == -1 else raw[offset : offset + size]

    async def write(self, path, raw, **kwargs):
        target = self.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        return path

    async def mkdir(self, path, **kwargs):
        self.path(path).mkdir()


@pytest.fixture
def environment(embedding, tmp_path, monkeypatch):
    embedding.chunking.publication_path = str(tmp_path / "registry.db")
    embedding.chunking.publish_content = True
    cfg = SimpleNamespace(
        embedding=SimpleNamespace(
            chunking=embedding.chunking,
            dimension=2,
            get_embedder=lambda: Embedder(),
            circuit_breaker=SimpleNamespace(
                failure_threshold=5, reset_timeout=60, max_reset_timeout=600
            ),
        ),
        storage=SimpleNamespace(vectordb=SimpleNamespace(name="context")),
    )
    monkeypatch.setattr("openviking_cli.utils.config.get_openviking_config", lambda: cfg)
    monkeypatch.setattr(
        "openviking_cli.utils.config.open_viking_config.OpenVikingConfigSingleton._instance", cfg
    )
    monkeypatch.setattr(
        embedding_utils, "get_openviking_config", lambda: SimpleNamespace(embedding=embedding)
    )
    fs = VikingFS.__new__(VikingFS)
    fs._async_agfs = DiskAGFS(tmp_path / "agfs")
    fs.acl_manager = None
    fs._bound_ctx = contextvars.ContextVar("content_test_bound_ctx", default=None)
    fs._ensure_access = AsyncMock()
    queue = Queue()
    manager = SimpleNamespace(EMBEDDING="embedding", get_queue=lambda _: queue)
    monkeypatch.setattr(embedding_utils, "get_viking_fs", lambda: fs)
    monkeypatch.setattr(embedding_utils, "get_queue_manager", lambda: manager)
    monkeypatch.setattr(embedding_utils, "_get_existing_created_at", AsyncMock(return_value=None))
    ctx = RequestContext(user=UserIdentifier("acct", "user"), role=Role.USER)
    return SimpleNamespace(
        fs=fs,
        ctx=ctx,
        queue=queue,
        store=configured_store(embedding),
        handler=TextEmbeddingHandler(Database()),
    )


async def stage(env, source):
    await env.fs._async_agfs.write(env.fs._uri_to_path(URI, ctx=env.ctx), source.encode())
    env.queue.messages.clear()
    assert await embedding_utils.vectorize_file(
        URI, {"name": "manual.md", "summary": "summary"}, "viking://resources", ctx=env.ctx
    )
    return list(env.queue.messages)


async def finish(env, messages):
    for msg in messages:
        result = await env.handler.on_dequeue({"data": msg.to_json()})
        assert result.outcome.value == "success", result.error


async def test_published_uri_reads_old_body_until_new_vectors_complete(environment):
    e = environment
    old = "# Manual\n\n旧维修知识。\n" * 20
    new = "# Manual\n\n新维修知识。\n" * 20
    first = await stage(e, old)
    await finish(e, first)
    next_messages = await stage(e, new)
    assert await e.fs.read_file(URI, ctx=e.ctx) == old
    assert await e.fs.read_file_bytes(URI, ctx=e.ctx) == old.encode()
    assert await e.fs.read(URI, offset=2, size=15, ctx=e.ctx) == old.encode()[2:17]
    assert await e.fs.read_file(URI, offset=1, limit=2, ctx=e.ctx) == "".join(
        old.splitlines(keepends=True)[1:3]
    )
    with draft_sources():
        assert await e.fs.read_file(URI, ctx=e.ctx) == new
    await finish(e, next_messages[:-1])
    assert await e.fs.read_file(URI, ctx=e.ctx) == old
    await finish(e, next_messages[-1:])
    assert await e.fs.read_file(URI, ctx=e.ctx) == new
    # Metadata and queued rows hold only revision references; not full source.
    assert all("_source_snapshot" not in m.context_data for m in next_messages)
    assert old.encode() not in Path(e.store.path).read_bytes()


async def test_failed_generation_and_current_acl_or_deletion(environment):
    e = environment
    old = "# Old\n正文\n" * 20
    first = await stage(e, old)
    await finish(e, first)
    pending = await stage(e, "# New\n新正文\n" * 20)
    generation = pending[0].context_data["index_generation"]
    e.store.fail(generation, "embedding unavailable")
    assert await e.fs.read_file(URI, ctx=e.ctx) == old
    e.fs._ensure_access.side_effect = PermissionDeniedError("revoked")
    with pytest.raises(PermissionDeniedError):
        await e.fs.read_file(URI, ctx=e.ctx)
    e.fs._ensure_access.side_effect = None
    e.fs._async_agfs.path(e.fs._uri_to_path(URI, ctx=e.ctx)).unlink()
    with pytest.raises(NotFoundError):
        await e.fs.read_file(URI, ctx=e.ctx)
    with pytest.raises(NotFoundError):
        await e.fs.read(URI, ctx=e.ctx)


async def test_query_assembly_keeps_source_revision_across_publication(environment):
    e = environment
    old = "# Old\n旧内容\n" * 20
    new = "# New\n新内容\n" * 20
    await finish(e, await stage(e, old))
    pending = await stage(e, new)

    class Reader:
        @query_snapshot
        async def assemble(self, *, ctx):
            before = await e.fs.read_file(URI, ctx=ctx)
            await finish(e, pending)
            after = await e.fs.read_file(URI, ctx=ctx)
            return before, after

    before, after = await Reader().assemble(ctx=e.ctx)
    assert before == after == old
    assert await e.fs.read_file(URI, ctx=e.ctx) == new


async def test_corrupt_private_revision_fails_instead_of_returning_working_source(environment):
    e = environment
    source = "# 维修\n正文\n" * 20
    await finish(e, await stage(e, source))
    with e.store.source_reference("acct", URI) as ref:
        path = blob_path(e.fs, e.ctx, ref["sha256"])
    await e.fs._async_agfs.write(path, b"corrupt")
    with pytest.raises(RuntimeError, match="corrupted"):
        await e.fs.read_file(URI, ctx=e.ctx)
    with pytest.raises(RuntimeError, match="verification"):
        await archive_source(e.fs, URI, e.ctx)


async def test_source_write_during_indexing_fails_publication_then_reindex_uses_latest(environment):
    e = environment
    old = "# Old\n旧正文\n" * 20
    new = "# New\n新正文\n" * 20
    edited = "# Edited\n最终编辑内容\n" * 20
    await finish(e, await stage(e, old))
    pending = await stage(e, new)
    await finish(e, pending[:1])
    await e.fs.write(URI, edited, ctx=e.ctx)
    results = [await e.handler.on_dequeue({"data": m.to_json()}) for m in pending[1:]]
    assert results[-1].outcome.value == "failed"
    generation = pending[0].context_data["index_generation"]
    assert e.store.state(generation)["error"] == "Source changed during index build"
    assert await e.fs.read_file(URI, ctx=e.ctx) == old
    e.queue.messages.clear()
    assert await embedding_utils.vectorize_file(
        URI, {"name": "manual.md"}, "viking://resources", ctx=e.ctx
    )
    await finish(e, e.queue.messages)
    assert await e.fs.read_file(URI, ctx=e.ctx) == edited


async def test_source_write_window_before_enqueue_rejects_stale_plan(environment, monkeypatch):
    from openviking.indexing import enqueue as module

    e = environment
    raw = "# Draft\n正文\n" * 20
    await e.fs._async_agfs.write(e.fs._uri_to_path(URI, ctx=e.ctx), raw.encode())
    original = module.plan_resource

    def edit_during_plan(*args, **kwargs):
        plan = original(*args, **kwargs)
        e.store.invalidate_draft("acct", URI)
        return plan

    monkeypatch.setattr(module, "plan_resource", edit_during_plan)
    with pytest.raises(RuntimeError, match="being prepared"):
        await embedding_utils.vectorize_file(
            URI, {"name": "manual.md"}, "viking://resources", ctx=e.ctx
        )
    assert not e.queue.messages


async def test_derived_body_gc_roots_include_active_reader_and_pending_revisions(environment):
    e = environment
    old = "# Old\n正文\n" * 20
    await finish(e, await stage(e, old))
    lease, manifest = e.store.capture("acct")
    old_sha = manifest[URI]["content"]["sha256"]
    pending = await stage(e, "# New\n新正文\n" * 20)
    ref, _ = await archive_source(e.fs, URI, e.ctx)
    assert {("acct", old_sha), ("acct", ref["sha256"])} <= e.store.protected_content_revisions()
    await finish(e, pending)
    assert ("acct", old_sha) in e.store.protected_content_revisions()
    e.store.release(lease)
    assert ("acct", old_sha) not in e.store.protected_content_revisions()
    assert ("acct", ref["sha256"]) in e.store.protected_content_revisions()
