"""Actual resource service/VikingFS job bridge; parsing and indexing are substitutes."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_wiki_namespaces import domains as domains, prepared as prepared
from test_wiki_namespaces import environment as environment, embedding as embedding, RAW

from openviking.server.identity import Role
from openviking.service.kb_job_runtime import DocumentJobRuntime
from openviking.service.resource_service import ResourceService


class Identity:
    def __init__(self):
        self.groups = ()
        self.exists = True
        self.refreshes = 0

    async def refresh_identity_registry_if_changed(self, account):
        self.refreshes += 1

    async def ensure_account_groups_loaded(self, account):
        pass

    def has_user(self, account, user):
        return self.exists

    def get_user_role(self, account, user):
        return Role.USER

    def get_user_group_ids(self, account, user):
        return self.groups


class Processor:
    def __init__(self, fs):
        self.fs, self.calls = fs, []

    async def process_resource(self, **kwargs):
        self.calls.append(kwargs)
        await kwargs['stage_callback']('parsing')
        content = Path(kwargs['path']).read_text()
        await kwargs['stage_callback']('finalizing')
        root = kwargs['to'] or kwargs['parent'] + '/manual.md'
        return {'status': 'success', 'root_uri': root,
                '_post_process': {'root_uri': root, 'body': content}}

    async def finish_prepared_resource(self, prepared, *, ctx, **kwargs):
        await self.fs.write_file(prepared['root_uri'], prepared['body'], ctx=ctx)
        return {'status': 'success'}


@pytest.fixture
async def runtime(domains, tmp_path):
    e, _, _, _, backend = domains
    backend.acl(RAW, [{'principal': 'user:user', 'level': 'write'}])
    processor = Processor(e.fs)
    resources = ResourceService(vikingdb=object(), viking_fs=e.fs,
                                resource_processor=processor, skill_processor=object())
    resources._enqueue_add_resource_job = AsyncMock(side_effect=AssertionError('Must not enqueue parser'))
    identities = Identity()
    source = tmp_path / 'manual.md'
    source.write_text('# Manual\nKnown fact at the tail.\n')
    resolved = SimpleNamespace(local_path=str(source), original_filename=source.name, cleanup=AsyncMock())
    uploads = SimpleNamespace(resolve_for_consume=AsyncMock(return_value=resolved))
    index = SimpleNamespace(execute=AsyncMock(return_value={'status': 'success'}))
    service = SimpleNamespace(viking_fs=e.fs, resources=resources)
    rt = DocumentJobRuntime(service, e.store, identities, uploads, reindex=index)
    return rt, e, processor, resolved, backend


def slot_empty(store):
    with store.db() as db:
        return db.execute('SELECT count(*) FROM kb_parse_slot').fetchone()[0] == 0


async def create(rt, e, *, kind='D1', key='test'):
    payload = {'temp_file_id': 'temp', 'parent': RAW} if kind == 'D1' else {'uri': RAW + '/manual/one.md'}
    return await rt.jobs.create(e.ctx, namespace_id='manuals', kind=kind, idempotency_key=key,
                               payload=payload, effective_config={
                                   'profile': 'document', 'parse_backend': 'auto', 'model_mode': 'auto',
                                   'structure_policy': 'bookmarks_preferred', 'backend_options': {},
                               })


async def test_d1_awaits_resource_materialization_before_written_and_indexes_separately(runtime):
    rt, e, processor, resolved, _ = runtime
    job = await create(rt, e)
    written = await rt.executor.run_once()
    assert written['job_id'] == job['job_id'] and written['state'] == 'written'
    assert b'Known fact at the tail.' in await e.fs.read(RAW + '/manual.md', ctx=e.ctx)
    assert rt.reindex.execute.await_count == 0
    assert slot_empty(e.store)
    rt.service.resources._enqueue_add_resource_job.assert_not_awaited()
    assert processor.calls[0]['build_index'] is False
    assert 'structure_policy' not in processor.calls[0]  # PDF-only defaults do not reach MD.
    resolved.cleanup.assert_awaited_once()
    indexed = await rt.executor.run_once()
    assert indexed['state'] == 'backoff' and indexed['checkpoint']['index_dispatched']
    assert indexed['info']['code'] == 'PUBLICATION_GATE_PENDING'
    assert rt.reindex.execute.call_args.kwargs['wait'] is True


async def test_d2_never_uses_parser_or_global_parse_slot(runtime):
    rt, e, processor, _, _ = runtime
    await create(rt, e, kind='D2')
    indexed = await rt.executor.run_once()
    assert indexed['state'] == 'backoff' and indexed['checkpoint']['index_dispatched']
    assert processor.calls == []
    rt.uploads.resolve_for_consume.assert_not_awaited()
    assert slot_empty(e.store)


async def test_identity_deleted_before_claim_fails_without_parser(runtime):
    rt, e, processor, _, _ = runtime
    await create(rt, e)
    rt.identities.exists = False
    failed = await rt.executor.run_once()
    assert failed['state'] == 'failed' and failed['info']['code'] == 'PERMISSION_DENIED'
    assert processor.calls == []
    assert slot_empty(e.store)


async def test_known_office_preflight_failure_does_not_quarantine_parse_slot(runtime, monkeypatch):
    rt, e, processor, resolved, _ = runtime
    await create(rt, e)
    resolved.original_filename = 'manual.docx'
    monkeypatch.setattr('openviking.parse.office_converter.OfficeConverter.is_available', lambda _: False)
    failed = await rt.executor.run_once()
    assert failed['state'] == 'failed' and failed['info']['code'] == 'FAILED_PRECONDITION'
    assert processor.calls == []
    assert slot_empty(e.store)
    resolved.cleanup.assert_awaited_once()


async def test_office_d1_uses_the_unified_document_parser_options(runtime, monkeypatch):
    rt, e, processor, resolved, _ = runtime
    await create(rt, e)
    resolved.original_filename = 'manual.docx'
    monkeypatch.setattr('openviking.parse.office_converter.OfficeConverter.is_available', lambda _: True)
    written = await rt.executor.run_once()
    assert written['state'] == 'written' and slot_empty(e.store)
    assert processor.calls[0]['structure_policy'] == 'bookmarks_preferred'
    assert processor.calls[0]['parse_backend'] == 'auto'
    resolved.cleanup.assert_awaited_once()
