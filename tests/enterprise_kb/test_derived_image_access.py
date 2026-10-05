"""Published image summaries cannot escape contributor permissions."""
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from test_wiki_namespaces import domains as domains, ONE, RAW, WIKI
from test_wiki_build_plan import prepared as prepared
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding

from openviking.indexing.content import archive_source
from openviking.indexing.derived_access import denied_derived_uris
from openviking.storage.expr import RawDSL
from openviking.storage.viking_vector_index_backend import VikingVectorIndexBackend

IMAGE = WIKI + '/images/pump.png'


async def publish_image(e):
    await e.fs.write_file_bytes(IMAGE, b'fixture image asset bytes', ctx=e.ctx)
    reference, _ = await archive_source(e.fs, IMAGE, e.ctx)
    reference['image_context_epochs'] = {ONE: e.store.observe_draft(e.ctx.account_id, ONE)}
    generation = e.store.begin(e.ctx.account_id, 'wiki', IMAGE, 'image-plan', ['image:one'], source_ref=reference)
    e.store.acknowledge(generation, 'image:one')
    e.store.seal(generation)
    assert e.store.state(generation)['state'] == 'published'


async def test_wider_asset_permission_still_excludes_denied_contributor_before_query(domains):
    e, planner, policy, admin, backend = domains
    await publish_image(e)
    assert await denied_derived_uris(e.fs.acl_manager, e.ctx) == ()
    backend.acl(RAW, [{'principal': 'group:private', 'level': 'read'}])
    backend.acl(IMAGE, [{'principal': 'user:user', 'level': 'read'}])
    assert await denied_derived_uris(e.fs.acl_manager, e.ctx) == (IMAGE,)
    facade = object.__new__(VikingVectorIndexBackend)
    facade.acl_manager = e.fs.acl_manager
    facade.query = AsyncMock(return_value=[])
    await facade.search(query_vector=[1], ctx=e.ctx)
    predicate = facade.query.call_args.kwargs['filter']
    assert any(isinstance(part, RawDSL) and part.payload.get('conds') == [IMAGE]
               and part.payload.get('para') == '-d=0' for part in predicate.conds)


async def test_source_revocation_during_backend_query_hides_summary_before_return(domains):
    e, planner, policy, admin, backend = domains
    await publish_image(e)
    facade = object.__new__(VikingVectorIndexBackend)
    facade.acl_manager = e.fs.acl_manager
    async def delayed(**kwargs):
        backend.acl(RAW, [{'principal': 'user:other', 'level': 'read'}])
        return [{'uri': IMAGE, 'abstract': 'private source derived diagnosis'}]
    facade.query = AsyncMock(side_effect=delayed)
    assert await facade.search(query_vector=[1], ctx=e.ctx) == []


async def test_group_removal_source_delete_and_acl_disable_reject_derived_content(domains):
    e, planner, policy, admin, backend = domains
    await publish_image(e)
    backend.acl(RAW, [{'principal': 'group:repair', 'level': 'read'}])
    member = replace(e.ctx, group_ids=('repair',))
    assert await denied_derived_uris(e.fs.acl_manager, member) == ()
    assert await denied_derived_uris(e.fs.acl_manager, replace(member, group_ids=())) == (IMAGE,)
    e.store.tombstone(e.ctx.account_id, ONE)
    assert await denied_derived_uris(e.fs.acl_manager, member) == (IMAGE,)
    e.fs.acl_manager.set_enabled(e.ctx.account_id, False)
    assert await denied_derived_uris(e.fs.acl_manager, member) == (IMAGE,)


async def test_fs_read_channels_apply_contributor_intersection(domains):
    from openviking_cli.exceptions import PermissionDeniedError
    e, planner, policy, admin, backend = domains
    await publish_image(e)
    assert (await e.fs._can_access_many([ONE, IMAGE], e.ctx))[IMAGE] is True
    e.store.tombstone(e.ctx.account_id, ONE)
    access = await e.fs._can_access_many([ONE, IMAGE], e.ctx)
    assert access[ONE] is True and access[IMAGE] is False
    with pytest.raises(PermissionDeniedError):
        await e.fs._ensure_access(IMAGE, e.ctx)
