"""Current contributor permissions for published image-derived candidates."""
from __future__ import annotations

import asyncio

from openviking.indexing.publication import _view, configured_store
from openviking.server.identity import Role
from openviking.storage.acl import AclAction, acl_allows
from openviking.storage.expr import RawDSL
from openviking.wiki.access import denied_namespace_roots, inside_denied_namespace


async def denied_derived_uris(manager, ctx, *, registry=None):
    if ctx.bypass_acl or ctx.role in (Role.ROOT, Role.ADMIN):
        return ()
    registry = registry if registry is not None else configured_store()
    if registry is None:
        return ()
    view = _view.get()
    owned = view is None
    if owned:
        lease, manifest = await asyncio.to_thread(registry.capture, ctx.account_id)
    else:
        if view[0] != ctx.account_id or view[1].path != registry.path:
            raise RuntimeError('Derived content view belongs to another account or registry')
        lease, manifest = view[2], view[3]
    try:
        dependencies = {uri: tuple(item.get('content', {}).get('image_context_epochs', {}))
                        for uri, item in manifest.items()
                        if item.get('content', {}).get('image_context_epochs')}
        wiki_pages = {uri: tuple(sorted(set(item.get('content', {}).get('wiki_contributors', []))))
                      for uri, item in manifest.items()
                      if item.get('content', {}).get('wiki_contributors')}
        combined = dict(dependencies)
        for uri, values in wiki_pages.items():
            combined[uri] = combined.get(uri, ()) + tuple(v for v in values if v not in combined.get(uri, ()))
        if not combined:
            return ()
        if manager is None or not manager.is_enabled(ctx.account_id):
            return tuple(sorted(combined))
        sources = sorted({source for values in combined.values() for source in values})
        permissions = await manager.resolve_many(sources, ctx)
        roots = await denied_namespace_roots(manager, ctx, registry=registry)
        deleted = await asyncio.to_thread(registry.tombstones, ctx.account_id)
        denied = {source for source in sources if inside_denied_namespace(source, roots)
                  or source not in permissions or not acl_allows(permissions[source], ctx, AclAction.READ)
                  or any(source == dead or source.startswith(dead.rstrip('/') + '/') for dead in deleted)}
        await asyncio.to_thread(registry.renew, lease)
        return tuple(sorted(uri for uri, values in combined.items() if any(source in denied for source in values)))
    finally:
        if owned:
            await asyncio.to_thread(registry.release, lease)


async def derived_candidate_filter(manager, ctx):
    denied = await denied_derived_uris(manager, ctx)
    return RawDSL({'op': 'must_not', 'field': 'uri', 'conds': list(denied), 'para': '-d=0'}) if denied else None


async def recheck_derived_candidates(manager, ctx, rows):
    denied = set(await denied_derived_uris(manager, ctx))
    return [row for row in rows if row.get('uri') not in denied]
