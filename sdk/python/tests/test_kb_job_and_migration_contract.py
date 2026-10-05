"""Current endpoint serialization in both SDK modes."""
import json

import httpx
import pytest
from openviking_sdk import AsyncHTTPClient, SyncHTTPClient
from openviking_sdk._utils import run_async


@pytest.mark.parametrize('sync', [False, True])
def test_collection_inspection_and_cas_migration_serialization(sync):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'status': 'ok', 'result': {'binding_digest': 'a' * 64}})
    client = SyncHTTPClient(url='http://test') if sync else AsyncHTTPClient(url='http://test')
    asynchronous = client._async_client if sync else client
    asynchronous._http = httpx.AsyncClient(base_url='http://test', transport=httpx.MockTransport(handler))
    try:
        inspected = client.kb.inspect_wiki_collection_migration('collection')
        if not sync:
            inspected = run_async(inspected)
        assert inspected['binding_digest'] == 'a' * 64
        migrated = client.kb.migrate_wiki_collection_domains('collection', expected_binding_digest='a' * 64)
        if not sync:
            migrated = run_async(migrated)
        assert migrated == inspected
    finally:
        client.close() if sync else run_async(client.close())
    assert requests[0].method == 'GET'
    assert requests[0].url.path == '/api/v1/kb/wiki/collections/collection/migration-inspection'
    assert requests[1].method == 'POST'
    assert requests[1].url.path == '/api/v1/kb/wiki/collections/collection/migrate-domains'
    assert json.loads(requests[1].content) == {'expected_binding_digest': 'a' * 64}


@pytest.mark.parametrize('sync', [False, True])
def test_wiki_execution_and_private_draft_sdk_serialization(sync):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(202 if request.method == 'POST' else 200,
            json={'status': 'ok', 'result': {'state': 'queued' if request.method == 'POST' else 'draft_ready'}})
    client = SyncHTTPClient(url='http://test') if sync else AsyncHTTPClient(url='http://test')
    asynchronous = client._async_client if sync else client
    asynchronous._http = httpx.AsyncClient(base_url='http://test', transport=httpx.MockTransport(handler))
    try:
        created = client.kb.execute_wiki_build('build', expected_plan_digest='b' * 64, idempotency_key='execute')
        if not sync:
            created = run_async(created)
        assert created['state'] == 'queued'
        draft = client.kb.get_wiki_draft('job', 2)
        if not sync:
            draft = run_async(draft)
        assert draft['state'] == 'draft_ready'
    finally:
        client.close() if sync else run_async(client.close())
    assert requests[0].url.path == '/api/v1/kb/wiki/builds/build/execute'
    assert json.loads(requests[0].content) == {'expected_plan_digest': 'b' * 64, 'idempotency_key': 'execute', 'profile': 'draft'}
    assert requests[1].url.path == '/api/v1/kb/wiki/jobs/job/drafts/2'
