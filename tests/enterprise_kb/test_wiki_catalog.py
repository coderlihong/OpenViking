"""Account-scoped schema metadata, actual private payloads and collection pins."""
import asyncio
from dataclasses import replace
import json
from unittest.mock import AsyncMock

import pytest
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding
from test_wiki_schema import freeze, package

from openviking.indexing.content import blob_path
from openviking.indexing.publication import PublicationStore
from openviking.server.identity import Role
from openviking.wiki.catalog import WikiCatalog
from openviking.wiki.schema import SchemaBundle,SchemaError
from openviking_cli.exceptions import ConflictError,NotFoundError,PermissionDeniedError,FailedPreconditionError
from openviking_cli.session.user_id import UserIdentifier


@pytest.fixture(autouse=True)
def mounted_transport_root(environment):
    # An AGFS mount exists before requests; the disk transport fixture starts empty.
    environment.fs._async_agfs.root.mkdir(parents=True, exist_ok=True)


async def register_domains(e, ctx=None, *, target='viking://resources/wiki', raw='viking://resources/raw', namespace='wiki', source_namespace='manuals'):
    from test_wiki_namespaces import ContextStore
    from openviking.storage.acl import AclManager
    from openviking.wiki.namespaces import NamespaceCatalog
    ctx = ctx or replace(e.ctx, role=Role.ADMIN)
    backend = getattr(e, 'catalog_backend', None)
    if backend is None:
        backend = ContextStore()
        e.catalog_backend = backend
        e.fs.acl_manager = AclManager(backend)
        e.fs._ensure_access = type(e.fs)._ensure_access.__get__(e.fs)
    e.fs.acl_manager.set_enabled(ctx.account_id, True)
    for root in (target, raw):
        e.fs._async_agfs.path(e.fs._uri_to_path(root, ctx=ctx)).mkdir(parents=True, exist_ok=True)
        backend.acl(root, [{'principal':'user:user','level':'write'}])
    domains = NamespaceCatalog(e.fs)
    await domains.register(ctx, namespace_id=source_namespace, root_uri=raw)
    await domains.register(ctx, namespace_id=namespace, root_uri=target)


async def setup(e):
    await register_domains(e)
    catalog=WikiCatalog(e.fs);ctx=replace(e.ctx,role=Role.ADMIN)
    bundle=freeze(package())
    await catalog.register_schema(ctx,bundle)
    await create(catalog,ctx)
    return catalog,ctx,bundle


async def create(catalog,ctx,**changes):
    args=dict(collection_id='manual-wiki',namespace_id='wiki',target_uri='viking://resources/wiki',
              raw_sources_prefix='viking://resources/raw',schema_id='business',schema_version='1')
    args.update(changes)
    return await catalog.register_collection(ctx,**args)


async def test_private_schema_persists_across_reopen_and_is_a_gc_root(environment):
    e=environment;c,ctx,b=await setup(e)
    pinned=await WikiCatalog(e.fs,registry=PublicationStore(e.store.path)).pin_collection(e.ctx,'manual-wiki',input_kind='uri')
    assert pinned.bundle==b and pinned.collection.schema_digest==b.digest
    assert pinned.capability_state=='structural_ready'
    row=c._schema_row('acct','business','1');ref=json.loads(row['reference'])
    assert set(ref)=={'size','sha256'} and '# Entity' not in row['reference']
    assert ('acct',ref['sha256']) in e.store.protected_content_revisions()
    assert '# Entity' in (await e.fs._async_agfs.read(blob_path(e.fs,ctx,ref['sha256']))).decode()


async def test_immutable_identity_is_idempotent_and_changed_contents_conflict(environment):
    c,ctx,b=await setup(environment)
    assert (await c.register_schema(ctx,b))['digest']==b.digest
    assert (await create(c,ctx)).schema_digest==b.digest
    changed=package();changed['Thing.template.md']+='changed'
    with pytest.raises(ConflictError):
        await c.register_schema(ctx,freeze(changed))
    with pytest.raises((ConflictError,FailedPreconditionError)):
        await create(c,ctx,target_uri='viking://resources/newwiki')


async def test_new_schema_version_does_not_drift_existing_pin(environment):
    c,ctx,b=await setup(environment)
    old=await c.pin_collection(ctx,'manual-wiki',input_kind='upload')
    files=package();files['Thing.template.md']+='new version'
    await c.register_schema(ctx,SchemaBundle.freeze('business','2',files))
    again=await c.pin_collection(ctx,'manual-wiki',input_kind='uri')
    assert old.bundle==again.bundle==b
    assert old.binding_digest==again.binding_digest


async def test_same_identifiers_are_independent_between_accounts(environment):
    e=environment;c,ctx,b=await setup(e)
    other=replace(ctx,user=UserIdentifier('other','user'))
    with pytest.raises(NotFoundError):
        await c.get_collection(other,'manual-wiki')
    files=package();files['Thing.template.md']+='other account'
    await register_domains(e, other)
    b2=freeze(files);await c.register_schema(other,b2);await create(c,other)
    assert (await c.pin_collection(other,'manual-wiki',input_kind='uri')).bundle==b2
    assert (await c.pin_collection(ctx,'manual-wiki',input_kind='uri')).bundle==b


@pytest.mark.parametrize('changes', [
    {'namespace_id':'wiki'}, {'target_uri':'viking://resources/wiki/nested'},
    {'raw_sources_prefix':'viking://resources/raw/nested'},
    {'target_uri':'viking://resources/raw/nested'},
])
async def test_duplicate_namespace_or_overlapping_roots_are_rejected(environment,changes):
    c,ctx,b=await setup(environment)
    args=dict(collection_id='second',namespace_id='second',target_uri='viking://resources/second',raw_sources_prefix='viking://resources/raw2')
    args.update(changes)
    with pytest.raises((ConflictError,SchemaError,FailedPreconditionError,NotFoundError)):
        await create(c,ctx,**args)


async def test_root_prefix_component_boundary_and_existing_file_checks(environment):
    e=environment;c,ctx,b=await setup(e)
    await register_domains(e, ctx, target='viking://resources/wiki2', raw='viking://resources/raw2', namespace='second', source_namespace='manuals2')
    assert (await create(c,ctx,collection_id='second',namespace_id='second',target_uri='viking://resources/wiki2',raw_sources_prefix='viking://resources/raw2')).namespace_id=='second'
    await e.fs.write('viking://resources/file.md','text',ctx=ctx)
    with pytest.raises(SchemaError,match='directories'):
        await create(c,ctx,collection_id='third',namespace_id='third',target_uri='viking://resources/file.md')


async def test_schema_registration_requires_admin_and_verified_bundle_hash(environment):
    e=environment;c=WikiCatalog(e.fs)
    with pytest.raises(PermissionDeniedError):
        await c.register_schema(e.ctx,freeze(package()))
    ctx=replace(e.ctx,role=Role.ADMIN)
    with pytest.raises(SchemaError,match='digest'):
        await c.register_schema(ctx,replace(freeze(package()),digest='0'*64))
    with pytest.raises(PermissionDeniedError):
        await c.register_schema(replace(ctx,bypass_acl=True),freeze(package()))


async def test_corrupt_private_payload_does_not_fallback_to_live_templates(environment):
    e=environment;c,ctx,b=await setup(e)
    ref=json.loads(c._schema_row('acct','business','1')['reference'])
    await e.fs._async_agfs.write(blob_path(e.fs,ctx,ref['sha256']),b'corrupt')
    with pytest.raises(SchemaError,match='corrupted'):
        await c.pin_collection(ctx,'manual-wiki',input_kind='uri')


async def test_collection_read_and_pin_recheck_existing_access(environment):
    e=environment;c,ctx,b=await setup(e)
    e.fs._ensure_access=AsyncMock(side_effect=PermissionDeniedError('denied'))
    with pytest.raises(PermissionDeniedError):
        await c.get_collection(e.ctx,'manual-wiki')
    with pytest.raises(PermissionDeniedError):
        await c.pin_collection(e.ctx,'manual-wiki',input_kind='uri')


async def test_uncompiled_rules_persist_as_draft_not_publication_ready(environment):
    e=environment;await register_domains(e);c=WikiCatalog(e.fs);ctx=replace(e.ctx,role=Role.ADMIN)
    files=package();files['Thing.schema.yaml']+='\nrules: [{id: identity, text: Unique identity}]'
    b=freeze(files)
    result=await c.register_schema(ctx,b)
    assert result['capability_state']=='draft_rules_pending'
    await create(c,ctx)
    assert (await c.pin_collection(ctx,'manual-wiki',input_kind='uri')).capability_state=='draft_rules_pending'
    with pytest.raises(SchemaError,match='input kind'):
        await c.pin_collection(ctx,'manual-wiki',input_kind='mixed')


async def test_pin_recompiles_coverage_instead_of_trusting_old_cached_ready(environment):
    e=environment;await register_domains(e);c=WikiCatalog(e.fs);ctx=replace(e.ctx,role=Role.ADMIN)
    files=package(**{'_shared.schema.yaml': 'shared_rules: [{id: evidence, text: Evidence must support each fact}]'})
    await c.register_schema(ctx,freeze(files));await create(c,ctx)
    with e.store.db() as db:
        db.execute("UPDATE wiki_schemas SET state='structural_ready'")
    pin=await c.pin_collection(e.ctx,'manual-wiki',input_kind='uri')
    assert pin.capability_state=='draft_rules_pending'
    assert c._schema_row('acct','business','1')['state']=='draft_rules_pending'


async def test_schema_conflicting_registrations_recheck_after_payload_write(environment,monkeypatch):
    import openviking.wiki.catalog as module
    e=environment;c=WikiCatalog(e.fs);ctx=replace(e.ctx,role=Role.ADMIN)
    original=module.archive_bytes
    waiting=0;barrier=asyncio.Event()
    async def paused(*args,**kwargs):
        nonlocal waiting
        result=await original(*args,**kwargs)
        waiting+=1
        if waiting==2:barrier.set()
        await asyncio.wait_for(barrier.wait(),3)
        return result
    monkeypatch.setattr(module,'archive_bytes',paused)
    files=package();files['Thing.template.md']+='different'
    results=await asyncio.gather(c.register_schema(ctx,freeze(package())),c.register_schema(ctx,freeze(files)),return_exceptions=True)
    assert sum(isinstance(result,ConflictError) for result in results)==1
    assert sum(isinstance(result,dict) for result in results)==1


async def test_collection_access_revoked_while_schema_read_does_not_return_pin(environment):
    e=environment;c,ctx,b=await setup(e)
    e.fs._ensure_access=AsyncMock(side_effect=[None,None,PermissionDeniedError('revoked')])
    with pytest.raises(PermissionDeniedError):
        await c.pin_collection(e.ctx,'manual-wiki',input_kind='uri')
