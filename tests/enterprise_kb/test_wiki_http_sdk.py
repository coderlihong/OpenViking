"""Real HTTP routing and async/sync SDK using SQLite/disk catalog fixtures."""
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding
from test_wiki_schema import package
from test_wiki_catalog import register_domains

from openviking.server.auth import get_request_context
from openviking.server.identity import Role
from openviking.server.models import ERROR_CODE_TO_HTTP_STATUS
from openviking.server.routers.wiki import router,get_catalog
from openviking.wiki.catalog import WikiCatalog
from openviking_cli.exceptions import OpenVikingError,PermissionDeniedError
from openviking_sdk import AsyncHTTPClient,SyncHTTPClient
from openviking_sdk.errors import ConflictError as SDKConflict,PermissionDeniedError as SDKDenied


@pytest.fixture
async def api(environment):
    e=environment;e.fs._async_agfs.root.mkdir(parents=True,exist_ok=True)
    await register_domains(e, target='viking://resources/wiki/sany', raw='viking://resources/raw/sany', namespace='sany-wiki')
    app=FastAPI();app.include_router(router)
    context=[replace(e.ctx,role=Role.ADMIN)]
    app.dependency_overrides[get_request_context]=lambda: context[0]
    app.dependency_overrides[get_catalog]=lambda: WikiCatalog(e.fs)
    @app.exception_handler(OpenVikingError)
    async def errors(request,exc):
        return JSONResponse(status_code=ERROR_CODE_TO_HTTP_STATUS.get(exc.code,500),content={
            'status':'error','error':{'code':exc.code,'message':exc.message,'details':exc.details}})
    return app,e,context


def client(app):
    c=AsyncHTTPClient(url='http://test',api_key='',extra_headers={})
    c._http=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test')
    return c


def binding():
    return dict(collection_id='sany',namespace_id='sany-wiki',target_uri='viking://resources/wiki/sany',
                raw_sources_prefix='viking://resources/raw/sany',schema_id='business',schema_version='1')


async def test_async_sdk_registers_and_reads_fixed_collection_over_http(api):
    app,e,ctx=api;c=client(app)
    try:
        assert c.kb is c.kb
        schema=await c.kb.register_schema(schema_id='business',version='1',files=package())
        assert (await c.kb.get_wiki_schema('business','1'))==schema
        registered=await c.kb.create_wiki_collection(**binding())
        selected=await c.kb.get_wiki_collection('sany')
        assert selected['schema_digest']==registered['schema_digest']==schema['digest']
        assert selected['capability_state']=='structural_ready'
    finally:await c.close()


async def test_sdk_registers_and_reads_entity_catalog_over_http(api):
    app,e,ctx=api;c=client(app)
    try:
        canonical='candidate-'+'ab'*32
        entry={'canonical_id':canonical,'entity_type':'Thing','schema_id':'business','schema_version':'1',
               'schema_digest':'cd'*32,'aliases':['SY215C挖掘机'],'source_uri':'viking://resources/wiki/sany/Thing.md'}
        registered=await c.kb.register_wiki_entities('sany-wiki',[entry])
        assert registered=={'registered':1,'aliases':{'SY215C挖掘机':canonical}}
        fetched=await c.kb.get_wiki_entity('sany-wiki',canonical)
        assert fetched['canonical_id']==canonical and fetched['aliases']==['SY215C挖掘机']
        rows=await c.kb.list_wiki_entities('sany-wiki')
        assert [r['canonical_id'] for r in rows]==[canonical]
    finally:await c.close()


def test_sync_sdk_uses_same_http_routes_and_result_contract(api):
    app,e,ctx=api;c=SyncHTTPClient(url='http://test',api_key='',extra_headers={})
    c._async_client._http=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test')
    try:
        assert c.kb is c.kb
        schema=c.kb.register_schema(schema_id='business',version='1',files=package())
        c.kb.create_wiki_collection(**binding())
        assert c.kb.get_wiki_collection('sany')['schema_digest']==schema['digest']
        assert c.kb.get_wiki_schema('business','1')==schema
    finally:c.close()


async def test_schema_and_collection_reads_refresh_old_optimistic_coverage(api):
    app,e,ctx=api;c=client(app)
    files=package(**{'_shared.schema.yaml': 'shared_rules: [{id: evidence, text: Each fact needs evidence}]'})
    try:
        await c.kb.register_schema(schema_id='business',version='1',files=files)
        await c.kb.create_wiki_collection(**binding())
        with e.store.db() as db:db.execute("UPDATE wiki_schemas SET state='structural_ready'")
        assert (await c.kb.get_wiki_schema('business','1'))['capability_state']=='draft_rules_pending'
        with e.store.db() as db:db.execute("UPDATE wiki_schemas SET state='structural_ready'")
        assert (await c.kb.get_wiki_collection('sany'))['capability_state']=='draft_rules_pending'
    finally:await c.close()


async def test_invalid_structural_gate_is_http_bad_input_not_server_error(api):
    app,e,ctx=api
    files=package();files['Thing.schema.yaml']+='\nstructural_gates: [{kind: [], fields: [sources]}]'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        response=await c.post('/api/v1/kb/wiki/schemas',json={'schema_id':'broken','version':'1','files':files})
        assert response.status_code==400 and response.json()['error']['code']=='INVALID_ARGUMENT'
    with e.store.db() as db:assert db.execute('SELECT count(*) FROM wiki_schemas').fetchone()[0]==0


async def test_sdk_preserves_conflict_and_permission_error_types(api):
    app,e,ctx=api;c=client(app)
    try:
        await c.kb.register_schema(schema_id='business',version='1',files=package())
        files=package();files['Thing.template.md']+='changed'
        with pytest.raises(SDKConflict):
            await c.kb.register_schema(schema_id='business',version='1',files=files)
        ctx[0]=e.ctx
        with pytest.raises(SDKDenied):
            await c.kb.register_schema(schema_id='other',version='1',files=package())
        with pytest.raises(SDKDenied):
            await c.kb.get_wiki_schema('business','1')
    finally:await c.close()


async def test_http_invalid_or_extra_fields_do_not_create_schema(api):
    app,e,ctx=api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        response=await c.post('/api/v1/kb/wiki/schemas',json={'schema_id':'business','version':'1','files':package(),'account_id':'other'})
        assert response.status_code==422
        response=await c.post('/api/v1/kb/wiki/schemas',json={'schema_id':'business','version':1,'files':package()})
        assert response.status_code==422
        response=await c.post('/api/v1/kb/wiki/schemas',json={'schema_id':'business','version':'1','files':{'../bad.md':'invalid'}})
        assert response.status_code==400 and response.json()['error']['code']=='INVALID_ARGUMENT'
    with e.store.db() as db:assert db.execute('SELECT count(*) FROM wiki_schemas').fetchone()[0]==0


async def test_merge_prepare_route_rejects_malformed_selections_over_http(api):
    from openviking.server.routers import wiki as wiki_router
    app,e,ctx=api
    app.dependency_overrides[wiki_router.get_merger]=lambda: AsyncMock()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        response=await c.post('/api/v1/kb/wiki/merges',json={'selections':[]})
        assert response.status_code==422
        response=await c.post('/api/v1/kb/wiki/merges',json={'selections':[{'build_id':'b','plan_digest':'xx','receipts':[{}]}]})
        assert response.status_code==422
        response=await c.post('/api/v1/kb/wiki/merges',json={'selections':[{'build_id':'b','plan_digest':'ab'*32,'receipts':[]}]})
        assert response.status_code==422
        response=await c.post('/api/v1/kb/wiki/merges',json={'selections':[{'build_id':'b','plan_digest':'ab'*32,'receipts':[{}],'extra':1}]})
        assert response.status_code==422


async def test_collection_get_enforces_existing_fs_permission(api):
    app,e,ctx=api;c=client(app)
    try:
        await c.kb.register_schema(schema_id='business',version='1',files=package())
        await c.kb.create_wiki_collection(**binding())
        ctx[0]=e.ctx;e.fs._ensure_access=AsyncMock(side_effect=PermissionDeniedError('denied'))
        with pytest.raises(SDKDenied):await c.kb.get_wiki_collection('sany')
    finally:await c.close()


def test_openapi_contains_preparation_routes_and_strict_request_models(api):
    app,_,_=api;spec=app.openapi()
    assert set(spec['paths'])=={'/api/v1/kb/wiki/schemas','/api/v1/kb/wiki/schemas/{schema_id}/{version}',
                              '/api/v1/kb/wiki/collections','/api/v1/kb/wiki/collections/{collection_id}',
                              '/api/v1/kb/wiki/builds','/api/v1/kb/wiki/ingestions',
                              '/api/v1/kb/wiki/builds/{build_id}',
                              '/api/v1/kb/wiki/builds/{build_id}/execute',
                              '/api/v1/kb/wiki/jobs/{job_id}/drafts/{draft_index}',
                              '/api/v1/kb/wiki/collections/{collection_id}/migrate-domains',
                              '/api/v1/kb/wiki/collections/{collection_id}/migration-inspection',
                              '/api/v1/kb/wiki/entities','/api/v1/kb/wiki/entities/{namespace_id}',
                              '/api/v1/kb/wiki/entities/{namespace_id}/{canonical_id}',
                              '/api/v1/kb/wiki/merges','/api/v1/kb/wiki/merges/publish'}
    assert spec['components']['schemas']['SchemaRequest']['additionalProperties'] is False
    assert 'account_id' not in spec['components']['schemas']['CollectionRequest']['properties']
    assert spec['components']['schemas']['BuildRequest']['additionalProperties'] is False
    assert spec['components']['schemas']['IngestionRequest']['additionalProperties'] is False
    execution = spec['components']['schemas']['ExecuteBuildRequest']
    assert execution['additionalProperties'] is False
    assert set(execution['required']) == {'expected_plan_digest', 'idempotency_key'}
    assert '202' in spec['paths']['/api/v1/kb/wiki/builds/{build_id}/execute']['post']['responses']
