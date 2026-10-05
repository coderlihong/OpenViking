"""Real registry fencing; physical GC capability remains explicitly unavailable."""
import asyncio
import hashlib
import json
import threading
from types import SimpleNamespace

import pytest
from test_registry_authority import setup as setup, _start

from openviking.indexing.publication import PublicationStore
from openviking.indexing.registry_authority import AuthorityRepository
from openviking.storage.viking_vector_index_backend import _SingleAccountBackend
from openviking.storage.vector_ids import vector_record_id_for_data
from openviking_cli.exceptions import ConflictError, FailedPreconditionError
from openviking_cli.utils.config.vectordb_config import VectorDBBackendConfig

ACCOUNT = 'acct'
URI = 'viking://resources/raw/document.md'


def stage(store, *, text=b'body', children=('c',), uri=URI):
    reference = {'sha256': hashlib.sha256(text).hexdigest(), 'size': len(text)}
    generation = store.begin(ACCOUNT, 'raw', uri, 'plan', list(children), source_ref=reference)
    return generation, reference


def publish(store, **kwargs):
    generation, reference = stage(store, **kwargs)
    store.seal(generation)
    for child in kwargs.get('children', ('c',)):
        store.acknowledge(generation, child)
    return generation, reference


def fields(generation, child='c'):
    return {'account_id': ACCOUNT, 'uri': URI, 'index_generation': generation,
            'index_plan_id': 'plan', 'chunk_id': child, 'level': 2, 'vector': [1., 0.],
            'context_type': 'resource', 'abstract': 'body'}


@pytest.fixture
def registry(tmp_path, monkeypatch):
    registry = PublicationStore(str(tmp_path/'registry.db'))
    monkeypatch.setattr('openviking.indexing.generation_write.configured_store', lambda: registry)
    return registry


class ControlledAdapterBoundary:
    """Instrument an actual blocking adapter await, with no GC proof capability."""
    mode = 'instrumented'
    USE_CONTENT_FIELD = False
    def __init__(self, *, error=None):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.error = error
        self.written = []
        self.existing = []
    def get_collection(self):
        return SimpleNamespace(get_meta_data=lambda: {'Fields': [
            {'FieldName': key} for key in ('id','account_id','uri','index_generation','index_plan_id','chunk_id','level','vector','context_type','abstract','search_tags')
        ]})
    def get(self, ids):
        return self.existing
    def upsert(self, payload):
        self.entered.set()
        assert self.release.wait(15)
        if self.error:
            raise self.error
        payloads = payload if isinstance(payload, list) else [payload]
        self.written.extend(payloads)
        return [item['id'] for item in payloads]


def backend(adapter):
    return _SingleAccountBackend(VectorDBBackendConfig(backend='local', name='context', dimension=2),
                                 ACCOUNT, shared_adapter=adapter)


@pytest.mark.parametrize('bulk', [False, True])
async def test_actual_upsert_await_window_blocks_gc_and_only_confirms_after_return(registry, bulk):
    generation, _ = stage(registry)
    adapter = ControlledAdapterBoundary()
    writer = asyncio.create_task(backend(adapter).upsert_many([fields(generation)]) if bulk else backend(adapter).upsert(fields(generation)))
    try:
        assert await asyncio.to_thread(adapter.entered.wait, 10)
        windows = registry.generation_writes(ACCOUNT)
        assert len(windows) == 1 and windows[0]['state'] == 'writing'
        assert json.loads(windows[0]['records'])[0]['generation'] == generation
        with pytest.raises(FailedPreconditionError, match='VECTOR_WRITE_RECOVERY_REQUIRED'):
            registry.physical_gc_prepare(ACCOUNT, [generation], 'while-writing', 0)
        adapter.release.set()
        assert await writer
        assert registry.generation_writes(ACCOUNT) == []
        with registry.db() as db:
            assert db.execute('SELECT state FROM generation_write_windows').fetchone()[0] == 'confirmed'
    finally:
        adapter.release.set()
        if not writer.done():
            await writer


async def test_adapter_exception_and_task_cancellation_keep_unknown_write_protection(registry):
    generation, _ = stage(registry)
    adapter = ControlledAdapterBoundary(error=RuntimeError('adapter outcome unknown'))
    adapter.release.set()
    with pytest.raises(RuntimeError, match='outcome unknown'):
        await backend(adapter).upsert(fields(generation))
    first = registry.generation_writes(ACCOUNT)[0]
    assert first['state'] == 'unknown'
    with pytest.raises(FailedPreconditionError, match='backend recovery'):
        registry.generation_write_confirm(ACCOUNT, first['token'], first['backend'])
    adapter = ControlledAdapterBoundary()
    writer = asyncio.create_task(backend(adapter).upsert(fields(generation)))
    assert await asyncio.to_thread(adapter.entered.wait, 10)
    writer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await writer
    assert {row['state'] for row in registry.generation_writes(ACCOUNT)} == {'unknown'}
    # The real underlying thread still exists and may mutate after cancellation.
    adapter.release.set()
    await asyncio.to_thread(lambda: adapter.release.wait(1))
    with pytest.raises(FailedPreconditionError, match='VECTOR_WRITE_RECOVERY_REQUIRED'):
        registry.physical_gc_prepare(ACCOUNT, [generation], 'cancelled', 0)


async def test_partial_metadata_write_inherits_managed_generation_before_hook(registry):
    generation, _ = publish(registry)
    adapter = ControlledAdapterBoundary()
    existing = fields(generation)
    existing['id'] = vector_record_id_for_data(existing)
    adapter.existing = [existing]
    writer = asyncio.create_task(backend(adapter).upsert({'id': existing['id'], 'search_tags': ['updated']}, options={'partial_update': True}))
    try:
        assert await asyncio.to_thread(adapter.entered.wait, 10)
        assert registry.generation_writes(ACCOUNT)
        adapter.release.set()
        await writer
        assert adapter.written[0]['index_generation'] == generation
        assert adapter.written[0]['search_tags'] == ['updated']
    finally:
        adapter.release.set()
        if not writer.done():
            await writer


async def test_generation_superseded_during_preparation_cannot_reach_actual_adapter(registry):
    generation, _ = stage(registry)
    adapter = ControlledAdapterBoundary()
    target = backend(adapter)
    original = target._prepare_upsert_payload
    def supersede(payload):
        stage(registry, text=b'new intent')
        return original(payload)
    target._prepare_upsert_payload = supersede
    with pytest.raises(FailedPreconditionError, match='superseded'):
        await target.upsert(fields(generation))
    assert not adapter.entered.is_set() and registry.generation_writes(ACCOUNT) == []


def test_gc_intent_fences_retired_generation_and_rejects_fabricated_physical_proof(registry):
    old, reference = publish(registry)
    lease, _ = registry.capture(ACCOUNT)
    publish(registry, text=b'replacement')
    with pytest.raises(FailedPreconditionError, match='leased'):
        registry.physical_gc_prepare(ACCOUNT, [old], 'held', 0)
    registry.release(lease)
    intent = registry.physical_gc_prepare(ACCOUNT, [old], 'gc', 0)
    assert json.loads(intent['manifest'])['delete_blobs'] == [reference['sha256']]
    assert registry.physical_gc_prepare(ACCOUNT, [old], 'gc', 0)['id'] == intent['id']
    with pytest.raises(FailedPreconditionError, match='GENERATION_GC_FENCED'):
        registry.generation_write_begin(ACCOUNT, 'actual-boundary', [fields(old)])
    with pytest.raises(FailedPreconditionError, match='PHYSICAL_GC_CAPABILITY_REQUIRED'):
        registry.physical_gc_execute(ACCOUNT, intent['id'])
    with pytest.raises(TypeError):
        registry.physical_gc_execute(ACCOUNT, intent['id'], proof={'all_indexes_deleted': True})
    assert registry.gc_audit(ACCOUNT) == []
    assert registry.physical_gc_get(ACCOUNT, intent['id'])['state'] == 'capability_required'


def test_unknown_source_writer_and_cross_account_gc_intent_remain_protected(registry):
    old, _ = publish(registry)
    publish(registry, text=b'replacement')
    token = registry.begin_write(ACCOUNT, URI)
    registry.uncertain_write(token)
    with pytest.raises(FailedPreconditionError, match='WRITE_RECOVERY_REQUIRED'):
        registry.physical_gc_prepare(ACCOUNT, [old], 'unknown', 0)
    with pytest.raises(ConflictError, match='account mismatch'):
        registry.physical_gc_prepare('other', [old], 'other', 0)
    assert registry.pending_writes(ACCOUNT, URI)


def test_shared_blob_kept_and_lexical_meta_members_are_part_of_gc_manifest(registry):
    old, reference = publish(registry)
    publish(registry, text=b'replacement')
    publish(registry, uri='viking://resources/raw/copy.md', text=b'body')
    with registry.db() as db:
        db.execute('INSERT INTO publication_lexical VALUES (?,?,?,?,?,?)', (old,'c',URI,'tokens','tokenizer','{}'))
        db.execute('INSERT INTO publication_metadata VALUES (?,?,?)', (old, '{}', '[]'))
    intent = registry.physical_gc_prepare(ACCOUNT, [old], 'shared', 0)
    manifest = json.loads(intent['manifest'])
    assert reference['sha256'] not in manifest['delete_blobs']
    assert manifest['generations'][0]['lexical_rows'] == 1
    assert manifest['generations'][0]['metadata_rows'] == 1
    with registry.db() as db:
        assert db.execute('SELECT count(*) FROM publication_lexical WHERE generation=?', (old,)).fetchone()[0] == 1


def test_authority_rpc_write_windows_and_gc_intent_survive_reopen(setup):
    process = _start(setup)
    try:
        repository = AuthorityRepository(setup[0], setup[2])
        old, _ = publish(repository)
        token = repository.generation_write_begin(ACCOUNT, 'worker-boundary', [fields(old)])
        publish(repository, text=b'replacement')
        repository.generation_write_unknown(ACCOUNT, token, 'worker-boundary')
        with pytest.raises(FailedPreconditionError, match='VECTOR_WRITE_RECOVERY_REQUIRED'):
            repository.physical_gc_prepare(ACCOUNT, [old], 'blocked', 0)
        assert repository.generation_writes(ACCOUNT)[0]['state'] == 'unknown'
    finally:
        process.terminate()
        process.join(15)
    process = _start(setup)
    try:
        repository = AuthorityRepository(setup[0], setup[2])
        assert repository.generation_writes(ACCOUNT)[0]['token'] == token
        with pytest.raises(FailedPreconditionError, match='VECTOR_WRITE_RECOVERY_REQUIRED'):
            repository.physical_gc_prepare(ACCOUNT, [old], 'blocked', 0)
    finally:
        process.terminate()
        process.join(15)


def _semantic(store, body):
    uri = 'viking://resources/raw/folder'
    epoch = store.observe_draft(ACCOUNT, uri + '/.abstract.md')
    reference = {'sha256': hashlib.sha256(body).hexdigest(), 'size': len(body), 'draft_epoch': epoch}
    generation = store.begin(ACCOUNT, 'raw', uri, 'semantic-plan', ['semantic:0'], semantic_refs={'0': reference})
    store.seal(generation)
    store.acknowledge(generation, 'semantic:0')
    assert store.state(generation)['state'] == 'published'
    return uri, generation, reference


def test_gc_query_lease_keeps_old_semantic_sidecar_and_tombstone_keeps_old_cache(registry):
    uri, old, semantic_ref = _semantic(registry, b'old abstract')
    lease = registry.gc_lease(ACCOUNT)
    _semantic(registry, b'new abstract')
    with registry.db() as db:
        assert (ACCOUNT, semantic_ref['sha256']) in registry._protected_blobs(db)
    registry.gc_release(lease)
    registry.tombstone(ACCOUNT, uri)
    with registry.db() as db:
        assert (ACCOUNT, semantic_ref['sha256']) in registry._protected_blobs(db)
    cache_ref = {'sha256': hashlib.sha256(b'old cache').hexdigest(), 'size': len(b'old cache')}
    body_ref = {'sha256': hashlib.sha256(b'old asset').hexdigest(), 'size': len(b'old asset'), 'image_semantics': cache_ref}
    generation = registry.begin(ACCOUNT, 'raw', URI, 'image-plan', ['c'], source_ref=body_ref)
    registry.seal(generation)
    registry.acknowledge(generation, 'c')
    publish(registry, text=b'new asset')
    registry.tombstone(ACCOUNT, URI)
    with registry.db() as db:
        assert (ACCOUNT, cache_ref['sha256']) in registry._protected_blobs(db)


async def test_real_local_vector_upsert_roundtrip_uses_managed_window(registry, tmp_path):
    from openviking.storage.vectordb_adapters.local_adapter import LocalCollectionAdapter
    adapter = LocalCollectionAdapter('managed_window', str(tmp_path/'vectors'), 'default')
    schema = {'Fields': [
        {'FieldName': 'id', 'FieldType': 'string', 'IsPrimaryKey': True},
        {'FieldName': 'vector', 'FieldType': 'vector', 'Dim': 4},
        *({'FieldName': key, 'FieldType': 'string'} for key in
          ('account_id','uri','index_generation','index_plan_id','chunk_id','context_type','abstract')),
        {'FieldName': 'level', 'FieldType': 'int64'},
    ]}
    assert adapter.create_collection('managed_window', schema, distance='cosine', sparse_weight=0., index_name='default')
    generation, _ = stage(registry)
    try:
        row = fields(generation)
        row['vector'] = [1., 0., 0., 0.]
        row['id'] = vector_record_id_for_data(row)
        assert await backend(adapter).upsert(row) == row['id']
        fetched = adapter.get([row['id']])
        assert fetched[0]['index_generation'] == generation and fetched[0]['chunk_id'] == 'c'
        assert registry.generation_writes(ACCOUNT) == []
        with registry.db() as db:
            assert db.execute('SELECT state FROM generation_write_windows').fetchone()[0] == 'confirmed'
        # The actual local adapter's synchronous write return is not a durable
        # all-writer/delete-visibility capability for physical retirement.
        registry.seal(generation)
        registry.acknowledge(generation, 'c')
        publish(registry, text=b'new body')
        intent = registry.physical_gc_prepare(ACCOUNT, [generation], 'local-gc', 0)
        with pytest.raises(FailedPreconditionError, match='PHYSICAL_GC_CAPABILITY_REQUIRED'):
            registry.physical_gc_execute(ACCOUNT, intent['id'])
        assert adapter.get([row['id']])
    finally:
        adapter.close()


async def test_none_delete_and_unknown_writer_never_create_released_audit(registry):
    old, reference = publish(registry)
    publish(registry, text=b'new body')
    registry.plan_gc(ACCOUNT, grace_seconds=0)
    with pytest.raises(FailedPreconditionError, match='PHYSICAL_GC_CAPABILITY_REQUIRED'):
        await registry.reclaim(ACCOUNT)
    token = registry.begin_write(ACCOUNT, URI)
    registry.uncertain_write(token)
    outcomes = await registry.reclaim(ACCOUNT)
    assert outcomes == [{'sha': reference['sha256'], 'released': False, 'reason': 'write_recovery_required'}]
    assert registry.gc_audit(ACCOUNT) == []


def test_physical_intent_execute_rechecks_current_blob_reachability(registry):
    old, reference = publish(registry)
    publish(registry, text=b'replacement')
    intent = registry.physical_gc_prepare(ACCOUNT, [old], 'before-reference', 0)
    publish(registry, text=b'body', uri='viking://resources/raw/referenced-again.md')
    with pytest.raises(ConflictError, match='referenced again'):
        registry.physical_gc_execute(ACCOUNT, intent['id'])
    assert registry.gc_audit(ACCOUNT) == []


async def test_mismatched_adapter_id_leaves_managed_window_unknown(registry):
    generation, _ = stage(registry)
    adapter = ControlledAdapterBoundary()
    adapter.release.set()
    def wrong_id(payload):
        return ['different-id']
    adapter.upsert = wrong_id
    with pytest.raises(RuntimeError, match='matching confirmed record ID'):
        await backend(adapter).upsert(fields(generation))
    assert registry.generation_writes(ACCOUNT)[0]['state'] == 'unknown'


def test_semantic_gc_plan_includes_retired_sidecar_only_after_reader_release(registry):
    uri, old, reference = _semantic(registry, b'old abstract')
    lease = registry.gc_lease(ACCOUNT)
    _semantic(registry, b'new abstract')
    assert reference['sha256'] not in {row['sha'] for row in registry.plan_gc(ACCOUNT, grace_seconds=0)}
    registry.gc_release(lease)
    assert {'account': ACCOUNT, 'sha': reference['sha256'], 'kind': 'semantic'} in registry.plan_gc(ACCOUNT, grace_seconds=0)


async def test_leased_published_generation_can_receive_current_acl_projection(registry):
    old, _ = publish(registry)
    lease, _ = registry.capture(ACCOUNT)
    publish(registry, text=b'replacement')
    adapter = ControlledAdapterBoundary()
    existing = fields(old)
    existing['id'] = vector_record_id_for_data(existing)
    adapter.existing = [existing]
    adapter.release.set()
    try:
        assert await backend(adapter).upsert({'id': existing['id'], 'search_tags': ['current-acl-projection']}, options={'partial_update': True})
        assert adapter.written[0]['index_generation'] == old
        assert registry.generation_writes(ACCOUNT) == []
        with pytest.raises(FailedPreconditionError, match='leased'):
            registry.physical_gc_prepare(ACCOUNT, [old], 'during-projection', 0)
    finally:
        registry.release(lease)
    with pytest.raises(FailedPreconditionError, match='retired'):
        await backend(adapter).upsert({'id': existing['id'], 'search_tags': ['too-late']}, options={'partial_update': True})
