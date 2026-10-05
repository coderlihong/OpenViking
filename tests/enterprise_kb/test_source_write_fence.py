"""Real VikingFS await windows cannot be mistaken for stable source revisions."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from test_content_publication import URI, environment as environment, finish, stage
from test_index_plan import embedding as embedding
from test_image_publication import image_env as image_env
from test_semantic_content_publication import ROOT, directory

from openviking.indexing.content import archive_source
from openviking.indexing.publication import PublicationStore


@pytest.mark.parametrize("method", ["write", "write_file", "write_file_bytes", "append_file"])
async def test_each_standard_write_blocks_capture_until_storage_confirms(environment, method):
    e = environment
    await e.fs.write(URI, "old", ctx=e.ctx)
    started, release = asyncio.Event(), asyncio.Event()
    original_write, original_read = e.fs._async_agfs.write, e.fs._async_agfs.read
    async def paused(path, raw, **kwargs):
        started.set()
        await release.wait()
        return await original_write(path, raw, **kwargs)
    async def read(path, offset=0, size=-1, **kwargs):
        return await original_read(path, offset, size)
    e.fs._async_agfs.write = paused
    e.fs._async_agfs.read = read
    e.fs._ensure_parent_dirs = AsyncMock()
    e.fs._async_agfs.pathlock_acquire_exact = AsyncMock(return_value={"lease_ref": "fixture", "path": e.fs._uri_to_path(URI, ctx=e.ctx)})
    e.fs._async_agfs.pathlock_release = AsyncMock()
    value = b"new" if method in {"write", "write_file_bytes"} else "new"
    task = asyncio.create_task(getattr(e.fs, method)(URI, value, ctx=e.ctx))
    try:
        await asyncio.wait_for(started.wait(), 3)
    except TimeoutError:
        release.set()
        await task
        raise
    try:
        assert e.store.pending_writes("acct", URI)[0]["state"] == "writing"
        with pytest.raises(RuntimeError, match="in-flight"):
            await archive_source(e.fs, URI, e.ctx)
    finally:
        release.set()
        await task
    assert not e.store.pending_writes("acct", URI)
    reference, raw = await archive_source(e.fs, URI, e.ctx)
    assert raw == (b"oldnew" if method == "append_file" else b"new")
    assert reference["draft_epoch"] == e.store.observe_draft("acct", URI)


def test_multiple_writers_and_registry_restart_preserve_all_fences(tmp_path):
    store = PublicationStore(str(tmp_path / "db"))
    initial = store.observe_draft("acct", URI)
    a, b = store.begin_write("acct", URI), store.begin_write("acct", URI)
    restarted = PublicationStore(store.path)
    restarted.finish_write(a)
    with pytest.raises(RuntimeError, match="in-flight"):
        store.observe_draft("acct", URI)
    assert [x["id"] for x in store.pending_writes("acct", URI)] == [b]
    assert store.observe_draft("other", URI) == 0
    assert store.observe_draft("acct", URI + ".other") == 0
    restarted.finish_write(b)
    assert store.observe_draft("acct", URI) == initial + 4
    with pytest.raises(RuntimeError):
        store.finish_write(b)


@pytest.mark.parametrize("failure", ["cancel", "transport"])
async def test_uncertain_write_keeps_old_public_version_and_blocks_new_capture(environment, failure):
    e = environment
    await finish(e, await stage(e, "published old"))
    started = asyncio.Event()
    async def failing(path, raw, **kwargs):
        started.set()
        if failure == "transport":
            raise IOError("transport result unknown")
        await asyncio.Event().wait()
    e.fs._async_agfs.write = failing
    task = asyncio.create_task(e.fs.write(URI, "new", ctx=e.ctx))
    await started.wait()
    if failure == "cancel":
        task.cancel()
    with pytest.raises((asyncio.CancelledError, IOError)):
        await task
    restarted = PublicationStore(e.store.path)
    writes = restarted.pending_writes("acct", URI)
    assert len(writes) == 1 and writes[0]["state"] == "uncertain"
    with pytest.raises(RuntimeError, match="uncertain"):
        await archive_source(e.fs, URI, e.ctx)
    with pytest.raises(RuntimeError, match="confirmed"):
        restarted.finish_write(writes[0]["id"])
    assert await e.fs.read_file(URI, ctx=e.ctx) == "published old"


async def test_write_started_after_capture_rejects_begin_and_final_publication(environment):
    e = environment
    await e.fs.write(URI, "body", ctx=e.ctx)
    ref, _ = await archive_source(e.fs, URI, e.ctx)
    token = e.store.begin_write("acct", URI)
    with pytest.raises(RuntimeError, match="in-flight"):
        e.store.begin("acct", "resources", URI, "plan", ["member"], source_ref=ref)
    e.store.finish_write(token)
    with pytest.raises(RuntimeError, match="changed"):
        e.store.begin("acct", "resources", URI, "plan", ["member"], source_ref=ref)
    fresh, _ = await archive_source(e.fs, URI, e.ctx)
    generation = e.store.begin("acct", "resources", URI, "plan", ["member"], source_ref=fresh)
    e.store.seal(generation)
    token = e.store.begin_write("acct", URI)
    assert not e.store.acknowledge(generation, "member")
    assert e.store.state(generation)["state"] == "failed"
    e.store.finish_write(token)


async def test_directory_sidecar_inflight_write_cannot_publish_new_vectors(environment):
    e = environment
    old = await directory(e, "old")
    await finish(e, old)
    newer = await directory(e, "new")
    token = e.store.begin_write("acct", ROOT + "/.overview.md")
    result = None
    for message in newer:
        result = await e.handler.on_dequeue({"data": message.to_json()})
    assert result.outcome.value == "failed"
    assert "in-flight" in result.error
    assert await e.fs.overview(ROOT, ctx=e.ctx) == "old overview"
    e.store.finish_write(token)


async def test_image_context_inflight_write_keeps_old_image_generation(image_env):
    from test_image_cache import prepare, stage as image_stage
    from test_image_publication import IMAGE, TEXT, consume, manifest
    e = image_env
    summary = await prepare(e)
    await consume(e, await image_stage(e, summary))
    old = manifest(e)[IMAGE]["generation"]
    messages = await image_stage(e, summary)
    token = e.store.begin_write("acct", TEXT)
    try:
        last = None
        for message in messages:
            last = await e.handler.on_dequeue({"data": message.to_json()})
        assert last.outcome.value == "failed"
        assert "in-flight" in last.error
        assert manifest(e)[IMAGE]["generation"] == old
    finally:
        e.store.finish_write(token)
