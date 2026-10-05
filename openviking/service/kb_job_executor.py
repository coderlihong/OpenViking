"""Durable worker execution with current identity and authority-fenced writes."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Awaitable, Callable
from uuid import uuid4

from openviking_cli.exceptions import PermissionDeniedError


class ParseSlotBusy(RuntimeError):
    pass


class WorkerLease:
    def __init__(self, repository, job, lease_seconds):
        self.repository, self.job, self.lease_seconds = repository, job, lease_seconds
        self.owner, self.fence = job['owner'], job['fence']

    async def assert_active(self):
        await asyncio.to_thread(self.repository.job_heartbeat, self.job['job_id'],
                                self.owner, self.fence, self.lease_seconds)

    async def checkpoint(self, stage, checkpoint, info=None):
        await asyncio.to_thread(self.repository.job_checkpoint, self.job['job_id'],
                                self.owner, self.fence, stage, checkpoint, info)

    async def run_parse(self, operation: Callable[[], Awaitable[dict]]):
        """Run one real parser/write operation under the shared backend slot.

        A normal return means the awaited operation completed its writes. Any
        exception/cancellation is conservatively treated as uncertain writes.
        Return after this step: written relinquishes this job's worker lease.
        """
        acquired = await asyncio.to_thread(self.repository.job_parse_acquire,
                                          self.job['job_id'], self.owner, self.fence)
        if not acquired:
            raise ParseSlotBusy('Global parser/write slot is occupied')
        try:
            await self.assert_active()
            checkpoint = await operation()
            if not isinstance(checkpoint, dict):
                raise ValueError('Parser operation must return a checkpoint object')
            await self.assert_active()
        except BaseException:
            with suppress(Exception):
                await asyncio.shield(asyncio.to_thread(
                    self.repository.job_parse_release, self.job['job_id'],
                    self.owner, self.fence, 'uncertain'))
            raise
        await asyncio.to_thread(self.repository.job_parse_release, self.job['job_id'],
                                self.owner, self.fence, 'written', checkpoint)
        return checkpoint


class JobExecutor:
    """Claim and execute one job stage using explicitly registered handlers.

    context_resolver(job) must reload identity/groups from the current identity
    service. Handlers receive (job, current_context, WorkerLease). A parser stage
    must use lease.run_parse; index/Wiki stages must checkpoint progress. Returning
    a draft_ready result is allowed only after the private-candidate gate.
    """
    def __init__(self, repository, handlers, context_resolver, *, owner=None, lease_seconds=30):
        self.repository = repository
        self.handlers = dict(handlers)
        self.context_resolver = context_resolver
        self.owner = owner or f'worker-{uuid4().hex}'
        self.lease_seconds = lease_seconds

    async def _heartbeat(self, lease):
        while True:
            await asyncio.sleep(self.lease_seconds / 3)
            await lease.assert_active()

    async def run_once(self):
        job = await asyncio.to_thread(self.repository.job_claim, self.owner,
                                      self.lease_seconds, None, list(self.handlers))
        if job is None:
            return None
        lease = WorkerLease(self.repository, job, self.lease_seconds)
        heartbeat = asyncio.create_task(self._heartbeat(lease))
        try:
            ctx = await self.context_resolver(job)
            if ctx is None or ctx.bypass_acl or ctx.account_id != job['account'] or ctx.user.user_id != job['principal']:
                raise PermissionDeniedError('Job execution requires the current original user identity')
            result = await self.handlers[job['kind']](job, ctx, lease)
            current = await asyncio.to_thread(self.repository.job_get, job['account'], job['job_id'])
            if current['state'] == 'written':
                return current
            if result and result.get('state') in {'ready', 'draft_ready'}:
                await asyncio.to_thread(self.repository.job_complete, job['job_id'],
                                        lease.owner, lease.fence, result['state'], result.get('receipt'))
            else:
                # An executed handler which cannot prove publication stays
                # resumable, carrying its persisted checkpoint and gate reason.
                await asyncio.to_thread(self.repository.job_fail, job['job_id'],
                                        lease.owner, lease.fence,
                                        {'code': 'PUBLICATION_GATE_PENDING'}, 30)
        except ParseSlotBusy:
            await asyncio.to_thread(self.repository.job_fail, job['job_id'],
                                    lease.owner, lease.fence, {'code': 'PARSE_SLOT_BUSY'}, 1)
        except asyncio.CancelledError:
            # Lease timeout recovers an ordinary stage; a running parser's slot
            # is already quarantined by run_parse and remains fenced.
            raise
        except Exception as exc:
            with suppress(ValueError):
                await asyncio.to_thread(self.repository.job_fail, job['job_id'],
                                        lease.owner, lease.fence,
                                        {'code': getattr(exc, 'code', 'EXECUTION_FAILED'), 'message': str(exc)})
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await heartbeat
        return await asyncio.to_thread(self.repository.job_get, job['account'], job['job_id'])
