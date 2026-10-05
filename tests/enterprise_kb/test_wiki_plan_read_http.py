"""Preparation-plan reads and bounded upload consumption, without job execution."""
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest
from test_wiki_build_http import api as api, client
from test_wiki_build_plan import prepared as prepared, ONE
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding
from test_wiki_ingestion_http import api as upload_api, upload, MATERIALIZED

from openviking.server.auth import get_request_context
from openviking.server.routers.wiki import _read_bounded_upload
from openviking.wiki.build_plan import CaptureLimits
from openviking_cli.exceptions import PermissionDeniedError as ServerPermissionDeniedError
from openviking_sdk.errors import NotFoundError, PermissionDeniedError, InvalidArgumentError
from openviking_cli.session.user_id import UserIdentifier
from openviking_sdk import SyncHTTPClient


async def test_async_sdk_reads_identical_persisted_plan(api):
    app, e, p = api
    c = client(app)
    try:
        original = await c.kb.build_wiki(source_uris=[ONE], collection_id='manual-wiki', idempotency_key='read')
        assert await c.kb.get_wiki_build(original['build_id']) == original
        assert original['state'] == 'prepared'
    finally:
        await c.close()


def test_sync_sdk_reads_identical_persisted_plan(api):
    app, e, p = api
    c = SyncHTTPClient(url='http://test', api_key='', extra_headers={})
    c._async_client._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
    try:
        original = c.kb.build_wiki(source_uris=[ONE], collection_id='manual-wiki', idempotency_key='read-sync')
        assert c.kb.get_wiki_build(original['build_id']) == original
    finally:
        c.close()


async def test_plan_read_rechecks_source_acl(api):
    app, e, p = api
    plan = await p.prepare_sources(e.ctx, 'manual-wiki', [ONE], input_kind='uri', idempotency_key='revoke')
    old = e.fs._ensure_access

    async def denied(uri, ctx, **kwargs):
        if uri == ONE:
            raise ServerPermissionDeniedError('source permission revoked')
        return await old(uri, ctx, **kwargs)

    e.fs._ensure_access = denied
    c = client(app)
    try:
        with pytest.raises(PermissionDeniedError, match='revoked'):
            await c.kb.get_wiki_build(plan.build_id)
    finally:
        await c.close()


async def test_plan_read_is_account_scoped(api):
    app, e, p = api
    plan = await p.prepare_sources(e.ctx, 'manual-wiki', [ONE], input_kind='uri', idempotency_key='account')
    app.dependency_overrides[get_request_context] = lambda: replace(e.ctx, user=UserIdentifier('other', 'user'))
    c = client(app)
    try:
        with pytest.raises(NotFoundError):
            await c.kb.get_wiki_build(plan.build_id)
    finally:
        await c.close()


async def test_missing_and_invalid_plan_ids_are_explicit(api):
    app, e, p = api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        assert (await c.get('/api/v1/kb/wiki/builds/missing')).status_code == 404
        assert (await c.get('/api/v1/kb/wiki/builds/bad%20id')).status_code == 400
    assert '/api/v1/kb/wiki/builds/{build_id}' in app.openapi()['paths']


async def test_oversized_upload_is_rejected_before_materialization_and_cleaned(upload_api):
    app, e, p, entries, tmp_path = upload_api
    p.limits = CaptureLimits(document_bytes=32)
    upload(upload_api, tmp_path, 'oversized', '#' + 'x' * 100)
    from openviking.server.routers.wiki import get_temp_upload_store
    original_store = app.dependency_overrides[get_temp_upload_store]()
    resolve = original_store.resolve_for_consume
    cleanup = AsyncMock()

    async def tracked(*args):
        resolved = await resolve(*args)
        resolved.cleanup = cleanup
        return resolved

    original_store.resolve_for_consume = tracked
    c = client(app)
    try:
        with pytest.raises(InvalidArgumentError, match='size limit'):
            await c.kb.ingest_wiki(temp_file_id='oversized', collection_id='manual-wiki', idempotency_key='limit')
        assert not await e.fs.exists(MATERIALIZED, ctx=e.ctx)
        cleanup.assert_awaited_once()
        with e.store.db() as db:
            assert db.execute('SELECT count(*) FROM wiki_build_plans').fetchone()[0] == 0
    finally:
        await c.close()


def test_bounded_reader_accepts_limit_and_never_reads_unbounded(monkeypatch):
    from io import BytesIO
    from pathlib import Path
    calls = []

    class Stream(BytesIO):
        def read(self, size=-1):
            calls.append(size)
            assert size == 33
            return super().read(size)

    monkeypatch.setattr(Path, 'open', lambda *args: Stream(b'x' * 32))
    assert _read_bounded_upload('ignored', 32) == b'x' * 32
    assert calls == [33]


@pytest.mark.parametrize('change', ['content', 'filename', 'source-edited'])
async def test_reused_upload_key_never_changes_source_before_conflict(upload_api, change):
    app, e, p, entries, tmp_path = upload_api
    upload(upload_api, tmp_path, 'original', '# Original')
    c = client(app)
    try:
        await c.kb.ingest_wiki(temp_file_id='original', collection_id='manual-wiki', idempotency_key='same')
        name = 'other.md' if change == 'filename' else 'manual.md'
        text = '# Changed' if change == 'content' else '# Original'
        if change == 'source-edited':
            await e.fs.write(MATERIALIZED, '# Externally edited', ctx=e.ctx)
        expected = await e.fs.read_file_bytes(MATERIALIZED, ctx=e.ctx)
        epoch = e.store.observe_draft(e.ctx.account_id, MATERIALIZED)
        upload(upload_api, tmp_path, 'replacement', text, name=name)
        from openviking_sdk.errors import ConflictError
        with pytest.raises(ConflictError):
            await c.kb.ingest_wiki(temp_file_id='replacement', collection_id='manual-wiki',
                                 update_policy='replace', idempotency_key='same')
        assert await e.fs.read_file_bytes(MATERIALIZED, ctx=e.ctx) == expected
        assert e.store.observe_draft(e.ctx.account_id, MATERIALIZED) == epoch
        if change == 'filename':
            assert not await e.fs.exists('viking://resources/raw/other.md/other.md', ctx=e.ctx)
    finally:
        await c.close()
