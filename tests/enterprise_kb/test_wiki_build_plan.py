"""Shared Wiki preparation with real SQLite, VikingFS and a disk AGFS transport.

Existing namespace/member ACLs are simulated; no export/publication claim.
"""
import asyncio
from dataclasses import replace
import json
from unittest.mock import AsyncMock

import pytest
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding
from test_wiki_catalog import setup

from openviking.indexing.content import blob_path
from openviking.indexing.publication import PublicationStore
from openviking.wiki.build_plan import CaptureLimits, WikiBuildPlanner
from openviking.wiki.schema import SchemaError
from openviking_cli.exceptions import ConflictError, NotFoundError, PermissionDeniedError, FailedPreconditionError
from openviking_cli.session.user_id import UserIdentifier

RAW = 'viking://resources/raw'
ONE = RAW + '/manual/one.md'
TWO = RAW + '/manual/chapter/two.md'


@pytest.fixture
async def prepared(environment):
    e = environment
    e.fs._async_agfs.root.mkdir(parents=True, exist_ok=True)

    async def listing(path, offset=0, limit=None, sort_by=None, sort_order='asc'):
        target = e.fs._async_agfs.path(path)
        if not target.exists():
            raise FileNotFoundError(path)
        children = sorted(target.iterdir(), key=lambda p: p.name, reverse=sort_order == 'desc')
        children = children[offset:] if limit is None else children[offset:offset + limit]
        return [{'name': child.name, 'isDir': child.is_dir(), 'size': child.stat().st_size}
                for child in children]

    e.fs._async_agfs.ls = listing
    await setup(e)
    await e.fs.write(ONE, '# 一章\n\n液压泵故障诊断。', ctx=e.ctx)
    await e.fs.write(TWO, '# 二章\n\n行走跑偏检查。', ctx=e.ctx)
    return e, WikiBuildPlanner(e.fs)


async def prepare(p, ctx, uris=(ONE,), kind='uri', key='task-1', recursive=True):
    return await p.prepare_sources(ctx, 'manual-wiki', uris, input_kind=kind,
                                   idempotency_key=key, recursive=recursive)


async def test_upload_and_uri_share_source_schema_and_namespace_plan(prepared):
    e, p = prepared
    upload = await prepare(p, e.ctx, kind='upload', key='upload')
    existing = await prepare(p, e.ctx, key='existing')
    a, b = json.loads(upload.manifest), json.loads(existing.manifest)
    assert a['collection'] == b['collection']
    assert a['sources'] == b['sources']
    assert a['binding_digest'] == b['binding_digest']
    assert a['authorization_state'] == b['authorization_state'] == 'namespace_export_pending'
    assert upload.state == existing.state == 'prepared'
    assert upload.sources[0].uri == ONE
    assert await p.read_source(e.ctx, upload.build_id, ONE) == '# 一章\n\n液压泵故障诊断。'


async def test_saved_plan_reopens_with_immutable_body_and_gc_root(prepared):
    e, p = prepared
    plan = await prepare(p, e.ctx)
    reopened = WikiBuildPlanner(e.fs, registry=PublicationStore(e.store.path))
    await e.fs.write(ONE, '新内容', ctx=e.ctx)
    assert await reopened.get(e.ctx, plan.build_id) == plan
    assert await reopened.read_source(e.ctx, plan.build_id, ONE) == '# 一章\n\n液压泵故障诊断。'
    assert ('acct', plan.sources[0].sha256) in e.store.protected_content_revisions()
    with e.store.db() as db:
        saved = db.execute('SELECT manifest FROM wiki_build_plans').fetchone()[0]
    assert '液压泵故障诊断' not in saved  # Text lives in account-private AGFS blobs.


async def test_preflight_rejects_external_content_change_even_without_epoch(prepared):
    e, p = prepared
    plan = await prepare(p, e.ctx)
    assert await p.check_current_inputs(e.ctx, plan.build_id) == plan
    epoch = e.store.observe_draft('acct', ONE)
    await e.fs._async_agfs.write(e.fs._uri_to_path(ONE, ctx=e.ctx), b'external edit')
    assert e.store.observe_draft('acct', ONE) == epoch
    with pytest.raises(ConflictError, match='content changed'):
        await p.check_current_inputs(e.ctx, plan.build_id)
    # The saved revision is still available for inspection, not publication.
    assert await p.read_source(e.ctx, plan.build_id, ONE) == '# 一章\n\n液压泵故障诊断。'


async def test_preflight_rejects_directory_additions_and_same_bytes_new_epoch(prepared):
    e, p = prepared
    tree = await prepare(p, e.ctx, (RAW + '/manual',))
    await e.fs.write(RAW + '/manual/added.md', 'added knowledge', ctx=e.ctx)
    with pytest.raises(ConflictError, match='membership'):
        await p.check_current_inputs(e.ctx, tree.build_id)
    file = await prepare(p, e.ctx, key='file')
    content = await p.read_source(e.ctx, file.build_id, ONE)
    await e.fs.write(ONE, content, ctx=e.ctx)
    with pytest.raises(ConflictError, match='before plan'):
        await p.check_current_inputs(e.ctx, file.build_id)


async def test_idempotency_same_sources_replays_and_changed_inputs_conflict(prepared):
    e, p = prepared
    first = await prepare(p, e.ctx, (TWO, ONE, ONE))
    assert await prepare(p, e.ctx, (ONE, TWO)) == first
    with pytest.raises(ConflictError, match='idempotency'):
        await prepare(p, e.ctx, (ONE,))
    await e.fs.write(ONE, 'changed', ctx=e.ctx)
    with pytest.raises(ConflictError, match='idempotency'):
        await prepare(p, e.ctx, (ONE, TWO))


async def test_concurrent_duplicate_preparations_create_one_plan(prepared, monkeypatch):
    import openviking.wiki.build_plan as module
    e, p = prepared
    archive = module.archive_bytes
    arrived = 0
    both_captured = asyncio.Event()
    async def barrier(fs, raw, ctx):
        nonlocal arrived
        reference = await archive(fs, raw, ctx)
        arrived += 1
        if arrived == 2:
            both_captured.set()
        await both_captured.wait()
        return reference
    monkeypatch.setattr(module, 'archive_bytes', barrier)
    a, b = await asyncio.gather(prepare(p, e.ctx), prepare(p, e.ctx))
    assert a == b
    with e.store.db() as db:
        assert db.execute('SELECT count(*) FROM wiki_build_plans').fetchone()[0] == 1


async def test_directory_recursion_preserves_source_uris_and_ignores_control_assets(prepared):
    e, p = prepared
    await e.fs.write(RAW + '/manual/.abstract.md', 'derived', ctx=e.ctx)
    await e.fs.write(RAW + '/manual/_toc.json', '{}', ctx=e.ctx)
    await e.fs._async_agfs.write(e.fs._uri_to_path(RAW + '/manual/image.png', ctx=e.ctx), b'image')
    all_sources = await prepare(p, e.ctx, (RAW + '/manual',))
    assert [source.uri for source in all_sources.sources] == [TWO, ONE]
    shallow = await prepare(p, e.ctx, (RAW + '/manual',), recursive=False, key='shallow')
    assert [source.uri for source in shallow.sources] == [ONE]
    assert len(json.loads(all_sources.manifest)['directories']) == 2


async def test_directory_membership_change_during_capture_rejects_plan(prepared):
    e, p = prepared
    original = e.fs.read_file_bytes
    fired = False
    async def read(uri, **kwargs):
        nonlocal fired
        raw = await original(uri, **kwargs)
        if uri == ONE and not fired:
            fired = True
            await e.fs.write(RAW + '/manual/late.md', 'late', ctx=e.ctx)
        return raw
    e.fs.read_file_bytes = read
    with pytest.raises(ConflictError, match='membership'):
        await prepare(p, e.ctx, (RAW + '/manual',))
    with e.store.db() as db:
        assert db.execute('SELECT count(*) FROM wiki_build_plans').fetchone()[0] == 0


async def test_source_write_after_archive_is_rejected_at_save(prepared, monkeypatch):
    import openviking.wiki.build_plan as module
    e, p = prepared
    archive = module.archive_bytes
    async def changed(fs, raw, ctx):
        ref = await archive(fs, raw, ctx)
        await fs.write(ONE, 'changed during preparation', ctx=ctx)
        return ref
    monkeypatch.setattr(module, 'archive_bytes', changed)
    with pytest.raises(ConflictError, match='before plan'):
        await prepare(p, e.ctx)


async def test_inflight_or_uncertain_source_write_is_never_captured(prepared):
    e, p = prepared
    token = e.store.begin_write('acct', ONE)
    with pytest.raises(FailedPreconditionError, match='in-flight'):
        await prepare(p, e.ctx)
    e.store.uncertain_write(token)
    with pytest.raises(FailedPreconditionError, match='uncertain'):
        await prepare(p, e.ctx)


async def test_source_revocation_before_save_rejects_without_creating_plan(prepared):
    e, p = prepared
    denied = False
    original = e.fs.read_file_bytes
    async def read(uri, **kwargs):
        nonlocal denied
        raw = await original(uri, **kwargs)
        denied = True
        return raw
    async def access(uri, *args, **kwargs):
        if uri == ONE and denied:
            raise PermissionDeniedError('revoked')
    e.fs.read_file_bytes = read
    e.fs._ensure_access = AsyncMock(side_effect=access)
    with pytest.raises(PermissionDeniedError):
        await prepare(p, e.ctx)
    with e.store.db() as db:
        assert db.execute('SELECT count(*) FROM wiki_build_plans').fetchone()[0] == 0


async def test_plan_lookup_and_private_body_require_current_permissions(prepared):
    e, p = prepared
    plan = await prepare(p, e.ctx)
    other = replace(e.ctx, user=UserIdentifier('other', 'user'))
    with pytest.raises(NotFoundError):
        await p.get(other, plan.build_id)
    with pytest.raises(PermissionDeniedError):
        await p.get(replace(e.ctx, bypass_acl=True), plan.build_id)
    e.fs._ensure_access = AsyncMock(side_effect=PermissionDeniedError('revoked'))
    with pytest.raises(PermissionDeniedError):
        await p.read_source(e.ctx, plan.build_id, ONE)


async def test_streaming_sources_stops_on_revocation_before_reading_next_blob(prepared):
    e, p = prepared
    plan = await prepare(p, e.ctx, (ONE, TWO))
    stream = p.iter_source_texts(e.ctx, plan.build_id)
    assert await anext(stream) == (TWO, '# 二章\n\n行走跑偏检查。')
    next_read = AsyncMock(wraps=e.fs._async_agfs.read)
    e.fs._async_agfs.read = next_read
    async def access(uri, *args, **kwargs):
        if uri == ONE:
            raise PermissionDeniedError('revoked')
    e.fs._ensure_access = AsyncMock(side_effect=access)
    with pytest.raises(PermissionDeniedError):
        await anext(stream)
    next_read.assert_not_awaited()


async def test_corrupt_immutable_body_or_manifest_has_no_live_fallback(prepared):
    e, p = prepared
    plan = await prepare(p, e.ctx)
    await e.fs._async_agfs.write(blob_path(e.fs, e.ctx, plan.sources[0].sha256), b'bad')
    with pytest.raises(SchemaError, match='corrupted'):
        await p.read_source(e.ctx, plan.build_id, ONE)
    with e.store.db() as db:
        db.execute("UPDATE wiki_build_plans SET manifest='{}' WHERE id=?", (plan.build_id,))
    with pytest.raises(SchemaError, match='corrupted'):
        await p.get(e.ctx, plan.build_id)


@pytest.mark.parametrize('uris,kind,match', [
    (['viking://resources/elsewhere.md'], 'upload', 'materialized'),
    (['viking://resources/wiki/entity.md'], 'uri', 'output tree'),
    (['viking://resources'], 'uri', 'Viking URI'),
    ([], 'uri', 'nonempty'),
    ([ONE + '#chunk'], 'uri', 'canonical'),
])
async def test_invalid_or_self_ingesting_inputs_are_rejected(prepared, uris, kind, match):
    e, p = prepared
    with pytest.raises(SchemaError, match=match):
        await prepare(p, e.ctx, uris, kind=kind)


@pytest.mark.parametrize('content', [b'', b'\xff\xfe', b'%PDF\x00binary'])
async def test_empty_non_utf8_and_binary_markdown_are_rejected(prepared, content):
    e, p = prepared
    await e.fs._async_agfs.write(e.fs._uri_to_path(ONE, ctx=e.ctx), content)
    with pytest.raises(SchemaError):
        await prepare(p, e.ctx)


@pytest.mark.parametrize('limit,uris', [
    (CaptureLimits(documents=1), (ONE, TWO)),
    (CaptureLimits(document_bytes=8), (ONE,)),
    (CaptureLimits(total_bytes=40), (ONE, TWO)),
    (CaptureLimits(entries=1), (RAW + '/manual',)),
    (CaptureLimits(depth=1), (RAW,)),
])
async def test_capture_limits_explicitly_fail_instead_of_truncating(prepared, limit, uris):
    e, _ = prepared
    p = WikiBuildPlanner(e.fs, limits=limit)
    with pytest.raises(SchemaError, match='limit|bounded'):
        await prepare(p, e.ctx, uris)
