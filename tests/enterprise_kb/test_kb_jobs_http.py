"""HTTP queue/state contract for the F3 durable job surface (KB-021).

Proves the durable job status fields the tasks/monitoring UI and both SDK modes
consume: visible waiting order, unknown progress, written/indexing/ready distinctions
transitions, failed/cancelled/retry, an empty queue never reporting ready, and
state surviving a store re-open. Only the HTTP contract is exercised here; the
browser-level UI acceptance stays NOT_RUN.
"""
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
from openviking.server.identity import Role
from openviking.server.models import ERROR_CODE_TO_HTTP_STATUS
from openviking.server.routers.kb_jobs import router, get_job_service
from openviking.service.kb_job_service import KBJobService
from openviking.storage.acl import AclManager
from openviking_cli.exceptions import OpenVikingError
from openviking_cli.session.user_id import UserIdentifier
from openviking_sdk import AsyncHTTPClient


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

    yield app, e, context, jobs, acl, repository, setup
    process.terminate()
    process.join(15)


def client(app):
    c = AsyncHTTPClient(url='http://test', extra_headers={})
    c._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
    return c


async def submit(c, key, uri=ONE, namespace='manuals'):
    return await c.kb.create_ingestion_job(
        namespace_id=namespace, kind='D2', idempotency_key=key, payload={'uri': uri})


async def test_queued_position_unknown_progress_and_fifo_order(api):
    """Visible waiting order is durable within the authorized window; progress is unknown without measurements."""
    app, _e, _ctx, _jobs, _acl, _repo, _setup = api
    c = client(app)
    try:
        first = await submit(c, 'q1')
        second = await submit(c, 'q2')
        third = await submit(c, 'q3')

        # Each create response is a snapshot at its own creation time.
        assert first['state'] == 'queued' and first['phase'] == 'queued'
        assert first['queue_position'] == 1 and first['queued_total'] == 1
        assert second['queue_position'] == 2 and second['queued_total'] == 2
        assert third['queue_position'] == 3 and third['queued_total'] == 3
        for job in (first, second, third):
            assert job['progress_known'] is False
            assert job['processed_units'] is None and job['total_units'] is None
            assert job['task_id'] == job['job_id']
            assert job['namespace_id'] == 'manuals'
            assert job['document_id'] == ONE

        fetched = await c.kb.get_ingestion_job(first['job_id'])
        assert fetched['queue_position'] == 1 and fetched['queued_total'] == 3
        rows = (await c._request('GET', '/api/v1/kb/jobs')).json()['result']
        # The list is newest-first for the UI, but positions follow FIFO order.
        by_id = {r['job_id']: r for r in rows}
        assert by_id[first['job_id']]['queue_position'] == 1
        assert by_id[second['job_id']]['queue_position'] == 2
        assert by_id[third['job_id']]['queue_position'] == 3
        assert [r['created'] for r in rows] == sorted(r['created'] for r in rows)[::-1]

        # Cancelling the head job removes it from the queue; the rest renumber.
        after = await c.kb.cancel_ingestion_job(first['job_id'])
        assert after['phase'] == 'cancelled' and after['queue_position'] == 0
        remaining = (await c._request('GET', '/api/v1/kb/jobs')).json()['result']
        # The cancelled job stays listed as history (position 0); only the
        # still-waiting jobs keep FIFO positions.
        waiting = [r for r in remaining if r['phase'] == 'queued']
        assert sorted(r['queue_position'] for r in waiting) == [1, 2]
        assert {r['queued_total'] for r in waiting} == {2}
        cancelled = next(r for r in remaining if r['job_id'] == first['job_id'])
        assert cancelled['phase'] == 'cancelled' and cancelled['queue_position'] == 0
    finally:
        await c.close()


async def test_written_then_indexing_then_ready_are_distinct_phases(api):
    """TC-3.6: written (persisted, not indexed) must not be reported as ready."""
    app, _e, _ctx, _jobs, _acl, repository, _setup = api
    c = client(app)
    try:
        job = await submit(c, 'phases')
        claim = repository.job_claim('worker', 30)
        assert claim['job_id'] == job['job_id']
        repository.job_parse_acquire(job['job_id'], 'worker', claim['fence'])
        repository.job_parse_release(job['job_id'], 'worker', claim['fence'], 'written',
                                     {'processed_units': 4, 'total_units': 10,
                                      'resume_granularity': 'document'})
        written = await c.kb.get_ingestion_job(job['job_id'])
        assert written['state'] == 'written' and written['phase'] == 'written'
        assert written['phase'] != 'ready'
        assert written['progress_known'] is True
        assert written['processed_units'] == 4 and written['total_units'] == 10
        assert written['resume_granularity'] == 'document'
        assert written['queue_position'] == 0

        claim = repository.job_claim('worker', 30)
        # An index-stage checkpoint must bind a real publication batch; the
        # empty batch has no submitted members, so ready stays gate-blocked.
        batch = repository.begin_batch('acct', 'resources')
        repository.job_checkpoint(job['job_id'], 'worker', claim['fence'], 'index',
                                  {'publication_batch_id': batch,
                                   'publication_namespace': 'resources'})
        indexing = await c.kb.get_ingestion_job(job['job_id'])
        assert indexing['phase'] == 'indexing' and indexing['phase'] != 'ready'

        with pytest.raises(ValueError, match='PUBLICATION_GATE_REQUIRED'):
            repository.job_complete(job['job_id'], 'worker', claim['fence'], 'ready', {'all': True})
        assert (await c.kb.get_ingestion_job(job['job_id']))['phase'] == 'indexing'
    finally:
        await c.close()


async def test_failed_retry_and_cancelled_are_visible(api):
    """TC-3.3/TC-3.6: failure reason, retry backoff, and terminal cancel."""
    app, _e, _ctx, _jobs, _acl, repository, _setup = api
    c = client(app)
    try:
        job = await submit(c, 'fail')
        claim = repository.job_claim('worker', 30)
        repository.job_fail(job['job_id'], 'worker', claim['fence'],
                            {'code': 'PARSE_BACKEND_UNAVAILABLE'}, 0)
        backoff = await c.kb.get_ingestion_job(job['job_id'])
        assert backoff['state'] == 'backoff' and backoff['phase'] == 'retry'
        assert backoff['retryable'] is True
        assert backoff['queue_position'] == 1 and backoff['queued_total'] == 1

        claim = repository.job_claim('worker', 30)
        repository.job_fail(job['job_id'], 'worker', claim['fence'], {'code': 'GATE_FAILED'})
        failed = await c.kb.get_ingestion_job(job['job_id'])
        assert failed['state'] == 'failed' and failed['phase'] == 'failed'
        assert failed['reason_code'] == 'GATE_FAILED'
        assert failed['retryable'] is False

        # Terminal failures stay terminal on a repeat cancel.
        assert (await c.kb.cancel_ingestion_job(job['job_id']))['state'] == 'failed'
        other = await submit(c, 'cancel-me')
        assert (await c.kb.cancel_ingestion_job(other['job_id']))['phase'] == 'cancelled'
    finally:
        await c.close()


async def test_fallback_success_is_persistent_and_reported_clear(api):
    """TC-3.6: successful auto-fallback shows written plus the alignment notice."""
    app, _e, _ctx, _jobs, _acl, repository, _setup = api
    c = client(app)
    try:
        job = await submit(c, 'fallback')
        claim = repository.job_claim('worker', 30)
        repository.job_parse_acquire(job['job_id'], 'worker', claim['fence'])
        repository.job_parse_release(job['job_id'], 'worker', claim['fence'], 'written', {
            'alignment_status': 'failed',
            'structure_source': 'markdown_headings',
            'fallback_strategy': 'rule_blocks',
            'artifact_uri': 'viking://resources/raw/manual/one.md',
        })
        written = await c.kb.get_ingestion_job(job['job_id'])
        assert written['phase'] == 'written'
        assert written['alignment_status'] == 'failed'
        assert written['structure_source'] == 'markdown_headings'
        assert written['fallback_strategy'] == 'rule_blocks'
        assert written['artifact_uri'] == ONE

        reattached = await c.kb.get_ingestion_job(job['job_id'])
        assert reattached['alignment_status'] == 'failed'
        assert reattached['phase'] == 'written'
    finally:
        await c.close()


async def test_empty_queue_is_not_ready_and_state_survives_reopen(api):
    """TC-3.2/TC-3.6: empty queue != success; durable state survives re-open."""
    app, e, context, _jobs, acl, _repository, setup_pair = api
    c = client(app)
    try:
        assert (await c._request('GET', '/api/v1/kb/jobs')).json()['result'] == []

        job = await submit(c, 'durable')
        claim = _repository.job_claim('worker', 30)
        _repository.job_parse_acquire(job['job_id'], 'worker', claim['fence'])
        _repository.job_parse_release(job['job_id'], 'worker', claim['fence'], 'written',
                                      {'alignment_status': 'failed',
                                       'structure_source': 'markdown_headings'})
        assert (await c.kb.get_ingestion_job(job['job_id']))['phase'] == 'written'

        # Re-open the store the way a server restart would: a fresh service bound
        # to the same authority database. The queue must not look empty or ready.
        reopened = KBJobService(e.fs, AuthorityRepository(setup_pair[0], setup_pair[2]))
        app.dependency_overrides[get_job_service] = lambda: reopened
        view = await c.kb.get_ingestion_job(job['job_id'])
        assert view['state'] == 'written' and view['phase'] == 'written'
        assert view['phase'] != 'ready'
        assert view['alignment_status'] == 'failed'
        rows = (await c._request('GET', '/api/v1/kb/jobs')).json()['result']
        assert [r['job_id'] for r in rows] == [job['job_id']]

        # A different principal must not see the queue at all.
        context[0] = replace(e.ctx, user=UserIdentifier('acct', 'someone-else'))
        acl.acl(RAW, [{'principal': 'user:someone-else', 'level': 'write'}])
        assert (await c._request('GET', '/api/v1/kb/jobs')).json()['result'] == []
    finally:
        await c.close()

def test_unknown_write_and_progress_projection_do_not_claim_terminal_success():
    from openviking.server.routers.kb_jobs import job_status
    row = {'job_id': 'job', 'namespace': 'manuals', 'state': 'write_recovery_required',
           'stage': 'parse_write', 'payload': {}, 'info': {'code': 'WRITE_RECOVERY_REQUIRED'},
           'checkpoint': {'processed_units': True, 'total_units': 4}}
    view = job_status(row)
    assert view['phase'] == 'write_recovery_required'
    assert view['reason_code'] == 'WRITE_RECOVERY_REQUIRED'
    assert view['retryable'] is False
    assert view['progress_known'] is False
    assert view['queue_position_scope'] == 'visible_window'
    row.update(state='running', lease_until=123)
    row['checkpoint'] = {'processed_units': 5, 'total_units': 4}
    view = job_status(row)
    assert view['progress_known'] is False
    assert view['heartbeat'] is None and view['lease_expires_at'] == 123
