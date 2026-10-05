"""Bounded, model-free diagnostic expansion over Wiki Markdown relations."""
import yaml

import pytest

from openviking.wiki.diagnostic import Configuration, DiagnosticExpander
from openviking.wiki.schema import SchemaBundle, SchemaError, compile_bundle

ROOT = 'viking://resources/wiki'


def package():
    return {
        'Symptom.template.md': '# 现象\n## 来源\n',
        '_shared.schema.yaml': 'shared_frontmatter: []\nshared_rules: []\n',
        'Symptom.schema.yaml': '''entity: Symptom
frontmatter:
  - {name: asserted_faults, type: list, ref_kind: Fault, reference_key: fault_uri}
  - {name: sources, type: list, ref_kind: source, required: true}
body_sections: {required: [来源]}
''',
        'Profile.schema.yaml': '''entity: Profile
frontmatter:
  - {name: parent_symptom, type: string, ref_kind: Symptom}
  - {name: config_spec_uri, type: string, ref_kind: ConfigSpec}
  - {name: device_uri, type: string, ref_kind: Device}
  - {name: direct_component_uri, type: string, ref_kind: Component}
  - {name: possible_faults, type: list, ref_kind: Fault}
  - {name: sources, type: list, ref_kind: source, required: true}
body_sections: {required: [来源]}
''',
        'Fault.schema.yaml': '''entity: Fault
frontmatter:
  - {name: affected_components, type: list, ref_kind: Component}
  - {name: sources, type: list, ref_kind: source, required: true}
body_sections: {required: [来源]}
''',
        'Component.schema.yaml': '''entity: Component
frontmatter:
  - {name: related_components, type: list, ref_kind: Component}
  - {name: sources, type: list, ref_kind: source, required: true}
body_sections: {required: [来源]}
''',
        'ConfigSpec.schema.yaml': '''entity: ConfigSpec
frontmatter:
  - {name: sources, type: list, ref_kind: source, required: true}
body_sections: {required: [来源]}
''',
        'Device.schema.yaml': '''entity: Device
frontmatter:
  - {name: sources, type: list, ref_kind: source, required: true}
body_sections: {required: [来源]}
''',
    }


@pytest.fixture
def schemas():
    bundle = SchemaBundle.freeze('sany-test', '1', package())
    return {schema.kind: schema for schema in compile_bundle(bundle)}


class FakeFS:
    """Records reads so fabrication or unbounded rereads are visible."""

    def __init__(self, files):
        self.files, self.reads = dict(files), []

    async def read_file(self, uri, ctx=None):
        self.reads.append(uri)
        if uri not in self.files:
            raise FileNotFoundError(uri)
        return self.files[uri]


def page(frontmatter, links=()):
    body = '## 来源\n' + '\n'.join(links)
    return '---\n' + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False) + '---\n' + body + '\n'


SYMPTOM = ROOT + '/Symptom/液压系统/动作慢/index.md'
PROFILE = ROOT + '/Symptom/液压系统/动作慢/profiles/国四.md'
SPEC = ROOT + '/ConfigSpec/SC-1/index.md'
FAULT_A = ROOT + '/Fault/HydraulicFailure/主泵磨损.md'
FAULT_B = ROOT + '/Fault/ElectricalFailure/线束断路.md'
COMP = ROOT + '/Component/液压/主泵.md'
COMP2 = ROOT + '/Component/液压/溢流阀.md'


def base_files():
    return {
        SYMPTOM: page({'asserted_faults': [{'fault_uri': FAULT_B}], 'sources': ['viking://resources/raw/a.md']},
                      ['[配置档](' + PROFILE + ')']),
        PROFILE: page({'parent_symptom': SYMPTOM, 'config_spec_uri': SPEC,
                       'direct_component_uri': COMP, 'possible_faults': [FAULT_A],
                       'sources': ['viking://resources/raw/a.md']}),
        FAULT_A: page({'affected_components': [COMP], 'sources': ['viking://resources/raw/a.md']}),
        FAULT_B: page({'affected_components': [COMP2], 'sources': ['viking://resources/raw/a.md']}),
        COMP: page({'sources': ['viking://resources/raw/a.md']}),
        COMP2: page({'sources': ['viking://resources/raw/a.md']}),
        SPEC: page({'sources': ['viking://resources/raw/a.md']}),
    }


def expander(files, schemas, **kwargs):
    fs = FakeFS(files)
    return fs, DiagnosticExpander(fs, object(), schemas, namespace_root=ROOT, **kwargs)


async def test_profile_and_generic_evidence_are_reported_separately(schemas):
    fs, engine = expander(base_files(), schemas)
    result = await engine.expand(SYMPTOM, configuration=Configuration(SPEC))
    assert result.applicability == 'applicable'
    assert [node.uri for node in result.configured] == [SYMPTOM, SPEC, PROFILE, FAULT_A, COMP]
    assert all(node.path == 'configured' for node in result.configured)
    assert [node.uri for node in result.generic] == [SYMPTOM, FAULT_B, COMP2]
    assert all(node.path == 'generic' for node in result.generic)
    assert FAULT_B not in [node.uri for node in result.configured]
    assert result.unknown == ()


async def test_unknown_configuration_is_undetermined_and_generic_is_evidence_only(schemas):
    fs, engine = expander(base_files(), schemas)
    result = await engine.expand(SYMPTOM)
    assert result.applicability == 'undetermined'
    assert 'configuration' in result.unknown
    assert result.configured == ()
    assert [node.uri for node in result.generic] == [SYMPTOM, FAULT_B, COMP2]
    assert all(node.path == 'generic' for node in result.generic)


async def test_generic_assertion_never_overrides_inapplicable_profile(schemas):
    fs, engine = expander(base_files(), schemas)
    result = await engine.expand(SYMPTOM, configuration=Configuration(SPEC, not_applicable=True))
    assert result.applicability == 'not_applicable'
    assert result.configured == ()
    assert 'profile:not_applicable' in result.unknown
    assert [node.uri for node in result.generic] == [SYMPTOM, FAULT_B, COMP2]


async def test_missing_edge_does_not_fabricate_a_relationship(schemas):
    files = base_files()
    files[SYMPTOM] = page({'asserted_faults': [{'fault_uri': ROOT + '/Fault/HydraulicFailure/不存在.md'}],
                           'sources': ['viking://resources/raw/a.md']}, ['[配置档](' + PROFILE + ')'])
    fs, engine = expander(files, schemas)
    result = await engine.expand(SYMPTOM)
    assert [node.uri for node in result.generic] == [SYMPTOM]
    assert any(gap.startswith('missing_edge:asserted_faults:') for gap in result.gaps)


async def test_no_cross_namespace_traversal(schemas):
    outside = 'viking://resources/other/Fault/HydraulicFailure/外部.md'
    files = base_files()
    files[FAULT_A] = page({'affected_components': [outside], 'sources': ['viking://resources/raw/a.md']})
    fs, engine = expander(files, schemas)
    result = await engine.expand(SYMPTOM, configuration=Configuration(SPEC))
    assert outside not in [node.uri for node in result.configured]
    assert 'outside_namespace:' + outside in result.gaps


async def test_traversal_terminates_on_cycles(schemas):
    files = base_files()
    files[SYMPTOM] = page({'sources': ['viking://resources/raw/a.md']}, ['[配置档](' + PROFILE + ')'])
    files[COMP] = page({'related_components': [COMP2], 'sources': ['viking://resources/raw/a.md']})
    files[COMP2] = page({'related_components': [COMP], 'sources': ['viking://resources/raw/a.md']})
    fs, engine = expander(files, schemas)
    result = await engine.expand(SYMPTOM, configuration=Configuration(SPEC))
    visited = [node.uri for node in result.configured]
    assert visited.count(COMP) == 1 and visited.count(COMP2) == 1
    assert fs.reads.count(COMP) == 1 and fs.reads.count(COMP2) == 1


async def test_depth_cap_is_enforced(schemas):
    files = base_files()
    chain = [ROOT + '/Component/液压/链%d.md' % index for index in range(6)]
    for index, uri in enumerate(chain):
        target = [chain[index + 1]] if index + 1 < len(chain) else []
        files[uri] = page({'related_components': target, 'sources': ['viking://resources/raw/a.md']})
    files[FAULT_A] = page({'affected_components': [chain[0]], 'sources': ['viking://resources/raw/a.md']})
    fs, engine = expander(files, schemas, max_depth=3)
    result = await engine.expand(SYMPTOM, configuration=Configuration(SPEC))
    assert result.truncated and 'depth_cap' in result.gaps
    assert max(node.depth for node in result.configured) == 3
    assert chain[-1] not in [node.uri for node in result.configured]


async def test_node_cap_bounds_wide_traversal(schemas):
    files = base_files()
    wide = [ROOT + '/Component/液压/宽%d.md' % index for index in range(10)]
    for uri in wide:
        files[uri] = page({'sources': ['viking://resources/raw/a.md']})
    files[FAULT_A] = page({'affected_components': wide, 'sources': ['viking://resources/raw/a.md']})
    fs, engine = expander(files, schemas, max_nodes=4)
    result = await engine.expand(SYMPTOM, configuration=Configuration(SPEC))
    assert result.truncated and 'node_cap' in result.gaps
    assert len(result.configured) == 4


async def test_fuzzy_normalization_uses_injected_unified_search_only(schemas):
    calls = []

    async def normalize(text):
        calls.append(text)
        return ('viking://resources/other/Symptom/别的现象/index.md',
                ROOT + '/Fault/HydraulicFailure/主泵磨损.md',
                ROOT + '/Symptom/液压系统/动作慢/index.md')

    fs, engine = expander(base_files(), schemas, normalize=normalize)
    assert await engine.normalize_symptom('机器动不了') == (SYMPTOM,)
    assert calls == ['机器动不了']
    fs, engine = expander(base_files(), schemas)
    assert await engine.normalize_symptom('机器动不了') == ()


async def test_start_outside_namespace_or_unreadable_is_rejected(schemas):
    fs, engine = expander(base_files(), schemas)
    with pytest.raises(SchemaError, match='outside'):
        await engine.expand('viking://resources/other/Symptom/液压系统/动作慢/index.md')
    with pytest.raises(SchemaError, match='readable'):
        await engine.expand(ROOT + '/Symptom/液压系统/缺失/index.md')