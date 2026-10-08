# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Knowledge roots and explicit export policy over the existing resource ACL.

No separate member grants, ACL projection or vector search is introduced here.
Root ACLs must already be restricted and enabled. These checks gate Wiki input
use; global namespace prefilters, ACL mutation fencing and task identity refresh
still need integration before the whole enterprise isolation gate is complete.
"""
from dataclasses import dataclass
import hashlib

from openviking.indexing.content import draft_sources
from openviking.storage.acl import AclAction, AclMode, _current_acl_view, acl_principals, normalize_acl_principal
from openviking.wiki.catalog import WikiCatalog, _json, _key
from openviking.wiki.resolver import ReferenceBinding
from openviking.wiki.schema import SchemaError
from openviking.wiki.validation import source_uri
from openviking_cli.exceptions import ConflictError, FailedPreconditionError, NotFoundError, PermissionDeniedError


@dataclass(frozen=True)
class KnowledgeNamespace:
    namespace_id: str
    root_uri: str


@dataclass(frozen=True)
class SourceAuthorization:
    account_id: str
    user_id: str
    principals: tuple[str, ...]
    target_namespace_id: str
    source_bindings: tuple[tuple[str, KnowledgeNamespace], ...]
    policy_revision: int
    root_acl_digests: tuple[tuple[str, str], ...]


class NamespaceCatalog:
    def __init__(self, fs, *, registry=None, max_source_uris=2048):
        catalog = WikiCatalog(fs, registry=registry)
        self.fs, self.registry = fs, catalog.registry
        # Source authorization accepts the same volume the capture stage admits;
        # the static default stays for callers without a planner's limits.
        if type(max_source_uris) is not int or max_source_uris < 1:
            raise SchemaError('Namespace source URI bound must be a positive integer')
        self.max_source_uris = max_source_uris

    @staticmethod
    def _context(ctx):
        if ctx.bypass_acl:
            raise PermissionDeniedError('Original authorized request context required for namespace policy')

    @staticmethod
    def _inside(root, uri):
        return uri == root or uri.startswith(root + '/')

    def _get(self, account, namespace_id):
        _key(namespace_id)
        return KnowledgeNamespace(namespace_id, self.registry.namespace_get(account, namespace_id))

    async def _root_acl(self, ctx, namespace, action, *, require_disjoint=False):
        self._context(ctx)
        manager = self.fs.acl_manager
        if manager is None or not manager.is_enabled(ctx.account_id):
            raise FailedPreconditionError('Knowledge namespaces require enabled resource ACLs')
        # Use the same actor/ACL resolver as normal VikingFS operations.
        await self.fs._ensure_access(namespace.root_uri, ctx, action=action)
        # A namespace root must be an existing directory before anything else;
        # a plain file URI is rejected here rather than as a registry conflict.
        with draft_sources():
            info = await self.fs.stat(namespace.root_uri, ctx=ctx, skip_count=True)
        if not info.get('isDir', False):
            raise FailedPreconditionError('Knowledge namespace root must be an existing directory')
        if require_disjoint:
            self.registry.namespace_registration_check(ctx.account_id, namespace.namespace_id, namespace.root_uri)
        acl = await manager.resolve(namespace.root_uri, ctx)
        if acl.mode != AclMode.RESTRICTED:
            raise FailedPreconditionError('Knowledge namespace root must have an explicit restricted ACL')
        return hashlib.sha256(_json(acl.context_fields()).encode()).hexdigest()

    async def register(self, ctx, *, namespace_id, root_uri):
        WikiCatalog._admin(ctx)
        _key(namespace_id)
        root = KnowledgeNamespace(namespace_id, source_uri(root_uri))
        await self._root_acl(ctx, root, AclAction.MANAGE, require_disjoint=True)
        self.registry.namespace_register(ctx.account_id, namespace_id, root.root_uri)
        return root

    async def get(self, ctx, namespace_id, *, action=AclAction.READ):
        self._context(ctx)
        root = self._get(ctx.account_id, namespace_id)
        await self._root_acl(ctx, root, action)
        return root

    async def lookup(self, ctx, uri, *, action=AclAction.READ):
        self._context(ctx)
        uri = source_uri(uri)
        await self.fs._ensure_access(uri, ctx, action=action)
        matches = [KnowledgeNamespace(row['namespace'], row['root_uri'])
                   for row in self.registry.namespace_list(ctx.account_id)
                   if self._inside(row['root_uri'], uri)]
        if len(matches) != 1:
            raise FailedPreconditionError('Source does not belong to a unique registered knowledge namespace')
        await self._root_acl(ctx, matches[0], action)
        return matches[0]

    async def reference_binding(self, ctx, uri):
        root = await self.lookup(ctx, uri)
        # This binds a source namespace, never certifies an entity kind/content.
        return ReferenceBinding(root.namespace_id, root.root_uri)

    async def set_export(self, ctx, *, source_namespace, target_namespace, principal, allowed):
        WikiCatalog._admin(ctx)
        if type(allowed) is not bool:
            raise SchemaError('Export allowed must be boolean')
        principal = normalize_acl_principal(principal)
        source = await self.get(ctx, source_namespace, action=AclAction.MANAGE)
        target = await self.get(ctx, target_namespace, action=AclAction.MANAGE)
        if source.namespace_id == target.namespace_id:
            raise SchemaError('Same-namespace use does not require an export edge')
        self.registry.namespace_set_export(ctx.account_id, source_namespace, target_namespace, principal, allowed)
        return {'source_namespace': source_namespace, 'target_namespace': target_namespace,
                'principal': principal, 'allowed': allowed}

    async def authorize_sources(self, ctx, *, target_namespace_id, target_uri, source_uris):
        self._context(ctx)
        if not isinstance(source_uris, (tuple, list)) or not source_uris or len(source_uris) > self.max_source_uris:
            raise SchemaError('A bounded nonempty source URI list is required')
        # Pin ONE current-ACL publication view for the whole authorization. Each
        # _root_acl/_ensure_access below resolves through acl._scroll_all, which
        # otherwise re-runs publication.capture (BEGIN IMMEDIATE + json.loads of
        # every snapshot manifest) per call. With ~2k sources that was ~2k
        # exclusive-lock captures for the SAME namespace roots: it saturated the
        # event loop and starved pathlock (reindex CONFLICT). A single ambient
        # view makes the batch read one consistent snapshot and capture once.
        with _current_acl_view(ctx):
            target_uri = source_uri(target_uri)
            target = await self.get(ctx, target_namespace_id, action=AclAction.WRITE)
            if not self._inside(target.root_uri, target_uri):
                raise PermissionDeniedError('Wiki output does not belong to its configured namespace')
            await self.fs._ensure_access(target_uri, ctx, action=AclAction.WRITE)
            bindings = tuple([(uri, await self.lookup(ctx, uri)) for uri in sorted({source_uri(uri) for uri in source_uris})])
            roots = {target.namespace_id: target, **{root.namespace_id: root for _, root in bindings}}
            acl_digests = tuple([(name, await self._root_acl(ctx, root, AclAction.WRITE if name == target.namespace_id else AclAction.READ))
                                 for name, root in sorted(roots.items())])
            principals = tuple(sorted(acl_principals(ctx)))
            revision = self.registry.namespace_authorize_exports(
                ctx.account_id, target.namespace_id,
                [root.namespace_id for _, root in bindings], principals)
            # Recheck child ACLs after resolving roots/export policy.
            for uri, _ in bindings:
                await self.fs._ensure_access(uri, ctx)
            await self.fs._ensure_access(target_uri, ctx, action=AclAction.WRITE)
        return SourceAuthorization(ctx.account_id, ctx.user.user_id, principals, target.namespace_id,
                                   bindings, revision, acl_digests)

    def check_policy_revision(self, ctx, authorization):
        self._context(ctx)
        if (authorization.account_id, authorization.user_id, authorization.principals) != (
                ctx.account_id, ctx.user.user_id, tuple(sorted(acl_principals(ctx)))):
            raise PermissionDeniedError('Source authorization belongs to another actor')
        self.registry.namespace_check_revision(ctx.account_id, authorization.policy_revision)

    async def recheck(self, ctx, authorization, *, target_uri):
        self.check_policy_revision(ctx, authorization)
        current = await self.authorize_sources(ctx, target_namespace_id=authorization.target_namespace_id,
                                               target_uri=target_uri, source_uris=[uri for uri, _ in authorization.source_bindings])
        if current != authorization:
            raise ConflictError('Namespace membership, actor or export policy changed during source use')
        return current

    async def check_source_use(self, ctx, authorization, *, uri, target_uri):
        """Recheck one streamed source, without rescanning the full input list."""
        self.check_policy_revision(ctx, authorization)
        uri, target_uri = source_uri(uri), source_uri(target_uri)
        source = next((root for captured_uri, root in authorization.source_bindings if captured_uri == uri), None)
        if source is None:
            raise PermissionDeniedError('Source is not part of this authorization')
        target = self._get(ctx.account_id, authorization.target_namespace_id)
        if not self._inside(target.root_uri, target_uri):
            raise PermissionDeniedError('Target is outside the authorized knowledge namespace')
        digests = dict(authorization.root_acl_digests)
        # One ambient view for this source's four ACL reads (source root, target
        # root, source child, target child) instead of four separate
        # publication.capture round-trips. The view is scoped to this call only,
        # so each streamed source still re-reads current ACLs.
        with _current_acl_view(ctx):
            for root, action in ((source, AclAction.READ), (target, AclAction.WRITE)):
                if await self._root_acl(ctx, root, action) != digests[root.namespace_id]:
                    raise ConflictError('Knowledge root ACL changed during source use')
            await self.fs._ensure_access(uri, ctx)
            await self.fs._ensure_access(target_uri, ctx, action=AclAction.WRITE)
        self.check_policy_revision(ctx, authorization)
