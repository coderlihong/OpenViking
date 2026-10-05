"""Real authority receipts for submitted members, not complete FLOW acceptance."""
import time

import pytest

from test_document_job_runtime import create, runtime as runtime
from test_wiki_namespaces import domains as domains, prepared as prepared
from test_wiki_namespaces import environment as environment, embedding as embedding, RAW
from test_registry_authority import setup as setup, _start
from test_image_publication import image_env as image_env, consume, manifest

from openviking.indexing.batch import current_batch_id
from openviking.indexing.registry_authority import AuthorityRepository
from openviking.telemetry import get_current_telemetry
from openviking.telemetry.request_wait_tracker import get_request_wait_tracker
from openviking.utils.embedding_utils import vectorize_file, vectorize_directory_meta


def retry_now(store, job_id):
    with store.db() as db:
        db.execute('UPDATE kb_jobs SET available_at=? WHERE job_id=?', (time.time() - 1, job_id))


async def test_durable_outer_batch_holds_all_members_and_resumes_without_reindex(runtime):
    rt, e, _, _, _ = runtime
    job = await create(rt, e, kind='D2')
    members = []
    uris = [RAW + '/manual/one.md', RAW + '/manual/image.png']

    async def producer(**kwargs):
        current = e.store.job_get('acct', job['job_id'])
        batch_id = current['checkpoint']['publication_batch_id']
        assert current_batch_id(e.store, 'acct', 'resources') == batch_id
        for uri in uris:
            generation = e.store.begin('acct', 'resources', uri, 'plan', ['child'], batch=batch_id)
            e.store.seal(generation)
            members.append(generation)
        e.store.acknowledge(members[0], 'child')
        # Even a fully received member cannot publish while the outer producer
        # is open or another submitted member lacks its backend receipt.
        lease, manifest = e.store.capture('acct')
        try:
            assert all(uri not in manifest for uri in uris)
        finally:
            e.store.release(lease)
        return {'status': 'completed', 'rebuilt_records': 2, 'failed_records': 0}

    rt.reindex.execute.side_effect = producer
    pending = await rt.executor.run_once()
    observation = pending['checkpoint']['index_publication']
    assert observation['state'] == 'queued'
    assert (observation['member_count'], observation['expected_count'], observation['received_count']) == (2, 2, 1)
    assert observation['all_submitted_members_complete'] is False
    retry_now(e.store, job['job_id'])
    assert (await rt.executor.run_once())['checkpoint']['index_publication'] == observation
    assert rt.reindex.execute.await_count == 1
    assert e.store.acknowledge(members[1], 'child')
    retry_now(e.store, job['job_id'])
    completed_members = await rt.executor.run_once()
    observation = completed_members['checkpoint']['index_publication']
    assert observation['state'] == 'published' and observation['all_submitted_members_complete']
    # Once every submitted member holds its receipt and the heads switched,
    # the committed-evidence gate verifies and the document becomes ready.
    assert completed_members['state'] == 'ready'
    assert completed_members['checkpoint']['publication_gate']['verified'] is True
    assert rt.reindex.execute.await_count == 1
    lease, manifest = e.store.capture('acct')
    try:
        assert [manifest[uri]['generation'] for uri in uris] == members
    finally:
        e.store.release(lease)


async def test_interrupted_open_batch_is_isolated_before_new_attempt(runtime):
    rt, e, _, _, _ = runtime
    job = await create(rt, e, kind='D2')
    claim = e.store.job_claim('dead-producer')
    old_batch = e.store.begin_batch('acct', 'resources')
    old = e.store.begin('acct', 'resources', RAW + '/manual/one.md', 'old', ['c'], batch=old_batch)
    e.store.seal(old)
    e.store.job_checkpoint(job['job_id'], 'dead-producer', claim['fence'], 'index',
                           {'publication_batch_id': old_batch, 'publication_namespace': 'resources'})
    with e.store.db() as db:
        db.execute('UPDATE kb_jobs SET lease_until=0 WHERE job_id=?', (job['job_id'],))
    e.store.job_recover()
    resumed = await rt.executor.run_once()
    assert resumed['checkpoint']['publication_batch_id'] != old_batch
    assert e.store.batch_state(old_batch)['state'] == 'failed'
    assert e.store.state(old)['state'] == 'failed'
    assert not e.store.acknowledge(old, 'c')
    assert rt.reindex.execute.await_count == 1


async def test_partial_index_failure_never_seals_batch(runtime):
    rt, e, _, _, _ = runtime
    await create(rt, e, kind='D2')
    rt.reindex.execute.return_value = {'status': 'completed', 'failed_records': 1}
    failed = await rt.executor.run_once()
    assert failed['state'] == 'failed'
    assert e.store.batch_state(failed['checkpoint']['publication_batch_id'])['state'] == 'failed'


async def test_mixed_publication_namespace_is_rejected_before_generation_creation(runtime):
    rt, e, _, _, _ = runtime
    await create(rt, e, kind='D2')

    async def producer(**kwargs):
        current_batch_id(e.store, 'acct', 'other-domain')

    rt.reindex.execute.side_effect = producer
    failed = await rt.executor.run_once()
    assert failed['state'] == 'failed'
    assert e.store.batch_state(failed['checkpoint']['publication_batch_id'])['state'] == 'failed'


async def test_real_text_image_semantic_consumers_finish_before_outer_seal_without_deadlock(runtime, image_env):
    rt, e, _, _, _ = runtime
    assert e is image_env
    await create(rt, e, kind='D2')
    root = RAW + '/manual'
    image, text = root + '/diagram.png', root + '/chapter.md'
    tracker = get_request_wait_tracker()
    telemetry_id = get_current_telemetry().telemetry_id
    tracker.register_request(telemetry_id)

    async def producers(**kwargs):
        e.queue.messages.clear()
        await e.fs.write(image, b'test image asset', ctx=e.ctx)
        await e.fs.write(text, '# Chapter\n\nSource content with an image.', ctx=e.ctx)
        assert await vectorize_file(image, {'name': 'diagram.png', 'summary': 'image context'}, root, ctx=e.ctx)
        assert await vectorize_file(text, {'name': 'chapter.md', 'summary': 'text'}, root, ctx=e.ctx)
        await e.fs.write(root + '/.abstract.md', 'directory abstract', ctx=e.ctx)
        await e.fs.write(root + '/.overview.md', 'directory overview', ctx=e.ctx)
        await vectorize_directory_meta(root, 'directory abstract', 'directory overview', ctx=e.ctx)
        for message in e.queue.messages:
            await consume(e, [message])
            assert not ({image, text, root} & set(manifest(e)))
        # This is the exact request-wait mechanism ReindexExecutor._run uses.
        # All vector receipts finish while the outer batch is still building;
        # waiting does not depend on published heads and therefore cannot cycle.
        await tracker.wait_for_request(telemetry_id, timeout=1)
        assert not ({image, text, root} & set(manifest(e)))
        return {'status': 'completed', 'rebuilt_records': len(e.queue.messages), 'failed_records': 0}

    rt.reindex.execute.side_effect = producers
    try:
        result = await rt.executor.run_once()
        observation = result['checkpoint']['index_publication']
        assert observation['state'] == 'published'
        assert observation['member_count'] == 3
        assert observation['expected_count'] == observation['received_count']
        assert {image, text, root} <= set(manifest(e))
        assert result['state'] == 'ready'
        assert result['checkpoint']['publication_gate']['verified'] is True
    finally:
        tracker.cleanup(telemetry_id)


def test_authority_status_reads_real_receipts_and_checks_job_fence_and_scope(setup):
    process = _start(setup)
    try:
        repository = AuthorityRepository(setup[0], setup[2])
        job = repository.job_create('acct', 'manuals', 'joint', 'D2', {'uri': RAW + '/one.md'}, {}, 'user')
        claim = repository.job_claim('worker')
        batch = repository.begin_batch('acct', 'resources')
        repository.job_checkpoint(job['job_id'], 'worker', claim['fence'], 'index',
                                  {'publication_batch_id': batch, 'publication_namespace': 'resources'})
        generation = repository.begin('acct', 'resources', RAW + '/one.md', 'plan', ['a', 'b'], batch=batch)
        repository.seal(generation)
        assert repository.job_publication_seal(job['job_id'], 'worker', claim['fence'])['state'] == 'queued'
        repository.acknowledge(generation, 'a')
        observation = repository.job_publication_status(job['job_id'], 'worker', claim['fence'])
        assert observation['expected_count'] == 2 and observation['received_count'] == 1
        with pytest.raises(ValueError, match='STALE_OWNER'):
            repository.job_publication_status(job['job_id'], 'worker', claim['fence'] + 1)
        with pytest.raises(ValueError, match='STALE_OWNER'):
            repository.job_publication_seal(job['job_id'], 'worker', claim['fence'] + 1)
        other = repository.begin_batch('other-account', 'resources')
        repository.job_checkpoint(job['job_id'], 'worker', claim['fence'], 'index',
                                  {'publication_batch_id': other, 'publication_namespace': 'resources'})
        with pytest.raises(ValueError, match='account/namespace'):
            repository.job_publication_status(job['job_id'], 'worker', claim['fence'])
    finally:
        process.terminate()
        process.join(10)


@pytest.mark.parametrize('lease_change', ['expired', 'replaced'])
async def test_stale_worker_cannot_seal_or_switch_heads(runtime, lease_change):
    rt, e, _, _, _ = runtime
    job = await create(rt, e, kind='D2')
    claim = e.store.job_claim('producer')
    batch = e.store.begin_batch('acct', 'resources')
    uri = RAW + '/manual/one.md'
    generation = e.store.begin('acct', 'resources', uri, 'plan', ['c'], batch=batch)
    e.store.seal(generation)
    assert not e.store.acknowledge(generation, 'c')
    e.store.job_checkpoint(job['job_id'], 'producer', claim['fence'], 'index',
                           {'publication_batch_id': batch, 'publication_namespace': 'resources'})
    with e.store.db() as db:
        if lease_change == 'expired':
            db.execute('UPDATE kb_jobs SET lease_until=0 WHERE job_id=?', (job['job_id'],))
        else:
            db.execute('UPDATE kb_jobs SET owner=?,fence=fence+1 WHERE job_id=?',
                       ('replacement', job['job_id']))
    with pytest.raises(ValueError, match='STALE_OWNER'):
        e.store.job_publication_seal(job['job_id'], 'producer', claim['fence'])
    assert e.store.batch_state(batch)['state'] == 'building'
    lease, heads = e.store.capture('acct')
    try:
        assert uri not in heads
    finally:
        e.store.release(lease)
    # With a valid current fence, the very same real receipt set can commit.
    with e.store.db() as db:
        db.execute('UPDATE kb_jobs SET owner=?,lease_until=? WHERE job_id=?',
                   ('replacement', time.time() + 30, job['job_id']))
    current = e.store.job_get('acct', job['job_id'])
    result = e.store.job_publication_seal(job['job_id'], 'replacement', current['fence'])
    assert result['state'] == 'published' and result['published']
