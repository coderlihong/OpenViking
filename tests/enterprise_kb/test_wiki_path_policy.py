"""Execute frozen SANY path declarations; never certify prose or publication."""
from dataclasses import asdict
import hashlib
from pathlib import Path
import zipfile

import pytest
import yaml

from openviking.wiki.schema import SchemaBundle, SchemaError, adapt_sany_path_policies, compile_bundle
from openviking.wiki.policies import policy_coverage, check_build_profile
from openviking.wiki.validation import inspect_entity
from openviking.wiki.path_policy import execute_path_policy

ROOT = 'viking://resources/sany-path-test'
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


@pytest.fixture
def adapter():
    with zipfile.ZipFile(Path('docs/spec/evidence/sany-schema-adapter-I026.zip')) as z:
        upstream = SchemaBundle.freeze('sany', 'i026', {n:z.read(n).decode() for n in z.namelist()})
    adapted = adapt_sany_path_policies(upstream)
    return upstream, adapted, {s.kind:s for s in compile_bundle(adapted)}


def text(bundle, kind):
    return dict(bundle.members)[kind+'.fewshot.md'].replace('viking://resources/<ns>',ROOT)


def test_eight_layouts_have_handlers_while_all_prose_remains_pending(adapter):
    old, new, schemas = adapter
    previous = compile_bundle(old)
    assert sum(len(s.uncompiled_rules) for s in previous) == 118
    assert sum(len(s.uncompiled_rules) for s in schemas.values()) == 110
    assert len(schemas) == 8
    for before in previous:
        after = schemas[before.kind]
        assert after.uncompiled_rules == tuple(r for r in before.uncompiled_rules if r != 'schema:path_layout')
        assert after.path_policy.version == 'sany-paths-v1'
        assert 'path_layout:' not in dict(old.members)[before.kind+'.schema.yaml']
    coverage = policy_coverage(new)
    assert all(e['path_policy'] for e in coverage['entities'])
    assert coverage['diagnostic_publishable'] is False
    # Since sany-page-rules-v1 every audited rule has a named enforcement home,
    # so strict advances to the publication gate instead of SCHEMA_POLICY_PENDING.
    with pytest.raises(SchemaError, match='WIKI_PUBLICATION_GATE_PENDING'):
        check_build_profile(new, 'strict_diagnostic')


@pytest.mark.parametrize('kind', [k for k in PATHS if k != 'DTC'])
def test_original_seven_fewshots_pass_paths_and_emit_bound_receipts(adapter,kind):
    _, bundle, schemas = adapter
    markdown = text(bundle,kind)
    draft = inspect_entity(schemas[kind],ROOT+'/'+PATHS[kind],markdown,namespace_root=ROOT)
    receipt = asdict(draft.policy_receipts[0])
    assert receipt['schema_digest'] == bundle.digest
    assert receipt['entity_sha256'] == hashlib.sha256(markdown.encode()).hexdigest()
    assert receipt['entity_uri'] == draft.uri
    assert receipt['namespace_root'] == ROOT
    assert receipt['status'] == 'PASS'
    assert draft.uncompiled_rules  # Successful path does not execute prose.


def test_dtc_original_source_mapping_remains_explicitly_blocked(adapter):
    _, bundle, schemas = adapter
    with pytest.raises(SchemaError,match='Viking URI'):
        inspect_entity(schemas['DTC'],ROOT+'/'+PATHS['DTC'],text(bundle,'DTC'),namespace_root=ROOT)
    # Isolate the layout executor without inventing a kb:// source migration.
    markdown = text(bundle,'DTC'); meta = yaml.safe_load(markdown.split('---',2)[1])
    assert execute_path_policy(schemas['DTC'],ROOT+'/'+PATHS['DTC'],meta,markdown,ROOT).status == 'PASS'


@pytest.mark.parametrize('kind,bad', [
    ('Component','Component/未知大类/a.md'),
    ('Component','Component/动臂油缸/nested/a.md'),
    ('ConfigSpec','ConfigSpec/SSY999/index.md'),
    ('ConfigSpec','ConfigSpec/SSY100000402/other.md'),
    ('Device','Device/SY135/SY999/index.md'),
    ('Device','Device/SY135/SY135C/nested/index.md'),
    ('DTC','DTC/SY35E_SSY100000823/E828_中文译名.md'),
    ('DTC','DTC/SY999_SSY100000823/E828.md'),
    ('Fault','Fault/UnknownFailure/a.md'),
    ('Fault','Fault/ThermalFailure/a.md'),
    ('Procedure','Procedure/repair/a.md'),
    ('Symptom','Symptom/动力与发动机/现象/现象.md'),
    ('Profile','Symptom/动力与发动机/发动机冒白烟/profiles/nested/a.md'),
])
def test_invalid_paths_and_local_field_mismatches_rejected(adapter,kind,bad):
    _, bundle, schemas = adapter
    markdown = text(bundle,kind); meta = yaml.safe_load(markdown.split('---',2)[1])
    with pytest.raises(SchemaError,match='Path policy violation'):
        execute_path_policy(schemas[kind],ROOT+'/'+bad,meta,markdown,ROOT)


def test_explicit_root_prevents_matching_kind_inside_another_collection(adapter):
    _, bundle, schemas = adapter
    md = text(bundle,'ConfigSpec')
    for root in [None,'viking://resources/another']:
        with pytest.raises(SchemaError,match='root'):
            inspect_entity(schemas['ConfigSpec'],ROOT+'/'+PATHS['ConfigSpec'],md,namespace_root=root)


def test_changed_prose_layout_does_not_inherit_audited_executor(adapter):
    _, bundle, _ = adapter
    files = dict(bundle.members); data = yaml.safe_load(files['Device.schema.yaml']); data['path_pattern'] += '/changed'
    files['Device.schema.yaml'] = yaml.safe_dump(data,allow_unicode=True)
    changed = SchemaBundle.freeze('changed','1',files)
    with pytest.raises(SchemaError,match='re-audit'):
        compile_bundle(changed)
    with pytest.raises(SchemaError,match='baseline changed'):
        adapt_sany_path_policies(changed)


def test_extractor_candidate_path_is_actually_checked_and_receipt_is_server_generated(adapter):
    from openviking.wiki.extraction import WikiDraftExtractor, ExtractionError
    _, bundle, schemas = adapter
    # Synthetic one-source fixture isolates the real extractor validation path;
    # it is not a maintenance knowledge gold or a source migration.
    source = 'viking://resources/path-test-source.md'
    original = text(bundle,'Device')
    _, header, body = original.split('---',2)
    metadata = yaml.safe_load(header)
    metadata['raw_refs'] = [source]
    metadata['current_manual'] = source
    body = '\n# SY135C\n' + ''.join('## '+section+'\nSY135C\n' for section in schemas['Device'].required_sections)
    markdown = '---\n'+yaml.safe_dump(metadata,allow_unicode=True,sort_keys=False)+'---'+body
    # Body links to previous examples are still under collection; source-only
    # evidence references the explicit synthetic input copied verbatim.
    item = {'kind':'Device','relative_path':PATHS['Device'],'markdown':markdown,
            'evidence':[{'claim':'SY135C','quote':'SY135C','source_uri':source}]}
    from types import SimpleNamespace
    x = WikiDraftExtractor(SimpleNamespace(fs=None))
    result = x._validate(item,schemas,ROOT,source,'SY135C')
    assert result['policy_receipts'][0]['entity_uri'] == ROOT+'/'+PATHS['Device']
    assert result['policy_receipts'][0]['entity_sha256'] == hashlib.sha256(result['markdown'].encode()).hexdigest()
    item['relative_path'] = 'Device/SY135/SY999/index.md'
    with pytest.raises(ExtractionError,match='Path policy violation'):
        x._validate(item,schemas,ROOT,source,'SY135C')
