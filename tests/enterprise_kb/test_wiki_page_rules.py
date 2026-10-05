"""Frozen SANY prose rules run as constrained executors (KB-023).

The eight audited fewshot pages must pass their page checks; every violation
variant and changeset duplicate must be rejected. Coverage is additive: the
policy matrix moves each audited rule to a named enforcement home instead of
leaving it pending.
"""
import zipfile
from pathlib import Path

import pytest
import yaml

from openviking.wiki.page_rules import VERSION, check_batch, check_page, compile_page_rules, coverage_addendum
from openviking.wiki.policies import check_build_profile, policy_coverage
from openviking.wiki.schema import SchemaBundle, SchemaError, adapt_sany_path_policies, compile_bundle
from openviking.wiki.validation import inspect_entity

SRC = 'viking://resources/<ns>'
ROOT = 'viking://resources/sany-page-rules-test'
BUNDLE_ZIP = Path(__file__).resolve().parents[2] / 'docs/spec/evidence/sany-schema-adapter-I026.zip'

PATHS = {
    'Component': 'Component/动臂油缸/动臂油缸.md',
    'ConfigSpec': 'ConfigSpec/SSY100000402/index.md',
    'Device': 'Device/SY135/SY135C/index.md',
    'DTC': 'DTC/SY35E_SSY100000823/E828.md',
    'Fault': 'Fault/ControlFailure/诊断测试.md',
    'Procedure': 'Procedure/inspection/诊断测试.md',
    'Profile': 'Symptom/动力与发动机/发动机冒白烟/profiles/国四和新欧五.md',
    'Symptom': 'Symptom/动力与发动机/发动机冒白烟/index.md',
}


def bundle():
    with zipfile.ZipFile(BUNDLE_ZIP) as archive:
        upstream = SchemaBundle.freeze('sany', 'i026', {n: archive.read(n).decode() for n in archive.namelist()})
    adapted = adapt_sany_path_policies(upstream)
    return upstream, adapted, {s.kind: s for s in compile_bundle(adapted)}, {n: t.replace(SRC, ROOT) for n, t in adapted.members}


@pytest.fixture(scope='module')
def sany():
    return bundle()


# ── Compile guards ───────────────────────────────────────────────────────────

def test_executor_compiles_only_for_the_full_audited_package(sany):
    _, adapted, schemas, _ = sany
    policy = compile_page_rules(adapted)
    assert policy.version == VERSION == 'sany-page-rules-v1'
    assert set(policy.entities) == set(schemas) == {'Component', 'ConfigSpec', 'Device', 'DTC',
                                                    'Fault', 'Procedure', 'Profile', 'Symptom'}


def test_rule_inventory_is_bidirectional_locked(sany):
    from openviking.wiki.page_rules import CLASSIFICATION, SHARED_CLASSIFICATION
    _, adapted, _, _ = sany
    for kind, rules in CLASSIFICATION.items():
        declared = {r for r in next(e for e in compile_bundle(adapted) if e.kind == kind).uncompiled_rules
                    if not r.startswith('shared:')}
        assert declared == set(rules), kind
    shared = {r.split(':', 1)[1] for e in compile_bundle(adapted) for r in e.uncompiled_rules if r.startswith('shared:')}
    assert shared == set(SHARED_CLASSIFICATION)


def test_invented_or_removed_rules_break_the_audit_lock(sany):
    _, adapted, _, members = sany
    def tampered(name, change):
        files = dict(adapted.members)
        data = yaml.safe_load(files[name])
        change(data)
        files[name] = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        return SchemaBundle.freeze(adapted.schema_id, adapted.version, files)
    with pytest.raises(SchemaError, match='re-audit'):
        compile_page_rules(tampered('Component.schema.yaml',
                                    lambda d: d['rules'].append({'id': 'invented_rule', 'when': 'x'})))
    with pytest.raises(SchemaError, match='re-audit'):
        compile_page_rules(tampered('Symptom.schema.yaml',
                                    lambda d: d.update(content_gates=[r for r in d['content_gates'] if r['id'] != 'no_measured_value'])))


def test_foreign_bundle_stays_pending_and_claims_no_executor():
    files = {'Thing.template.md': '# Entity\n## 来源\n',
             'Thing.schema.yaml': '''entity: Thing
frontmatter:
  - {name: title, type: string, required: true}
  - {name: sources, type: list, ref_kind: source, required: true}
body_sections: {required: [来源]}
rules:
  - {id: no_wishful_reasoning, when: 断言必须有证据}
'''}
    coverage = policy_coverage(SchemaBundle.freeze('business', '1', files))
    assert all(entity['uncompiled_rules'] for entity in coverage['entities'])
    assert 'rules_executor' not in coverage


# ── Replay: the eight audited pages must pass ────────────────────────────────

@pytest.mark.parametrize('kind', list(PATHS))
def test_every_audited_fewshot_passes_inspection_and_page_checks(sany, kind):
    _, adapted, schemas, members = sany
    uri = ROOT + '/' + PATHS[kind]
    markdown = members[kind + '.fewshot.md']
    if kind != 'DTC':  # audit fixture: DTC provenance uses kb:// sources, accepted by the path-policy tests too
        inspect_entity(schemas[kind], uri, markdown, namespace_root=ROOT)
    meta = yaml.safe_load(markdown.split('---')[1])
    receipts = check_page(schemas[kind], uri, meta, markdown)
    assert receipts and all(r.get('status') in {'PASS', 'N/A'} for r in receipts)


def test_batch_replay_passes_for_the_whole_changeset(sany):
    _, adapted, schemas, members = sany
    planned = [(kind, ROOT + '/' + path,
                yaml.safe_load(members[kind + '.fewshot.md'].split('---')[1])) for kind, path in PATHS.items()]
    receipts = check_batch(planned)
    assert [r['rule'] for r in receipts] == ['identity_uniqueness', 'one_model_one_page', 'identity_triple', 'no_cross_model_merge']
    assert all(r['status'] == 'PASS' for r in receipts)


# ── Negative probes: violations must be rejected ────────────────────────────

def rewritten(members, kind, change):
    parts = members[kind + '.fewshot.md'].split('---', 2)
    meta = yaml.safe_load(parts[1])
    change(meta)
    return '---\n' + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + '---' + parts[2]


def expect_reject(sany, label_match, kind, uri, markdown):
    _, adapted, schemas, _ = sany
    with pytest.raises(SchemaError, match=label_match):
        if kind != 'DTC':
            inspect_entity(schemas[kind], uri, markdown, namespace_root=ROOT)
        check_page(schemas[kind], uri, yaml.safe_load(markdown.split('---')[1]), markdown)


def test_dtc_orphan_without_gap_marker_is_rejected(sany):
    _, _, _, members = sany
    expect_reject(sany, 'no_orphan', 'DTC', ROOT + '/' + PATHS['DTC'],
                  rewritten(members, 'DTC', lambda m: (m.pop('related_symptoms'), m.pop('related_faults'))))


def test_component_profile_page_carrying_applicable_models_is_rejected(sany):
    _, _, _, members = sany
    expect_reject(sany, 'applicable', 'Component', ROOT + '/Component/动臂油缸/profiles/国四和新欧五.md',
                  rewritten(members, 'Component', lambda m: m.__setitem__('applicable_models', ['SY135C'])))


def test_symptom_body_measured_value_is_rejected(sany):
    _, _, _, members = non_dtc_members = (sany[0], sany[1], sany[2],
                                          {n: t for n, t in sany[3].items()})
    expect_reject(sany, 'measured value', 'Symptom', ROOT + '/' + PATHS['Symptom'],
                  members['Symptom.fewshot.md'].replace('## 相似现象区分\n', '## 相似现象区分\n系统压力 14 MPa 时触发。\n'))


def test_procedure_targeting_a_symptom_uri_is_rejected(sany):
    _, _, _, members = sany
    expect_reject(sany, 'Component URIs only', 'Procedure', ROOT + '/' + PATHS['Procedure'],
                  rewritten(members, 'Procedure', lambda m: m.__setitem__(
                      'target_components', [ROOT + '/Symptom/整机/液压油温高/液压油温高.md'])))


def test_changeset_duplicates_are_rejected(sany):
    _, adapted, _, members = sany
    metas = {kind: yaml.safe_load(members[kind + '.fewshot.md'].split('---')[1]) for kind in PATHS}
    with pytest.raises(SchemaError, match='identity_uniqueness'):
        check_batch([('DTC', ROOT + '/DTC/SY35E_SSY100000823/E828.md', metas['DTC']),
                     ('DTC', ROOT + '/DTC/SY35E_SSY100000823-2/E828.md', metas['DTC'])])
    with pytest.raises(SchemaError, match='one_model_one_page'):
        check_batch([('Device', ROOT + '/Device/SY135/SY135C/index.md', metas['Device']),
                     ('Device', ROOT + '/Device/SY135/SY135C/index-2.md', metas['Device'])])
    with pytest.raises(SchemaError, match='identity_triple'):
        check_batch([('Procedure', ROOT + '/' + PATHS['Procedure'], metas['Procedure']),
                     ('Procedure', ROOT + '/Procedure/inspection/重复页.md', metas['Procedure'])])


# ── Policy coverage wiring ───────────────────────────────────────────────────

def test_policy_coverage_moves_all_audited_rules_to_named_homes(sany):
    _, adapted, _, _ = sany
    coverage = policy_coverage(adapted)
    assert coverage['rules_executor'] == {'version': VERSION, 'compiled': 110}
    assert all(not entity['uncompiled_rules'] for entity in coverage['entities'])
    addendum = coverage_addendum(adapted)
    page_checked = sum(len(v['page_checked']) for v in addendum['entities'].values())
    delegated = sum(len(v['delegated']) for v in addendum['entities'].values())
    assert page_checked + delegated == 110


def test_strict_profile_advances_to_the_six_publication_gates(sany):
    _, adapted, _, _ = sany
    with pytest.raises(SchemaError, match='WIKI_PUBLICATION_GATE_PENDING'):
        check_build_profile(adapted, 'strict_diagnostic')
