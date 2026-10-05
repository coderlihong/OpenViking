# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Apply current registered-root permissions in addition to per-resource ACLs."""
from __future__ import annotations

import asyncio

from openviking.indexing.publication import configured_store
from openviking.server.identity import Role
from openviking.storage.acl import AclAction, AclMode, acl_allows
from openviking.storage.expr import RawDSL


async def denied_namespace_roots(manager, ctx, *, action=AclAction.READ, registry=None):
    if ctx.bypass_acl or ctx.role in (Role.ROOT, Role.ADMIN):
        return ()  # Preserve the existing trusted maintenance/admin ACL contract.
    registry = registry if registry is not None else configured_store()
    if registry is None:
        return ()
    namespaces = await asyncio.to_thread(registry.namespace_list, ctx.account_id)
    roots = tuple(sorted({row["root_uri"] for row in namespaces}))
    if not roots:
        return ()
    if manager is None or not manager.is_enabled(ctx.account_id):
        return roots  # Disabling ACLs must not turn registered private domains public.
    effective = await manager.resolve_many(roots, ctx)
    return tuple(root for root in roots if root not in effective
                 or effective[root].mode != AclMode.RESTRICTED
                 or not effective[root].enabled
                 or not acl_allows(effective[root], ctx, action))


async def namespace_candidate_filter(manager, ctx):
    roots = await denied_namespace_roots(manager, ctx)
    if not roots:
        return None
    # The path depth parameter excludes whole subtrees, with segment boundaries.
    return RawDSL({"op": "must_not", "field": "uri", "conds": list(roots), "para": "-d=-1"})


def inside_denied_namespace(uri, roots):
    return any(uri == root or uri.startswith(root + "/") for root in roots)
