"""Source-aware checks reject invented targets; structure alone is not publication."""
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from openviking.wiki.schema import EntitySchema, Field, SchemaError
from openviking.wiki.validation import ResolvedReference, inspect_entity, resolve_references, source_uri

URI = 'viking://resources/wiki/Symptom/smoke.md'
FAULT = 'viking://resources/wiki/Fault/fuel.md'
SOURCE = 'viking://resources/raw/manual/engine.md'
SCHEMA = EntitySchema('Symptom', (Field('title', 'string', True, None), Field('status', 'string', True, None, ('active',)),
                                     Field('asserted_faults', 'list', False, 'Fault'), Field('sources', 'list', True, 'source')),
                      ('现象定义', '来源'), ('profile_backing_required',))
MD = f'''---
title: 冒白烟
status: active
asserted_faults:
  - fault_uri: {FAULT}
sources:
  - {SOURCE}
---
## 现象定义
白烟。[原文]({SOURCE})
## 来源
原手册。
'''


def inspect(content=MD):
    return inspect_entity(SCHEMA, URI, content, reference_keys={'asserted_faults': 'fault_uri'})


def test_nested_edges_and_sources_are_enumerated_without_losing_uncompiled_rules():
    draft = inspect()
    assert {(r.uri, r.expected_kind, r.field) for r in draft.references} == {
        (FAULT, 'Fault', 'asserted_faults'), (SOURCE, 'source', 'sources'), (SOURCE, None, 'body')}
    assert draft.uncompiled_rules == ('profile_backing_required',)
    assert inspect(MD + '\nextra').content_hash != draft.content_hash


@pytest.mark.parametrize('old,new', [('status: active', 'status: invented'), ('title: 冒白烟', 'title: 17'),
                                    ('title: 冒白烟', 'title: ""'), ('status: active', 'status: active\nextra_field: value'),
                                    ('title: 冒白烟', 'title: 冒白烟\ntitle: duplicate')])
def test_invalid_fields_are_rejected(old, new):
    with pytest.raises(SchemaError):
        inspect(MD.replace(old,new))


def test_code_fence_heading_does_not_satisfy_required_section():
    with pytest.raises(SchemaError, match='section'):
        inspect(MD.replace('## 来源\n原手册。', '```md\n## 来源\n```'))
    with pytest.raises(SchemaError, match='out of order'):
        inspect(MD.replace('## 现象定义', '## tmp').replace('## 来源', '## 现象定义').replace('## tmp', '## 来源'))


@pytest.mark.parametrize('uri', [SOURCE+'#line', SOURCE+'?version=1', 'https://example.com/doc',
                                'viking://resources/wiki/%2e%2e/f.md', 'viking://resources/wiki/%252e%252e/f.md',
                                'viking://resources/wiki/%2fother/f.md', 'viking://resources/wiki//f.md',
                                'viking://resources/wiki/%00/f.md', 'viking://resources/'])
def test_reference_uri_is_readable_canonical_resource_without_position(uri):
    with pytest.raises(SchemaError):
        source_uri(uri)


def test_nested_relation_shape_must_be_explicit_and_exact():
    with pytest.raises(SchemaError):
        inspect_entity(SCHEMA, URI, MD)
    with pytest.raises(SchemaError):
        inspect(MD.replace('fault_uri:', 'scope: guessed\n    fault_uri:'))


async def test_authorized_external_sources_allowed_but_edges_stay_in_collection():
    fault = ResolvedReference('Fault','acct','wiki','fault-v1',True,True,True,'a'*64)
    source = ResolvedReference(None,'acct','raw','source-v1',True,True)
    resolver = AsyncMock(side_effect=lambda uri: fault if uri == FAULT else source)
    dependencies = dict(await resolve_references(inspect(), resolver,account_id='acct',namespace_id='wiki',schema_digest='a'*64))
    assert dependencies[FAULT].revision == 'fault-v1' and dependencies[SOURCE].namespace_id == 'raw'
    for bad in [replace(fault,namespace_id='other'), replace(fault,account_id='other'), replace(fault,kind='Component')]:
        with pytest.raises(SchemaError,match='mismatch'):
            await resolve_references(inspect(),AsyncMock(return_value=bad),account_id='acct',namespace_id='wiki',schema_digest='a'*64)


@pytest.mark.parametrize('change', [{'authorized':False},{'has_content':False},{'revision':''}])
async def test_planned_names_inaccessible_or_unversioned_targets_are_not_evidence(change):
    target=ResolvedReference('Fault','acct','wiki','v1',True,True,True,'a'*64)
    with pytest.raises(SchemaError,match='actual content'):
        await resolve_references(inspect(),AsyncMock(return_value=replace(target,**change)),account_id='acct',namespace_id='wiki',schema_digest='a'*64)


async def test_source_changes_between_frontmatter_and_body_validation_fail():
    fault=ResolvedReference('Fault','acct','wiki','v1',True,True,True,'a'*64)
    source=ResolvedReference(None,'acct','raw','v1',True,True)
    resolver=AsyncMock(side_effect=[fault,source,replace(source,revision='v2')])
    with pytest.raises(SchemaError,match='changed'):
        await resolve_references(inspect(),resolver,account_id='acct',namespace_id='wiki',schema_digest='a'*64)


def test_encoded_unicode_reference_and_raw_uri_share_dependency_identity():
    assert source_uri('viking://resources/raw/%E4%B8%AD%E6%96%87.md') == 'viking://resources/raw/中文.md'
    with pytest.raises(SchemaError, match='encoding'):
        source_uri('viking://resources/raw/%ff.md')


def test_empty_declared_sources_cannot_be_replaced_by_body_link_only():
    with pytest.raises(SchemaError, match='declared source'):
        inspect(MD.replace('sources:\n  - '+SOURCE, 'sources: []'))


@pytest.mark.parametrize('changes', [{'entity_validated':False},{'validation_schema_hash':'b'*64}])
async def test_existing_entity_without_fixed_schema_validation_is_not_a_valid_target(changes):
    target=ResolvedReference('Fault','acct','wiki','v1',True,True,True,'a'*64)
    with pytest.raises(SchemaError,match='fixed schema'):
        await resolve_references(inspect(),AsyncMock(return_value=replace(target,**changes)),account_id='acct',namespace_id='wiki',schema_digest='a'*64)


@pytest.mark.parametrize('changes', [{'account_id':'other'}, {'namespace_id':''}, {'namespace_id':None}])
async def test_source_reference_requires_same_account_and_actual_namespace(changes):
    fault=ResolvedReference('Fault','acct','wiki','fault-v1',True,True,True,'a'*64)
    source=replace(ResolvedReference(None,'acct','raw','source-v1',True,True),**changes)
    resolver=AsyncMock(side_effect=lambda uri: fault if uri==FAULT else source)
    with pytest.raises(SchemaError,match='account mismatch|namespace is not registered'):
        await resolve_references(inspect(),resolver,account_id='acct',namespace_id='wiki',schema_digest='a'*64)
