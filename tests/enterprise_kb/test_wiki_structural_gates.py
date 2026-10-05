"""Explicit schema gates reject unsafe diagnosis structures without trusting prose."""
from pathlib import Path
import zipfile

import pytest
import yaml

from openviking.wiki.schema import SchemaBundle, SchemaError, adapt_sany_structural_gates, compile_bundle
from openviking.wiki.validation import inspect_entity
from test_wiki_schema import freeze, package

ROOT = 'viking://resources/sany'
PROFILE = ROOT + '/Symptom/动力与发动机/发动机冒白烟/profiles/国四和新欧五.md'


@pytest.fixture
def sany():
    path = Path(__file__).resolve().parents[2] / 'docs/spec/evidence/sany-schema-adapter-I020.zip'
    with zipfile.ZipFile(path) as archive:
        upstream = SchemaBundle.freeze('sany', '2+openviking.1', {n: archive.read(n).decode() for n in archive.namelist()})
    bundle = adapt_sany_structural_gates(upstream)
    schemas = {schema.kind: schema for schema in compile_bundle(bundle)}
    return upstream, bundle, schemas


def example(sany, kind):
    return dict(sany[1].members)[kind + '.fewshot.md'].replace('viking://resources/<ns>', ROOT)


def rewrite(text, change):
    _, header, body = text.split('---', 2)
    metadata = yaml.safe_load(header)
    change(metadata)
    return '---\n' + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) + '---' + body


def inspect(sany, kind, text=None, uri=None):
    return inspect_entity(sany[2][kind], uri or (PROFILE if kind == 'Profile' else ROOT + '/' + kind + '/example.md'),
                          example(sany, kind) if text is None else text)


def test_adapter_adds_explicit_gates_and_preserves_original_package(sany):
    upstream, bundle, schemas = sany
    assert upstream.digest == '83816ecc46b394bd48b83e205498584d4e2acb8afa4995f251040f5bef6f7266'
    assert bundle.digest != upstream.digest and bundle.version != upstream.version
    assert 'structural_gates' not in dict(upstream.members)['Profile.schema.yaml']
    assert upstream.digest in dict(bundle.members)['structural-adapter.manifest.yaml']
    assert len(schemas['Profile'].structural_gates) == 5
    with pytest.raises(SchemaError, match='baseline changed'):
        adapt_sany_structural_gates(bundle)


@pytest.mark.parametrize('kind', ['Component', 'ConfigSpec', 'Device', 'Fault', 'Procedure', 'Profile', 'Symptom'])
def test_actual_adapted_examples_pass_structure_but_keep_all_pending_rules(sany, kind):
    draft = inspect(sany, kind)
    assert any(ref.expected_kind == 'source' for ref in draft.references)
    assert any(rule.startswith('shared:') for rule in draft.uncompiled_rules)
    assert 'schema:path_layout' in draft.uncompiled_rules
    assert draft.uncompiled_rules  # Neither structure nor example flags authorize publication.


@pytest.mark.parametrize('change', [
    lambda m: m.pop('config_spec_uri'),
    lambda m: m.update(device_uri=ROOT + '/Device/SY135/SY135C/index.md'),
])
def test_profile_rejects_zero_or_two_configuration_anchors(sany, change):
    with pytest.raises(SchemaError, match='Exactly one'):
        inspect(sany, 'Profile', rewrite(example(sany, 'Profile'), change))


def test_device_anchor_is_valid_without_configspec_and_cross_component_candidates_allowed(sany):
    def change(m):
        m.pop('config_spec_uri')
        m['device_uri'] = ROOT + '/Device/SY135/SY135C/index.md'
    assert inspect(sany, 'Profile', rewrite(example(sany, 'Profile'), change)).kind == 'Profile'


@pytest.mark.parametrize('kind,field', [('Profile', 'possible_faults'), ('Procedure', 'fault_refs'),
                                      ('Procedure', 'target_components'), ('Fault', 'affected_components'),
                                      ('Device', 'raw_refs'), ('ConfigSpec', 'device_refs')])
def test_required_diagnosis_and_source_lists_cannot_be_empty(sany, kind, field):
    with pytest.raises(SchemaError):
        inspect(sany, kind, rewrite(example(sany, kind), lambda m: m.update({field: []})))


@pytest.mark.parametrize('kind,field', [('Profile', 'possible_faults'), ('Procedure', 'fault_refs'),
                                      ('Fault', 'affected_components'), ('ConfigSpec', 'device_refs')])
def test_duplicate_edges_are_rejected_without_reordering_evidence(sany, kind, field):
    def duplicate(m):
        m[field].append(m[field][0])
    with pytest.raises(SchemaError, match='Duplicate'):
        inspect(sany, kind, rewrite(example(sany, kind), duplicate))


def test_device_forbids_common_sources_even_when_raw_refs_exists(sany):
    with pytest.raises(SchemaError, match='Forbidden'):
        inspect(sany, 'Device', rewrite(example(sany, 'Device'), lambda m: m.update(sources=list(m['raw_refs']))))


def test_profile_must_live_under_its_actual_parent_symptom(sany):
    with pytest.raises(SchemaError, match='parent profiles'):
        inspect(sany, 'Profile', uri=ROOT + '/Symptom/其他/其他现象/profiles/配置.md')
    with pytest.raises(SchemaError, match='parent profiles'):
        inspect(sany, 'Profile', uri=PROFILE.replace('国四和新欧五.md', 'nested/配置.md'))


def test_profile_fault_section_order_must_match_ordered_frontmatter(sany):
    text = example(sany, 'Profile')
    def reverse(m):
        m['possible_faults'].reverse()
    with pytest.raises(SchemaError, match='Body reference order'):
        inspect(sany, 'Profile', rewrite(text, reverse))
    with pytest.raises(SchemaError, match='Body reference order'):
        inspect(sany, 'Profile', text.replace('## 可能失效机理', '## 其他机理').replace('## 推荐诊断流程', '## 可能失效机理\n没有机理引用。\n## 推荐诊断流程'))


def test_procedure_class_selects_required_body_template_and_enum(sany):
    text = example(sany, 'Procedure')
    assert inspect(sany, 'Procedure').kind == 'Procedure'
    with pytest.raises(SchemaError, match='section'):
        inspect(sany, 'Procedure', rewrite(text, lambda m: m.update(class_name='repair')))
    with pytest.raises(SchemaError, match='enum'):
        inspect(sany, 'Procedure', rewrite(text, lambda m: m.update(class_name='measurement')))
    with pytest.raises(SchemaError, match='enum'):
        inspect(sany, 'Fault', rewrite(example(sany, 'Fault'), lambda m: m.update(class_name='FluidThing')))


def test_configspec_bom_reference_retains_declared_spec_attributes(sany):
    draft = inspect(sany, 'ConfigSpec')
    refs = [ref for ref in draft.references if ref.field == 'bom_composition']
    assert len(refs) == 3 and all(ref.expected_kind == 'Component' for ref in refs)
    for mutation in [lambda m: m['bom_composition'][0].update(extra='invented'),
                     lambda m: m['bom_composition'][0].update(spec=12),
                     lambda m: m['bom_composition'][0].update(spec='')]:
        with pytest.raises(SchemaError, match='object reference|reference attribute'):
            inspect(sany, 'ConfigSpec', rewrite(example(sany, 'ConfigSpec'), mutation))


def test_symptom_nested_fault_key_is_taken_from_fixed_schema_not_caller(sany):
    text = rewrite(example(sany, 'Symptom'), lambda m: m.update(asserted_faults=[{'fault_uri': ROOT + '/Fault/FluidFailure/燃油水分多.md'}]))
    draft = inspect(sany, 'Symptom', text)
    assert any(ref.field == 'asserted_faults' and ref.expected_kind == 'Fault' for ref in draft.references)
    with pytest.raises(SchemaError, match='conflicts'):
        inspect_entity(sany[2]['Symptom'], ROOT + '/Symptom/example.md', text,
                       reference_keys={'asserted_faults': 'guessed_uri'})


def test_shared_policies_and_path_declarations_are_not_silently_marked_executable():
    files = package(**{'_shared.schema.yaml': 'shared_rules: [{id: source_evidence, text: Every claim requires evidence}]'})
    files['Thing.schema.yaml'] += '\npath_regex: "^Thing/.+$"'
    schema = compile_bundle(freeze(files))[0]
    assert schema.uncompiled_rules == ('shared:source_evidence', 'schema:path_layout')
    with pytest.raises(SchemaError, match='executable adapters'):
        compile_bundle(freeze(files), require_executable_rules=True)


@pytest.mark.parametrize('declaration', [
    {'kind': 'execute_script', 'fields': ['sources']},
    {'kind': 'nonempty', 'fields': ['missing']},
    {'kind': 'exactly_one', 'fields': ['title', 'sources']},
    {'kind': 'forbid', 'fields': ['title']},
    {'kind': 'body_reference_order', 'fields': ['sources'], 'section': 'Absent'},
    {'kind': 'nonempty', 'fields': ['sources'], 'script': 'unsafe'},
])
def test_unknown_contradictory_or_ill_typed_gates_are_rejected(declaration):
    files = package()
    data = yaml.safe_load(files['Thing.schema.yaml']);data['structural_gates'] = [declaration]
    files['Thing.schema.yaml'] = yaml.safe_dump(data)
    with pytest.raises(SchemaError):
        compile_bundle(freeze(files))


@pytest.mark.parametrize('templates', [
    [{'class_names': ['a'], 'required': ['来源']}, {'class_names': ['a'], 'required': ['来源']}],
    [{'class_names': ['a'], 'required': ['Other']}],
    [{'class_names': ['a'], 'required': ['来源']}],  # Does not cover b.
])
def test_body_templates_cannot_overlap_omit_base_or_leave_class_unchecked(templates):
    files = package()
    data = yaml.safe_load(files['Thing.schema.yaml'])
    data['frontmatter'].append({'name': 'class_name', 'type': 'string', 'required': True})
    data['class_name'] = {'enum_families': ['a', 'b']}
    data['body_sections']['templates'] = templates
    files['Thing.schema.yaml'] = yaml.safe_dump(data)
    with pytest.raises(SchemaError):
        compile_bundle(freeze(files))


def test_legacy_dtc_example_with_kb_source_is_not_accepted_as_viking_knowledge(sany):
    with pytest.raises(SchemaError, match='Viking URI'):
        inspect(sany, 'DTC')


@pytest.mark.parametrize('change', [
    lambda data: data.update(structural_gates=[{'kind': [], 'fields': ['sources']}]),
    lambda data: data['frontmatter'][0].update(type={}),
    lambda data: data['frontmatter'][1].update(reference_key='uri', reference_attributes={'spec': {}}),
])
def test_malformed_declaration_types_raise_schema_error_instead_of_internal_error(change):
    files=package();data=yaml.safe_load(files['Thing.schema.yaml']);change(data)
    files['Thing.schema.yaml']=yaml.safe_dump(data)
    with pytest.raises(SchemaError):compile_bundle(freeze(files))
