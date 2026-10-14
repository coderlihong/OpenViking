# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Run durable document jobs through the existing resource and reindex services."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import asdict
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
    def __init__(self, service, repository, identity_manager, uploads, *, reindex=None,
                 wiki_limits=None):
        self.service, self.repository = service, repository
        self.identities, self.uploads = identity_manager, uploads
        self.wiki_limits = wiki_limits
        # W1/W2 authorize the plan's full source list; the bound must match the
        # capture side so a real handbook tree survives submission and execution.
        # wiki_limits arrives as the pydantic config section or a CaptureLimits.
        documents = getattr(wiki_limits, 'documents', None) if wiki_limits is not None else None
        self.jobs = KBJobService(service.viking_fs, repository,
                                 **({'max_source_uris': documents} if documents is not None else {}))
        self.reindex = reindex or get_reindex_executor()
        self.executor = JobExecutor(repository, {'D1': self.ingest, 'D2': self.index,
            'W1': self.extract_wiki, 'W2': self.extract_wiki}, self.resolve_context,
            # The store's BEGIN IMMEDIATE busy timeout is 30s, so one contended
            # renewal can legally block for the whole old 30s lease. 120s keeps
            # three renewal ticks of headroom under lock storms.
            lease_seconds=120)

    async def extract_wiki(self, job, ctx, lease):
        from dataclasses import asdict
        from openviking.wiki.build_plan import WikiBuildPlanner
        from openviking.wiki.extraction import ExtractionLimits, WikiDraftExtractor
        from openviking.wiki.policies import EXTRACTOR_VERSION, POLICY_VERSION
        planner = WikiBuildPlanner(self.service.viking_fs, registry=self.repository,
                                   limits=self.wiki_limits)
        extraction = getattr(self.wiki_limits, 'extraction', None)
        if extraction is not None:
            fields = extraction.model_dump() if hasattr(extraction, 'model_dump') else asdict(extraction)
            extractor = WikiDraftExtractor(planner, limits=ExtractionLimits(**fields))
        else:
            extractor = WikiDraftExtractor(planner)
        _, model_profile = extractor._model()
        configuration = job['effective_config']
        if (configuration.get('profile') != 'wiki_draft'
                or configuration.get('extractor_version') != EXTRACTOR_VERSION
                or configuration.get('policy_version') != POLICY_VERSION
                or configuration.get('model_profile') != model_profile
                or configuration.get('limits') != asdict(extractor.limits)):
            raise ConflictError('Wiki extraction configuration changed since job submission')
        prior = job.get('checkpoint') or {}
        prior_receipt = prior.get('draft_receipt')
        prior_config = (prior_receipt or {}).get('configuration') or {}
        resume_complete = (isinstance(prior_receipt, dict)
                           and prior_receipt.get('state') == 'draft_ready'
                           and prior_receipt.get('private') is True
                           and prior_receipt.get('diagnostic_publishable') is False
                           and prior_receipt.get('build_id') == job['payload']['build_id']
                           and prior_receipt.get('plan_digest') == job['payload']['plan_digest']
                           and prior_config.get('extractor_version') == EXTRACTOR_VERSION
                           and prior_config.get('policy_version') == POLICY_VERSION
                           and prior_config.get('model_profile') == model_profile
                           and prior_config.get('limits') == asdict(extractor.limits))
        async def refresh():
            # Identity liveness + current role/groups only. This must NOT call
            # resolve_context: that re-runs _authorize -> authorize_sources, a
            # full ACL scroll over every plan source. In the receipt replay loop
            # below that is O(sources) per receipt = quadratic, and it saturated
            # the event loop (starving pathlock -> reindex CONFLICT). The batch
            # is bracketed by authorize_inputs before and policy.recheck after,
            # so per-receipt full re-authorization is redundant.
            ctx.role, ctx.group_ids = await self._current_identity(job)
        async def checkpoint(progress):
            await refresh()
            await lease.checkpoint('wiki_extract', {'extraction_progress': progress})
        if resume_complete:
            # A prior attempt already produced a complete terminal receipt but the
            # worker died (or the durable gate rejected it) before commit. Every
            # candidate blob is content-addressed and durable, so re-authorize and
            # re-commit that exact receipt in the replay loop below instead of
            # re-running thousands of model calls from scratch.
            receipt = prior_receipt
        else:
            await lease.checkpoint('wiki_extract', prior)
            receipt = await extractor.extract(ctx, job['payload']['build_id'],
                expected_plan_digest=job['payload']['plan_digest'],
                profile=configuration['extraction_profile'], checkpoint=checkpoint,
                resume=prior.get('extraction_progress'))
        # Read every server-written blob again under all current contributor
        # permissions before recording a terminal private candidate result.
        # Authorization is bracketed once around the batch, not per receipt:
        # each full-plan authorization scan is O(sources), so per-receipt
        # re-authorization is quadratic and ground a 2k-source run for hours.
        # Nothing terminal is recorded until the closing recheck passes.
        from openviking.wiki.namespaces import NamespaceCatalog
        await refresh()
        ticket = await planner.authorize_inputs(ctx, job['payload']['build_id'])
        policy = NamespaceCatalog(self.service.viking_fs, registry=self.repository,
                                  max_source_uris=getattr(self.wiki_limits, 'documents', None)
                                  or planner.limits.documents)
        # Fetch the fixed plan once for the whole replay batch. planner.get
        # re-reads the plan row and re-runs _ensure_access for every source, so
        # calling it per receipt made the terminal re-authorization quadratic at
        # real scale; the batch is already bracketed by authorize_inputs above
        # and policy.recheck below.
        plan = await planner.get(ctx, job['payload']['build_id'])
        binding = plan.body['collection']
        total = len(receipt['receipts'])
        for index, draft in enumerate(receipt['receipts']):
            await refresh()
            policy.check_policy_revision(ctx, ticket)
            await extractor.read_draft_payload(ctx, job['payload']['build_id'],
                expected_plan_digest=job['payload']['plan_digest'], receipt=draft, plan=plan)
            if (index + 1) % 256 == 0 or index + 1 == total:
                await lease.checkpoint('wiki_extract',
                                       {'extraction_progress': job.get('checkpoint', {}).get('extraction_progress') or {},
                                        'receipt_replay_progress': {'done': index + 1, 'total': total}})
        await refresh()
        policy.check_policy_revision(ctx, ticket)
        await policy.recheck(ctx, ticket, target_uri=binding['target_uri'])
        await lease.checkpoint('wiki_validate', {'draft_receipt': receipt,
                                               'diagnostic_published': False})
        return {'state': 'draft_ready', 'receipt': receipt}

    async def _current_identity(self, job):
        """Cheap identity liveness + current role/groups, WITHOUT authorization.

        Returns ``(role, group_ids)``. Deliberately does not call _authorize:
        that path re-runs authorize_sources (a full ACL scroll over every plan
        source) which is far too expensive to repeat per receipt/checkpoint.
        Callers that need a fresh authorization use resolve_context.
        """
        if self.identities is None:
            raise PermissionDeniedError('Durable jobs require a current identity registry')
        account, principal = job['account'], job['principal']
        await self.identities.refresh_identity_registry_if_changed(account)
        await self.identities.ensure_account_groups_loaded(account)
        if not self.identities.has_user(account, principal):
            raise PermissionDeniedError('Job creator is no longer an active user')
        return (Role(self.identities.get_user_role(account, principal)),
                tuple(self.identities.get_user_group_ids(account, principal)))

    async def resolve_context(self, job):
        account, principal = job['account'], job['principal']
        role, group_ids = await self._current_identity(job)
        ctx = RequestContext(
            user=UserIdentifier(account, principal),
            role=role,
            group_ids=group_ids,
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
                'duration_ms')
        summary = {key: result[key] for key in keys if key in result}
        warnings = result.get('warnings') or []
        summary['warnings'] = [str(warning)[:1024] for warning in warnings[:20]]
        checkpoint.update(root_uri=uri, index_result=summary, index_dispatched=False)
        await lease.checkpoint('index', checkpoint)
        if (result.get('status') not in {'success', 'completed'}
                or result.get('failed_records', 0)
                or result.get('unsupported_records', 0)
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
    runtime = DocumentJobRuntime(service, repository, identities, TempUploadStore(server_config),
                                 wiki_limits=server_config.wiki)
    app.state.kb_job_runtime = runtime
    return asyncio.create_task(runtime.run(), name='openviking-kb-document-jobs')


async def stop_document_job_worker(task):
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
