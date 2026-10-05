# SPDX-License-Identifier: AGPL-3.0
"""Managed vector/metadata mutation window at the last async adapter boundary."""
from contextlib import asynccontextmanager

from openviking.indexing.publication import configured_store
from openviking.service.task_tracker_concurrency import run_to_completion
from openviking_cli.exceptions import FailedPreconditionError


def backend_identity(adapter):
    # This identifies the configured adapter boundary, not a claimed vendor
    # operation-status/fencing capability. Do not put credentials in this value.
    return ':'.join((type(adapter).__name__, str(getattr(adapter, 'mode', 'unknown')),
                     str(getattr(adapter, '_project_path', '')), str(getattr(adapter, '_collection_name', 'context'))))


@asynccontextmanager
async def managed_write_window(adapter, payloads):
    managed = [payload for payload in payloads if payload.get('index_generation')]
    if not managed:
        yield
        return
    registry = configured_store()
    if registry is None:
        raise FailedPreconditionError('Managed vector writes require the publication authority')
    import asyncio
    identity = backend_identity(adapter)
    groups = {}
    for payload in managed:
        identity_fields = {key: payload[key] for key in (
            'account_id', 'uri', 'index_generation', 'index_plan_id', 'chunk_id', 'level', 'is_leaf'
        ) if key in payload}
        groups.setdefault(payload.get('account_id'), []).append(identity_fields)
    tokens = []
    submitted = False
    try:
        for account, records in groups.items():
            token = await asyncio.to_thread(registry.generation_write_begin, account, identity, records)
            tokens.append((account, token))
        submitted = True
        yield
        for account, token in tokens:
            await run_to_completion(lambda account=account, token=token: asyncio.to_thread(
                registry.generation_write_confirm, account, token, identity))
    except BaseException:
        for account, token in tokens:
            try:
                command = registry.generation_write_unknown if submitted else registry.generation_write_confirm
                await run_to_completion(lambda account=account, token=token, command=command: asyncio.to_thread(
                    command, account, token, identity))
            except Exception:
                # Lost authority replies cannot authorize replay/clearing.
                # The durable writing token already blocks unsafe GC.
                pass
        raise
