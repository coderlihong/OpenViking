"""Discriminating private candidate grouping over actual ACL/SQLite/disk AGFS.

The extraction model is a fixture. These checks do not certify Sany identity,
semantic truth, public graph/ready publication or human-edit concurrency CAS.
"""
from dataclasses import replace
import hashlib
import json
from unittest.mock import AsyncMock

import pytest
import yaml
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding
from test_wiki_build_plan import prepared as prepared
from test_wiki_namespaces import domains as domains, RAW, ONE, TWO

from openviking.indexing.content import blob_path
from openviking.wiki.catalog import WikiCatalog
from openviking.wiki.extraction import WikiDraftExtractor
from openviking.wiki.identity import IdentityPolicy, resolve_candidate_identity
from openviking.wiki.merge import DraftSelection, MergeLimits, PrivateWikiMerger
from openviking.wiki.schema import SchemaBundle, SchemaError
from openviking_cli.exceptions import ConflictError, FailedPreconditionError, NotFoundError, PermissionDeniedError
from openviking_cli.session.user_id import UserIdentifier

TARGET='viking://resources/merge-wiki'


def schema_files(*, with_policy=True, normalization='exact'):
    files={}
    for kind in ('Component','Device'):
        source='raw_refs' if kind=='Device' else 'sources'
        files[kind+'.schema.yaml']=f'''entity: {kind}
frontmatter:
  - {{name: title, type: string, required: true}}
  - {{name: id, type: string}}
  - {{name: aliases, type: list}}
  - {{name: business_code, type: string}}
  - {{name: model, type: string}}
  - {{name: pressure, type: number}}
  - {{name: {source}, type: list, ref_kind: source, required: true}}
body_sections: {{required: [来源]}}
'''
        files[kind+'.template.md']='# '+kind+'\n## 来源\n'
    files['Fault.schema.yaml']=f'''entity: Fault
frontmatter:
  - {{name: title, type: string, required: true}}
  - {{name: id, type: string}}
  - {{name: business_code, type: string}}
  - {{name: model, type: string}}
  - {{name: sources, type: list, ref_kind: source, required: true}}
  - {{name: asserted_devices, type: list, ref_kind: Device, reference_key: device_uri}}
body_sections: {{required: [来源]}}
'''
    files['Fault.template.md']='# Fault\n## 来源\n'
    if with_policy:
        files['identity.policy.yaml']=yaml.safe_dump({'version':1,'policy_version':'fixture-business-v1','entities':[
            {'kind':kind,'key_fields':['business_code'],'scope_fields':['model'],'normalization':normalization}
            for kind in ('Component','Device','Fault')]},sort_keys=False)
    return files


async def install(e,p,c,a,b,*,target=TARGET,namespace='merge',collection='parts',files=None):
    e.fs._async_agfs.path(e.fs._uri_to_path(target,ctx=a)).mkdir(parents=True,exist_ok=True)
    b.acl(target,[{'principal':'user:user','level':'write'}])
    await c.register(a,namespace_id=namespace,root_uri=target)
    staging=target+'-raw'
    e.fs._async_agfs.path(e.fs._uri_to_path(staging,ctx=a)).mkdir(parents=True,exist_ok=True)
    b.acl(staging,[{'principal':'user:user','level':'write'}])
    await c.register(a,namespace_id=namespace+'-staging',root_uri=staging)
    bundle=SchemaBundle.freeze('parts','1',files or schema_files())
    catalog=WikiCatalog(e.fs)
    await catalog.register_schema(a,bundle)
    await catalog.register_collection(a,collection_id=collection,namespace_id=namespace,target_uri=target,
                                      raw_sources_prefix=staging,schema_id=bundle.schema_id,schema_version=bundle.version)
    await c.set_export(a,source_namespace='manuals',target_namespace=namespace,principal='user:user',allowed=True)
    return bundle


@pytest.fixture
async def merging(domains,request):
    e,p,c,a,b=domains
    mode=getattr(request,'param','exact')
    bundle=await install(e,p,c,a,b,files=schema_files(with_policy=mode is not None,normalization=mode or 'exact'))
    model=AsyncMock()
    extractor=WikiDraftExtractor(p,model=model,model_profile={'model':'fixture-merge','provider':'fixture'})
    return e,p,c,a,b,bundle,model,extractor,PrivateWikiMerger(extractor)


async def extracted(env,uri,*,code='P-001',model='SY215C',title='主泵',pressure=10,kind='Component',key='one',
                    quoted_code=None,quoted_model=None,metadata_changes=None,target=TARGET,collection='parts',rewrite=True):
    e,p,c,a,b,bundle,llm,x,merger=env
    quote=f'{quoted_code or code} {quoted_model or model} {title} 标准压力 {pressure} MPa。'
    if rewrite:
        await e.fs.write(uri,'# 测试资料\n\n'+quote,ctx=a)
    plan=await p.prepare_sources(e.ctx,collection,[uri],input_kind='uri',idempotency_key=key)
    source='raw_refs' if kind=='Device' else 'sources'
    metadata={'title':title,'id':'temporary-'+key,'business_code':code,'model':model,
              **({'pressure':pressure} if kind!='Fault' else {}),source:[uri]}
    if metadata_changes:
        metadata_changes(metadata)
    candidate={'kind':kind,'relative_path':kind+'/'+key+'.md',
               'markdown':'---\n'+yaml.safe_dump(metadata,allow_unicode=True,sort_keys=False)+'---\n## 来源\n'+quote+f' [原文]({uri})',
               'evidence':[{'claim':quote,'quote':quote,'source_uri':uri}]}
    llm.get_completion_async.return_value=json.dumps({'entities':[candidate]},ensure_ascii=False)
    result=await x.extract(e.ctx,plan.build_id,expected_plan_digest=plan.digest)
    return DraftSelection(plan.build_id,plan.digest,tuple(result['receipts']))


async def merged(env,*selections):
    e,p,c,a,b,bundle,llm,x,m=env
    receipt=await m.merge(e.ctx,selections)
    data=await m.read_merge(e.ctx,receipt,expected_revision=receipt['revision'])
    return receipt,data


async def test_same_explicit_identity_and_scope_groups_synonym_names_preserving_all_evidence(merging):
    one=await extracted(merging,ONE,title='主泵',key='one')
    two=await extracted(merging,TWO,title='液压泵',key='two')
    receipt,data=await merged(merging,one,two)
    assert receipt['group_count']==1 and receipt['conflict_count']==0 and receipt['pending_identity_count']==0
    group=data['groups'][0]
    assert set(group['contributors'])=={ONE,TWO}
    assert set(group['alias_suggestions'])=={'主泵','液压泵'} and not group['alias_ownership_verified']
    assert set(group['provisional_ids'])=={'temporary-one','temporary-two'}
    pressure=group['field_observations']['pressure']
    assert len(pressure)==1 and len(pressure[0]['provenance'])==2
    assert len(group['body_variants'])==len(group['evidence'])==2
    assert group['identity']['components']['applicability_scope']=={'model':'SY215C'}
    assert data['private'] and not data['diagnostic_publishable'] and data['human_revision_cas']=='not_supplied'
    assert data['valid_graph']==[group['candidate_id']] and not data['excluded_from_valid_graph']
    assert group['in_valid_graph'] and data['alias_ownership']=={'主泵':group['candidate_id'],'液压泵':group['candidate_id']}
    with pytest.raises(NotFoundError):
        await merging[0].fs.stat(TARGET+'/Component/one.md',ctx=merging[0].ctx)


async def test_same_name_different_business_entities_never_merge(merging):
    one=await extracted(merging,ONE,code='P-001',key='one')
    two=await extracted(merging,TWO,code='P-002',key='two')
    receipt,data=await merged(merging,one,two)
    assert receipt['group_count']==2
    assert len(data['alias_collisions']['主泵'])==2
    assert len({group['candidate_id'] for group in data['groups']})==2


async def test_same_code_different_model_applicability_never_merge(merging):
    one=await extracted(merging,ONE,model='SY215C',key='one')
    two=await extracted(merging,TWO,model='SY215C-Pro',key='two')
    receipt,data=await merged(merging,one,two)
    assert receipt['group_count']==2
    assert {group['identity']['components']['applicability_scope']['model'] for group in data['groups']}=={'SY215C','SY215C-Pro'}


async def test_same_business_code_different_type_keeps_distinct_identity_and_source_fields(merging):
    one=await extracted(merging,ONE,kind='Component',key='one')
    two=await extracted(merging,TWO,kind='Device',key='two')
    receipt,data=await merged(merging,one,two)
    assert receipt['group_count']==2
    device=next(group for group in data['groups'] if group['kind']=='Device')
    assert device['source_fields']==['raw_refs'] and 'sources' not in device['field_observations']


async def test_contradictory_parameters_preserve_values_and_do_not_choose_a_winner(merging):
    one=await extracted(merging,ONE,pressure=10,key='one')
    two=await extracted(merging,TWO,pressure=12,key='two')
    receipt,data=await merged(merging,one,two)
    assert receipt['group_count']==receipt['conflict_count']==1
    group=data['groups'][0]
    assert group['state']=='conflict' and group['conflicts']==['pressure']
    assert {entry['value'] for entry in group['field_observations']['pressure']}=={10,12}
    assert all(entry['provenance'] for entry in group['field_observations']['pressure'])
    assert 'selected_fields' not in group and not group['diagnostic_publishable']


@pytest.mark.parametrize('field',['business_code','model'])
async def test_missing_key_or_scope_does_not_guess_from_model_id_or_title(merging,field):
    one=await extracted(merging,ONE,key='one',metadata_changes=lambda m:m.pop(field))
    two=await extracted(merging,TWO,key='two',metadata_changes=lambda m:m.pop(field))
    receipt,data=await merged(merging,one,two)
    assert receipt['group_count']==receipt['pending_identity_count']==2
    assert all(group['identity']['reason']=='identity_field_missing:'+field for group in data['groups'])
    assert all(not group['alias_suggestions'] for group in data['groups'])


@pytest.mark.parametrize('changes,reason',[
    ({'code':'P-001','quoted_code':'P-001-extra'},'business_code'),
    ({'model':'SY215','quoted_model':'SY215C'},'model'),
    ({'code':'fabricated','quoted_code':'P-001'},'business_code'),
])
async def test_identity_values_require_actual_quote_with_identifier_boundaries(merging,changes,reason):
    one=await extracted(merging,ONE,key='one',**changes)
    receipt,data=await merged(merging,one)
    assert receipt['pending_identity_count']==1
    assert data['groups'][0]['identity']['reason']=='identity_evidence_missing:'+reason


async def test_exact_candidate_replays_do_not_create_duplicates(merging):
    one=await extracted(merging,ONE,key='one')
    receipt,data=await merged(merging,one,one)
    assert receipt['group_count']==1 and len(data['inputs'])==1
    assert len(data['groups'][0]['body_variants'])==1
    second,data2=await merged(merging,one)
    assert receipt['revision']==second['revision'] and data==data2


async def test_cross_namespace_merge_is_rejected_even_when_both_inputs_authorized(merging):
    e,p,c,a,b,bundle,llm,x,m=merging
    one=await extracted(merging,ONE,key='one')
    other=TARGET+'-other'
    await install(e,p,c,a,b,target=other,namespace='merge-other',collection='parts-other')
    two=await extracted(merging,TWO,key='two',target=other,collection='parts-other')
    with pytest.raises(SchemaError,match='cannot cross target namespace'):
        await m.merge(e.ctx,[one,two])


async def test_private_read_rechecks_all_sources_and_does_not_fall_back_to_remaining_source(merging):
    e,p,c,a,b,bundle,llm,x,m=merging
    one=await extracted(merging,ONE,key='one')
    two=await extracted(merging,TWO,key='two')
    receipt,data=await merged(merging,one,two)
    b.acl(TWO,[{'principal':'user:other','level':'read'}])
    with pytest.raises(PermissionDeniedError):
        await m.read_merge(e.ctx,receipt,expected_revision=receipt['revision'])


async def test_wider_target_does_not_disclose_merged_private_sources(merging):
    e,p,c,a,b,bundle,llm,x,m=merging
    one=await extracted(merging,ONE,key='one')
    receipt,data=await merged(merging,one)
    b.acl(TARGET,[{'principal':'user:user','level':'write'},{'principal':'user:other','level':'write'}])
    other=replace(e.ctx,user=UserIdentifier('acct','other'))
    with pytest.raises(PermissionDeniedError):
        await m.read_merge(other,receipt,expected_revision=receipt['revision'])


async def test_revoke_export_or_disable_acl_refuses_private_merge_read(merging):
    e,p,c,a,b,bundle,llm,x,m=merging
    one=await extracted(merging,ONE,key='one')
    receipt,data=await merged(merging,one)
    await c.set_export(a,source_namespace='manuals',target_namespace='merge',principal='user:user',allowed=False)
    with pytest.raises(PermissionDeniedError):
        await m.read_merge(e.ctx,receipt,expected_revision=receipt['revision'])
    await c.set_export(a,source_namespace='manuals',target_namespace='merge',principal='user:user',allowed=True)
    e.fs.acl_manager.set_enabled('acct',False)
    with pytest.raises((PermissionDeniedError,FailedPreconditionError)):
        await m.read_merge(e.ctx,receipt,expected_revision=receipt['revision'])


async def test_standard_same_bytes_write_invalidates_merged_source_epoch(merging):
    e,p,c,a,b,bundle,llm,x,m=merging
    one=await extracted(merging,ONE,key='one')
    receipt,data=await merged(merging,one)
    text=await p.read_source(e.ctx,one.build_id,ONE)
    await e.fs.write(ONE,text,ctx=a)
    with pytest.raises(ConflictError,match='input changed'):
        await m.read_merge(e.ctx,receipt,expected_revision=receipt['revision'])


async def test_revision_mismatch_corrupted_blob_and_cross_account_never_return_content(merging):
    e,p,c,a,b,bundle,llm,x,m=merging
    one=await extracted(merging,ONE,key='one')
    receipt,data=await merged(merging,one)
    with pytest.raises(ConflictError):
        await m.read_merge(e.ctx,receipt,expected_revision='0'*64)
    other=replace(e.ctx,user=UserIdentifier('other','user'))
    with pytest.raises((NotFoundError,FileNotFoundError,PermissionDeniedError)):
        await m.read_merge(other,receipt,expected_revision=receipt['revision'])
    await e.fs._async_agfs.write(blob_path(e.fs,e.ctx,receipt['reference']['sha256']),b'corrupted')
    with pytest.raises(SchemaError,match='corrupted'):
        await m.read_merge(e.ctx,receipt,expected_revision=receipt['revision'])


async def test_merge_limits_and_public_profile_refuse_before_retention(merging):
    e,p,c,a,b,bundle,llm,x,m=merging
    one=await extracted(merging,ONE,key='one')
    with pytest.raises(SchemaError,match='Only private'):
        await m.merge(e.ctx,[one],profile='strict_diagnostic')
    tiny=PrivateWikiMerger(x,limits=MergeLimits(total_input_bytes=1))
    with pytest.raises(SchemaError,match='input byte bound'):
        await tiny.merge(e.ctx,[one])


@pytest.mark.parametrize('change',[
    lambda p:p['entities'][0].update(key_fields=['title']),
    lambda p:p['entities'][0].update(key_fields=['id']),
    lambda p:p['entities'][0].update(scope_fields=['not_declared']),
    lambda p:p['entities'][0].update(normalization='fuzzy'),
    lambda p:p['entities'][0].update(scope_fields=['business_code']),
    lambda p:p.update(version=True),
])
def test_unsupported_identity_declarations_fail_closed(change):
    files=schema_files();policy=yaml.safe_load(files['identity.policy.yaml']);change(policy)
    files['identity.policy.yaml']=yaml.safe_dump(policy)
    with pytest.raises(SchemaError):
        IdentityPolicy.compile(SchemaBundle.freeze('parts','1',files))


def test_unconfigured_policy_remains_pending_and_tuple_separates_schema_account_namespace():
    bundle=SchemaBundle.freeze('parts','1',schema_files(with_policy=False))
    policy=IdentityPolicy.compile(bundle)
    assert resolve_candidate_identity(policy,bundle,{'id':'model-temp','title':'主泵'},[],account_id='a',namespace_id='n',kind='Component')['state']=='pending_identity'
    bundle=SchemaBundle.freeze('parts','1',schema_files())
    policy=IdentityPolicy.compile(bundle)
    meta={'business_code':'P-001','model':'SY215C'};proof=[{'quote':'P-001 SY215C'}]
    base=dict(account_id='a',namespace_id='n',kind='Component')
    first=resolve_candidate_identity(policy,bundle,meta,proof,**base)['canonical_id']
    for changes in ({'account_id':'b'},{'namespace_id':'other'},{'kind':'Device'}):
        assert resolve_candidate_identity(policy,bundle,meta,proof,**{**base,**changes})['canonical_id']!=first
    next_bundle=SchemaBundle.freeze('parts','2',schema_files())
    assert resolve_candidate_identity(IdentityPolicy.compile(next_bundle),next_bundle,meta,proof,**base)['canonical_id']!=first


def test_normalization_is_explicit_and_never_synonym_or_near_name_matching():
    bundle=SchemaBundle.freeze('parts','1',schema_files(normalization='casefold_space'))
    policy=IdentityPolicy.compile(bundle)
    args=dict(account_id='a',namespace_id='n',kind='Component')
    proof=[{'quote':'P-001 SY215C'}]
    one=resolve_candidate_identity(policy,bundle,{'business_code':'P-001','model':'SY215C'},proof,**args)
    two=resolve_candidate_identity(policy,bundle,{'business_code':'p-001','model':'sy215c'},proof,**args)
    assert one['canonical_id']==two['canonical_id']
    exact=SchemaBundle.freeze('parts','1',schema_files(normalization='exact'))
    exact_policy=IdentityPolicy.compile(exact)
    lower=resolve_candidate_identity(exact_policy,exact,{'business_code':'p-001','model':'sy215c'},[{'quote':'p-001 sy215c'}],**args)
    upper=resolve_candidate_identity(exact_policy,exact,{'business_code':'P-001','model':'SY215C'},proof,**args)
    assert lower['canonical_id']!=upper['canonical_id']


@pytest.mark.parametrize('merging',['casefold_space'],indirect=True)
async def test_explicit_casefold_business_codes_group_without_false_parameter_conflict(merging):
    one=await extracted(merging,ONE,code='P-001',model='SY215C',key='one')
    two=await extracted(merging,TWO,code='p-001',model='sy215c',key='two')
    receipt,data=await merged(merging,one,two)
    assert receipt['group_count']==1 and receipt['conflict_count']==0
    assert len(data['groups'][0]['field_observations']['business_code'])==2  # Raw spelling survives.
    assert data['groups'][0]['identity']['components']['business_key']=={'business_code':'p-001'}


@pytest.mark.parametrize('merging',[None],indirect=True)
async def test_missing_identity_policy_does_not_infer_from_matching_code_title_or_id(merging):
    one=await extracted(merging,ONE,key='one')
    two=await extracted(merging,TWO,key='two')
    receipt,data=await merged(merging,one,two)
    assert receipt['group_count']==receipt['pending_identity_count']==2
    assert all(group['identity']['reason']=='identity_policy_missing' for group in data['groups'])


async def test_explicit_rebuild_with_authorized_remaining_source_keeps_identity_but_drops_revoked_contribution(merging):
    e,p,c,a,b,bundle,llm,x,m=merging
    one=await extracted(merging,ONE,key='one')
    two=await extracted(merging,TWO,key='two')
    old,old_data=await merged(merging,one,two)
    b.acl(TWO,[{'principal':'user:other','level':'read'}])
    with pytest.raises(PermissionDeniedError):
        await m.read_merge(e.ctx,old,expected_revision=old['revision'])
    new,new_data=await merged(merging,one)
    assert old_data['groups'][0]['candidate_id']==new_data['groups'][0]['candidate_id']
    assert new_data['groups'][0]['contributors']==[ONE]
    assert len(new_data['groups'][0]['evidence'])==1
    assert new['private'] and not new['diagnostic_publishable']


async def published(env,*selections,**merge_kwargs):
    e,p,c,a,b,bundle,llm,x,m=env
    receipt,data=await merged(env,*selections)
    result=await m.publish(e.ctx,receipt,expected_revision=receipt['revision'],**merge_kwargs)
    return receipt,data,result


async def test_publish_materializes_each_admitted_body_variant_as_real_md_node(merging):
    e,p,c,a,b,bundle,llm,x,m=merging
    one=await extracted(merging,ONE,title='主泵',key='one')
    two=await extracted(merging,TWO,title='液压泵',key='two')
    receipt,data,result=await published(merging,one,two)
    assert result['state']=='private_published' and result['private'] and not result['diagnostic_publishable']
    assert result['group_count']==1 and result['published_count']==2
    assert {page['uri'] for page in result['pages']}=={TARGET+'/Component/one.md',TARGET+'/Component/two.md'}
    for page in result['pages']:
        node=await e.fs.read(page['uri'],ctx=e.ctx)
        node=node.decode() if isinstance(node,bytes) else node
        assert '标准压力 10 MPa。' in node and '[原文](' in node and 'business_code: P-001' in node
    # Same revision republish is idempotent: content identical, no rewrite failures.
    again=await m.publish(e.ctx,receipt,expected_revision=receipt['revision'])
    assert again['published_count']==0 and again['group_count']==1


async def test_publish_never_materializes_unresolved_conflict_or_pending_identity_groups(merging):
    e,p,c,a,b,bundle,llm,x,m=merging
    conflict_one=await extracted(merging,ONE,pressure=10,key='one')
    conflict_two=await extracted(merging,TWO,pressure=12,key='two')
    receipt,data=await merged(merging,conflict_one,conflict_two)
    assert data['valid_graph']==[]
    result=await m.publish(e.ctx,receipt,expected_revision=receipt['revision'])
    assert result['pages']==[] and result['published_count']==0
    with pytest.raises(NotFoundError):
        await e.fs.read(TARGET+'/Component/one.md',ctx=e.ctx)
    pending_one=await extracted(merging,ONE,key='p1',metadata_changes=lambda meta:meta.pop('business_code'))
    pending_two=await extracted(merging,TWO,key='p2',metadata_changes=lambda meta:meta.pop('business_code'))
    preceipt,pdata=await merged(merging,pending_one,pending_two)
    presult=await m.publish(e.ctx,preceipt,expected_revision=preceipt['revision'])
    assert presult['pages']==[] and presult['published_count']==0


async def test_publish_refuses_modified_human_page_and_stale_revision(merging):
    e,p,c,a,b,bundle,llm,x,m=merging
    one=await extracted(merging,ONE,key='one')
    receipt,data,result=await published(merging,one)
    uri=result['pages'][0]['uri']
    await e.fs.write(uri,'# human edited\n',ctx=e.ctx)
    with pytest.raises(ConflictError,match='later edit'):
        await m.publish(e.ctx,receipt,expected_revision=receipt['revision'])
    with pytest.raises(ConflictError):
        await m.publish(e.ctx,receipt,expected_revision='0'*64)


async def test_publish_requires_authorized_context_and_live_source_permissions(merging):
    e,p,c,a,b,bundle,llm,x,m=merging
    one=await extracted(merging,ONE,key='one')
    receipt,data=await merged(merging,one)
    other=replace(e.ctx,user=UserIdentifier('acct','other'))
    with pytest.raises(PermissionDeniedError):
        await m.publish(other,receipt,expected_revision=receipt['revision'])
    b.acl(ONE,[{'principal':'user:other','level':'read'}])
    with pytest.raises(PermissionDeniedError):
        await m.publish(e.ctx,receipt,expected_revision=receipt['revision'])
    with pytest.raises(NotFoundError):
        await e.fs.read(TARGET+'/Component/one.md',ctx=e.ctx)


async def test_source_read_memory_bound_is_checked_before_retaining_merge(merging):
    e,p,c,a,b,bundle,llm,x,m=merging
    one=await extracted(merging,ONE,key='one')
    limited=PrivateWikiMerger(x,limits=MergeLimits(total_source_bytes=1))
    with pytest.raises(SchemaError,match='source byte bound'):
        await limited.merge(e.ctx,[one])


async def test_publish_preflight_dangling_relation_target_blocks_whole_batch(merging):
    e,p,c,a,b,bundle,llm,x,m=merging
    # Fault declares asserted_devices; the target is neither in this batch nor on disk,
    # so closure must abort before any page of the batch is written.
    missing=target_uri=TARGET+'/Device/absent.md'
    device=await extracted(merging,ONE,key='dev',kind='Device',title='主泵')
    fault=await extracted(merging,TWO,key='f',kind='Fault',title='异响故障',
                          metadata_changes=lambda meta:meta.update(asserted_devices=[{'device_uri':missing}]))
    receipt,data=await merged(merging,device,fault)
    assert data['valid_graph']
    with pytest.raises(SchemaError,match='relation target is missing'):
        await m.publish(e.ctx,receipt,expected_revision=receipt['revision'])
    with pytest.raises(NotFoundError):
        await e.fs.read(TARGET+'/Device/dev.md',ctx=e.ctx)
    with pytest.raises(NotFoundError):
        await e.fs.read(TARGET+'/Fault/f.md',ctx=e.ctx)


async def test_publish_enqueues_page_chunks_through_shared_text_plan(embedding, merging):
    from openviking.storage.collection_schemas import TextEmbeddingHandler
    from openviking.storage.vector_ids import vector_record_id_for_data

    e,p,c,a,b,bundle,llm,x,m=merging
    one=await extracted(merging,ONE,title='主泵',key='one')
    two=await extracted(merging,TWO,title='液压泵',key='two')
    receipt,data,result=await published(merging,one,two)
    assert result['indexed_chunks']>0 and len(e.queue.messages)==result['indexed_chunks']
    uris={TARGET+'/Component/one.md',TARGET+'/Component/two.md'}
    assert {msg.context_data['uri'] for msg in e.queue.messages}==uris
    assert all(msg.context_data['level']==2 for msg in e.queue.messages)
    assert all(msg.context_data['source_uri'] in uris for msg in e.queue.messages)
    plan_ids={msg.context_data['index_plan_id'] for msg in e.queue.messages}
    assert len(plan_ids)==2
    # Registry pinned per-page generations; the snapshot was verified against disk.
    for msg in e.queue.messages:
        state=e.store.state(msg.context_data['index_generation'])
        assert state['uri']==msg.context_data['uri'] and state['plan']==msg.context_data['index_plan_id']
        disk=await e.fs.read_file_bytes(msg.context_data['uri'],ctx=e.ctx)
        assert msg.context_data['content_revision']==hashlib.sha256(disk).hexdigest()
    # The shared consumer indexes every chunk; nothing else is retrievable.
    database=e.handler._vikingdb
    for msg in e.queue.messages:
        outcome=await e.handler.on_dequeue({'data':msg.to_json()})
        assert outcome.outcome.value=='success',outcome.error
    assert len(database.records)==len(e.queue.messages)
    assert {r['uri'] for r in database.records.values()}==uris
    # Each page's evidence quote survives chunking exactly once across its chunks.
    chunk_text=''.join(r['chunk_text'] for r in database.records.values())
    assert chunk_text.count('标准压力 10 MPa。')==2
    assert all(r['id']==vector_record_id_for_data(r) for r in database.records.values())


async def test_publish_crash_mid_write_replay_indexes_whole_batch(embedding, merging):
    e,p,c,a,b,bundle,llm,x,m=merging
    one=await extracted(merging,ONE,title='主泵',key='one')
    two=await extracted(merging,TWO,title='液压泵',key='two')
    receipt,data=await merged(merging,one,two)
    uris={TARGET+'/Component/one.md',TARGET+'/Component/two.md'}

    real_write=e.fs.write
    writes={'n':0}
    async def crashing_write(uri,content,ctx=None,**kw):
        writes['n']+=1
        if writes['n']>1:
            raise RuntimeError('simulated crash between page writes')
        return await real_write(uri,content,ctx=ctx,**kw)
    e.fs.write=crashing_write
    try:
        with pytest.raises(RuntimeError,match='simulated crash'):
            await m.publish(e.ctx,receipt,expected_revision=receipt['revision'])
    finally:
        e.fs.write=real_write
    # Crash left exactly one page on disk and nothing in the index queue.
    assert writes['n']==2 and e.queue.messages==[]
    present=set()
    for u in uris:
        try:
            await e.fs.read(u,ctx=e.ctx)
            present.add(u)
        except NotFoundError:
            pass
    assert len(present)==1

    again=await m.publish(e.ctx,receipt,expected_revision=receipt['revision'])
    assert again['published_count']==1 and again['indexed_chunks']>0
    # Replay heals the batch: every planned page reaches the index, including
    # the page the crashed attempt had already written but never enqueued.
    assert {msg.context_data['uri'] for msg in e.queue.messages}==uris
    for msg in e.queue.messages:
        state=e.store.state(msg.context_data['index_generation'])
        assert state['uri']==msg.context_data['uri']
        disk=await e.fs.read_file_bytes(msg.context_data['uri'],ctx=e.ctx)
        assert msg.context_data['content_revision']==hashlib.sha256(disk).hexdigest()
    for u in uris:
        node=await e.fs.read(u,ctx=e.ctx)
        node=node.decode() if isinstance(node,bytes) else node
        assert '标准压力 10 MPa。' in node



async def test_publish_closes_relations_against_same_batch_pages(merging):
    e,p,c,a,b,bundle,llm,x,m=merging
    device_uri=TARGET+'/Device/dev.md'
    device=await extracted(merging,ONE,key='dev',kind='Device',title='主泵')
    fault=await extracted(merging,TWO,key='f',kind='Fault',title='异响故障',
                          metadata_changes=lambda meta:meta.update(asserted_devices=[{'device_uri':device_uri}]))
    receipt,data,result=await published(merging,device,fault)
    assert result['published_count']==2
    assert {page['uri'] for page in result['pages']}=={TARGET+'/Device/dev.md',TARGET+'/Fault/f.md'}
    node=await e.fs.read(device_uri,ctx=e.ctx)
    node=node.decode() if isinstance(node,bytes) else node
    assert 'business_code: P-001' in node
