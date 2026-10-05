"""Namespace/export checks over actual AclManager + VikingFS and SQLite policy.

The ACL context/vector store is a fixture. These are not production grants or
complete namespace search/commit/identity-refresh verification.
"""
from dataclasses import replace

import pytest
from test_wiki_build_plan import prepared as prepared
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding

from openviking.storage.acl import AclManager, AclMode, DirectAcl, EffectiveAcl
from openviking.storage.expr import Or, PathScope
from openviking.server.identity import Role
from openviking.wiki.namespaces import NamespaceCatalog
from openviking.wiki.schema import SchemaError
from openviking_cli.exceptions import ConflictError, FailedPreconditionError, NotFoundError, PermissionDeniedError
from openviking_cli.session.user_id import UserIdentifier

RAW='viking://resources/raw'
WIKI='viking://resources/wiki'
ONE=RAW+'/manual/one.md'
TWO=RAW+'/manual/chapter/two.md'


class ContextStore:
    def __init__(self):self.records={}
    def acl(self,uri,entries,mode=AclMode.RESTRICTED):
        acl=EffectiveAcl.from_permissions(DirectAcl.from_entries(entries),DirectAcl(),mode=mode)
        self.records[uri]={'id':uri,'uri':uri,**acl.context_fields()}
    async def scroll(self,*,filter,ctx,**kwargs):
        assert isinstance(filter,Or) and all(isinstance(c,PathScope) and c.depth==0 for c in filter.conds)
        return [dict(row) for row in self.records.values() if row['uri'] in {c.path for c in filter.conds}],None


@pytest.fixture
async def domains(prepared):
    e,planner=prepared
    backend=ContextStore()
    backend.acl(RAW,[{'principal':'user:user','level':'read'}])
    backend.acl(WIKI,[{'principal':'user:user','level':'write'}])
    e.fs._async_agfs.path(e.fs._uri_to_path(WIKI,ctx=e.ctx)).mkdir(parents=True,exist_ok=True)
    e.fs.acl_manager=AclManager(backend);e.fs.acl_manager.set_enabled('acct',True)
    # Restore actual permission resolution, rather than the prior source fixture mock.
    e.fs._ensure_access=type(e.fs)._ensure_access.__get__(e.fs)
    catalog=NamespaceCatalog(e.fs)
    admin=replace(e.ctx,role=Role.ADMIN)
    await catalog.register(admin,namespace_id='manuals',root_uri=RAW)
    await catalog.register(admin,namespace_id='wiki',root_uri=WIKI)
    return e,planner,catalog,admin,backend


async def grant(c,admin,**changes):
    args=dict(source_namespace='manuals',target_namespace='wiki',principal='user:user',allowed=True)
    args.update(changes)
    return await c.set_export(admin,**args)


async def authorize(c,ctx,uris=(ONE,)):
    return await c.authorize_sources(ctx,target_namespace_id='wiki',target_uri=WIKI,source_uris=uris)


async def test_registered_roots_reopen_resolve_canonical_uri_and_raw_reference_binding(domains):
    e,p,c,admin,b=domains
    from openviking.indexing.publication import PublicationStore
    reopened=NamespaceCatalog(e.fs,registry=PublicationStore(e.store.path))
    assert (await reopened.lookup(e.ctx,ONE)).namespace_id=='manuals'
    assert (await reopened.get(e.ctx,'wiki')).root_uri==WIKI
    binding=await reopened.reference_binding(e.ctx,ONE)
    assert binding.namespace_id=='manuals' and binding.kind is None and binding.validated_sha256 is None
    assert (await reopened.register(admin,namespace_id='manuals',root_uri=RAW)).root_uri==RAW


async def test_read_and_write_permissions_do_not_implicitly_authorize_export(domains):
    e,p,c,admin,b=domains
    await e.fs._ensure_access(ONE,e.ctx)
    with pytest.raises(PermissionDeniedError,match='Explicit'):
        await authorize(c,e.ctx)
    await grant(c,admin)
    ticket=await authorize(c,e.ctx,(TWO,ONE))
    assert [uri for uri,_ in ticket.source_bindings]==[TWO,ONE]
    assert ticket.target_namespace_id=='wiki'
    assert await c.recheck(e.ctx,ticket,target_uri=WIKI)==ticket


async def test_group_membership_and_one_way_export_are_respected(domains):
    e,p,c,admin,b=domains
    b.acl(RAW,[{'principal':'group:repair','level':'read'}])
    b.acl(WIKI,[{'principal':'group:repair','level':'write'}])
    group=replace(e.ctx,user=UserIdentifier('acct','member'),group_ids=('repair',))
    await grant(c,admin,principal='group:repair')
    assert (await authorize(c,group)).user_id=='member'
    with pytest.raises(PermissionDeniedError):
        await authorize(c,replace(group,group_ids=()))
    with pytest.raises(PermissionDeniedError):
        await c.authorize_sources(group,target_namespace_id='manuals',target_uri=RAW,source_uris=[WIKI+'/entity.md'])


async def test_export_changes_invalidate_ticket_and_idempotent_grant_does_not(domains):
    e,p,c,admin,b=domains
    await grant(c,admin);ticket=await authorize(c,e.ctx)
    await grant(c,admin)
    c.check_policy_revision(e.ctx,ticket)
    await grant(c,admin,allowed=False)
    with pytest.raises(ConflictError,match='policy changed'):
        c.check_policy_revision(e.ctx,ticket)
    with pytest.raises(PermissionDeniedError,match='Explicit'):
        await authorize(c,e.ctx)


async def test_source_acl_revocation_is_observed_even_if_export_remains(domains):
    e,p,c,admin,b=domains
    await grant(c,admin);ticket=await authorize(c,e.ctx)
    b.acl(RAW,[{'principal':'user:other','level':'read'}])
    with pytest.raises(PermissionDeniedError):
        await c.check_source_use(e.ctx,ticket,uri=ONE,target_uri=WIKI)


async def test_target_acl_revocation_is_observed(domains):
    e,p,c,admin,b=domains
    await grant(c,admin);ticket=await authorize(c,e.ctx)
    b.acl(WIKI,[{'principal':'user:user','level':'read'}])
    with pytest.raises(PermissionDeniedError):
        await c.check_source_use(e.ctx,ticket,uri=ONE,target_uri=WIKI)


async def test_more_permissive_child_acl_does_not_bypass_wiki_root_membership(domains):
    e,p,c,admin,b=domains
    b.acl(ONE,[{'principal':'user:outsider','level':'read'}])
    outsider=replace(e.ctx,user=UserIdentifier('acct','outsider'))
    with pytest.raises(PermissionDeniedError):
        await e.fs._ensure_access(ONE,outsider)  # Registered root also gates URI reads.
    with pytest.raises(PermissionDeniedError):
        await c.lookup(outsider,ONE)


@pytest.mark.parametrize('change', [{'bypass_acl':True}, {'user':UserIdentifier('acct','other')}, {'group_ids':('new-group',)}])
async def test_authorization_cannot_be_reused_by_another_actor_or_group_context(domains,change):
    e,p,c,admin,b=domains
    await grant(c,admin);ticket=await authorize(c,e.ctx)
    with pytest.raises(PermissionDeniedError):
        c.check_policy_revision(replace(e.ctx,**change),ticket)


async def test_cross_account_same_uri_cannot_resolve_policy(domains):
    e,p,c,admin,b=domains
    other=replace(admin,user=UserIdentifier('other','user'))
    e.fs.acl_manager.set_enabled('other',True)
    with pytest.raises(NotFoundError):
        await c.get(other,'manuals')


async def test_registration_requires_enabled_acl_restricted_directory_and_nonoverlapping_roots(domains):
    e,p,c,admin,b=domains
    e.fs.acl_manager.set_enabled('acct',False)
    with pytest.raises(FailedPreconditionError):
        await c.get(admin,'manuals')
    e.fs.acl_manager.set_enabled('acct',True)
    with pytest.raises(ConflictError,match='overlap'):
        await c.register(admin,namespace_id='nested',root_uri=RAW+'/manual')
    with pytest.raises(FailedPreconditionError,match='directory'):
        await c.register(admin,namespace_id='file',root_uri=ONE)
    b.acl(RAW,[],mode=AclMode.NONE)
    with pytest.raises(FailedPreconditionError,match='restricted'):
        await c.get(admin,'manuals')


async def test_exports_require_admin_and_manage_rights_and_known_roots(domains):
    e,p,c,admin,b=domains
    with pytest.raises(PermissionDeniedError):await grant(c,e.ctx)
    with pytest.raises(NotFoundError):await grant(c,admin,target_namespace='missing')
    with pytest.raises(SchemaError):await grant(c,admin,target_namespace='manuals')
    with pytest.raises(SchemaError):await grant(c,admin,allowed='true')


async def test_unknown_source_or_target_outside_registered_namespace_is_rejected(domains):
    e,p,c,admin,b=domains
    await grant(c,admin)
    with pytest.raises(FailedPreconditionError):await authorize(c,admin,['viking://resources/raw2/one.md'])
    with pytest.raises(PermissionDeniedError):
        await c.authorize_sources(admin,target_namespace_id='wiki',target_uri='viking://resources/wiki2',source_uris=[ONE])


async def test_plan_execution_stream_requires_export_and_stops_before_revoked_next_source(domains):
    from unittest.mock import AsyncMock
    e,p,c,admin,b=domains
    plan=await p.prepare_sources(e.ctx,'manual-wiki',[ONE,TWO],input_kind='uri',idempotency_key='execute')
    stream=p.authorized_source_texts(e.ctx,plan.build_id)
    with pytest.raises(PermissionDeniedError,match='Explicit'):await anext(stream)
    await grant(c,admin)
    stream=p.authorized_source_texts(e.ctx,plan.build_id)
    assert (await anext(stream))[0]==TWO
    read=AsyncMock(wraps=e.fs._async_agfs.read);e.fs._async_agfs.read=read
    await grant(c,admin,allowed=False)
    with pytest.raises(ConflictError,match='policy changed'):await anext(stream)
    read.assert_not_awaited()
