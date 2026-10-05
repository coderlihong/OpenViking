"""Real HTTP routing and async/sync SDK for the W2 URI+collection build entry.

The ACL context store and embedding backend are fixtures; the route only
prepares an immutable source plan and does not run extraction or publication.
"""
import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from test_wiki_build_plan import prepared as prepared, ONE, TWO
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding

from openviking.server.auth import get_request_context
from openviking.server.models import ERROR_CODE_TO_HTTP_STATUS
from openviking.server.routers.wiki import router, get_planner
from openviking_cli.exceptions import OpenVikingError
from openviking_sdk import AsyncHTTPClient, SyncHTTPClient


@pytest.fixture
def api(prepared):
    e, p = prepared
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_request_context] = lambda: e.ctx
    app.dependency_overrides[get_planner] = lambda: p

    @app.exception_handler(OpenVikingError)
    async def errors(request, exc):
        return JSONResponse(status_code=ERROR_CODE_TO_HTTP_STATUS.get(exc.code, 500), content={
            'status': 'error', 'error': {'code': exc.code, 'message': exc.message, 'details': exc.details}})

    return app, e, p


def client(app):
    c = AsyncHTTPClient(url='http://test', api_key='', extra_headers={})
    c._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
    return c


async def test_async_sdk_prepares_uri_build_plan_over_http(api):
    app, e, p = api
    c = client(app)
    try:
        plan = await c.kb.build_wiki(source_uris=[ONE, TWO], collection_id='manual-wiki',
                                     idempotency_key='task-1')
        assert plan['state'] == 'prepared'
        assert plan['build_id']
        stored = await p.get(e.ctx, plan['build_id'])
        assert {s.uri for s in stored.sources} == {ONE, TWO}
    finally:
        await c.close()


def test_sync_sdk_uses_same_route_and_contract(api):
    app, e, p = api
    c = SyncHTTPClient(url='http://test', api_key='', extra_headers={})
    c._async_client._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
    try:
        plan = c.kb.build_wiki(source_uris=[ONE], collection_id='manual-wiki', idempotency_key='sync-1')
        assert plan['state'] == 'prepared'
    finally:
        c.close()


async def test_idempotent_replay_returns_same_build_id(api):
    app, e, p = api
    c = client(app)
    try:
        first = await c.kb.build_wiki(source_uris=[ONE], collection_id='manual-wiki', idempotency_key='k')
        second = await c.kb.build_wiki(source_uris=[ONE], collection_id='manual-wiki', idempotency_key='k')
        assert first['build_id'] == second['build_id']
    finally:
        await c.close()


async def test_extra_or_missing_fields_are_rejected(api):
    app, e, p = api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        response = await c.post('/api/v1/kb/wiki/builds', json={
            'source_uris': [ONE], 'collection_id': 'manual-wiki',
            'idempotency_key': 'k', 'namespace_id': 'sneaky'})
        assert response.status_code == 422
        response = await c.post('/api/v1/kb/wiki/builds', json={'source_uris': [ONE], 'collection_id': 'manual-wiki'})
        assert response.status_code == 422


async def test_non_markdown_source_is_http_bad_input(api):
    app, e, p = api
    binary = 'viking://resources/raw/manual/binary.pdf'
    await e.fs.write(binary, 'not markdown', ctx=e.ctx)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        response = await c.post('/api/v1/kb/wiki/builds', json={
            'source_uris': [binary], 'collection_id': 'manual-wiki', 'idempotency_key': 'k'})
        assert response.status_code == 400 and response.json()['error']['code'] == 'INVALID_ARGUMENT'


def test_openapi_exposes_build_and_configuration_paths(api):
    app, _, _ = api
    spec = app.openapi()
    assert '/api/v1/kb/wiki/builds' in spec['paths']
    assert spec['components']['schemas']['BuildRequest']['additionalProperties'] is False

@pytest.mark.parametrize('key', ['', 'x' * 129, 'x' * 256, 'bad key', '键', 'bad/key', 7, 'x\n'])
async def test_build_key_boundaries_reject_before_persistence(api, key):
    app, e, _ = api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        response = await c.post('/api/v1/kb/wiki/builds', json={
            'source_uris': [ONE], 'collection_id': 'manual-wiki', 'idempotency_key': key})
        assert response.status_code == 422
    with e.store.db() as db:
        assert db.execute('SELECT count(*) FROM wiki_build_plans').fetchone()[0] == 0


async def test_build_128_character_key_returns_http_200_prepared(api):
    app, _, _ = api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        response = await c.post('/api/v1/kb/wiki/builds', json={
            'source_uris': [ONE], 'collection_id': 'manual-wiki', 'idempotency_key': 'a_.+-' + 'x' * 123})
        assert response.status_code == 200
        assert response.json()['result']['state'] == 'prepared'


def test_openapi_matches_preparation_key_and_defaults(api):
    app, _, _ = api
    models = app.openapi()['components']['schemas']
    build, ingestion = models['BuildRequest'], models['IngestionRequest']
    for model in (build, ingestion):
        key = model['properties']['idempotency_key']
        assert key['minLength'] == 1 and key['maxLength'] == 128
        assert key['pattern'] == r'^[A-Za-z0-9_.+\-]+$'
        assert 'idempotency_key' in model['required']
    assert build['properties']['recursive']['default'] is True
    assert ingestion['properties']['update_policy']['default'] == 'fail'
    assert ingestion['properties']['update_policy']['enum'] == ['fail', 'replace']


async def test_real_application_maps_invalid_prepare_key_to_sdk_bad_input(api):
    from openviking.server.app import create_app
    from openviking.server.config import ServerConfig
    from openviking_sdk.errors import InvalidArgumentError

    _, e, p = api
    app = create_app(config=ServerConfig())
    app.dependency_overrides[get_request_context] = lambda: e.ctx
    app.dependency_overrides[get_planner] = lambda: p
    c = client(app)
    try:
        response = await c._http.post('/api/v1/kb/wiki/builds', json={
            'source_uris': [ONE], 'collection_id': 'manual-wiki', 'idempotency_key': 'x' * 129})
        assert response.status_code == 400
        assert response.json()['error']['code'] == 'INVALID_ARGUMENT'
        with pytest.raises(InvalidArgumentError):
            await c.kb.build_wiki(source_uris=[ONE], collection_id='manual-wiki', idempotency_key='x' * 129)
    finally:
        await c.close()


def test_real_application_sync_sdk_preserves_bad_input_code(api):
    from openviking.server.app import create_app
    from openviking.server.config import ServerConfig
    from openviking_sdk.errors import InvalidArgumentError

    _, e, p = api
    app = create_app(config=ServerConfig())
    app.dependency_overrides[get_request_context] = lambda: e.ctx
    app.dependency_overrides[get_planner] = lambda: p
    c = SyncHTTPClient(url='http://test', api_key='', extra_headers={})
    c._async_client._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
    try:
        with pytest.raises(InvalidArgumentError):
            c.kb.build_wiki(source_uris=[ONE], collection_id='manual-wiki', idempotency_key='x' * 129)
    finally:
        c.close()
