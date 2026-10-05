"""Actual Wiki/namespace HTTP+SDK against an independent authority process."""
from dataclasses import replace
from types import SimpleNamespace
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from test_content_publication import environment as environment
from test_index_plan import embedding as embedding
from test_registry_authority import setup as authority_setup, _start
from test_wiki_schema import package
from test_wiki_namespaces import ContextStore

from openviking.indexing.publication import configured_store, PublicationStore
from openviking.server.auth import get_request_context
from openviking.server.identity import Role
from openviking.server.models import ERROR_CODE_TO_HTTP_STATUS
from openviking.server.routers import wiki, namespaces
from openviking.storage.acl import AclManager
from openviking.wiki.build_plan import WikiBuildPlanner
from openviking.wiki.namespaces import NamespaceCatalog
from openviking_cli.exceptions import OpenVikingError, ConflictError, PermissionDeniedError
from openviking_cli.session.user_id import UserIdentifier
from openviking_sdk import AsyncHTTPClient, SyncHTTPClient
from openviking_sdk.errors import ConflictError as SDKConflict, NotFoundError as SDKNotFound, FailedPreconditionError as SDKPrecondition

RAW = 'viking://resources/raw'
WIKI = 'viking://resources/wiki'
ONE = RAW + '/one.md'


@pytest.fixture
async def remote(environment, embedding, authority_setup, monkeypatch, request):
    if getattr(request, 'param', None) in {'legacy', 'legacy-invalid'}:
        from openviking.wiki.catalog import _json
        from openviking.wiki.schema import SchemaBundle
        bundle = SchemaBundle.freeze('business', '1', package())
        binding = dict(collection_id='legacy', namespace_id='wiki', target_uri=WIKI,
                       raw_sources_prefix=RAW, schema_id='business', schema_version='1', schema_digest=bundle.digest)
        if request.param == 'legacy-invalid':
            binding['target_uri'] = 'viking://resources/outside'
        # Simulate pre-upgrade persisted bytes before any authority is running.
        prior = PublicationStore(authority_setup[1])
        with prior.db() as db:
            db.execute('INSERT INTO wiki_collections VALUES (?,?,?)', ('acct', 'legacy', _json(binding)))
    process = _start(authority_setup)
    e = environment
    embedding.chunking.publication_path = ''
    embedding.chunking.publication_authority_socket = authority_setup[0]
    embedding.chunking.publication_authority_token_file = authority_setup[2]
    e.store = configured_store(embedding)
    e.fs._async_agfs.root.mkdir(parents=True, exist_ok=True)
    await e.fs.write(ONE, '# Manual\n\nDiagnostic evidence.', ctx=e.ctx)
    e.fs._async_agfs.path(e.fs._uri_to_path(WIKI, ctx=e.ctx)).mkdir(parents=True, exist_ok=True)

    async def listing(path, offset=0, limit=None, sort_by=None, sort_order='asc'):
        target = e.fs._async_agfs.path(path)
        children = sorted(target.iterdir(), key=lambda p: p.name)
        children = children[offset:] if limit is None else children[offset:offset + limit]
        return [{'name': child.name, 'isDir': child.is_dir(), 'size': child.stat().st_size} for child in children]
    e.fs._async_agfs.ls = listing
    backend = ContextStore()
    backend.acl(RAW, [{'principal': 'user:user', 'level': 'read'}])
    backend.acl(WIKI, [{'principal': 'user:user', 'level': 'write'}])
    e.fs.acl_manager = AclManager(backend)
    e.fs.acl_manager.set_enabled('acct', True)
    e.fs._ensure_access = type(e.fs)._ensure_access.__get__(e.fs)
    context = [replace(e.ctx, role=Role.ADMIN)]
    app = FastAPI()
    app.include_router(wiki.router)
    app.include_router(namespaces.router)
    app.dependency_overrides[get_request_context] = lambda: context[0]
    # Use real production dependencies and configured_store, not catalog overrides.
    service = SimpleNamespace(viking_fs=e.fs)
    monkeypatch.setattr(wiki, 'get_service', lambda: service)
    monkeypatch.setattr(namespaces, 'get_service', lambda: service)

    @app.exception_handler(OpenVikingError)
    async def errors(request, exc):
        return JSONResponse(status_code=ERROR_CODE_TO_HTTP_STATUS.get(exc.code, 500), content={
            'status': 'error', 'error': {'code': exc.code, 'message': exc.message, 'details': exc.details}})
    try:
        yield app, e, context, backend
    finally:
        process.terminate()
        process.join(15)


def _client(app):
    client = AsyncHTTPClient(url='http://test', api_key='', extra_headers={})
    client._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
    return client


async def _register(client):
    for namespace, root in [('raw', RAW), ('wiki', WIKI)]:
        await client.kb.register_namespace(namespace_id=namespace, root_uri=root)
    await client.kb.register_schema(schema_id='business', version='1', files=package())
    return await client.kb.create_wiki_collection(
        collection_id='manual', namespace_id='wiki', target_uri=WIKI, raw_sources_prefix=RAW,
        schema_id='business', schema_version='1')


async def test_async_http_sdk_prepared_replay_export_and_current_policy(remote):
    app, e, context, backend = remote
    client = _client(app)
    try:
        collection = await _register(client)
        assert (await client.kb.get_wiki_collection('manual'))['schema_digest'] == collection['schema_digest']
        for namespace, root in [('raw', RAW), ('wiki', WIKI)]:
            await client.kb.register_namespace(namespace_id=namespace, root_uri=root)
        plan = await client.kb.build_wiki(source_uris=[ONE], collection_id='manual', idempotency_key='plan')
        assert plan['state'] == 'prepared'
        assert await client.kb.get_wiki_build(plan['build_id']) == plan
        assert await client.kb.build_wiki(source_uris=[ONE], collection_id='manual', idempotency_key='plan') == plan
        context[0] = e.ctx
        planner = WikiBuildPlanner(e.fs)
        with pytest.raises(PermissionDeniedError, match='Explicit'):
            await planner.authorize_inputs(e.ctx, plan['build_id'])
        context[0] = replace(e.ctx, role=Role.ADMIN)
        await client.kb.set_export(source_namespace='raw', target_namespace='wiki', principal='user:user', allowed=True)
        ticket = await planner.authorize_inputs(e.ctx, plan['build_id'])
        assert [body async for _, body in planner.authorized_source_texts(e.ctx, plan['build_id'])] == ['# Manual\n\nDiagnostic evidence.']
        await client.kb.set_export(source_namespace='raw', target_namespace='wiki', principal='user:user', allowed=False)
        with pytest.raises(ConflictError, match='policy changed'):
            NamespaceCatalog(e.fs).check_policy_revision(e.ctx, ticket)
        # Existing preparation remains a private inspectable plan after revocation.
        assert (await client.kb.get_wiki_build(plan['build_id']))['state'] == 'prepared'
    finally:
        await client.close()


def test_sync_http_sdk_catalog_roundtrip(remote):
    app, e, context, backend = remote
    client = SyncHTTPClient(url='http://test', api_key='', extra_headers={})
    client._async_client._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
    try:
        schema = client.kb.register_schema(schema_id='business', version='1', files=package())
        assert client.kb.get_wiki_schema('business', '1') == schema
        client.kb.register_namespace(namespace_id='raw', root_uri=RAW)
        assert client.kb.get_namespace('raw')['root_uri'] == RAW
    finally:
        client.close()


async def test_authority_http_preserves_conflict_notfound_and_account_scope(remote):
    app, e, context, backend = remote
    client = _client(app)
    try:
        await _register(client)
        changed = package()
        changed['Thing.template.md'] += 'changed'
        with pytest.raises(SDKConflict):
            await client.kb.register_schema(schema_id='business', version='1', files=changed)
        plan = await client.kb.build_wiki(source_uris=[ONE], collection_id='manual', idempotency_key='scope')
        context[0] = replace(context[0], user=UserIdentifier('other', 'user'))
        with pytest.raises(SDKNotFound):
            await client.kb.get_wiki_build(plan['build_id'])
        with pytest.raises(SDKNotFound):
            await client.kb.get_wiki_schema('business', '1')
    finally:
        await client.close()


async def test_authority_plan_domain_command_rechecks_binding_epochs_and_idempotency(remote):
    app, e, context, backend = remote
    client = _client(app)
    try:
        await _register(client)
        original = await client.kb.build_wiki(source_uris=[ONE], collection_id='manual', idempotency_key='cas')
        row = e.store.wiki_plan_get('acct', original['build_id'])
        body = json.loads(row['manifest'])
        body['binding_digest'] = 'a' * 64
        with pytest.raises(ConflictError, match='collection changed'):
            e.store.wiki_plan_persist('acct', 'wiki', 'forged-binding', json.dumps(body))
        token = e.store.begin_write('acct', ONE)
        with pytest.raises(RuntimeError, match='in-flight'):
            e.store.wiki_plan_persist('acct', 'wiki', 'during-write', row['manifest'])
        e.store.finish_write(token)
        with pytest.raises(ConflictError, match='input changed'):
            e.store.wiki_plan_check_epochs('acct', json.loads(row['manifest']))
    finally:
        await client.close()


async def test_authority_w1_http_materializes_upload_and_replays_prepared_plan(remote, tmp_path):
    from openviking.server.temp_upload_store import ResolvedTempUpload
    app, e, context, backend = remote
    client = _client(app)
    upload = tmp_path / 'upload.md'
    upload.write_text('# Uploaded evidence\n\nMaintenance instructions.')

    async def resolve(temp_file_id, ctx):
        return ResolvedTempUpload('local', temp_file_id, 'uploaded.md', str(upload))
    app.dependency_overrides[wiki.get_temp_upload_store] = lambda: SimpleNamespace(resolve_for_consume=resolve)
    try:
        await _register(client)
        first = await client.kb.ingest_wiki(temp_file_id='first', collection_id='manual', idempotency_key='upload')
        assert first['state'] == 'prepared'
        assert await client.kb.get_wiki_build(first['build_id']) == first
        # The temp upload is consumed/cleaned by the real route; a fresh upload
        # of identical bytes must reuse the same persisted source plan.
        upload.write_text('# Uploaded evidence\n\nMaintenance instructions.')
        replay = await client.kb.ingest_wiki(temp_file_id='replay', collection_id='manual', idempotency_key='upload')
        assert replay == first
        planner = WikiBuildPlanner(e.fs)
        plan = await planner.get(context[0], first['build_id'])
        assert await planner.read_source(context[0], plan.build_id, plan.sources[0].uri) == '# Uploaded evidence\n\nMaintenance instructions.'
    finally:
        await client.close()


@pytest.mark.parametrize('change', [
    {'namespace_id':'never-registered'},
    {'target_uri':'viking://resources/outside'},
    {'raw_sources_prefix':'viking://resources/unregistered'},
    {'target_uri':WIKI + '/output', 'raw_sources_prefix':WIKI + '/raw'},
])
async def test_new_collection_sdk_rejects_missing_or_mismatched_registered_domains(remote, change):
    app, e, context, backend = remote
    client = _client(app)
    try:
        for namespace, root in [('raw', RAW), ('wiki', WIKI)]:
            await client.kb.register_namespace(namespace_id=namespace, root_uri=root)
        await client.kb.register_schema(schema_id='business', version='1', files=package())
        binding = dict(collection_id='invalid', namespace_id='wiki', target_uri=WIKI,
                       raw_sources_prefix=RAW, schema_id='business', schema_version='1')
        binding.update(change)
        with pytest.raises(SDKPrecondition):
            await client.kb.create_wiki_collection(**binding)
        with pytest.raises(SDKNotFound):
            await client.kb.get_wiki_collection('invalid')
    finally:
        await client.close()


@pytest.mark.parametrize('remote', ['legacy'], indirect=True)
async def test_legacy_http_read_and_prepare_block_until_explicit_admin_cas_migration(remote):
    import hashlib
    from openviking.wiki.catalog import _json
    app, e, context, backend = remote
    client = _client(app)
    try:
        await client.kb.register_schema(schema_id='business', version='1', files=package())
        for namespace, root in [('raw', RAW), ('wiki', WIKI)]:
            await client.kb.register_namespace(namespace_id=namespace, root_uri=root)
        with pytest.raises(SDKPrecondition, match='legacy_domain_pending'):
            await client.kb.get_wiki_collection('legacy')
        with pytest.raises(SDKPrecondition, match='legacy_domain_pending'):
            await client.kb.build_wiki(source_uris=[ONE], collection_id='legacy', idempotency_key='old')
        binding = e.store.wiki_collection_get('acct', 'legacy')
        digest = hashlib.sha256(_json(binding).encode()).hexdigest()
        inspected = await client.kb.inspect_wiki_collection_migration('legacy')
        assert inspected['binding_digest'] == digest and inspected['migration_required'] is True
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as http:
            inspection = await http.get('/api/v1/kb/wiki/collections/legacy/migration-inspection')
            assert inspection.status_code == 200
            assert inspection.json()['result']['binding_digest'] == digest
            assert inspection.json()['result']['domain_state'] == 'legacy_domain_pending'
            response = await http.post('/api/v1/kb/wiki/collections/legacy/migrate-domains', json={'expected_binding_digest':'0' * 64})
            assert response.status_code == 409
            context[0] = e.ctx
            assert (await http.get('/api/v1/kb/wiki/collections/legacy/migration-inspection')).status_code == 403
            assert (await http.post('/api/v1/kb/wiki/collections/legacy/migrate-domains', json={'expected_binding_digest':digest})).status_code == 403
            context[0] = replace(e.ctx, role=Role.ADMIN)
            assert (await http.post('/api/v1/kb/wiki/collections/legacy/migrate-domains', json={'expected_binding_digest':digest})).status_code == 200
        migrated = await client.kb.migrate_wiki_collection_domains('legacy', expected_binding_digest=digest)
        assert migrated['namespace_id'] == 'wiki'
        assert (await client.kb.inspect_wiki_collection_migration('legacy'))['domain_state'] == 'registered'
        assert (await client.kb.get_wiki_collection('legacy'))['namespace_id'] == 'wiki'
        assert (await client.kb.build_wiki(source_uris=[ONE], collection_id='legacy', idempotency_key='migrated'))['state'] == 'prepared'
    finally:
        await client.close()


@pytest.mark.parametrize('remote', ['legacy-invalid'], indirect=True)
async def test_illegal_legacy_binding_cannot_be_migrated_by_admin(remote):
    import hashlib
    from openviking.wiki.catalog import _json
    app, e, context, backend = remote
    client = _client(app)
    try:
        await client.kb.register_schema(schema_id='business', version='1', files=package())
        for namespace, root in [('raw', RAW), ('wiki', WIKI)]:
            await client.kb.register_namespace(namespace_id=namespace, root_uri=root)
        digest = hashlib.sha256(_json(e.store.wiki_collection_get('acct', 'legacy')).encode()).hexdigest()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as http:
            response = await http.post('/api/v1/kb/wiki/collections/legacy/migrate-domains', json={'expected_binding_digest':digest})
            assert response.status_code == 412
        with pytest.raises(SDKPrecondition, match='legacy_domain_pending'):
            await client.kb.get_wiki_collection('legacy')
    finally:
        await client.close()


async def test_empty_namespace_account_does_not_waive_collection_registration_gate(remote):
    app, e, context, backend = remote
    client = _client(app)
    try:
        assert e.store.namespace_list('acct') == []
        await client.kb.register_schema(schema_id='business', version='1', files=package())
        with pytest.raises(SDKPrecondition):
            await client.kb.create_wiki_collection(collection_id='invalid', namespace_id='wiki',
                target_uri=WIKI, raw_sources_prefix=RAW, schema_id='business', schema_version='1')
        assert e.store.namespace_list('acct') == []
        with pytest.raises(SDKNotFound):
            await client.kb.get_wiki_collection('invalid')
    finally:
        await client.close()


async def test_registered_collection_receipt_never_waives_current_raw_domain_membership(remote):
    from openviking_sdk.errors import PermissionDeniedError as SDKDenied
    app, e, context, backend = remote
    client = _client(app)
    try:
        await _register(client)
        context[0] = e.ctx
        assert (await client.kb.get_wiki_collection('manual'))['namespace_id'] == 'wiki'
        backend.acl(RAW, [{'principal':'user:another','level':'read'}])
        with pytest.raises(SDKDenied):
            await client.kb.get_wiki_collection('manual')
        with pytest.raises(SDKDenied):
            await client.kb.build_wiki(source_uris=[ONE], collection_id='manual', idempotency_key='revoked')
    finally:
        await client.close()


async def test_registered_namespace_cannot_be_reused_for_a_second_wiki_collection(remote):
    app, e, context, backend = remote
    client = _client(app)
    try:
        await _register(client)
        with pytest.raises(SDKConflict):
            await client.kb.create_wiki_collection(collection_id='second', namespace_id='wiki',
                target_uri=WIKI + '/another', raw_sources_prefix=RAW + '/another',
                schema_id='business', schema_version='1')
        with pytest.raises(SDKNotFound):
            await client.kb.get_wiki_collection('second')
    finally:
        await client.close()


async def test_one_registered_raw_domain_feeds_independent_collections_only_with_each_export(remote):
    app, e, context, backend = remote
    client = _client(app)
    wiki2 = 'viking://resources/wiki2'
    e.fs._async_agfs.path(e.fs._uri_to_path(wiki2, ctx=e.ctx)).mkdir(parents=True, exist_ok=True)
    backend.acl(wiki2, [{'principal':'user:user','level':'write'}])
    try:
        await client.kb.register_schema(schema_id='business', version='1', files=package())
        for namespace, root in [('raw', RAW), ('wiki', WIKI), ('wiki2', wiki2)]:
            await client.kb.register_namespace(namespace_id=namespace, root_uri=root)
        for collection, namespace, target, raw in [
            ('one', 'wiki', WIKI, RAW + '/incoming-one'),
            ('two', 'wiki2', wiki2, RAW + '/incoming-two'),
        ]:
            await client.kb.create_wiki_collection(collection_id=collection, namespace_id=namespace,
                target_uri=target, raw_sources_prefix=raw, schema_id='business', schema_version='1')
        first = await client.kb.build_wiki(source_uris=[ONE], collection_id='one', idempotency_key='one')
        second = await client.kb.build_wiki(source_uris=[ONE], collection_id='two', idempotency_key='two')
        await client.kb.set_export(source_namespace='raw', target_namespace='wiki', principal='user:user', allowed=True)
        planner = WikiBuildPlanner(e.fs)
        assert (await planner.authorize_inputs(e.ctx, first['build_id'])).target_namespace_id == 'wiki'
        with pytest.raises(PermissionDeniedError, match='Explicit'):
            await planner.authorize_inputs(e.ctx, second['build_id'])
        await client.kb.set_export(source_namespace='raw', target_namespace='wiki2', principal='user:user', allowed=True)
        assert (await planner.authorize_inputs(e.ctx, second['build_id'])).target_namespace_id == 'wiki2'
        await client.kb.set_export(source_namespace='raw', target_namespace='wiki', principal='user:user', allowed=False)
        with pytest.raises(PermissionDeniedError, match='Explicit'):
            await planner.authorize_inputs(e.ctx, first['build_id'])
        assert (await planner.authorize_inputs(e.ctx, second['build_id'])).target_namespace_id == 'wiki2'
    finally:
        await client.close()
