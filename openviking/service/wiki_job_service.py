"""Submit fixed Wiki plans and read private drafts under current permissions."""
from __future__ import annotations

from dataclasses import asdict
import json

from openviking.service.kb_job_service import KBJobService
from openviking.storage.acl import AclAction
from openviking.wiki.extraction import WikiDraftExtractor
from openviking.wiki.policies import EXTRACTOR_VERSION, POLICY_VERSION, check_build_profile
from openviking_cli.exceptions import ConflictError, InvalidArgumentError, NotFoundError


class WikiJobService:
    def __init__(self, planner, *, extractor=None):
        self.planner = planner
        self.extractor = extractor or WikiDraftExtractor(planner)
        self.jobs = KBJobService(planner.fs, planner.registry)

    async def create(self, ctx, build_id, *, expected_plan_digest, idempotency_key, profile='draft'):
        plan = await self.planner.get(ctx, build_id)
        if plan.digest != expected_plan_digest:
            raise ConflictError('Wiki execution expected plan digest does not match')
        body = json.loads(plan.manifest)
        pin = await self.planner.catalog.pin_collection(
            ctx, body['collection']['collection_id'], input_kind=body['input_kind'])
        check_build_profile(pin.bundle, profile)
        await self.planner.authorize_inputs(ctx, build_id)
        _, model_profile = self.extractor._model()
        payload = {'build_id': build_id, 'plan_digest': plan.digest,
                   'collection_id': pin.collection.collection_id,
                   'source_uris': [source.uri for source in plan.sources],
                   'to': pin.collection.target_uri}
        job = {'account': ctx.account_id, 'principal': ctx.user.user_id,
               'namespace': pin.collection.namespace_id,
               'kind': 'W1' if body['input_kind'] == 'upload' else 'W2', 'payload': payload}
        await self.jobs._authorize(ctx, job, AclAction.WRITE)
        configuration = {'profile': 'wiki_draft', 'extraction_profile': profile,
                         'extractor_version': EXTRACTOR_VERSION, 'policy_version': POLICY_VERSION,
                         'model_profile': model_profile, 'limits': asdict(self.extractor.limits)}
        try:
            return self.planner.registry.job_create(ctx.account_id, job['namespace'],
                idempotency_key, job['kind'], payload, configuration, ctx.user.user_id)
        except ValueError as exc:
            if str(exc).startswith('IDEMPOTENCY_CONFLICT:'):
                raise ConflictError(str(exc)) from exc
            raise InvalidArgumentError(str(exc)) from exc

    async def read_draft(self, ctx, job_id, index):
        job = await self.jobs.get(ctx, job_id)
        if job['kind'] not in {'W1', 'W2'} or job['state'] != 'draft_ready':
            raise NotFoundError(job_id, 'Completed private Wiki draft')
        receipt = (job.get('checkpoint') or {}).get('draft_receipt', {})
        drafts = receipt.get('receipts', [])
        if type(index) is not int or not 0 <= index < len(drafts):
            raise NotFoundError(str(index), 'Wiki draft chunk')
        return await self.extractor.read_draft(ctx, job['payload']['build_id'],
            expected_plan_digest=job['payload']['plan_digest'], receipt=drafts[index])
