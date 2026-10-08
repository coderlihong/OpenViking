"""Actual registered worker callbacks preserve leases and current identity."""
import asyncio
import sqlite3
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


async def test_heartbeat_survives_transient_store_lock(environment, monkeypatch):
    """A transient 'database is locked' during one renewal must not kill the
    heartbeat task: a dead heartbeat silently forfeits the lease, the job is
    requeued mid-run (attempt++), and minutes of resume replay are discarded."""
    e = environment
    job = e.store.job_create('acct', 'manuals', 'hb-lock', 'W1', {}, {}, 'user')
    real = e.store.job_heartbeat
    ticks = {'n': 0}

    def flaky(*args, **kwargs):
        ticks['n'] += 1
        if ticks['n'] == 1:
            raise sqlite3.OperationalError('database is locked')
        return real(*args, **kwargs)

    monkeypatch.setattr(e.store, 'job_heartbeat', flaky)

    async def identity(job):
        return e.ctx

    alive = []

    async def handler(job, ctx, lease):
        # Span several heartbeat ticks; without renewal the .06s lease expires
        # and this assert_active raises STALE_OWNER.
        await asyncio.sleep(.25)
        await lease.assert_active()
        alive.append(job['attempt'])
        return None

    result = await JobExecutor(e.store, {'W1': handler}, identity, lease_seconds=.06).run_once()
    assert alive == [1], 'lease must survive a transient renewal failure without requeue'
    assert result['info']['code'] == 'PUBLICATION_GATE_PENDING'
    assert ticks['n'] > 1, 'heartbeat must keep renewing after a transient error'


async def test_heartbeat_stops_when_fence_lost(environment, monkeypatch):
    """STALE_OWNER (ValueError) means the lease is genuinely gone; the heartbeat
    must exit instead of spinning renewals for a job another worker now owns."""
    e = environment
    job = e.store.job_create('acct', 'manuals', 'hb-stale', 'W1', {}, {}, 'user')
    ticks = {'n': 0}

    def stealing(*args, **kwargs):
        ticks['n'] += 1
        if ticks['n'] == 2:
            raise ValueError('STALE_OWNER: job lease/fence is not active')
        return True

    monkeypatch.setattr(e.store, 'job_heartbeat', stealing)

    async def identity(job):
        return e.ctx

    async def handler(job, ctx, lease):
        await asyncio.sleep(.3)
        return None

    # The heartbeat exits at the STALE_OWNER tick, so the lease lapses; the
    # closing job_fail then trips STALE_OWNER too and is suppressed inside
    # run_once, leaving the expired row untouched for lease recovery.
    result = await JobExecutor(e.store, {'W1': handler}, identity, lease_seconds=.06).run_once()
    assert ticks['n'] == 2, 'heartbeat must exit after STALE_OWNER, not keep ticking'
    assert result['state'] == 'running' and result['attempt'] == 1
