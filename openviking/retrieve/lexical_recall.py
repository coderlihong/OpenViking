"""Bind the existing find/search pipeline to authority BM25 and current ACLs."""
from __future__ import annotations

import asyncio

from openviking.indexing.derived_access import denied_derived_uris
from openviking.indexing.publication import _view, configured_store
from openviking_cli.exceptions import FailedPreconditionError
from openviking_cli.utils.config.open_viking_config import OpenVikingConfigSingleton


def configured_lexical_recall(fs):
    config = OpenVikingConfigSingleton._instance
    if config is None or not getattr(config.embedding.chunking, 'lexical_enabled', False):
        return None
    registry = configured_store(config.embedding)
    if registry is None:
        raise FailedPreconditionError('Lexical retrieval requires a publication authority')

    async def recall(*, query, ctx, context_type, target_dirs, scope_dsl, limit):
        if context_type not in (None, 'resource'):
            return []
        view = _view.get()
        owned = view is None
        if owned:
            lease, manifest = await asyncio.to_thread(registry.capture, ctx.account_id)
        else:
            if view[0] != ctx.account_id or view[1].path != registry.path:
                raise RuntimeError('Lexical view belongs to a different account/store')
            lease, manifest = view[2], view[3]
        try:
            # Derived records published from contributor sources carry the source
            # intersection rule too; the bare URI ACL below cannot see it.
            denied = await denied_derived_uris(fs.acl_manager, ctx, registry=registry)
            uris = [uri for uri in manifest if not target_dirs or any(
                uri == root or uri.startswith(root.rstrip('/') + '/') for root in target_dirs)]
            allowed = await fs._can_access_many(uris, ctx)
            rows = await asyncio.to_thread(registry.lexical_search, ctx.account_id, lease,
                                           [uri for uri in uris if allowed.get(uri, False) and uri not in denied], query.query, limit,
                                           **({'metadata_filter': scope_dsl} if scope_dsl is not None else {}))
            await asyncio.to_thread(registry.renew, lease)
            # Check again before handing derived content to rerank; revocation
            # during the authority call must not release the cached chunk text.
            current = await fs._can_access_many([row['uri'] for row in rows], ctx)
            current_denied = await denied_derived_uris(fs.acl_manager, ctx, registry=registry)
            return [row for row in rows
                    if current.get(row['uri'], False) and row['uri'] not in current_denied]
        finally:
            if owned:
                await asyncio.to_thread(registry.release, lease)
    return recall
