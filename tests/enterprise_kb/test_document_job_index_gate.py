"""Durable index outcomes remain distinct from full publication receipts."""
import time

import pytest

from test_document_job_runtime import create, runtime as runtime
from test_wiki_namespaces import domains as domains, prepared as prepared
from test_wiki_namespaces import environment as environment, embedding as embedding, RAW


@pytest.mark.parametrize('outcome', [
    {'status': 'completed', 'failed_records': 2, 'rebuilt_records': 5},
    {'status': 'completed', 'unsupported_records': 1},
    {'status': 'running'},
    {'status': 'completed', 'uri': 'viking://resources/wrong'},
])
async def test_partial_or_wrong_index_result_never_records_success(runtime, outcome):
    rt, e, _, _, _ = runtime
    await create(rt, e, kind='D2')
    rt.reindex.execute.return_value = outcome
    result = await rt.executor.run_once()
    assert result['state'] == 'failed'
    assert result['checkpoint']['index_dispatched'] is False
    for key, value in outcome.items():
        assert result['checkpoint']['index_result'][key] == value
    assert 'publication_gate' not in result['checkpoint']


async def test_successful_d1_waits_for_joint_receipt_without_reparsing_or_reindex(runtime):
    rt, e, processor, _, _ = runtime
    job = await create(rt, e)
    assert (await rt.executor.run_once())['state'] == 'written'
    rt.reindex.execute.return_value = {
        'status': 'completed', 'uri': RAW + '/manual.md',
        'rebuilt_records': 17, 'failed_records': 0,
        'warnings': ['x' * 2048] * 30,
    }
    result = await rt.executor.run_once()
    assert result['state'] == 'backoff' and result['stage'] == 'index'
    checkpoint = result['checkpoint']
    assert checkpoint['index_result']['rebuilt_records'] == 17
    assert len(checkpoint['index_result']['warnings']) == 20
    assert all(len(warning) == 1024 for warning in checkpoint['index_result']['warnings'])
    # A completed reindex request is not publication evidence: the gate reads
    # committed authority artifacts. The mocked reindex enqueued nothing, so
    # only body_tree has a real gap; the other categories have no submitted
    # members to prove or deny and stay unverified.
    gate = checkpoint['publication_gate']
    assert gate['verified'] is False
    assert set(gate['missing']) == {'body_tree'}
    assert gate['missing']['body_tree'] == [RAW + '/manual.md']
    # Advance only the durable retry clock. The next worker must reuse the
    # successful observation, while ready remains forbidden by the authority.
    with e.store.db() as db:
        db.execute('UPDATE kb_jobs SET available_at=? WHERE job_id=?',
                   (time.time() - 1, job['job_id']))
    resumed = await rt.executor.run_once()
    assert resumed['state'] == 'backoff'
    assert resumed['checkpoint'] == checkpoint
    assert len(processor.calls) == 1
    assert rt.reindex.execute.await_count == 1


async def test_identity_revoked_during_index_does_not_record_success(runtime):
    rt, e, _, _, _ = runtime
    await create(rt, e, kind='D2')

    async def revoke(**kwargs):
        rt.identities.exists = False
        return {'status': 'completed', 'rebuilt_records': 3, 'failed_records': 0}

    rt.reindex.execute.side_effect = revoke
    result = await rt.executor.run_once()
    assert result['state'] == 'failed'
    assert result['info']['code'] == 'PERMISSION_DENIED'
    assert result['checkpoint']['index_dispatched'] is False
    assert result['checkpoint']['index_result']['rebuilt_records'] == 3
