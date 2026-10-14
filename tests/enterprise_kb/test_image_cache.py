"""Private cache restoration and context dependencies across image publication."""

import hashlib
from unittest.mock import AsyncMock

import pytest
from test_image_publication import IMAGE, ROOT, TEXT, consume, image_env, manifest
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding

from openviking.indexing.content import blob_path
from openviking.indexing.image_cache import (
    cache_key,
    load_image_semantics,
    load_twin_image_semantics,
    occurrence_identity,
    semantics_signature,
)
from openviking.indexing.image_context import collect_image_contexts
from openviking.utils.embedding_utils import vectorize_file

COPY = ROOT + "/other/images/a.png"


async def test_uncited_copy_reuses_published_twin_summary_without_vlm(image_env):
    e = image_env
    # Publish the cited copy so its summary is trusted and reusable.
    await consume(e, await stage(e, await prepare(e)))
    published = manifest(e)[IMAGE]["content"]["image_semantics"]["sha256"]

    # A byte-identical copy under another directory cites no markdown of its own.
    await e.fs.write(COPY, b"asset", ctx=e.ctx)
    twin = await load_twin_image_semantics(e.fs, COPY, e.ctx)

    assert twin is not None
    assert twin["summary"] == "purpose 0\n\npurpose 1"
    # The reused record must not claim the twin's citing chapter as its own.
    assert twin["image_occurrences"] == [
        {
            "summary": "purpose 0\n\npurpose 1",
            "context_status": "twin_reuse",
            "asset_sha256": hashlib.sha256(b"asset").hexdigest(),
            "semantics_signature": semantics_signature(),
        }
    ]
    # Reuse reads the published cache, never writes a new one.
    assert manifest(e)[IMAGE]["content"]["image_semantics"]["sha256"] == published


async def test_novel_asset_has_no_twin_and_still_needs_the_model(image_env):
    e = image_env
    await consume(e, await stage(e, await prepare(e)))
    await e.fs.write(COPY, b"genuinely different bytes", ctx=e.ctx)
    assert await load_twin_image_semantics(e.fs, COPY, e.ctx) is None


def test_cache_key_includes_context_signature_so_context_change_invalidates():
    signature = "model-prompt-v1"
    base = [
        {"context_uri": "viking://resources/a.md", "context_hash": "A", "context_epoch": 1},
    ]
    changed_context = [
        {"context_uri": "viking://resources/a.md", "context_hash": "B", "context_epoch": 1},
    ]
    changed_epoch = [
        {"context_uri": "viking://resources/a.md", "context_hash": "A", "context_epoch": 2},
    ]
    assert cache_key(signature, base) == cache_key(signature, list(base))
    assert cache_key(signature, base) != cache_key(signature, changed_context)
    assert cache_key(signature, base) != cache_key(signature, changed_epoch)
    assert cache_key(signature, base) != cache_key("model-prompt-v2", base)
    assert occurrence_identity(base[0]) == ("viking://resources/a.md", "A", 1)


async def test_stored_payload_binds_context_signature_and_load_rejects_stale_key(image_env, monkeypatch):
    # The stored payload carries a cache_key binding the model/prompt signature
    # to every reference context. If a stale/foreign cache presents a key that
    # does not match the current contexts, load must refuse to reuse it.
    import json

    e = image_env
    summary = await prepare(e)
    await consume(e, await stage(e, summary))
    ref = manifest(e)[IMAGE]["content"]["image_semantics"]
    raw = e.fs._handle_agfs_read(
        await e.fs._async_agfs.read(blob_path(e.fs, e.ctx, ref["sha256"]))
    )
    payload = json.loads(raw)
    assert payload["cache_key"] == cache_key(semantics_signature(), payload["occurrences"])
    monkeypatch.setattr(
        "openviking.indexing.image_cache.cache_key", lambda signature, records: "stale-key"
    )
    with pytest.raises(ValueError, match="cache key"):
        await load_image_semantics(e.fs, IMAGE, e.ctx)


async def test_each_joint_occurrence_input_contains_summary_and_image(image_env, embedding, monkeypatch):
    from test_image_context import JointEmbedder

    e = image_env
    await e.fs.write(IMAGE, b"joint asset", ctx=e.ctx)
    await e.fs.write(TEXT, "# Valve\n\n![inspection](images/a.png)\n\n![assembly](images/a.png)\n", ctx=e.ctx)
    e.fs.ls = AsyncMock(return_value=[{"name": "chapter.md", "uri": TEXT}])
    model = JointEmbedder()
    monkeypatch.setattr(type(embedding), "get_embedder", lambda self: model)
    e.handler._embedder = model
    occurrences = await collect_image_contexts(e.fs, IMAGE, e.ctx)
    for index, record in enumerate(occurrences):
        record.update(
            summary=f"purpose {index}",
            asset_sha256=hashlib.sha256(b"joint asset").hexdigest(),
            semantics_signature=semantics_signature(),
        )
    summary = {"name": "a.png", "summary": "combined", "image_occurrences": occurrences}
    await vectorize_file(IMAGE, summary, ROOT + "/images", ctx=e.ctx)
    messages = e.queue.messages
    assert len(messages) == 2
    for message in messages:
        types = [part["type"] for part in message.message]
        assert types == ["text", "image_url"]
        assert message.message[0]["text"].startswith("purpose ")
    assert [m.message[0]["text"] for m in messages] == ["purpose 0", "purpose 1"]


async def prepare(e):
    await e.fs.write(IMAGE, b"asset", ctx=e.ctx)
    await e.fs.write(TEXT, "# Valve\n\n![inspection](images/a.png)\n\n![assembly](images/a.png)\n", ctx=e.ctx)
    e.fs.ls = AsyncMock(return_value=[{"name": "chapter.md", "uri": TEXT}])
    occurrences = await collect_image_contexts(e.fs, IMAGE, e.ctx)
    for i, record in enumerate(occurrences):
        record.update(summary=f"purpose {i}", asset_sha256=hashlib.sha256(b"asset").hexdigest(), semantics_signature=semantics_signature())
    summary = {"name": "a.png", "summary": "combined", "image_occurrences": occurrences}
    return summary


async def stage(e, summary):
    start = len(e.queue.messages)
    await vectorize_file(IMAGE, summary, ROOT + "/images", ctx=e.ctx)
    return e.queue.messages[start:]


async def test_private_cache_survives_restart_and_keeps_same_occurrence_plan(image_env):
    from openviking.indexing.publication import PublicationStore
    e = image_env
    summary = await prepare(e)
    first = await stage(e, summary)
    await consume(e, first)
    old = manifest(e)[IMAGE]
    e.store = PublicationStore(e.store.path)
    restored = await load_image_semantics(e.fs, IMAGE, e.ctx)
    assert restored["image_occurrences"] == summary["image_occurrences"]
    second = await stage(e, restored)
    assert {m.context_data["chunk_id"] for m in second} == {m.context_data["chunk_id"] for m in first}
    assert manifest(e)[IMAGE]["generation"] == old["generation"]
    cache = old["content"]["image_semantics"]
    assert ("acct", cache["sha256"]) in e.store.protected_content_revisions()
    # SQL stores private blob reference and dependency epochs, not summaries.
    with e.store.db() as db:
        reference = db.execute("SELECT reference FROM sources WHERE generation=?", (old["generation"],)).fetchone()[0]
    assert "purpose 0" not in reference


@pytest.mark.parametrize("change", ["asset", "context", "model"])
async def test_cache_rejects_changed_input_or_semantics(image_env, monkeypatch, change):
    e = image_env
    await consume(e, await stage(e, await prepare(e)))
    if change == "asset":
        await e.fs.write(IMAGE, b"new asset", ctx=e.ctx)
    elif change == "context":
        await e.fs.write(TEXT, "new context ![different](images/a.png)", ctx=e.ctx)
    else:
        monkeypatch.setattr("openviking.indexing.image_cache.semantics_signature", lambda: "new model/prompt")
    with pytest.raises(ValueError, match="SEMANTICS_REQUIRED"):
        await load_image_semantics(e.fs, IMAGE, e.ctx)


async def test_context_edit_before_enqueue_is_rejected(image_env):
    e = image_env
    summary = await prepare(e)
    await e.fs.write(TEXT, "edited before enqueue", ctx=e.ctx)
    with pytest.raises(RuntimeError, match="context changed before"):
        await stage(e, summary)
    assert not e.queue.messages


async def test_context_edit_during_embedding_prevents_publication(image_env):
    e = image_env
    summary = await prepare(e)
    await consume(e, await stage(e, summary))
    old = manifest(e)[IMAGE]["generation"]
    messages = await stage(e, summary)
    await consume(e, messages[:1])
    await e.fs.write(TEXT, "edited during embedding", ctx=e.ctx)
    result = await e.handler.on_dequeue({"data": messages[1].to_json()})
    assert result.outcome.value == "failed"
    assert "context changed" in result.error
    assert manifest(e)[IMAGE]["generation"] == old


async def test_cache_corruption_and_source_access_fail_without_fallback(image_env):
    e = image_env
    await consume(e, await stage(e, await prepare(e)))
    ref = manifest(e)[IMAGE]["content"]["image_semantics"]
    e.fs._ensure_access.side_effect = PermissionError("denied")
    with pytest.raises(PermissionError):
        await load_image_semantics(e.fs, IMAGE, e.ctx)
    e.fs._ensure_access.side_effect = None
    await e.fs._async_agfs.write(blob_path(e.fs, e.ctx, ref["sha256"]), b"corrupt")
    with pytest.raises(RuntimeError, match="corrupted"):
        await load_image_semantics(e.fs, IMAGE, e.ctx)


async def test_semantics_configuration_change_after_vlm_rejected_before_enqueue(image_env, monkeypatch):
    e = image_env
    summary = await prepare(e)
    monkeypatch.setattr("openviking.indexing.image_cache.semantics_signature", lambda: "new version")
    with pytest.raises(ValueError, match="changed after"):
        await stage(e, summary)
    assert not e.queue.messages


async def test_cached_fallback_summary_keeps_asset_binding_after_restore(image_env):
    from test_image_publication import stage as simple_stage
    e = image_env
    await consume(e, await simple_stage(e))
    e.fs.ls = AsyncMock(return_value=[])
    cached = await load_image_semantics(e.fs, IMAGE, e.ctx)
    await e.fs.write(IMAGE, b"changed after cache read", ctx=e.ctx)
    with pytest.raises(ValueError, match="changed after"):
        await stage(e, cached)


async def test_cache_gc_roots_include_pending_and_held_query_versions(image_env):
    from copy import deepcopy
    from openviking.indexing.publication import pin_query_snapshot, query_snapshot
    e = image_env
    old = await prepare(e)
    await consume(e, await stage(e, old))
    old_cache = manifest(e)[IMAGE]["content"]["image_semantics"]["sha256"]
    newer = deepcopy(old)
    newer["image_occurrences"][0]["summary"] = "changed meaning"
    @query_snapshot
    async def held(*, ctx):
        # A query pins its view as soon as it passes validation, before any read.
        pin_query_snapshot(ctx)
        messages = await stage(e, newer)
        with e.store.db() as db:
            import json
            ref = json.loads(db.execute("SELECT reference FROM sources WHERE generation=?", (messages[0].context_data["index_generation"],)).fetchone()[0])
        next_cache = ref["image_semantics"]["sha256"]
        assert next_cache != old_cache
        assert {("acct", old_cache), ("acct", next_cache)} <= e.store.protected_content_revisions()
        await consume(e, messages)
        assert ("acct", old_cache) in e.store.protected_content_revisions()
    await held(ctx=e.ctx)
    assert ("acct", old_cache) not in e.store.protected_content_revisions()
