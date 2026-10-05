"""HTTP and both SDK modes use durable authority jobs under current ACLs."""
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding
from test_wiki_namespaces import ContextStore, RAW, ONE
from test_registry_authority import setup as setup, _start

from openviking.indexing.registry_authority import AuthorityRepository
from openviking.server.auth import get_request_context
from openviking.server.models import ERROR_CODE_TO_HTTP_STATUS
from openviking.server.identity import Role
from openviking.server.routers.kb_jobs import router, get_job_service
from openviking.service.kb_job_service import KBJobService
from openviking.storage.acl import AclManager
from openviking_cli.exceptions import OpenVikingError
from openviking_cli.session.user_id import UserIdentifier
from openviking_sdk import AsyncHTTPClient, SyncHTTPClient
from openviking_sdk.errors import ConflictError, PermissionDeniedError, NotFoundError


@pytest.fixture
async def api(environment, setup):
    e = environment
    e.fs._async_agfs.root.mkdir(parents=True, exist_ok=True)
    await e.fs.write(ONE, '# Source', ctx=e.ctx)
    acl = ContextStore()
    acl.acl(RAW, [{'principal': 'user:user', 'level': 'write'}])
    e.fs.acl_manager = AclManager(acl)
    e.fs.acl_manager.set_enabled('acct', True)
    e.fs._ensure_access = type(e.fs)._ensure_access.__get__(e.fs)
    admin = replace(e.ctx, role=Role.ADMIN)
    process = _start(setup)
    repository = AuthorityRepository(setup[0], setup[2])
    jobs = KBJobService(e.fs, repository)
    await jobs.namespaces.register(admin, namespace_id='manuals', root_uri=RAW)
    context = [admin]
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_request_context] = lambda: context[0]
    app.dependency_overrides[get_job_service] = lambda: jobs

    @app.exception_handler(OpenVikingError)
    async def errors(request, exc):
        return JSONResponse(status_code=ERROR_CODE_TO_HTTP_STATUS.get(exc.code, 500), content={
            'status': 'error', 'error': {'code': exc.code, 'message': exc.message}})

    yield app, e, context, jobs, acl
    process.terminate()
    process.join(15)


def client(app):
    c = AsyncHTTPClient(url='http://test', extra_headers={})
    c._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
    return c


async def test_authority_http_job_idempotency_query_cancel_async_and_sync(api):
    app, e, context, jobs, acl = api
    c = client(app)
    try:
        args = dict(namespace_id='manuals', kind='D2', idempotency_key='uri-job', payload={'uri': ONE})
        first = await c.kb.create_ingestion_job(**args)
        assert first['state'] == 'queued' and first['kind'] == 'D2'
        assert first['effective_config']['parse_backend'] == 'auto'
        assert (await c.kb.create_ingestion_job(**args))['job_id'] == first['job_id']
        with pytest.raises(ConflictError):
            await c.kb.create_ingestion_job(**{**args, 'options': {'parse_backend': 'logics'}})
        assert (await c.kb.get_ingestion_job(first['job_id']))['digest'] == first['digest']
        sync = SyncHTTPClient(url='http://test', extra_headers={})
        sync._async_client._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
        try:
            assert sync.kb.get_ingestion_job(first['job_id'])['job_id'] == first['job_id']
            assert sync.kb.cancel_ingestion_job(first['job_id'])['state'] == 'cancelled'
            uploaded = sync.kb.create_ingestion_job(namespace_id='manuals', kind='D1',
                idempotency_key='upload-job', payload={'temp_file_id': 'stored-upload'})
            assert uploaded['payload']['parent'] == RAW
        finally:
            sync.close()
        assert (await c.kb.cancel_ingestion_job(uploaded['job_id']))['state'] == 'cancelled'
        assert len(jobs.repository.job_list(e.ctx.account_id)) == 2
    finally:
        await c.close()


async def test_current_acl_account_and_owner_are_checked_on_job_reads_and_cancel(api):
    app, e, context, jobs, acl = api
    c = client(app)
    try:
        job = await c.kb.create_ingestion_job(namespace_id='manuals', kind='D2',
            idempotency_key='access', payload={'uri': ONE})
        context[0] = replace(e.ctx, user=UserIdentifier('other-account', 'user'))
        with pytest.raises(NotFoundError):
            await c.kb.get_ingestion_job(job['job_id'])
        context[0] = replace(e.ctx, user=UserIdentifier('acct', 'different-user'))
        with pytest.raises(PermissionDeniedError):
            await c.kb.get_ingestion_job(job['job_id'])
        context[0] = e.ctx
        acl.acl(RAW, [{'principal': 'user:different-user', 'level': 'write'}])
        with pytest.raises(PermissionDeniedError):
            await c.kb.get_ingestion_job(job['job_id'])
        with pytest.raises(PermissionDeniedError):
            await c.kb.cancel_ingestion_job(job['job_id'])
        assert jobs.repository.job_get('acct', job['job_id'])['state'] == 'queued'
    finally:
        await c.close()


async def test_job_http_202_and_strict_options_do_not_expose_unsupported_wiki_execution(api):
    app, e, _, jobs, _ = api
    body = {'namespace_id': 'manuals', 'kind': 'D2', 'idempotency_key': 'contract', 'payload': {'uri': ONE}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        response = await c.post('/api/v1/kb/ingestion-jobs', json=body)
        assert response.status_code == 202
        for changes in [{'kind': 'W2'}, {'owner': 'forged'}, {'options': {'unknown': True}},
                        {'payload': {'uri': ONE, 'mode': 'prune_orphans'}}, {'idempotency_key': 'x' * 129}]:
            response = await c.post('/api/v1/kb/ingestion-jobs', json={**body, **changes})
            assert response.status_code == 422
    assert len(jobs.repository.job_list('acct')) == 1
    paths = app.openapi()['paths']
    assert '/api/v1/kb/jobs/{job_id}' in paths
    assert '/api/v1/kb/jobs/{job_id}/cancel' in paths


async def test_mixed_inputs_destinations_and_binary_uri_are_rejected_before_persistence(api):
    app, _, _, jobs, _ = api
    body = {'namespace_id': 'manuals', 'kind': 'D1', 'idempotency_key': 'invalid'}
    invalid = [
        ('D1', {'temp_file_id': 'upload', 'uri': ONE}),
        ('D1', {'temp_file_id': 'upload', 'build_id': 'prepared'}),
        ('D1', {'temp_file_id': 'upload', 'to': RAW, 'parent': RAW}),
        ('D1', {}),
        ('D2', {'uri': ONE, 'temp_file_id': 'upload'}),
        ('D2', {'uri': ONE, 'build_id': 'prepared'}),
        ('D2', {'uri': ONE, 'parent': RAW}),
        ('D2', {'uri': ONE, 'to': RAW}),
        ('D2', {}),
    ]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        for kind, payload in invalid:
            response = await c.post('/api/v1/kb/ingestion-jobs', json={**body, 'kind': kind, 'payload': payload})
            assert response.status_code == 422
        for suffix in ('.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx'):
            response = await c.post('/api/v1/kb/ingestion-jobs', json={
                **body, 'kind': 'D2', 'payload': {'uri': RAW + '/binary' + suffix}})
            assert response.status_code == 400
            assert response.json()['error']['code'] == 'INVALID_ARGUMENT'
    assert jobs.repository.job_list('acct') == []


async def test_parse_quarantine_resolve_http_releases_only_without_pending_writes(api, setup):
    app, e, context, jobs, _ = api
    c = client(app)
    try:
        job = await c.kb.create_ingestion_job(namespace_id='manuals', kind='D2',
            idempotency_key='quarantine', payload={'uri': ONE})
        jobs.repository.job_claim('worker-q')
        jobs.repository.job_parse_acquire(job['job_id'], 'worker-q', 1)
        assert jobs.repository.job_cancel('acct', job['job_id'])['state'] == 'write_recovery_required'
        import sqlite3
        db_path = setup[1]
        with sqlite3.connect(db_path) as raw:
            raw.execute("UPDATE kb_parse_slot SET state='uncertain' WHERE slot=1")
            raw.execute("INSERT INTO source_writes VALUES ('token-live', 'acct', "
                        "'viking://resources/raw/x.md', 'active', 2)")
            raw.execute("INSERT INTO source_write_refs VALUES ('token-live', 'local-file', 'op-1', '{}')")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as raw_http:
            response = await raw_http.post(f'/api/v1/kb/jobs/{job["job_id"]}/parse-quarantine/resolve')
            assert response.status_code == 400
            assert response.json()['error']['code'] == 'INVALID_ARGUMENT'
            assert 'SOURCE_WRITES_PENDING' in response.json()['error']['message']
        assert jobs.repository.job_get('acct', job['job_id'])['state'] == 'write_recovery_required'
        with sqlite3.connect(db_path) as raw:
            raw.execute("DELETE FROM source_writes")
            raw.execute("DELETE FROM source_write_refs")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as raw_http:
            response = await raw_http.post(f'/api/v1/kb/jobs/{job["job_id"]}/parse-quarantine/resolve')
            assert response.status_code == 200
            result = response.json()['result']
        assert result['state'] == 'backoff' and result['stage'] == 'validate'
        assert result['info']['code'] == 'PARSE_QUARANTINE_RESOLVED_NO_PENDING_WRITES'
        assert jobs.repository.job_get('acct', job['job_id'])['state'] == 'backoff'
        paths = app.openapi()['paths']
        assert '/api/v1/kb/jobs/{job_id}/parse-quarantine/resolve' in paths
    finally:
        await c.close()


async def test_authorized_list_filters_principal_before_limit_and_rechecks_acl(api):
    app, e, context, jobs, acl = api
    c = client(app)
    try:
        original = await c.kb.create_ingestion_job(namespace_id='manuals', kind='D2',
            idempotency_key='list-own', payload={'uri': ONE})
        for i in range(5):
            jobs.repository.job_create('acct', 'manuals', f'foreign-{i}', 'D2',
                {'uri': ONE}, {}, 'other-user')
        rows = await c.kb.list_ingestion_jobs(namespace_id='manuals', limit=1)
        assert [row['job_id'] for row in rows] == [original['job_id']]
        assert rows[0]['queue_position_scope'] == 'visible_window'
        sync = SyncHTTPClient(url='http://test', extra_headers={})
        sync._async_client._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
        try:
            assert sync.kb.list_ingestion_jobs(limit=1)[0]['job_id'] == original['job_id']
        finally:
            sync.close()
        context[0] = e.ctx
        acl.acl(RAW, [{'principal': 'user:other-user', 'level': 'write'}])
        assert await c.kb.list_ingestion_jobs() == []
        with pytest.raises(PermissionDeniedError):
            await c.kb.list_ingestion_jobs(namespace_id='manuals')
    finally:
        await c.close()
