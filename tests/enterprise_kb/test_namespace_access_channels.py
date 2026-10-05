"""Registered-root ACL intersections, with actual ACL resolver and URI operations."""
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from test_wiki_namespaces import domains as domains, RAW, ONE
from test_wiki_namespaces import prepared as prepared, environment as environment, embedding as embedding

from openviking.storage.acl import AclAction, AclMode
from openviking.storage.expr import PathScope, RawDSL
from openviking.storage.vectordb.index.cuvs_index import matches_filter
from openviking.storage.vectordb_adapters.qdrant_adapter import QdrantCollectionAdapter
from openviking.storage.viking_vector_index_backend import VikingVectorIndexBackend
from openviking.wiki.access import denied_namespace_roots, namespace_candidate_filter
from openviking_cli.exceptions import PermissionDeniedError
from openviking_cli.utils.config.vectordb_config import VectorDBBackendConfig


async def test_wider_child_acl_does_not_bypass_registered_root(domains):
    e, _, _, _, backend = domains
    backend.acl(RAW, [{'principal': 'group:private', 'level': 'write'}])
    backend.acl(ONE, [{'principal': 'user:user', 'level': 'write'}])
    for action in (AclAction.READ, AclAction.WRITE):
        with pytest.raises(PermissionDeniedError):
            await e.fs._ensure_access(ONE, e.ctx, action=action)
    assert await e.fs._can_access_many([ONE, RAW + '-other/file.md'], e.ctx) == {
        ONE: False, RAW + '-other/file.md': True,
    }


async def test_root_read_only_does_not_grant_child_write(domains):
    e, _, _, _, backend = domains
    backend.acl(ONE, [{'principal': 'user:user', 'level': 'write'}])
    await e.fs._ensure_access(ONE, e.ctx)
    with pytest.raises(PermissionDeniedError):
        await e.fs._ensure_access(ONE, e.ctx, action=AclAction.WRITE)


async def test_current_group_removal_and_acl_disable_reject_domain(domains):
    e, _, _, _, backend = domains
    backend.acl(RAW, [{'principal': 'group:repair', 'level': 'read'}])
    backend.acl(ONE, [{'principal': 'user:user', 'level': 'read'}])
    member = replace(e.ctx, group_ids=('repair',))
    await e.fs._ensure_access(ONE, member)
    with pytest.raises(PermissionDeniedError):
        await e.fs._ensure_access(ONE, replace(member, group_ids=()))
    e.fs.acl_manager.set_enabled(e.ctx.account_id, False)
    with pytest.raises(PermissionDeniedError):
        await e.fs._ensure_access(ONE, member)


async def test_root_becoming_public_does_not_make_domain_public(domains):
    e, _, _, _, backend = domains
    backend.acl(RAW, [], mode=AclMode.NONE)
    assert RAW in await denied_namespace_roots(e.fs.acl_manager, e.ctx, registry=e.store)
    with pytest.raises(PermissionDeniedError):
        await e.fs._ensure_access(ONE, e.ctx)


@pytest.mark.parametrize('operation', ['read', 'stat', 'ls', 'tree', 'grep', 'read_file_bytes'])
async def test_uri_channels_reject_before_returning_private_content(domains, operation):
    e, _, _, _, backend = domains
    backend.acl(RAW, [{'principal': 'group:private', 'level': 'read'}])
    backend.acl(ONE, [{'principal': 'user:user', 'level': 'read'}])
    method = getattr(e.fs, operation)
    with pytest.raises(PermissionDeniedError):
        if operation == 'grep':
            await method(RAW, pattern='.*', ctx=e.ctx)
        elif operation in {'ls', 'tree'}:
            await method(RAW, ctx=e.ctx)
        else:
            await method(ONE, ctx=e.ctx)


async def test_dense_and_filter_candidates_exclude_denied_subtrees_before_query(domains):
    e, _, _, _, backend = domains
    backend.acl(RAW, [{'principal': 'group:private', 'level': 'read'}])
    facade = object.__new__(VikingVectorIndexBackend)
    facade.acl_manager = e.fs.acl_manager
    facade.query = AsyncMock(return_value=[])
    await facade.search(query_vector=[1.0], ctx=e.ctx)
    dense_filter = facade.query.call_args.kwargs['filter']
    assert any(isinstance(c, RawDSL) and c.payload.get('para') == '-d=-1'
               for c in dense_filter.conds)
    await facade.filter_in_tenant(e.ctx, target_directories=[RAW], extra_filter={'op': 'must', 'field': 'level', 'conds': [2]})
    assert facade.query.await_count == 2


async def test_path_exclusion_matches_descendants_but_not_sibling_prefix(domains):
    e, _, _, _, backend = domains
    backend.acl(RAW, [{'principal': 'group:private', 'level': 'read'}])
    exclusion = await namespace_candidate_filter(e.fs.acl_manager, e.ctx)
    native = dict(exclusion.payload, conds=[p.replace('viking://', '/') for p in exclusion.payload['conds']])
    assert not matches_filter({'uri': '/resources/raw/manual/file.md'}, native, {'uri': 'path'})
    assert not matches_filter({'uri': '/resources/raw'}, native, {'uri': 'path'})
    assert matches_filter({'uri': '/resources/raw-other/file.md'}, native, {'uri': 'path'})
    config = VectorDBBackendConfig.model_validate({'backend': 'qdrant', 'qdrant': {'url': 'http://localhost:6333'}})
    adapter = QdrantCollectionAdapter.from_config(config)
    compiled = adapter._compile_filter(exclusion)
    assert compiled['must_not'] == [adapter._compile_filter(PathScope('uri', RAW, depth=-1))]
