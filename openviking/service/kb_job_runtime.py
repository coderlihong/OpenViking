# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Run durable document jobs through the existing resource and reindex services."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path

from openviking.indexing.publication import configured_store
from openviking.server.identity import RequestContext, Role
from openviking.server.temp_upload_store import TempUploadStore
from openviking.service.kb_job_executor import JobExecutor
from openviking.service.kb_job_service import KBJobService
from openviking.service.reindex_executor import get_reindex_executor
from openviking.storage.acl import AclAction
from openviking_cli.exceptions import (
    ConflictError, FailedPreconditionError, InvalidArgumentError, PermissionDeniedError, ProcessingError,
)
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils import get_logger

logger = get_logger(__name__)


def _document_publication_gate(repository, job_id, owner, fence):
    """Joint-publication coverage read from committed authority evidence.

    The seven categories (body_tree, images, parent_child, semantic_levels,
    bm25, metadata, version_switch) are each proven by their own durable
    artifact; nothing here accepts the reindex result summary as proof.
    """
    return asyncio.to_thread(repository.job_publication_ready, job_id, owner, fence)


class _DocumentPublicationBatch:
    """Bind every producer to the durable job's pre-created publication batch."""
    def __init__(self, registry, batch_id, account, namespace):
        self.registry, self.id = registry, batch_id
        self.account, self.namespace = account, namespace

    def join(self, registry, account, namespace):
        if (registry.path, account, namespace) != (self.registry.path, self.account, self.namespace):
            raise ValueError('Document publication batch cannot cross registry/account/namespace')
        return self.id


class DocumentJobRuntime:
    def __init__(self, service, repository, identity_manager, uploads, *, reindex=None):
        self.service, self.repository = service, repository
        self.identities, self.uploads = identity_manager, uploads
        self.jobs = KBJobService(service.viking_fs, repository)
        self.reindex = reindex or get_reindex_executor()
        self.executor = JobExecutor(repository, {'D1': self.ingest, 'D2': self.index,
            'W1': self.extract_wiki, 'W2': self.extract_wiki}, self.resolve_context)

    async def extract_wiki(self, job, ctx, lease):
        from dataclasses import asdict
        from openviking.wiki.build_plan import WikiBuildPlanner
        from openviking.wiki.extraction import WikiDraftExtractor
        from openviking.wiki.policies import EXTRACTOR_VERSION, POLICY_VERSION
        planner = WikiBuildPlanner(self.service.viking_fs, registry=self.repository)
        extractor = WikiDraftExtractor(planner)
        _, model_profile = extractor._model()
        configuration = job['effective_config']
        if (configuration.get('profile') != 'wiki_draft'
                or configuration.get('extractor_version') != EXTRACTOR_VERSION
                or configuration.get('policy_version') != POLICY_VERSION
                or configuration.get('model_profile') != model_profile
                or configuration.get('limits') != asdict(extractor.limits)):
            raise ConflictError('Wiki extraction configuration changed since job submission')
        await lease.checkpoint('wiki_extract', job.get('checkpoint') or {})
        async def refresh():
            current = await self.resolve_context(job)
            ctx.role, ctx.group_ids = current.role, current.group_ids
        async def checkpoint(progress):
            await refresh()
            await lease.checkpoint('wiki_extract', {'extraction_progress': progress})
        receipt = await extractor.extract(ctx, job['payload']['build_id'],
            expected_plan_digest=job['payload']['plan_digest'],
            profile=configuration['extraction_profile'], checkpoint=checkpoint,
            resume=(job.get('checkpoint') or {}).get('extraction_progress'))
        # Read every server-written blob again under all current contributor
        # permissions before recording a terminal private candidate result.
        for draft in receipt['receipts']:
            await refresh()
            await extractor.read_draft(ctx, job['payload']['build_id'],
                expected_plan_digest=job['payload']['plan_digest'], receipt=draft)
        await refresh()
        await lease.checkpoint('wiki_validate', {'draft_receipt': receipt,
                                               'diagnostic_published': False})
        return {'state': 'draft_ready', 'receipt': receipt}

    async def resolve_context(self, job):
        if self.identities is None:
            raise PermissionDeniedError('Durable jobs require a current identity registry')
        account, principal = job['account'], job['principal']
        await self.identities.refresh_identity_registry_if_changed(account)
        await self.identities.ensure_account_groups_loaded(account)
        if not self.identities.has_user(account, principal):
            raise PermissionDeniedError('Job creator is no longer an active user')
        ctx = RequestContext(
            user=UserIdentifier(account, principal),
            role=Role(self.identities.get_user_role(account, principal)),
            group_ids=tuple(self.identities.get_user_group_ids(account, principal)),
        )
        await self.jobs._authorize(ctx, job, AclAction.WRITE)
        return ctx

    async def ingest(self, job, ctx, lease):
        if job['stage'] == 'index':
            return await self.index(job, ctx, lease)
        payload = job['payload']
        resolved = await self.uploads.resolve_for_consume(payload['temp_file_id'], ctx)
        options = {key: value for key, value in job['effective_config'].items()
                   if key in {'structure_policy', 'parse_backend', 'model_mode',
                              'backend_options', 'section_target_tokens'}}

        async def parse_and_write():
            async def before_stage(stage):
                # Refresh groups/role at the parser-to-persistent-write boundary.
                current = await self.resolve_context(job)
                ctx.role, ctx.group_ids = current.role, current.group_ids
                await lease.assert_active()

            await before_stage('validate')
            # This is the existing awaited parser/materializer. It deliberately
            # performs no queue-producing semantic or embedding work while
            # holding the global parse/write slot.
            result = await self.service.resources._execute_resource_ingestion(
                path=resolved.local_path, ctx=ctx, defer_post_processing=False,
                to=payload.get('to'), parent=payload.get('parent'),
                build_index=False, summarize=False, manage_watch=False,
                allow_local_path_resolution=True, source_name=resolved.original_filename,
                temp_file_id=payload['temp_file_id'], stage_callback=before_stage,
                **options,
            )
            if result.get('status') == 'error' or not result.get('root_uri'):
                raise ProcessingError('Resource parser/materializer did not complete')
            return {'root_uri': result['root_uri'], 'materialized': True,
                    'info': result.get('info', []),
                    'alignment_status': result.get('alignment_status'),
                    'structure_source': result.get('structure_source')}
        try:
            suffix = Path(resolved.original_filename or resolved.local_path).suffix.lower()
            office = suffix in {'.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx'}
            if office:
                from openviking.parse.office_converter import OfficeConverter

                if not OfficeConverter().is_available():
                    raise FailedPreconditionError('Office jobs require an available Office-to-PDF converter')
            if suffix != '.pdf' and not office:
                if (options.get('parse_backend', 'auto') not in {'auto', 'local'}
                        or options.get('model_mode', 'auto') != 'auto'
                        or options.get('backend_options')):
                    raise InvalidArgumentError('Model parser options require a PDF input')
                options = {}  # Markdown uses its existing local parser contract.
            await lease.run_parse(parse_and_write)
        finally:
            await resolved.cleanup()

    async def index(self, job, ctx, lease):
        checkpoint = dict(job.get('checkpoint') or {})
        if checkpoint.get('publication_batch_id'):
            observed = await asyncio.to_thread(self.repository.job_publication_status,
                                               job['job_id'], lease.owner, lease.fence)
            if observed['state'] in {'queued', 'published'}:
                checkpoint['index_publication'] = observed
                gate = await _document_publication_gate(self.repository, job['job_id'],
                                                        lease.owner, lease.fence)
                if gate['verified']:
                    checkpoint['publication_gate'] = dict(gate, code='PUBLICATION_VERIFIED')
                    await lease.checkpoint('index', checkpoint)
                    return {'state': 'ready'}
                checkpoint['publication_gate'] = gate
                await lease.checkpoint('index', checkpoint, {'code': 'PUBLICATION_GATE_PENDING'})
                return None  # Resume real receipts, never regenerate queued work.
            if observed['state'] in {'building', 'failed'}:
                # A lost producer never sealed this batch; any late receipts
                # remain invisible after fail_batch. A failed batch from an
                # earlier attempt is equally unresumable: its members were
                # marked failed, so a new attempt must rebuild from scratch.
                await asyncio.to_thread(self.repository.fail_batch, observed['batch_id'],
                                        'Document producer interrupted before durable seal')
                checkpoint.pop('publication_batch_id')
                checkpoint.pop('index_publication', None)
                checkpoint['index_dispatched'] = False
            else:
                raise ProcessingError('Document publication batch failed')
        if checkpoint.get('index_dispatched'):
            # Existing queue completion cannot prove the complete publication
            # barrier. Keep the checkpoint without repeatedly generating vectors.
            return None
        uri = checkpoint.get('root_uri') if job['kind'] == 'D1' else job['payload']['uri']
        await self.service.viking_fs._ensure_access(uri, ctx, action=AclAction.WRITE)
        await lease.assert_active()
        from openviking.indexing.batch import bind_text_batch
        # Resource producers currently store their generations in resources.
        # This differs from the registered user namespace; joins enforce the
        # actual publication identity instead of silently mixing either scope.
        namespace = 'resources'
        batch_id = await asyncio.to_thread(self.repository.begin_batch, job['account'], namespace)
        checkpoint.update(root_uri=uri, publication_batch_id=batch_id,
                          publication_namespace=namespace)
        await lease.checkpoint('index', checkpoint)
        batch = _DocumentPublicationBatch(self.repository, batch_id, job['account'], namespace)
        try:
            with bind_text_batch(batch):
                result = await self.reindex.execute(
                    uri=uri, mode=job['payload'].get('mode', 'semantic_and_vectors'),
                    wait=True, recursive=job['payload'].get('recursive', True),
                    tags=job['payload'].get('tags'), tag_mode=job['payload'].get('tag_mode', 'replace'),
                    ctx=ctx,
                )
        except BaseException:
            # Cancellation leaves no producer-open batch eligible to publish.
            await asyncio.shield(asyncio.to_thread(self.repository.fail_batch, batch_id,
                                                   'Document reindex producer did not complete'))
            raise
        # Reindex reports partial embedding failures in counters even when its
        # request completed. Persist the bounded observation before deciding the
        # next state; completion of a request is not proof of joint publication.
        keys = ('status', 'uri', 'mode', 'object_type', 'scanned_records',
                'rebuilt_records', 'failed_records', 'unsupported_records',
                'semantics_required_records', 'duration_ms', 'code')
        summary = {key: result[key] for key in keys if key in result}
        warnings = result.get('warnings') or []
        summary['warnings'] = [str(warning)[:1024] for warning in warnings[:20]]
        checkpoint.update(root_uri=uri, index_result=summary, index_dispatched=False)
        await lease.checkpoint('index', checkpoint)
        if (result.get('status') not in {'success', 'completed'}
                or result.get('failed_records', 0)
                or result.get('semantics_required_records', 0)
                or result.get('uri', uri) != uri):
            await asyncio.to_thread(self.repository.fail_batch, batch_id, 'Document indexing reported failures')
            raise ProcessingError('Resource indexing did not complete without failures; see index_result')
        # Revalidate the original creator after the potentially long model work.
        try:
            current = await self.resolve_context(job)
            ctx.role, ctx.group_ids = current.role, current.group_ids
            await self.service.viking_fs._ensure_access(uri, ctx, action=AclAction.WRITE)
            await lease.assert_active()
            await asyncio.to_thread(self.repository.job_publication_seal,
                                    job['job_id'], lease.owner, lease.fence)
        except BaseException:
            await asyncio.shield(asyncio.to_thread(self.repository.fail_batch, batch_id,
                                                   'Document authorization or final seal failed'))
            raise
        observed = await asyncio.to_thread(self.repository.job_publication_status,
                                           job['job_id'], lease.owner, lease.fence)
        if observed['state'] == 'failed':
            raise ProcessingError('Document publication batch failed its actual member gates')
        gate = await _document_publication_gate(self.repository, job['job_id'],
                                                lease.owner, lease.fence)
        checkpoint.update(index_dispatched=True, publication_gate=gate)
        checkpoint['index_publication'] = observed
        if gate['verified']:
            checkpoint['publication_gate'] = dict(gate, code='PUBLICATION_VERIFIED')
            await lease.checkpoint('index', checkpoint)
            return {'state': 'ready'}
        await lease.checkpoint('index', checkpoint, {'code': 'PUBLICATION_GATE_PENDING'})
        return None

    async def run(self):
        while True:
            try:
                result = await self.executor.run_once()
                if result is None:
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Durable KB worker paused after an execution error')
                await asyncio.sleep(1)


def start_document_job_worker(app, service, server_config):
    repository = configured_store()
    identities = getattr(app.state, 'api_key_manager', None)
    if repository is None or identities is None:
        return None
    runtime = DocumentJobRuntime(service, repository, identities, TempUploadStore(server_config))
    app.state.kb_job_runtime = runtime
    return asyncio.create_task(runtime.run(), name='openviking-kb-document-jobs')


async def stop_document_job_worker(task):
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
