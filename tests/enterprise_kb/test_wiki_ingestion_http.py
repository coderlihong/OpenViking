"""Real HTTP routing and async/sync SDK for the W1 upload+collection entry.

The temp-upload store, ACL context store and embedding backend are fixtures; the
route only materializes the uploaded Markdown under the collection raw prefix and
prepares an immutable source plan. It does not run extraction or publication.
"""
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from test_wiki_build_plan import prepared as prepared
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding

from openviking.server.auth import get_request_context
from openviking.server.models import ERROR_CODE_TO_HTTP_STATUS
from openviking.server.routers.wiki import router, get_planner, get_temp_upload_store
from openviking.server.temp_upload_store import ResolvedTempUpload
from openviking_cli.exceptions import OpenVikingError
from openviking_sdk import AsyncHTTPClient, SyncHTTPClient

RAW = 'viking://resources/raw'
NAME = 'manual.md'
MATERIALIZED = f'{RAW}/{NAME}/{NAME}'


@pytest.fixture
def api(prepared, tmp_path):
    e, p = prepared
    uploads = {}

    async def resolve_for_consume(temp_file_id, ctx):
        entry = uploads[temp_file_id]
        return ResolvedTempUpload('local', temp_file_id, entry['name'], str(entry['path']))

    store = SimpleNamespace(resolve_for_consume=resolve_for_consume)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_request_context] = lambda: e.ctx
    app.dependency_overrides[get_planner] = lambda: p
    app.dependency_overrides[get_temp_upload_store] = lambda: store

    @app.exception_handler(OpenVikingError)
    async def errors(request, exc):
        return JSONResponse(status_code=ERROR_CODE_TO_HTTP_STATUS.get(exc.code, 500), content={
            'status': 'error', 'error': {'code': exc.code, 'message': exc.message, 'details': exc.details}})

    return app, e, p, uploads, tmp_path


def upload(api, tmp_path, temp_file_id, text, name=NAME):
    _, _, _, uploads, _ = api
    path = tmp_path / f'{temp_file_id}.md'
    path.write_text(text, encoding='utf-8')
    uploads[temp_file_id] = {'name': name, 'path': path}
    return temp_file_id


def client(app):
    c = AsyncHTTPClient(url='http://test', api_key='', extra_headers={})
    c._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
    return c


async def test_async_sdk_materializes_upload_and_prepares_plan(api):
    app, e, p, uploads, tmp_path = api
    upload(api, tmp_path, 't1', '# 一章\n\n液压泵故障诊断。')
    c = client(app)
    try:
        plan = await c.kb.ingest_wiki(temp_file_id='t1', collection_id='manual-wiki',
                                      idempotency_key='upload-1')
        assert plan['state'] == 'prepared'
        stored = await p.get(e.ctx, plan['build_id'])
        assert [s.uri for s in stored.sources] == [MATERIALIZED]
        assert await p.read_source(e.ctx, plan['build_id'], MATERIALIZED) == '# 一章\n\n液压泵故障诊断。'
        assert await e.fs.exists(MATERIALIZED, ctx=e.ctx)
    finally:
        await c.close()


def test_sync_sdk_uses_same_route_and_contract(api):
    app, e, p, uploads, tmp_path = api
    upload(api, tmp_path, 't2', '# 二章\n\n行走跑偏检查。')
    c = SyncHTTPClient(url='http://test', api_key='', extra_headers={})
    c._async_client._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
    try:
        plan = c.kb.ingest_wiki(temp_file_id='t2', collection_id='manual-wiki', idempotency_key='sync-1')
        assert plan['state'] == 'prepared'
    finally:
        c.close()


async def test_idempotent_replay_returns_same_build_id(api):
    app, e, p, uploads, tmp_path = api
    upload(api, tmp_path, 't3', '# 一章\n\n液压泵故障诊断。')
    c = client(app)
    try:
        first = await c.kb.ingest_wiki(temp_file_id='t3', collection_id='manual-wiki', idempotency_key='k')
        second = await c.kb.ingest_wiki(temp_file_id='t3', collection_id='manual-wiki', idempotency_key='k')
        assert first['build_id'] == second['build_id']
    finally:
        await c.close()


async def test_existing_target_conflicts_unless_replace(api):
    app, e, p, uploads, tmp_path = api
    upload(api, tmp_path, 't4', '# 一章\n\n液压泵故障诊断。')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        first = await c.post('/api/v1/kb/wiki/ingestions', json={
            'temp_file_id': 't4', 'collection_id': 'manual-wiki', 'idempotency_key': 'k1'})
        assert first.status_code == 200
        upload(api, tmp_path, 't5', '# 一章\n\n已修订的液压泵故障诊断。')
        conflict = await c.post('/api/v1/kb/wiki/ingestions', json={
            'temp_file_id': 't5', 'collection_id': 'manual-wiki', 'idempotency_key': 'k2'})
        assert conflict.status_code == 409
        replaced = await c.post('/api/v1/kb/wiki/ingestions', json={
            'temp_file_id': 't5', 'collection_id': 'manual-wiki',
            'update_policy': 'replace', 'idempotency_key': 'k3'})
        assert replaced.status_code == 200
    assert await p.read_source(e.ctx, replaced.json()['result']['build_id'], MATERIALIZED) \
        == '# 一章\n\n已修订的液压泵故障诊断。'


async def test_extra_or_missing_fields_are_rejected(api):
    app, e, p, uploads, tmp_path = api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        response = await c.post('/api/v1/kb/wiki/ingestions', json={
            'temp_file_id': 't', 'collection_id': 'manual-wiki',
            'idempotency_key': 'k', 'source_uris': ['x']})
        assert response.status_code == 422
        response = await c.post('/api/v1/kb/wiki/ingestions', json={
            'temp_file_id': 't', 'collection_id': 'manual-wiki'})
        assert response.status_code == 422
        response = await c.post('/api/v1/kb/wiki/ingestions', json={
            'temp_file_id': 't', 'collection_id': 'manual-wiki',
            'update_policy': 'merge', 'idempotency_key': 'k'})
        assert response.status_code == 422


async def test_unsupported_upload_is_http_bad_input(api):
    app, e, p, uploads, tmp_path = api
    upload(api, tmp_path, 't6', 'archive bytes', name='manual.zip')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        response = await c.post('/api/v1/kb/wiki/ingestions', json={
            'temp_file_id': 't6', 'collection_id': 'manual-wiki', 'idempotency_key': 'k'})
        assert response.status_code == 400 and response.json()['error']['code'] == 'INVALID_ARGUMENT'


@pytest.mark.parametrize('suffix', ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx'])
async def test_document_upload_invokes_shared_materializer_and_captures_tree(api, monkeypatch, suffix):
    from openviking.server.routers import wiki

    app, e, p, _, tmp_path = api
    upload(api, tmp_path, 'document', 'parser fixture', name='routing-doc' + suffix)
    calls = []

    async def ingest(**kwargs):
        calls.append(kwargs)
        root = kwargs['to']
        await e.fs.write_file_bytes(root + '/index.md', b'# Container\n', ctx=e.ctx)
        await e.fs.write_file_bytes(root + '/chapter/body.md', b'# Chapter\nRepair evidence.', ctx=e.ctx)
        return {'status': 'ok', 'root_uri': root}

    monkeypatch.setattr(wiki, 'get_service', lambda: SimpleNamespace(
        resources=SimpleNamespace(_execute_resource_ingestion=ingest)))
    c = client(app)
    try:
        plan = await c.kb.ingest_wiki(temp_file_id='document', collection_id='manual-wiki',
                                    idempotency_key='document-' + suffix[1:])
        stored = await p.get(e.ctx, plan['build_id'])
        assert plan['state'] == 'prepared'
        assert {s.uri for s in stored.sources} == {
            RAW + '/routing-doc/index.md', RAW + '/routing-doc/chapter/body.md'}
        assert len(calls) == 1
        assert calls[0]['source_name'] == 'routing-doc' + suffix
        assert calls[0]['ctx'] is e.ctx
        assert calls[0]['build_index'] is False
        assert calls[0]['summarize'] is False
        assert calls[0]['to_is_directory'] is True
    finally:
        await c.close()


def test_openapi_exposes_ingestion_and_build_paths(api):
    app, _, _, _, _ = api
    spec = app.openapi()
    assert '/api/v1/kb/wiki/ingestions' in spec['paths']
    assert spec['components']['schemas']['IngestionRequest']['additionalProperties'] is False

@pytest.mark.parametrize('key', ['', 'x' * 129, 'x' * 256, 'bad key', '键', 'bad/key', 7, 'x\n'])
async def test_ingestion_key_boundaries_do_not_consume_upload(api, key):
    app, e, _, _, tmp_path = api
    upload(api, tmp_path, 'boundary', '# Boundary')
    path = tmp_path / 'boundary.md'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        response = await c.post('/api/v1/kb/wiki/ingestions', json={
            'temp_file_id': 'boundary', 'collection_id': 'manual-wiki', 'idempotency_key': key})
        assert response.status_code == 422
    assert path.read_text() == '# Boundary'
    assert not await e.fs.exists(MATERIALIZED, ctx=e.ctx)
    with e.store.db() as db:
        assert db.execute('SELECT count(*) FROM wiki_build_plans').fetchone()[0] == 0


async def test_ingestion_128_character_key_returns_http_200_prepared(api):
    app, _, _, _, tmp_path = api
    upload(api, tmp_path, 'accepted', '# Boundary')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        response = await c.post('/api/v1/kb/wiki/ingestions', json={
            'temp_file_id': 'accepted', 'collection_id': 'manual-wiki', 'idempotency_key': 'a_.+-' + 'x' * 123})
        assert response.status_code == 200
        assert response.json()['result']['state'] == 'prepared'
