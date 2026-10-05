"""Actual registered worker callbacks preserve leases and current identity."""
import asyncio
from dataclasses import replace

import pytest
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding

from openviking.service.kb_job_executor import JobExecutor
from openviking_cli.session.user_id import UserIdentifier


async def test_executor_calls_real_async_operation_and_persists_written(environment):
    e = environment
    job = e.store.job_create('acct', 'manuals', 'one', 'D1', {}, {}, 'user')
    calls = []

    async def identity(job):
        return e.ctx

    async def handler(job, ctx, lease):
        assert ctx is e.ctx
        async def operation():
            calls.append(job['job_id'])
            await asyncio.sleep(.1)
            await lease.assert_active()
            return {'actual_callback': True}
        await lease.run_parse(operation)

    executor = JobExecutor(e.store, {'D1': handler}, identity, lease_seconds=.06)
    result = await executor.run_once()
    assert calls == [job['job_id']]
    assert result['state'] == 'written' and result['checkpoint']['actual_callback'] is True
    assert result['owner'] is None


async def test_executor_reloads_identity_and_rejects_other_user_before_writes(environment):
    e = environment
    job = e.store.job_create('acct', 'manuals', 'one', 'D1', {}, {}, 'user')
    called = []
    async def identity(job):
        return replace(e.ctx, user=UserIdentifier('acct', 'changed-user'))
    async def handler(job, ctx, lease):
        called.append(True)
    result = await JobExecutor(e.store, {'D1': handler}, identity).run_once()
    assert called == []
    assert result['state'] == 'failed' and result['info']['code'] == 'PERMISSION_DENIED'


async def test_executor_write_exception_quarantines_slot_and_does_not_retry(environment):
    e = environment
    job = e.store.job_create('acct', 'manuals', 'one', 'D1', {}, {}, 'user')
    async def identity(job):
        return e.ctx
    async def handler(job, ctx, lease):
        async def operation():
            raise RuntimeError('unknown backend outcome')
        await lease.run_parse(operation)
    result = await JobExecutor(e.store, {'D1': handler}, identity).run_once()
    assert result['state'] == 'write_recovery_required'
    assert e.store.job_claim('another') is None
    assert e.store.job_get('acct', job['job_id'])['state'] != 'ready'
