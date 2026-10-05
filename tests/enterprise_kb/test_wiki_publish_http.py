"""Real HTTP routing for the private wiki merge publication endpoint.

The extraction model and ACL store are fixtures; the route publishes an
already-validated private merge receipt into the collection target tree.
"""
import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from openviking.server.auth import get_request_context
from openviking.server.models import ERROR_CODE_TO_HTTP_STATUS
from openviking.server.routers.wiki import router, get_merger
from openviking_cli.exceptions import OpenVikingError
from openviking_sdk import AsyncHTTPClient

from test_content_publication import environment as environment
from test_index_plan import embedding as embedding
from test_wiki_build_plan import prepared as prepared
from test_wiki_candidate_merge import extracted, merged, merging
from test_wiki_namespaces import domains as domains, ONE


@pytest.fixture
def api(merging):
    e, p, c, a, b, bundle, model, extractor, merger = merging
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_request_context] = lambda: e.ctx
    app.dependency_overrides[get_merger] = lambda: merger

    @app.exception_handler(OpenVikingError)
    async def errors(request, exc):
        return JSONResponse(status_code=ERROR_CODE_TO_HTTP_STATUS.get(exc.code, 500), content={
            'status': 'error', 'error': {'code': exc.code, 'message': exc.message, 'details': exc.details}})

    return app, e, merger


def client(app):
    c = AsyncHTTPClient(url='http://test', api_key='', extra_headers={})
    c._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
    return c


async def test_http_publish_materializes_admitted_groups(api, merging):
    app, e, merger = api
    selection = await extracted(merging, ONE, key='one')
    receipt, _ = await merged(merging, selection)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as http:
        response = await http.post('/api/v1/kb/wiki/merges/publish', json={
            'reference': receipt['reference'], 'revision': receipt['revision']})
        assert response.status_code == 200
        result = response.json()['result']
    assert result['state'] == 'private_published' and result['published_count'] >= 1
    page = await e.fs.read(result['pages'][0]['uri'], ctx=e.ctx)
    page = page.decode() if isinstance(page, bytes) else page
    assert 'business_code: P-001' in page


async def test_http_stale_revision_conflict_maps_to_409(api, merging):
    app, e, merger = api
    selection = await extracted(merging, ONE, key='one')
    receipt, _ = await merged(merging, selection)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as http:
        response = await http.post('/api/v1/kb/wiki/merges/publish', json={
            'reference': receipt['reference'], 'revision': '0' * 64})
        assert response.status_code == 409
        assert response.json()['error']['code'] == 'CONFLICT'


async def test_http_bad_receipt_shape_maps_to_400_or_422(api, merging):
    app, e, merger = api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as http:
        response = await http.post('/api/v1/kb/wiki/merges/publish', json={
            'reference': {'sha256': 'z' * 64, 'size': 'big'}, 'revision': '0' * 64})
        assert response.status_code in (400, 422)


async def test_http_missing_revision_field_is_422(api):
    app, e, merger = api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as http:
        response = await http.post('/api/v1/kb/wiki/merges/publish', json={'reference': {}})
        assert response.status_code == 422


def test_openapi_exposes_publish_route_with_cas_revision(api):
    app, e, merger = api
    spec = app.openapi()
    assert '/api/v1/kb/wiki/merges/publish' in spec['paths']
    publish = spec['components']['schemas']['PublishMergeRequest']
    assert publish['additionalProperties'] is False
    assert publish['properties']['revision']['pattern'] == r'^[0-9a-f]{64}$'
