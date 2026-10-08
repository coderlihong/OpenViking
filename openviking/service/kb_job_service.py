"""Current-identity authorization around durable KB job repository commands."""
from __future__ import annotations

from pathlib import PurePosixPath

from openviking.storage.acl import AclAction
from openviking.wiki.namespaces import NamespaceCatalog
from openviking.wiki.schema import SchemaError
from openviking.wiki.validation import source_uri
from openviking_cli.exceptions import ConflictError, FailedPreconditionError, InvalidArgumentError, NotFoundError, PermissionDeniedError


class KBJobService:
    def __init__(self, fs, repository, *, max_source_uris=2048):
        self.fs, self.repository = fs, repository
        self.namespaces = NamespaceCatalog(fs, registry=repository, max_source_uris=max_source_uris)

    async def _authorize(self, ctx, job, action):
        if ctx.bypass_acl or job['account'] != ctx.account_id or job['principal'] != ctx.user.user_id:
            raise PermissionDeniedError('Jobs require their original user identity')
        await self.namespaces.get(ctx, job['namespace'], action=action)
        payload = job['payload']
        sources = list(payload.get('source_uris', []))
        if payload.get('uri'):
            sources.append(payload['uri'])
        for uri in sources:
            await self.fs._ensure_access(uri, ctx, action=AclAction.READ)
        target = payload.get('to') or payload.get('parent')
        if target:
            await self.fs._ensure_access(target, ctx, action=action)
        if job.get('kind') in {'W1', 'W2'}:
            await self.namespaces.authorize_sources(ctx, target_namespace_id=job['namespace'],
                target_uri=target, source_uris=sources)

    async def _authorize_row_visibility(self, ctx, job):
        """List-path row check: ownership + namespace read only.

        Source-set re-authorization here is quadratic at scale (each W1 row
        replays ~2000+ per-URI ACL/stat walks over the whole collection) and
        redundant: sources were gated when the job was created and are
        re-checked by the executor before any work runs. Listing reveals only
        the fact of a job and its namespace, which this check covers.
        """
        if ctx.bypass_acl or job['account'] != ctx.account_id or job['principal'] != ctx.user.user_id:
            raise PermissionDeniedError('Jobs require their original user identity')
        await self.namespaces.get(ctx, job['namespace'], action=AclAction.READ)

    async def create(self, ctx, *, namespace_id, kind, idempotency_key, payload, effective_config):
        if ctx.bypass_acl:
            raise PermissionDeniedError('Original user identity required')
        if kind not in {'D1', 'D2'}:
            raise FailedPreconditionError('Wiki execution is unavailable; existing Wiki entrypoints remain preparation only')
        allowed = {'mode', 'recursive', 'tags', 'tag_mode'}
        allowed.update({'temp_file_id', 'to', 'parent'} if kind == 'D1' else {'uri'})
        if set(payload) - allowed or (payload.get('to') is not None and payload.get('parent') is not None):
            raise InvalidArgumentError('Document jobs require a single input kind and one destination')
        namespace = await self.namespaces.get(ctx, namespace_id, action=AclAction.WRITE)
        payload = dict(payload)
        try:
            for key in ('uri', 'to', 'parent'):
                if payload.get(key) is not None:
                    payload[key] = source_uri(payload[key])
        except SchemaError as exc:
            raise InvalidArgumentError(str(exc)) from exc
        if kind == 'D2':
            if not isinstance(payload.get('uri'), str) or not payload['uri'].startswith('viking://'):
                raise InvalidArgumentError('D2 requires a source Viking URI')
            if not self.namespaces._inside(namespace.root_uri, payload['uri']):
                raise InvalidArgumentError('D2 source must belong to its registered namespace')
            if PurePosixPath(payload['uri']).suffix.lower() in {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx'}:
                raise InvalidArgumentError('D2 binary PDF/Office sources require document parsing; direct indexing is unsupported')
        elif kind == 'D1':
            if not isinstance(payload.get('temp_file_id'), str) or not payload['temp_file_id']:
                raise InvalidArgumentError('D1 requires a stored temp_file_id')
            if not payload.get('to') and not payload.get('parent'):
                payload['parent'] = namespace.root_uri
        target = payload.get('to') or payload.get('parent')
        if target and not self.namespaces._inside(namespace.root_uri, target):
            raise InvalidArgumentError('Job target must belong to its registered namespace root')
        prospective = {'account': ctx.account_id, 'principal': ctx.user.user_id,
                       'namespace': namespace_id, 'kind': kind, 'payload': payload}
        await self._authorize(ctx, prospective, AclAction.WRITE)
        try:
            return self.repository.job_create(ctx.account_id, namespace_id, idempotency_key,
                                              kind, payload, effective_config, ctx.user.user_id)
        except ValueError as exc:
            if str(exc).startswith('IDEMPOTENCY_CONFLICT:'):
                raise ConflictError(str(exc)) from exc
            raise InvalidArgumentError(str(exc)) from exc

    async def get(self, ctx, job_id):
        job = self.repository.job_get(ctx.account_id, job_id)
        if job is None:
            raise NotFoundError(job_id, 'KB job')
        await self._authorize(ctx, job, AclAction.READ)
        return job

    async def list(self, ctx, *, namespace_id=None, limit=100):
        if ctx.bypass_acl:
            raise PermissionDeniedError('Original user identity required for job listing')
        if namespace_id is not None:
            await self.namespaces.get(ctx, namespace_id, action=AclAction.READ)
        rows = self.repository.job_list(ctx.account_id, namespace=namespace_id,
                                        limit=limit, principal=ctx.user.user_id)
        visible = []
        for row in rows:
            try:
                await self._authorize_row_visibility(ctx, row)
            except (PermissionDeniedError, NotFoundError):
                continue
            visible.append(row)
        return visible

    async def cancel(self, ctx, job_id):
        job = await self.get(ctx, job_id)
        await self._authorize(ctx, job, AclAction.WRITE)
        return self.repository.job_cancel(ctx.account_id, job_id)

    async def resolve_parse_quarantine(self, ctx, job_id):
        job = await self.get(ctx, job_id)
        await self._authorize(ctx, job, AclAction.WRITE)
        try:
            return self.repository.job_parse_quarantine_resolve(job_id)
        except ValueError as exc:
            raise InvalidArgumentError(str(exc)) from exc
