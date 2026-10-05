"""Frozen Wiki schema inputs do not drift or silently compile textual policies."""
import pytest

from openviking.wiki.schema import SchemaBundle, SchemaError, adapt_sany_profile_example, compile_bundle


def package(**changes):
    files = {'Thing.template.md': '# Entity\n## 来源\n', 'Thing.schema.yaml': '''entity: Thing
frontmatter:
  - {name: title, type: string, required: true}
  - {name: sources, type: list, ref_kind: source, required: true}
body_sections: {required: [来源]}
'''}
    files.update(changes)
    return files


def freeze(files):
    return SchemaBundle.freeze('business', '1', files)


def test_freeze_preserves_full_contents_and_stable_hash_after_caller_mutation():
    files = package(**{'vocab/terms.yaml': 'terms: [a]'})
    first = freeze(files)
    assert first.digest == freeze(dict(reversed(list(files.items())))).digest
    files['Thing.template.md'] += 'changed'
    assert dict(first.members)['Thing.template.md'] == '# Entity\n## 来源\n'
    assert first.digest != freeze(files).digest
    files['vocab/terms.yaml'] = 'terms: [b]'
    assert freeze(files).digest != first.digest


@pytest.mark.parametrize('path', ['../other.md', '/tmp/other.md', 'a/../b.md', 'a\\b.md', './a.md', 'script.py', 'bad\x00.md'])
def test_bundle_rejects_noncanonical_or_executable_member_paths(path):
    with pytest.raises(SchemaError):
        freeze(package(**{path: 'text'}))


@pytest.mark.parametrize('text', ['entity: Thing\nentity: Other', 'entity: Thing\na: &x [a]\nb: *x'])
def test_yaml_duplicates_and_aliases_are_not_silently_resolved(text):
    with pytest.raises(SchemaError):
        compile_bundle(freeze(package(**{'Thing.schema.yaml': text})))


def test_shared_source_exemption_and_declared_reference_targets():
    bundle = freeze({'Device.template.md': '# Device', '_shared.schema.yaml': 'shared_frontmatter: [{name: sources, type: list, required: true}]',
                     'Device.schema.yaml': 'entity: Device\nshared_not_required: [sources]\nfrontmatter: [{name: raw_refs, type: list, ref_kind: source, required: true}]'})
    fields = {f.name: f for f in compile_bundle(bundle)[0].fields}
    assert not fields['sources'].required and fields['raw_refs'].required
    bad = package(**{'Thing.schema.yaml': 'entity: Thing\nfrontmatter: [{name: link, type: string, ref_kind: Missing}]'})
    with pytest.raises(SchemaError, match='not declared'):
        compile_bundle(freeze(bad))


def test_natural_language_rules_remain_uncompiled_and_block_strict_registration():
    files = package();files['Thing.schema.yaml'] += '\nrules: [{id: scoped_identity, text: Do not merge unlike configurations}]'
    bundle = freeze(files)
    assert compile_bundle(bundle)[0].uncompiled_rules == ('scoped_identity',)
    with pytest.raises(SchemaError, match='executable adapters'):
        compile_bundle(bundle, require_executable_rules=True)
    # Markdown-only packages can be frozen for editing, not misrepresented as executable.
    with pytest.raises(SchemaError, match='declarations'):
        compile_bundle(freeze({'template.md': '# Human template'}))


def test_adapter_preserves_upstream_and_requires_known_conflict():
    upstream = freeze({'Profile.fewshot.md': '每条必须是 direct_component_uri 那个件自己的机理；',
                       'Profile.schema.yaml': 'entity: Profile\n# possible_faults 可跨多个 Component'})
    adapted = adapt_sany_profile_example(upstream)
    assert adapted.version != upstream.version and adapted.digest != upstream.digest
    assert '必须是' in dict(upstream.members)['Profile.fewshot.md']
    assert 'affected_components' in dict(adapted.members)['Profile.fewshot.md']
    assert upstream.digest in dict(adapted.members)['adapter.manifest.yaml']
    with pytest.raises(SchemaError, match='changed'):
        adapt_sany_profile_example(adapted)


def test_bad_declaration_shapes_fail_with_schema_error():
    for text in ['entity: Thing\nbody_sections: []', 'entity: Thing\nrules: {}',
                 'entity: Thing\nfrontmatter: [{name: x, type: string, required: yesplease}]',
                 'entity: Thing\nshared_not_required: [[nested]]']:
        with pytest.raises(SchemaError):
            compile_bundle(freeze(package(**{'Thing.schema.yaml': text})))
