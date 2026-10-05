"""Real HTTP routing and async/sync SDK for knowledge namespace configuration.

The ACL context store and embedding backend are fixtures; these are not
production grants, and the router only registers/reads configuration.
"""
import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from test_wiki_build_plan import prepared as prepared
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding
from test_wiki_namespaces import domains, RAW, ONE, WIKI

from openviking.server.auth import get_request_context
from openviking.server.models import ERROR_CODE_TO_HTTP_STATUS
from openviking.server.routers.namespaces import router, get_namespace_catalog
from openviking_cli.exceptions import OpenVikingError
from openviking_sdk import AsyncHTTPClient, SyncHTTPClient
from openviking_sdk.errors import ConflictError as SDKConflict


@pytest.fixture
def api(domains):
    e, p, c, admin, b = domains
    app = FastAPI()
    app.include_router(router)
    context = [admin]
    app.dependency_overrides[get_request_context] = lambda: context[0]
    app.dependency_overrides[get_namespace_catalog] = lambda: c

    @app.exception_handler(OpenVikingError)
    async def errors(request, exc):
        return JSONResponse(status_code=ERROR_CODE_TO_HTTP_STATUS.get(exc.code, 500), content={
            'status': 'error', 'error': {'code': exc.code, 'message': exc.message, 'details': exc.details}})

    return app, e, context, c


def client(app):
    c = AsyncHTTPClient(url='http://test', api_key='', extra_headers={})
    c._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
    return c


async def test_async_sdk_registers_reads_and_grants_over_http(api):
    app, e, ctx, catalog = api
    c = client(app)
    try:
        registered = await c.kb.register_namespace(namespace_id='manuals', root_uri=RAW)
        assert registered['namespace_id'] == 'manuals' and registered['root_uri'] == RAW
        assert (await c.kb.get_namespace('manuals'))['root_uri'] == RAW
        granted = await c.kb.set_export(source_namespace='manuals', target_namespace='wiki',
                                        principal='user:user', allowed=True)
        assert granted == {'source_namespace': 'manuals', 'target_namespace': 'wiki',
                           'principal': 'user:user', 'allowed': True}
        revoked = await c.kb.set_export(source_namespace='manuals', target_namespace='wiki',
                                        principal='user:user', allowed=False)
        assert revoked['allowed'] is False
    finally:
        await c.close()


def test_sync_sdk_uses_same_http_routes_and_result_contract(api):
    app, e, ctx, catalog = api
    c = SyncHTTPClient(url='http://test', api_key='', extra_headers={})
    c._async_client._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
    try:
        c.kb.register_namespace(namespace_id='manuals', root_uri=RAW)
        assert c.kb.get_namespace('manuals')['namespace_id'] == 'manuals'
    finally:
        c.close()


async def test_http_extra_fields_and_bad_types_are_rejected(api):
    app, e, ctx, catalog = api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        response = await c.post('/api/v1/kb/namespaces',
                                json={'namespace_id': 'manuals', 'root_uri': RAW, 'account_id': 'other'})
        assert response.status_code == 422
        response = await c.post('/api/v1/kb/namespaces', json={'namespace_id': 'manuals'})
        assert response.status_code == 422
        response = await c.post('/api/v1/kb/namespaces/manuals/exports',
                                json={'target_namespace': 'wiki', 'principal': 'user:user', 'allowed': 'true'})
        assert response.status_code == 422
    with e.store.db() as db:
        assert db.execute('SELECT count(*) FROM kb_namespaces').fetchone()[0] == 2


async def test_http_overlap_conflict_maps_to_409(api):
    app, e, ctx, catalog = api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        response = await c.post('/api/v1/kb/namespaces',
                                json={'namespace_id': 'nested', 'root_uri': RAW + '/manual'})
        assert response.status_code == 409 and response.json()['error']['code'] == 'CONFLICT'


async def test_http_file_uri_root_is_precondition_failure_not_conflict(api):
    app, e, ctx, catalog = api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        response = await c.post('/api/v1/kb/namespaces', json={'namespace_id': 'file', 'root_uri': ONE})
        assert response.status_code == 412
        assert response.json()['error']['code'] == 'FAILED_PRECONDITION'


async def test_sdk_preserves_conflict_error_type(api):
    app, e, ctx, catalog = api
    c = client(app)
    try:
        with pytest.raises(SDKConflict):
            await c.kb.register_namespace(namespace_id='nested', root_uri=RAW + '/manual')
    finally:
        await c.close()


def test_openapi_exposes_namespace_configuration_only(api):
    app, _, _, _ = api
    spec = app.openapi()
    assert set(spec['paths']) == {
        '/api/v1/kb/namespaces',
        '/api/v1/kb/namespaces/{namespace_id}',
        '/api/v1/kb/namespaces/{source_namespace}/exports',
    }
    assert spec['components']['schemas']['NamespaceRequest']['additionalProperties'] is False
    assert 'account_id' not in spec['components']['schemas']['NamespaceRequest']['properties']