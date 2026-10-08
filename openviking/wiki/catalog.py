# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Account-scoped collection/schema persistence using existing AGFS revisions.

This is not a namespace/ACL registry or a Wiki builder. Registered schemas remain
structural/draft capabilities; publication still needs all domain/graph gates.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import re

from openviking.indexing.content import archive_bytes, blob_path
from openviking.indexing.publication import configured_store
from openviking.server.identity import Role
from openviking.storage.acl import AclAction
from openviking.wiki.schema import SchemaBundle, SchemaError, compile_bundle
from openviking.wiki.validation import source_uri
from openviking_cli.exceptions import ConflictError, NotFoundError, PermissionDeniedError, FailedPreconditionError


def _key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.+\-]{1,128}', value):
        raise SchemaError('Invalid catalog identifier')
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


@dataclass(frozen=True)
class CollectionBinding:
    collection_id: str
    namespace_id: str
    target_uri: str
    raw_sources_prefix: str
    schema_id: str
    schema_version: str
    schema_digest: str


@dataclass(frozen=True)
class PinnedSchema:
    collection: CollectionBinding
    bundle: SchemaBundle
    binding_digest: str
    capability_state: str


class WikiCatalog:
    def __init__(self, fs, *, registry=None):
        self.fs = fs
        self.registry = registry if registry is not None else configured_store()
        if self.registry is None:
            raise SchemaError('Wiki catalog requires the shared durable registry')

    @staticmethod
    def _admin(ctx):
        if ctx.bypass_acl or ctx.role not in {Role.ROOT, Role.ADMIN}:
            raise PermissionDeniedError('Account administrator required to register Wiki schema/collections')

    def _schema_row(self, account, schema_id, version):
        return self.registry.wiki_schema_get(account, schema_id, version)

    async def describe_schema(self, ctx, schema_id, version):
        self._admin(ctx)
        _key(schema_id); _key(version)
        bundle, state = await self._read_schema(ctx, schema_id, version)
        return {"schema_id":schema_id,"version":version,"digest":bundle.digest,"capability_state":state}

    async def _read_schema(self, ctx, schema_id, version):
        row = self._schema_row(ctx.account_id, schema_id, version)
        reference = json.loads(row['reference'])
        raw = self.fs._handle_agfs_read(await self.fs._async_agfs.read(blob_path(self.fs,ctx,reference['sha256'])))
        if len(raw)!=reference['size'] or hashlib.sha256(raw).hexdigest()!=reference['sha256']:
            raise SchemaError('Stored schema package is missing or corrupted')
        payload = json.loads(raw)
        bundle = SchemaBundle.freeze(payload['schema_id'], payload['version'], dict(payload['members']))
        if bundle.digest != row['digest'] or (bundle.schema_id, bundle.version) != (schema_id, version):
            raise SchemaError('Stored schema does not match catalog identity')
        declarations = compile_bundle(bundle)
        state = 'draft_rules_pending' if any(e.uncompiled_rules for e in declarations) else 'structural_ready'
        # Compiler coverage changes must not leave an old optimistic state active.
        self.registry.wiki_schema_refresh(ctx.account_id, schema_id, version, bundle.digest, state)
        return bundle, state

    async def register_schema(self, ctx, bundle):
        self._admin(ctx)
        _key(bundle.schema_id); _key(bundle.version)
        # Recompute a dataclass supplied by a caller; never trust its digest.
        frozen = SchemaBundle.freeze(bundle.schema_id, bundle.version, dict(bundle.members))
        if frozen != bundle:
            raise SchemaError('Schema bundle digest or members do not match')
        declarations = compile_bundle(bundle)
        state = 'draft_rules_pending' if any(e.uncompiled_rules for e in declarations) else 'structural_ready'
        # Check immutable identity before writing a new private blob.
        self.registry.wiki_schema_check(ctx.account_id, bundle.schema_id, bundle.version, bundle.digest)
        raw = _json({'schema_id':bundle.schema_id,'version':bundle.version,'members':bundle.members}).encode()
        await self.fs._ensure_parent_dirs(blob_path(self.fs,ctx,hashlib.sha256(raw).hexdigest()),ctx=ctx)
        reference = await archive_bytes(self.fs,raw,ctx)
        self.registry.wiki_schema_register(ctx.account_id, bundle.schema_id, bundle.version, bundle.digest, _json(reference), state)
        return {'schema_id':bundle.schema_id,'version':bundle.version,'digest':bundle.digest,'capability_state':state}

    async def register_collection(self,ctx,*,collection_id,namespace_id,target_uri,raw_sources_prefix,schema_id,schema_version):
        self._admin(ctx)
        for key in (collection_id,namespace_id,schema_id,schema_version):
            _key(key)
        target_uri,raw_sources_prefix=source_uri(target_uri),source_uri(raw_sources_prefix)
        def overlaps(a,b):
            return a==b or a.startswith(b+'/') or b.startswith(a+'/')
        if overlaps(target_uri,raw_sources_prefix):
            raise SchemaError('Wiki and raw source roots must not overlap')
        for uri in (target_uri,raw_sources_prefix):
            await self.fs._ensure_access(uri,ctx,action=AclAction.WRITE)
            try:
                existing = await self.fs.stat(uri, ctx=ctx, skip_count=True)
            except NotFoundError:
                pass  # A later ingestion may create this authorized prefix.
            else:
                if not existing.get("isDir", False):
                    raise SchemaError("Collection prefixes must be directories")
        row=self._schema_row(ctx.account_id,schema_id,schema_version)
        binding=CollectionBinding(collection_id,namespace_id,target_uri,raw_sources_prefix,schema_id,schema_version,row['digest'])
        await self._domains(ctx, binding, action=AclAction.MANAGE)
        self.registry.wiki_collection_register(ctx.account_id, asdict(binding))
        return binding

    async def get_collection(self,ctx,collection_id):
        _key(collection_id)
        if ctx.bypass_acl:
            raise PermissionDeniedError('Original authorized request context required')
        binding = CollectionBinding(**self.registry.wiki_collection_get(ctx.account_id, collection_id))
        self.registry.wiki_collection_domain_check(ctx.account_id, collection_id)
        await self._domains(ctx, binding, action=AclAction.READ)
        await self.fs._ensure_access(binding.target_uri,ctx)
        return binding

    async def _domains(self, ctx, binding, *, action):
        from openviking.wiki.namespaces import NamespaceCatalog
        domains = NamespaceCatalog(self.fs, registry=self.registry)
        try:
            target = await domains.get(ctx, binding.namespace_id, action=action)
            raw = await domains.lookup(ctx, binding.raw_sources_prefix, action=action)
        except NotFoundError as exc:
            raise FailedPreconditionError('Collection requires registered target and independent raw source domains') from exc
        if not domains._inside(target.root_uri, binding.target_uri) or target.namespace_id == raw.namespace_id:
            raise FailedPreconditionError('Collection target/raw prefixes must belong to independent registered domains')
        return target, raw

    async def inspect_collection_migration(self, ctx, collection_id):
        """Account-admin configuration inspection; never return source content."""
        self._admin(ctx)
        _key(collection_id)
        binding = self.registry.wiki_collection_get(ctx.account_id, collection_id)
        try:
            self.registry.wiki_collection_domain_check(ctx.account_id, collection_id)
        except FailedPreconditionError:
            state, required = 'legacy_domain_pending', True
        else:
            state, required = 'registered', False
        return {'collection': binding, 'binding_digest': hashlib.sha256(_json(binding).encode()).hexdigest(),
                'domain_state': state, 'migration_required': required}

    async def migrate_collection(self, ctx, collection_id, *, expected_binding_digest):
        """Explicit admin CAS migration of an old immutable binding; no ACL receipt."""
        self._admin(ctx)
        _key(collection_id)
        binding = CollectionBinding(**self.registry.wiki_collection_get(ctx.account_id, collection_id))
        await self._domains(ctx, binding, action=AclAction.MANAGE)
        bundle, _ = await self._read_schema(ctx, binding.schema_id, binding.schema_version)
        if bundle.digest != binding.schema_digest:
            raise SchemaError('Stored schema does not match legacy collection binding')
        self.registry.wiki_collection_migrate(ctx.account_id, collection_id, expected_binding_digest)
        return await self.get_collection(ctx, collection_id)

    async def pin_collection(self,ctx,collection_id,*,input_kind):
        if input_kind not in {'upload','uri'}:
            raise SchemaError('Wiki input kind must be upload or uri')
        binding=await self.get_collection(ctx,collection_id)
        await self.fs._ensure_access(binding.target_uri,ctx,action=AclAction.WRITE)
        if input_kind=='upload':
            await self.fs._ensure_access(binding.raw_sources_prefix,ctx,action=AclAction.WRITE)
        bundle, state = await self._read_schema(ctx, binding.schema_id, binding.schema_version)
        if bundle.digest!=binding.schema_digest:
            raise SchemaError('Stored schema does not match collection binding')
        await self.fs._ensure_access(binding.target_uri,ctx,action=AclAction.WRITE)
        if input_kind=='upload':
            await self.fs._ensure_access(binding.raw_sources_prefix,ctx,action=AclAction.WRITE)
        self.registry.wiki_collection_domain_check(ctx.account_id, collection_id)
        await self._domains(ctx, binding, action=AclAction.READ)
        return PinnedSchema(binding,bundle,hashlib.sha256(_json(asdict(binding)).encode()).hexdigest(),state)

    async def register_entities(self,ctx,namespace_id,entries):
        """Persist canonical identities produced by the identity compiler; no synthesis here."""
        self._admin(ctx)
        _key(namespace_id)
        if not isinstance(entries,list) or not entries:
            raise SchemaError('Entity registration requires a non-empty entry list')
        result=self.registry.wiki_entity_register(ctx.account_id,namespace_id,entries)
        from openviking.wiki.namespaces import NamespaceCatalog
        domains=NamespaceCatalog(self.fs,registry=self.registry)
        try:
            await domains.get(ctx,namespace_id,action=AclAction.READ)
        except NotFoundError as exc:
            raise FailedPreconditionError('Entity catalog requires a registered knowledge namespace') from exc
        return result

    async def get_entity(self,ctx,namespace_id,canonical_id):
        self._admin(ctx)
        _key(namespace_id)
        if not isinstance(canonical_id,str) or not re.fullmatch(r'candidate-[0-9a-f]{64}',canonical_id):
            raise SchemaError('Invalid canonical entity identity')
        row=self.registry.wiki_entity_get(ctx.account_id,namespace_id,canonical_id)
        return {**row,'aliases':json.loads(row['aliases'])}

    async def list_entities(self,ctx,namespace_id):
        self._admin(ctx)
        _key(namespace_id)
        rows=self.registry.wiki_entity_list(ctx.account_id,namespace_id)
        for row in rows:
            row['aliases']=json.loads(row['aliases'])
        return rows
