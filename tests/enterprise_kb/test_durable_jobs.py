"""Durable scheduling and real independent workers against one authority."""
import multiprocessing
import time
from contextlib import contextmanager

import pytest
from test_registry_authority import setup as setup, _start

from openviking.indexing.publication import PublicationStore
from openviking.indexing.registry_authority import AuthorityRepository


@pytest.fixture
def store(tmp_path):
    return PublicationStore(str(tmp_path / 'jobs.db'))


def submit(store, key='key', kind='D1', namespace='manuals', account='a', config=None):
    return store.job_create(account, namespace, key, kind, {'source': key}, config or {}, 'user')


def test_idle_claim_opens_no_write_transaction(store):
    """The ~1/min claim poll must not queue on the single writer when idle."""
    calls = []
    original = store.db

    @contextmanager
    def spy(*args, **kwargs):
        calls.append(kwargs.get('readonly', False))
        with original(*args, **kwargs) as db:
            yield db

    store.db = spy
    assert store.job_claim('worker') is None
    assert calls and all(calls), 'an idle poll must only open read-only transactions'
    del store.db  # restore the real writer path for the positive control
    submit(store)
    calls.clear()
    store.db = spy
    claimed = store.job_claim('worker')
    assert claimed is not None
    assert not all(calls), 'a due job still claims through a write transaction'


def test_idempotency_scope_payload_and_effective_config(store):
    first = submit(store)
    assert submit(store)['job_id'] == first['job_id']
    with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'):
        submit(store, config={'parse_backend': 'other'})
    assert submit(store, namespace='other')['job_id'] != first['job_id']
    assert submit(store, account='b')['job_id'] != first['job_id']
    assert store.job_get('b', first['job_id']) is None
    assert PublicationStore(store.path).job_get('a', first['job_id'])['state'] == 'queued'


def test_claim_recovery_late_owner_and_persisted_checkpoint(store):
    job = submit(store)
    first = store.job_claim('first', .03)
    store.job_checkpoint(job['job_id'], 'first', first['fence'], 'validate', {'revision': 4})
    time.sleep(.04)
    second = store.job_claim('second', 30)
    assert second['attempt'] == 2 and second['fence'] > first['fence']
    assert second['checkpoint'] == {'revision': 4}
    for operation in [
        lambda: store.job_heartbeat(job['job_id'], 'first', first['fence']),
        lambda: store.job_checkpoint(job['job_id'], 'first', first['fence'], 'index', {}),
        lambda: store.job_parse_acquire(job['job_id'], 'first', first['fence']),
    ]:
        with pytest.raises(ValueError, match='STALE_OWNER'):
            operation()


def test_parse_slot_cross_account_backend_release_and_cancel(store):
    a = submit(store, 'one')
    b = submit(store, 'two', account='b', namespace='other')
    x, y = store.job_claim('x'), store.job_claim('y')
    assert store.job_parse_acquire(a['job_id'], 'x', x['fence'])
    assert not store.job_parse_acquire(b['job_id'], 'y', y['fence'])
    store.job_parse_release(a['job_id'], 'x', x['fence'], 'written', {'bytes': 5})
    assert store.job_get('a', a['job_id'])['state'] == 'written'
    assert store.job_parse_acquire(b['job_id'], 'y', y['fence'])
    cancelled = store.job_cancel('b', b['job_id'])
    assert cancelled['state'] == 'write_recovery_required'
    with pytest.raises(ValueError, match='STALE_OWNER'):
        store.job_parse_release(b['job_id'], 'y', y['fence'], 'written')
    next_job = submit(store, 'third')
    # Claim the earlier written stage first and terminate it without fake ready.
    indexed = store.job_claim('index')
    store.job_fail(indexed['job_id'], 'index', indexed['fence'], {'code': 'GATE_PENDING'})
    next_claim = store.job_claim('next')
    assert next_claim['job_id'] == next_job['job_id']
    assert not store.job_parse_acquire(next_job['job_id'], 'next', next_claim['fence'])


@pytest.mark.parametrize('kind', ['D1', 'D2', 'W1', 'W2'])
def test_no_job_can_report_ready_from_queue_or_self_reported_receipt(store, kind):
    job = submit(store, kind=kind)
    claim = store.job_claim('worker')
    with pytest.raises(ValueError, match='PUBLICATION_GATE_REQUIRED'):
        store.job_complete(job['job_id'], 'worker', claim['fence'], 'ready', {'all_gates': True})
    assert store.job_get('a', job['job_id'])['state'] == 'running'


def test_draft_ready_requires_exact_wiki_profile_and_private_checkpoint(store):
    job = submit(store, kind='W2', config={'profile': 'wiki_draft'})
    claim = store.job_claim('worker')
    assert store.job_parse_acquire(job['job_id'], 'worker', claim['fence'])
    store.job_parse_release(job['job_id'], 'worker', claim['fence'], 'written')
    claim = store.job_claim('worker')
    store.job_checkpoint(job['job_id'], 'worker', claim['fence'], 'wiki_extract', {})
    store.job_checkpoint(job['job_id'], 'worker', claim['fence'], 'wiki_validate',
                         {'candidate_uri': 'viking://private/candidate', 'diagnostic_published': False})
    with pytest.raises(ValueError, match='Fixed-plan private candidate receipt'):
        store.job_complete(job['job_id'], 'worker', claim['fence'], 'draft_ready')
    assert store.job_get('a', job['job_id'])['state'] == 'running'


def _gate_submitted_batch(store, kind='D2', uri='viking://resources/raw/document.md',
                          source_ref=None, key='key'):
    """Bind an index-stage job to a batch whose member receipts are complete
    but whose joint publication transaction has not run yet."""
    job = submit(store, key=key, kind=kind)
    claim = store.job_claim('worker')
    batch = store.begin_batch('a', 'resources')
    store.job_checkpoint(job['job_id'], 'worker', claim['fence'], 'index',
                         {'publication_batch_id': batch, 'publication_namespace': 'resources'})
    generation = store.begin('a', 'resources', uri, 'plan', ['c'], batch=batch,
                             source_ref=source_ref)
    store.seal(generation)
    assert not store.acknowledge(generation, 'c')  # single-member batch waits for seal_batch
    with store.db() as db:
        db.execute("INSERT INTO publication_lexical VALUES (?,?,?,?,?,?)",
                   (generation, 'c', uri, 'tokens', 'tokenizer',
                    '{"parent_chunk_id": "p"}'))
    return job, claim, batch, uri, generation


def test_ready_gate_rejects_out_of_band_edits_before_joint_publication(store):
    """KB-006: an out-of-band edit landing after member receipts but before the
    joint publication transaction must fail the batch, so the ready decision
    rejects instead of verifying stale evidence."""
    for edited in ('superseded', 'draft-mutation', 'inflight-write'):
        uri = f'viking://resources/raw/document-{edited}.md'
        source_ref = None
        if edited == 'draft-mutation':
            source_ref = {'sha256': 'a' * 64, 'size': 5,
                          'draft_epoch': store.observe_draft('a', uri)}
        job, claim, batch, uri, generation = _gate_submitted_batch(store, uri=uri,
                                                                   source_ref=source_ref,
                                                                   key=f'gate-{edited}')
        if edited == 'superseded':
            replacement = store.begin('a', 'resources', uri, 'out-of-band', ['r'])
            store.seal(replacement)
        elif edited == 'draft-mutation':
            store.invalidate_draft('a', uri)
        else:
            token = store.begin_write('a', uri)
        assert not store.seal_batch(batch)
        assert store.batch_state(batch)['state'] == 'failed'
        with pytest.raises(ValueError, match='PUBLICATION_GATE_REQUIRED'):
            store.job_complete(job['job_id'], 'worker', claim['fence'], 'ready', {'all_gates': True})
        assert store.job_get('a', job['job_id'])['state'] == 'running'
        if edited == 'inflight-write':
            store.finish_write(token)


def test_ready_keeps_published_tree_serving_during_out_of_band_replacement(store):
    """替换期间公开旧树: once the joint transaction has published, a later index
    attempt may move the desired pointer without retracting the served tree, so
    the ready gate still verifies the committed generation."""
    job, claim, batch, uri, generation = _gate_submitted_batch(store)
    assert store.seal_batch(batch)
    assert store.batch_state(batch)['state'] == 'published'
    replacement = store.begin('a', 'resources', uri, 'out-of-band', ['r'])
    store.seal(replacement)
    assert store.job_complete(job['job_id'], 'worker', claim['fence'], 'ready', {'all_gates': True})
    assert store.job_get('a', job['job_id'])['state'] == 'ready'


def test_stage_checkpoint_cannot_skip_or_acquire_parse_without_global_slot(store):
    job = submit(store)
    claim = store.job_claim('worker')
    for stage in ('parse_write', 'index', 'publish_gate'):
        with pytest.raises(ValueError):
            store.job_checkpoint(job['job_id'], 'worker', claim['fence'], stage, {})


def _parse_worker(endpoint, token_file, owner, output):
    repository = AuthorityRepository(endpoint, token_file)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = repository.job_claim(owner, 5)
        if not job:
            if all(j['state'] == 'failed' for j in repository.job_list('a')):
                return
            time.sleep(.02)
            continue
        if job['stage'] == 'index':
            repository.job_fail(job['job_id'], owner, job['fence'], {'code': 'FULL_PUBLICATION_GATE_UNIMPLEMENTED'})
            continue
        if not repository.job_parse_acquire(job['job_id'], owner, job['fence']):
            repository.job_fail(job['job_id'], owner, job['fence'], {'code': 'PARSE_SLOT_BUSY'}, .02)
            continue
        output.put(('start', job['job_id'], owner, time.monotonic()))
        time.sleep(.08)
        repository.job_parse_release(job['job_id'], owner, job['fence'], 'written', {'parsed_by': owner})
        output.put(('end', job['job_id'], owner, time.monotonic()))
    raise RuntimeError('workers did not finish')


def _crash_worker(endpoint, token_file, parse, output):
    repository = AuthorityRepository(endpoint, token_file)
    job = repository.job_claim('crashing-worker', .15)
    if parse:
        assert repository.job_parse_acquire(job['job_id'], 'crashing-worker', job['fence'])
    output.put(job)
    time.sleep(30)


def test_two_worker_processes_three_jobs_share_single_parse_slot(setup):
    process = _start(setup)
    repository = AuthorityRepository(setup[0], setup[2])
    ctx = multiprocessing.get_context('spawn')
    output = ctx.Queue()
    workers = [ctx.Process(target=_parse_worker, args=(setup[0], setup[2], f'worker-{i}', output)) for i in range(2)]
    try:
        jobs = [submit(repository, f'job-{i}', kind=kind, namespace=f'backend-{i}')
                for i, kind in enumerate(['D1', 'D2', 'W1'])]
        for worker in workers:
            worker.start()
        events = [output.get(timeout=15) for _ in range(6)]
        for worker in workers:
            worker.join(15)
            assert worker.exitcode == 0
        active = 0
        for event, _, _, _ in sorted(events, key=lambda e: e[3]):
            active += 1 if event == 'start' else -1
            assert active in {0, 1}
        assert active == 0
        for job in jobs:
            result = repository.job_get('a', job['job_id'])
            assert result['state'] == 'failed'
            assert result['checkpoint']['parsed_by'].startswith('worker-')
            assert result['info']['code'] == 'FULL_PUBLICATION_GATE_UNIMPLEMENTED'
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(15)
        process.terminate()
        process.join(15)


@pytest.mark.parametrize('parse', [False, True])
def test_worker_kill_restart_and_unknown_writer_quarantine(setup, parse):
    process = _start(setup)
    repository = AuthorityRepository(setup[0], setup[2])
    job = submit(repository)
    ctx = multiprocessing.get_context('spawn')
    output = ctx.Queue()
    worker = ctx.Process(target=_crash_worker, args=(setup[0], setup[2], parse, output))
    try:
        worker.start()
        old = output.get(timeout=15)
        worker.terminate()
        worker.join(15)
        time.sleep(.2)
        process.terminate()
        process.join(15)
        process = _start(setup)
        repository.job_recover()
        result = repository.job_get('a', job['job_id'])
        assert result['state'] == ('write_recovery_required' if parse else 'backoff')
        with pytest.raises(ValueError, match='STALE_OWNER'):
            repository.job_heartbeat(job['job_id'], old['owner'], old['fence'])
        if parse:
            submit(repository, 'second')
            claimed = repository.job_claim('new')
            assert not repository.job_parse_acquire(claimed['job_id'], 'new', claimed['fence'])
        else:
            claimed = repository.job_claim('new')
            assert claimed['job_id'] == job['job_id'] and claimed['attempt'] == 2
    finally:
        if worker.is_alive():
            worker.terminate()
        worker.join(15)
        process.terminate()
        process.join(15)


def test_known_aborted_parser_releases_slot_and_backoff_does_not_hold_parser(store):
    a = submit(store, 'aborted')
    b = submit(store, 'next')
    first, second = store.job_claim('first'), store.job_claim('second')
    assert store.job_parse_acquire(a['job_id'], 'first', first['fence'])
    assert store.job_parse_release(a['job_id'], 'first', first['fence'], 'aborted')
    assert store.job_parse_acquire(b['job_id'], 'second', second['fence'])
    store.job_parse_release(b['job_id'], 'second', second['fence'], 'written')
    claim = store.job_claim('retry')
    store.job_fail(claim['job_id'], 'retry', claim['fence'], {'code': 'RETRY'}, 10)
    assert store.job_get('a', claim['job_id'])['state'] == 'backoff'
    store.job_cancel('a', claim['job_id'])
    assert store.job_get('a', claim['job_id'])['state'] == 'cancelled'
    with pytest.raises(ValueError, match='STALE_OWNER'):
        store.job_heartbeat(claim['job_id'], 'retry', claim['fence'])


def test_quarantine_without_source_writes_resolves_but_pending_write_never_does(store):
    import sqlite3

    crashed = submit(store, 'crashed-mid-parse')
    claim = store.job_claim('crashed-worker')
    assert store.job_parse_acquire(crashed['job_id'], 'crashed-worker', claim['fence'])
    db_path = store.path
    with sqlite3.connect(db_path) as raw:
        raw.execute("UPDATE kb_parse_slot SET state='uncertain' WHERE slot=1")
        raw.execute("UPDATE kb_jobs SET state='write_recovery_required',owner=NULL,lease_until=0 WHERE job_id=?",
                    (crashed['job_id'],))
    resolved = store.job_parse_quarantine_resolve(crashed['job_id'])
    assert resolved['state'] == 'backoff' and resolved['stage'] == 'validate'
    assert store.job_get('a', crashed['job_id'])['state'] == 'backoff'
    next_claim = store.job_claim('next-worker')
    assert next_claim['job_id'] == crashed['job_id'] and next_claim['attempt'] == 2

    quarantined = submit(store, 'crashed-with-write')
    second = store.job_claim('second-crashed')
    assert store.job_parse_acquire(quarantined['job_id'], 'second-crashed', second['fence'])
    with sqlite3.connect(db_path) as raw:
        raw.execute(
            "INSERT INTO source_writes(id, account, uri, state, created) "
            "VALUES ('token-live', 'a', 'viking://resources/raw/y.md', 'active', 2)"
        )
        raw.execute("INSERT INTO source_write_refs VALUES ('token-live', 'local-file', 'op-2', '{}')")
        raw.execute("UPDATE kb_parse_slot SET state='uncertain' WHERE slot=1")
        raw.execute("UPDATE kb_jobs SET state='write_recovery_required',owner=NULL,lease_until=0 WHERE job_id=?",
                    (quarantined['job_id'],))
    with pytest.raises(ValueError, match='SOURCE_WRITES_PENDING'):
        store.job_parse_quarantine_resolve(quarantined['job_id'])
    assert store.job_get('a', quarantined['job_id'])['state'] == 'write_recovery_required'
    blocked = store.job_claim('third-worker')
    assert blocked is None or blocked['job_id'] != quarantined['job_id']
    with sqlite3.connect(db_path) as raw:
        raw.execute("DELETE FROM source_writes WHERE id='token-live'")
        raw.execute("DELETE FROM source_write_refs WHERE token='token-live'")
    with pytest.raises(ValueError, match='STALE_OWNER'):
        store.job_parse_release(quarantined['job_id'], 'second-crashed', second['fence'], 'uncertain')


def test_d2_uri_index_can_skip_parser_while_d1_cannot(store):
    d2 = submit(store, 'uri', kind='D2')
    claim = store.job_claim('uri-worker')
    assert store.job_checkpoint(d2['job_id'], 'uri-worker', claim['fence'], 'index', {'index_dispatched': True})
    assert store.job_get('a', d2['job_id'])['stage'] == 'index'
    d1 = submit(store, 'upload')
    claim = store.job_claim('upload-worker')
    with pytest.raises(ValueError, match='skip'):
        store.job_checkpoint(d1['job_id'], 'upload-worker', claim['fence'], 'index', {})


def test_manual_maintenance_text_in_json_columns_does_not_wedge_claims(store):
    import sqlite3

    job = submit(store)
    with sqlite3.connect(store.path) as raw:
        raw.execute("UPDATE kb_jobs SET info='manual maintenance note' WHERE job_id=?", (job['job_id'],))
    claimed = store.job_claim('next-worker')
    assert claimed is not None and claimed['job_id'] == job['job_id']
    assert claimed['info'] == {'raw': 'manual maintenance note'}
    with sqlite3.connect(store.path) as raw:
        raw.execute("UPDATE kb_jobs SET checkpoint='' WHERE job_id=?", (job['job_id'],))
    assert store.job_get('a', job['job_id'])['checkpoint'] == {}


def test_failed_batch_from_earlier_attempt_resets_instead_of_raising(store):
    batch = store.begin_batch('a', 'manuals')
    store.fail_batch(batch, 'Document producer interrupted before durable seal')
    job = submit(store, 'uri-doc', kind='D2')
    first = store.job_claim('first')
    assert store.job_checkpoint(job['job_id'], 'first', first['fence'], 'index',
                                {'publication_batch_id': batch, 'publication_namespace': 'manuals'})
    observed = store.job_publication_status(job['job_id'], 'first', first['fence'])
    assert observed['state'] == 'failed'
