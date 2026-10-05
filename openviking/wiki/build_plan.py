# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Durable, shared Wiki input preparation after upload materialization or URI selection.

A prepared plan is not a queued/ready job. Namespace/export authorization,
extraction, domain validation and publication must still run before execution.
Only derived Markdown is archived; source citations keep their original URI.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path

from openviking.indexing.content import archive_bytes, blob_path, draft_sources
from openviking.storage.acl import AclAction
from openviking.wiki.catalog import WikiCatalog, _json, _key
from openviking.wiki.schema import SchemaError
from openviking.wiki.validation import source_uri
from openviking_cli.exceptions import ConflictError, FailedPreconditionError, NotFoundError, PermissionDeniedError


@dataclass(frozen=True)
class CaptureLimits:
    documents: int = 2048
    entries: int = 16384
    depth: int = 64
    document_bytes: int = 8 * 1024 * 1024
    total_bytes: int = 64 * 1024 * 1024

    def __post_init__(self):
        if any(type(value) is not int or value < 1 for value in asdict(self).values()):
            raise ValueError('Wiki capture limits must be positive integers')


@dataclass(frozen=True)
class CapturedSource:
    uri: str
    sha256: str
    size: int
    epoch: int


@dataclass(frozen=True)
class WikiBuildPlan:
    build_id: str
    digest: str
    manifest: str
    state: str = 'prepared'

    @property
    def sources(self):
        return tuple(CapturedSource(**item) for item in json.loads(self.manifest)['sources'])


class WikiBuildPlanner:
    def __init__(self, fs, *, registry=None, limits=None):
        self.catalog = WikiCatalog(fs, registry=registry)
        self.fs, self.registry = fs, self.catalog.registry
        self.limits = limits or CaptureLimits()

    @staticmethod
    def _context(ctx):
        if ctx.bypass_acl:
            raise PermissionDeniedError('Original authorized request context required')

    @staticmethod
    def _contains(root, uri):
        return uri == root or uri.startswith(root + '/')

    async def _enumerate(self, ctx, roots, recursive, target):
        """Enumerate authorized MD files; compare a second walk before saving.

        This catches observed membership changes, but is not a directory mutation
        transaction. Final publication must revalidate membership under a fence.
        """
        files, directories = set(), {}
        remaining = self.limits.entries
        pending = [(uri, 0) for uri in reversed(roots)]
        while pending:
            uri, depth = pending.pop()
            if depth > self.limits.depth:
                raise SchemaError('Wiki source directory depth limit exceeded')
            if self._contains(target, uri) or self._contains(uri, target):
                raise SchemaError('Wiki inputs must not include their output tree')
            await self.fs._ensure_access(uri, ctx)
            epoch = self.registry.observe_draft(ctx.account_id, uri)
            with draft_sources():
                item = await self.fs.stat(uri, ctx=ctx, skip_count=True)
            if not item.get('isDir', False):
                if uri.rsplit('/', 1)[-1].startswith('.') or not uri.lower().endswith('.md'):
                    raise SchemaError('Wiki source files must be Markdown; parse binary inputs first')
                if item.get('size', 0) > self.limits.document_bytes:
                    raise SchemaError('Wiki source document size limit exceeded')
                files.add(uri)
                if len(files) > self.limits.documents:
                    raise SchemaError('Wiki source document count limit exceeded')
                continue
            if uri in directories:
                continue
            directories[uri] = epoch
            with draft_sources():
                # Fetch at most budget+1 rather than silently truncating a tree.
                entries = await self.fs.ls(uri, node_limit=remaining + 1, sort_by='name', ctx=ctx)
            remaining -= len(entries)
            if remaining < 0:
                raise SchemaError('Wiki source entry count limit exceeded')
            for entry in reversed(entries):
                name = entry.get('name')
                if not isinstance(name, str) or not name or '/' in name or '\\' in name:
                    raise SchemaError('Invalid Wiki directory entry')
                if name.startswith('.'):
                    continue
                child = source_uri(uri + '/' + name)
                if entry.get('uri') != child:
                    raise SchemaError('Wiki listing entry is outside its source directory')
                if entry.get('isDir', False) and not recursive:
                    continue
                if not entry.get('isDir', False) and not name.lower().endswith('.md'):
                    continue  # Images/control metadata are not independent text knowledge.
                if entry.get('access') == 'denied':
                    raise PermissionDeniedError('Wiki source contains unreadable Markdown')
                pending.append((child, depth + 1))
        if not files:
            raise SchemaError('Wiki source contains no Markdown documents')
        return tuple(sorted(files)), tuple(sorted(directories.items()))

    async def prepare_sources(self, ctx, collection_id, source_uris, *, input_kind,
                              idempotency_key, recursive=True, upload_source=None):
        """The common stage for W1 materialized sources and W2 existing sources.

        W1 callers must finish parsing/writing under the collection raw prefix
        first. This method itself does not upload, parse or schedule a model.
        """
        self._context(ctx)
        _key(idempotency_key)
        if not isinstance(source_uris, (list, tuple)) or not source_uris or len(source_uris) > self.limits.documents:
            raise SchemaError('A bounded nonempty list of Wiki source URIs is required')
        if type(recursive) is not bool:
            raise SchemaError('recursive must be boolean')
        roots = tuple(sorted({source_uri(uri) for uri in source_uris}))
        pin = await self.catalog.pin_collection(ctx, collection_id, input_kind=input_kind)
        if input_kind == 'upload' and any(not self._contains(pin.collection.raw_sources_prefix, uri) for uri in roots):
            raise SchemaError('Uploaded Wiki sources must be materialized under collection raw_sources_prefix')
        files, directories = await self._enumerate(ctx, roots, recursive, pin.collection.target_uri)
        captured, total = [], 0
        for uri in files:
            await self.fs._ensure_access(uri, ctx)
            before = self.registry.observe_draft(ctx.account_id, uri)
            with draft_sources():
                raw = await self.fs.read_file_bytes(uri, ctx=ctx)
            total += len(raw)
            if len(raw) > self.limits.document_bytes or total > self.limits.total_bytes:
                raise SchemaError('Wiki source content size limit exceeded')
            try:
                text = raw.decode('utf-8')
            except UnicodeDecodeError as exc:
                raise SchemaError('Wiki source is not UTF-8 Markdown') from exc
            if not text.strip() or '\x00' in text:
                raise SchemaError('Wiki source is empty or binary Markdown')
            await self.fs._ensure_access(uri, ctx)
            if self.registry.observe_draft(ctx.account_id, uri) != before:
                raise ConflictError('Wiki source changed while being captured')
            await self.fs._ensure_parent_dirs(blob_path(self.fs, ctx, hashlib.sha256(raw).hexdigest()), ctx=ctx)
            ref = await archive_bytes(self.fs, raw, ctx)
            captured.append(CapturedSource(uri, ref['sha256'], ref['size'], before))
        if await self._enumerate(ctx, roots, recursive, pin.collection.target_uri) != (files, directories):
            raise ConflictError('Wiki source membership changed while being captured')
        for uri in files:
            await self.fs._ensure_access(uri, ctx)
        await self.fs._ensure_access(pin.collection.target_uri, ctx, action=AclAction.WRITE)
        if input_kind == 'upload':
            await self.fs._ensure_access(pin.collection.raw_sources_prefix, ctx, action=AclAction.WRITE)
        return self._persist_plan(ctx, pin, input_kind, roots, recursive,
                                  captured, directories, idempotency_key, upload_source=upload_source)

    async def prepare_upload(self, ctx, collection_id, filename, content, *, update_policy, idempotency_key):
        """W1 stage: materialize one uploaded Markdown under the collection raw prefix.

        The content must already be Markdown (uploaded ``.md`` or a parser product
        that W1 writes before this stage). Binary PDF/Office conversion is not done
        here; the file is stored at ``raw_sources_prefix/<name>/<name>`` so relative
        image siblings produced by a parser stay addressable, then the shared
        :meth:`prepare_sources` captures it. An identical byte-for-byte source always
        replays; different bytes at an existing target require ``update_policy='replace'``.
        """
        self._context(ctx)
        _key(idempotency_key)
        if update_policy not in {'fail', 'replace'}:
            raise SchemaError('Wiki update_policy must be fail or replace')
        if not isinstance(filename, str) or not filename or filename != Path(filename).name \
                or filename in {'.', '..'} or '\\' in filename or filename.startswith('.'):
            raise SchemaError('Wiki upload requires a plain file name')
        if not filename.lower().endswith('.md'):
            raise SchemaError('Wiki upload must be Markdown; parse PDF/Office inputs first')
        if not isinstance(content, (bytes, bytearray)):
            raise SchemaError('Wiki upload content must be bytes')
        content = bytes(content)
        if not content or len(content) > self.limits.document_bytes:
            raise SchemaError('Wiki upload exceeds the document size limit')
        try:
            text = content.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise SchemaError('Wiki upload is not UTF-8 Markdown') from exc
        if not text.strip() or '\x00' in text:
            raise SchemaError('Wiki upload is empty or binary Markdown')
        pin = await self.catalog.pin_collection(ctx, collection_id, input_kind='upload')
        prefix = pin.collection.raw_sources_prefix
        root = f'{prefix}/{filename}'
        uri = f'{root}/{filename}'
        await self.fs._ensure_access(root, ctx, action=AclAction.WRITE)
        await self.fs._ensure_access(uri, ctx, action=AclAction.WRITE)
        # Reject already-bound keys before changing the working source. The
        # final persistence check still handles concurrent preparation requests.
        old = self.registry.wiki_plan_find(ctx.account_id, pin.collection.namespace_id, idempotency_key)
        prior = json.loads(self._decode(old).manifest) if old else None
        if prior is not None:
            sources = prior['sources']
            if prior['input_kind'] != 'upload' or prior['binding_digest'] != pin.binding_digest \
                    or prior['roots'] != [uri] or len(sources) != 1 \
                    or sources[0]['uri'] != uri or sources[0]['size'] != len(content) \
                    or sources[0]['sha256'] != hashlib.sha256(content).hexdigest():
                raise ConflictError('Wiki idempotency key already names different input revisions or parameters')
        try:
            existing = await self.fs.stat(uri, ctx=ctx, skip_count=True)
        except NotFoundError:
            existing = None
        if existing is not None:
            if existing.get('isDir', False):
                raise ConflictError('Wiki upload target already names a directory')
            with draft_sources():
                previous = await self.fs.read_file_bytes(uri, ctx=ctx)
            if previous != content and update_policy != 'replace':
                raise ConflictError('Wiki upload target exists; replace requires update_policy=replace')
        else:
            previous = None
        if prior is not None and previous != content:
            raise ConflictError('Wiki source changed after preparation; use a new idempotency key')
        # Skip the rewrite for byte-identical replays: a no-op write would still
        # advance the source epoch and make the same idempotency key look like a
        # different revision set.
        if previous != content:
            with draft_sources():
                await self.fs.write_file_bytes(uri, content, ctx=ctx)
        return await self.prepare_sources(ctx, collection_id, [uri], input_kind='upload',
                                          idempotency_key=idempotency_key)

    async def prepare_materialized_upload(self, ctx, collection_id, source_name, *, materialize,
                                          idempotency_key, source_sha256, source_size,
                                          update_policy='fail'):
        """W1 stage for PDF/Office: parse via the shared ingestion, then capture.

        ``materialize`` is an async callback ``(root_uri) -> None`` that runs the
        shared document ingestion (the same PDF/Office parser D1 uses) and
        persists the derived Markdown tree under ``root_uri``. This method does
        not parse anything itself; it only fixes the destination under the
        collection raw prefix and then reuses the same :meth:`prepare_sources`
        capture as W1 Markdown uploads and W2. The caller must still satisfy the
        registered schema/collection binding and the raw-prefix write ACL.
        """
        self._context(ctx)
        _key(idempotency_key)
        if update_policy not in {'fail', 'replace'}:
            raise SchemaError('Wiki update_policy must be fail or replace')
        if (not isinstance(source_sha256, str) or len(source_sha256) != 64
                or any(c not in '0123456789abcdef' for c in source_sha256)
                or type(source_size) is not int or source_size <= 0):
            raise SchemaError('Wiki document upload requires a valid source hash and size')
        if not isinstance(source_name, str) or not source_name \
                or source_name != Path(source_name).name \
                or source_name in {'.', '..'} or '\\' in source_name or source_name.startswith('.'):
            raise SchemaError('Wiki upload requires a plain file name')
        pin = await self.catalog.pin_collection(ctx, collection_id, input_kind='upload')
        root = f'{pin.collection.raw_sources_prefix}/{Path(source_name).stem}'
        await self.fs._ensure_access(root, ctx, action=AclAction.WRITE)
        identity = {'filename': source_name, 'sha256': source_sha256, 'size': source_size}
        old = self.registry.wiki_plan_find(ctx.account_id, pin.collection.namespace_id, idempotency_key)
        if old:
            prior = self._decode(old)
            body = json.loads(prior.manifest)
            if (body.get('upload_source') != identity or body['input_kind'] != 'upload'
                    or body['binding_digest'] != pin.binding_digest or body['roots'] != [root]):
                raise ConflictError('Wiki idempotency key already names different input revisions or parameters')
            # Recheck all captured bytes/members/ACLs/epochs without parsing or
            # rewriting: even byte-identical writes invalidate the fixed plan.
            return await self.check_current_inputs(ctx, prior.build_id)
        if await self.fs.exists(root, ctx=ctx):
            if update_policy == 'fail':
                raise ConflictError('Wiki upload target exists; replace requires update_policy=replace')
            # The shared parser currently overlays an existing tree. That can
            # retain chapters/images absent from the new upload; destructive
            # pre-delete would also lose the old source on parser failure.
            raise FailedPreconditionError(
                'WIKI_REPLACEMENT_GATE_PENDING: document replacement requires a staged whole-tree commit; '
                'use an unchanged prepared plan or a new source filename')
        await materialize(root)
        return await self.prepare_sources(ctx, collection_id, [root], input_kind='upload',
                                          idempotency_key=idempotency_key, upload_source=identity)

    def _persist_plan(self, ctx, pin, input_kind, roots, recursive, captured, directories, idempotency_key,
                      *, upload_source=None):
        """Freeze the captured revision set and persist one immutable plan."""
        body = {
            'version': 1, 'collection': asdict(pin.collection), 'binding_digest': pin.binding_digest,
            'input_kind': input_kind, 'roots': roots, 'recursive': recursive,
            'sources': [asdict(source) for source in captured], 'directories': directories,
            'schema_capability_state': pin.capability_state,
            'authorization_state': 'namespace_export_pending',
        }
        if upload_source is not None:
            body['upload_source'] = upload_source
        manifest = _json(body)
        row = self.registry.wiki_plan_persist(ctx.account_id, pin.collection.namespace_id, idempotency_key, manifest)
        return self._decode(row)

    @staticmethod
    def _decode(row):
        if hashlib.sha256(row['manifest'].encode()).hexdigest() != row['digest']:
            raise SchemaError('Stored Wiki build plan is corrupted')
        return WikiBuildPlan(row['id'], row['digest'], row['manifest'], row['state'])

    async def get(self, ctx, build_id):
        self._context(ctx)
        _key(build_id)
        plan = self._decode(self.registry.wiki_plan_get(ctx.account_id, build_id))
        body = json.loads(plan.manifest)
        pin = await self.catalog.pin_collection(ctx, body['collection']['collection_id'], input_kind=body['input_kind'])
        if pin.binding_digest != body['binding_digest']:
            raise ConflictError('Stored Wiki plan collection binding changed')
        for source in plan.sources:
            await self.fs._ensure_access(source.uri, ctx)
        return plan

    async def read_source(self, ctx, build_id, uri):
        """Read the immutable captured revision after current source ACL checks."""
        plan = await self.get(ctx, build_id)
        uri = source_uri(uri)
        source = next((source for source in plan.sources if source.uri == uri), None)
        if source is None:
            raise NotFoundError(uri, 'Wiki plan source')
        return await self._read_captured(ctx, source, json.loads(plan.manifest)['collection']['target_uri'])

    async def _read_captured(self, ctx, source, target):
        await self.fs._ensure_access(source.uri, ctx)
        raw = self.fs._handle_agfs_read(await self.fs._async_agfs.read(blob_path(self.fs, ctx, source.sha256)))
        if len(raw) != source.size or hashlib.sha256(raw).hexdigest() != source.sha256:
            raise SchemaError('Captured Wiki source is missing or corrupted')
        await self.fs._ensure_access(source.uri, ctx)
        await self.fs._ensure_access(target, ctx, action=AclAction.WRITE)
        return raw.decode('utf-8')

    async def iter_source_texts(self, ctx, build_id):
        """Stream a fixed plan once, checking each source before and after reading.

        Workers must not reload/check all N sources for every document. No whole
        corpus text is retained here; only the current captured body is yielded.
        """
        plan = await self.get(ctx, build_id)
        target = json.loads(plan.manifest)['collection']['target_uri']
        for source in plan.sources:
            yield source.uri, await self._read_captured(ctx, source, target)

    async def check_current_inputs(self, ctx, build_id):
        """Preflight current membership, bytes, ACLs and standard write epochs.

        This does not authorize export, promote state or atomically publish. A
        future publisher must repeat dependency/ACL checks in its commit fence.
        """
        plan = await self.get(ctx, build_id)
        body = json.loads(plan.manifest)
        expected = (tuple(source.uri for source in plan.sources),
                    tuple(tuple(item) for item in body['directories']))
        roots, target = body['roots'], body['collection']['target_uri']
        if await self._enumerate(ctx, roots, body['recursive'], target) != expected:
            raise ConflictError('Wiki source membership changed since preparation')
        for source in plan.sources:
            with draft_sources():
                raw = await self.fs.read_file_bytes(source.uri, ctx=ctx)
            if len(raw) != source.size or hashlib.sha256(raw).hexdigest() != source.sha256:
                raise ConflictError('Wiki source content changed since preparation')
            await self.fs._ensure_access(source.uri, ctx)
        if await self._enumerate(ctx, roots, body['recursive'], target) != expected:
            raise ConflictError('Wiki source membership changed during preflight')
        await self.fs._ensure_access(target, ctx, action=AclAction.WRITE)
        self.registry.wiki_plan_check_epochs(ctx.account_id, body)
        return plan

    async def authorize_inputs(self, ctx, build_id):
        """Require registered domains and explicit export before Wiki source use.

        Prepared legacy plans remain inspectable. This execution precheck never
        interprets their namespace_export_pending marker as permission.
        """
        from openviking.wiki.namespaces import NamespaceCatalog
        plan = await self.check_current_inputs(ctx, build_id)
        binding = json.loads(plan.manifest)['collection']
        return await NamespaceCatalog(self.fs, registry=self.registry).authorize_sources(
            ctx, target_namespace_id=binding['namespace_id'], target_uri=binding['target_uri'],
            source_uris=[source.uri for source in plan.sources])

    async def authorized_source_texts(self, ctx, build_id):
        """Execution input stream: domains/export plus current ACLs and fixed text.

        This does not execute schema rules/models or provide a commit fence.
        """
        from openviking.wiki.namespaces import NamespaceCatalog
        plan = await self.get(ctx, build_id)
        target = json.loads(plan.manifest)['collection']['target_uri']
        policy = NamespaceCatalog(self.fs, registry=self.registry)
        ticket = await self.authorize_inputs(ctx, build_id)
        for source in plan.sources:
            await policy.check_source_use(ctx, ticket, uri=source.uri, target_uri=target)
            text = await self._read_captured(ctx, source, target)
            await policy.check_source_use(ctx, ticket, uri=source.uri, target_uri=target)
            yield source.uri, text
        await policy.recheck(ctx, ticket, target_uri=target)
