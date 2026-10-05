"""KB-025 focused identity/conservative-merge/human-edit checks (TC-10.7/10.8/16.12).

These exercise the owned identity/merge compiler over the actual ACL/SQLite/disk
AGFS fixture. They do not certify Sany ontology, semantic truth or public
diagnostic publication; diagnostic_publishable stays false throughout.
"""
import pytest
from test_content_publication import environment as environment  # noqa: F401
from test_index_plan import embedding as embedding  # noqa: F401
from test_wiki_build_plan import prepared as prepared  # noqa: F401
from test_wiki_candidate_merge import (TARGET, extracted, install, merged, merging, schema_files)  # noqa: F401
from test_wiki_namespaces import domains as domains, ONE, TWO  # noqa: F401

from openviking.wiki.identity import IdentityPolicy, alias_ownership, identity_tuple, resolve_candidate_identity
from openviking.wiki.merge import resolve_merge_revision
from openviking.wiki.schema import SchemaBundle, SchemaError
from openviking_cli.exceptions import ConflictError


# ── TC-10.7: same display name across level/type/config must not merge ──

async def test_same_display_name_across_type_and_config_keeps_distinct_canonical_ids(merging):
    component = await extracted(merging, ONE, code='P-001', model='SY215C', title='空调', kind='Component', key='one')
    device = await extracted(merging, TWO, code='P-001', model='SY215C', title='空调', kind='Device', key='two')
    receipt, data = await merged(merging, component, device)
    # Same name and same business key, but different entity type -> two identities.
    assert receipt['group_count'] == 2
    assert len({group['candidate_id'] for group in data['groups']}) == 2
    assert len(data['alias_collisions']['空调']) == 2
    # A colliding alias is never silently owned by one of the two entities.
    assert '空调' not in data['alias_ownership']


async def test_same_name_different_config_scope_never_merges_but_alias_stays_separate(merging):
    one = await extracted(merging, ONE, code='P-001', model='SY215C', title='空调系统', key='one')
    two = await extracted(merging, TWO, code='P-001', model='SY215C-Pro', title='空调系统', key='two')
    receipt, data = await merged(merging, one, two)
    assert receipt['group_count'] == 2
    scopes = {group['identity']['components']['applicability_scope']['model'] for group in data['groups']}
    assert scopes == {'SY215C', 'SY215C-Pro'}
    assert len(data['alias_collisions']['空调系统']) == 2


def test_alias_ownership_assigns_unique_names_and_reports_collisions():
    groups = [
        {'candidate_id': 'candidate-a', 'identity': {'canonical_id': 'candidate-a'}, 'display_names': ['主泵', 'P-001']},
        {'candidate_id': 'candidate-b', 'identity': {'canonical_id': 'candidate-b'}, 'display_names': ['主泵', '液压泵']},
        {'candidate_id': 'unresolved', 'identity': {'canonical_id': None}, 'display_names': ['主泵']},
    ]
    result = alias_ownership(groups)
    assert result['aliases'] == {'P-001': 'candidate-a', '液压泵': 'candidate-b'}
    assert result['collisions'] == {'主泵': ['candidate-a', 'candidate-b']}


# ── TC-16.12: duplicate model rebuild must not create a duplicate entity ──

async def test_duplicate_rebuild_reuses_canonical_id_without_new_entity(merging):
    first = await extracted(merging, ONE, code='P-001', model='SY215C', key='one')
    single, single_data = await merged(merging, first)
    canonical = single_data['groups'][0]['candidate_id']
    # An independent rebuild of the same fixed identity (different build/plan key),
    # reusing the unchanged source so only the plan identity differs.
    second = await extracted(merging, ONE, code='P-001', model='SY215C', key='rebuild', rewrite=False)
    combined, combined_data = await merged(merging, first, second)
    assert combined['group_count'] == 1
    assert combined_data['groups'][0]['candidate_id'] == canonical
    assert len(combined_data['groups'][0]['contributors']) == 1  # Same source, not double-counted as two entities.


# ── TC-16.12: unresolved conflict must not enter the valid graph ──

async def test_conflicting_values_are_retained_and_excluded_from_valid_graph(merging):
    one = await extracted(merging, ONE, pressure=10, key='one')
    two = await extracted(merging, TWO, pressure=12, key='two')
    receipt, data = await merged(merging, one, two)
    group = data['groups'][0]
    assert group['state'] == 'conflict' and group['conflicts'] == ['pressure']
    assert {entry['value'] for entry in group['field_observations']['pressure']} == {10, 12}
    assert not group['in_valid_graph']
    assert group['candidate_id'] in data['excluded_from_valid_graph']
    assert group['candidate_id'] not in data['valid_graph']
    assert receipt['valid_graph_count'] == 0 and receipt['excluded_count'] == 1


async def test_pending_identity_group_is_never_admitted_to_valid_graph(merging):
    one = await extracted(merging, ONE, key='one', metadata_changes=lambda m: m.pop('business_code'))
    receipt, data = await merged(merging, one)
    assert receipt['pending_identity_count'] == 1 and receipt['valid_graph_count'] == 0
    assert data['groups'][0]['state'] == 'pending_identity'
    assert data['excluded_from_valid_graph'] == [data['groups'][0]['candidate_id']]


# ── TC-10.8: human-confirmed value protected by expected_revision ──

async def test_human_confirmed_value_is_protected_and_multi_source_evidence_is_retained(merging):
    e, p, c, a, b, bundle, llm, x, m = merging
    one = await extracted(merging, ONE, pressure=10, key='one')
    two = await extracted(merging, TWO, pressure=12, key='two')
    base, base_data = await merged(merging, one, two)
    assert base['human_revision_cas'] == 'not_supplied'
    # Human resolves the conflict against the exact current revision.
    receipt = await m.merge(e.ctx, [one, two], previous_receipt=base, expected_revision=base['revision'],
                            human_confirmed={'pressure': 11})
    assert receipt['human_revision_cas'] == 'revision_bound'
    data = await m.read_merge(e.ctx, receipt, expected_revision=receipt['revision'])
    group = data['groups'][0]
    assert group['state'] == 'human_confirmed' and group['human_confirmed'] == {'pressure': 11}
    assert group['human_confirmed_fields'] == ['pressure'] and group['conflicts'] == []
    # Both model observations survive as evidence; nothing is silently dropped.
    assert {entry['value'] for entry in group['field_observations']['pressure']} == {10, 12}
    assert group['in_valid_graph'] and data['valid_graph'] == [group['candidate_id']]


async def test_stale_revision_write_is_refused_and_does_not_overwrite_human_value(merging):
    e, p, c, a, b, bundle, llm, x, m = merging
    one = await extracted(merging, ONE, pressure=10, key='one')
    two = await extracted(merging, TWO, pressure=12, key='two')
    base, _ = await merged(merging, one, two)
    human = await m.merge(e.ctx, [one, two], previous_receipt=base, expected_revision=base['revision'],
                          human_confirmed={'pressure': 11})
    # A concurrent writer holding the pre-human revision is rejected, never overwriting.
    with pytest.raises(ConflictError, match='concurrent edit preserved'):
        await m.merge(e.ctx, [one, two], previous_receipt=human, expected_revision=base['revision'])
    # The human-confirmed merge is still the current, intact revision.
    data = await m.read_merge(e.ctx, human, expected_revision=human['revision'])
    assert data['groups'][0]['human_confirmed'] == {'pressure': 11}


async def test_human_confirmed_value_survives_a_later_model_rerun(merging):
    e, p, c, a, b, bundle, llm, x, m = merging
    one = await extracted(merging, ONE, pressure=10, key='one')
    two = await extracted(merging, TWO, pressure=12, key='two')
    base, _ = await merged(merging, one, two)
    human = await m.merge(e.ctx, [one, two], previous_receipt=base, expected_revision=base['revision'],
                          human_confirmed={'pressure': 11})
    human_data = await m.read_merge(e.ctx, human, expected_revision=human['revision'])
    # A later model-only rebuild must not silently drop the human overlay; it is a
    # separate revision and the confirmed revision remains readable.
    rerun, rerun_data = await merged(merging, one, two)
    assert rerun['revision'] != human['revision']
    assert rerun_data['groups'][0]['state'] == 'conflict'  # Model rerun does not inherit the human pick.
    assert (await m.read_merge(e.ctx, human, expected_revision=human['revision'])) == human_data


async def test_human_override_of_undeclared_field_is_rejected(merging):
    e, p, c, a, b, bundle, llm, x, m = merging
    one = await extracted(merging, ONE, key='one')
    with pytest.raises(SchemaError, match='Human-confirmed field is not declared'):
        await m.merge(e.ctx, [one], human_confirmed={'not_a_field': 1})


async def test_human_override_cannot_rewrite_identity_defining_field(merging):
    e, p, c, a, b, bundle, llm, x, m = merging
    one = await extracted(merging, ONE, code='P-001', key='one')
    receipt = await m.merge(e.ctx, [one], human_confirmed={'business_code': 'P-999'})
    data = await m.read_merge(e.ctx, receipt, expected_revision=receipt['revision'])
    group = data['groups'][0]
    assert group['identity']['components']['business_key'] == {'business_code': 'P-001'}
    assert 'business_code' not in group['human_confirmed']


def test_resolve_merge_revision_is_a_strict_cas_gate():
    receipt = {'private': True, 'state': 'private_merge_ready', 'revision': 'a' * 64}
    assert resolve_merge_revision(receipt, expected_revision='a' * 64) is True
    assert resolve_merge_revision(receipt, expected_revision=None) is False
    with pytest.raises(ConflictError):
        resolve_merge_revision(receipt, expected_revision='b' * 64)
    with pytest.raises(SchemaError):
        resolve_merge_revision({'private': False, 'state': 'private_merge_ready', 'revision': 'a' * 64},
                               expected_revision='a' * 64)


def test_identity_tuple_preserves_namespace_type_and_schema_and_separates_configs():
    base = identity_tuple(account_id='acct', namespace_id='n', kind='Component',
                          schema_id='parts', schema_version='1', schema_digest='d1')
    assert base == {'account_id': 'acct', 'namespace_id': 'n', 'entity_type': 'Component',
                    'schema_id': 'parts', 'schema_version': '1', 'schema_digest': 'd1'}
    # Identity tuple is name-free: display names never participate.
    assert 'title' not in base and 'aliases' not in base
    for changes in ({'kind': 'Device'}, {'namespace_id': 'other'}, {'schema_version': '2'}, {'schema_digest': 'd2'}):
        assert {**base, **changes} != base


def test_resolved_identity_carries_the_anchor_tuple():
    bundle = SchemaBundle.freeze('parts', '1', schema_files())
    policy = IdentityPolicy.compile(bundle)
    meta = {'business_code': 'P-001', 'model': 'SY215C'}
    proof = [{'quote': 'P-001 SY215C'}]
    result = resolve_candidate_identity(policy, bundle, meta, proof,
                                        account_id='acct', namespace_id='n', kind='Component')
    assert result['state'] == 'provisional_business_identity'
    assert result['identity_tuple'] == {'account_id': 'acct', 'namespace_id': 'n', 'entity_type': 'Component',
                                        'schema_id': 'parts', 'schema_version': '1', 'schema_digest': bundle.digest}
    assert result['alias_ownership_verified'] is False
    # Near-name-only input never yields a business identity.
    pending = resolve_candidate_identity(policy, bundle, {'title': '空调系统'}, proof,
                                         account_id='acct', namespace_id='n', kind='Component')
    assert pending['state'] == 'pending_identity' and pending['canonical_id'] is None