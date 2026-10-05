# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Immutable schema inputs shared by upload and URI Wiki builders.

Callers supply already-authorized file contents. This module performs no storage
reads, registration, extraction, natural-language rule execution or publication.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from openviking.wiki.path_policy import PathPolicy

import yaml


class SchemaError(ValueError):
    pass


class _UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for key, value in node.value:
        name = loader.construct_object(key)
        if not isinstance(name, str) or name in result:
            raise SchemaError('Schema mapping keys must be unique strings')
        result[name] = loader.construct_object(value)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _yaml(text):
    try:
        if any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(text)):
            raise SchemaError('Schema aliases are unsupported; use explicit declarations')
        value = yaml.load(text, Loader=_UniqueLoader)
    except yaml.YAMLError as exc:
        raise SchemaError('Invalid schema YAML') from exc
    if not isinstance(value, dict):
        raise SchemaError('Schema document must be a mapping')
    return value


@dataclass(frozen=True)
class SchemaBundle:
    schema_id: str
    version: str
    members: tuple[tuple[str, str], ...]
    digest: str

    @classmethod
    def freeze(cls, schema_id: str, version: str, files: Mapping[str, str]):
        if not schema_id or not version or not isinstance(schema_id, str) or not isinstance(version, str):
            raise SchemaError('Explicit schema ID and version required')
        if not files or len(files) > 256:
            raise SchemaError('Schema requires 1–256 files')
        members, total = [], 0
        for name, text in files.items():
            if not isinstance(name, str) or not isinstance(text, str):
                raise SchemaError('Schema members must be named UTF-8 text')
            path = PurePosixPath(name)
            if (path.is_absolute() or '..' in path.parts or '\\' in name or any(ord(c) < 32 for c in name)
                    or str(path) != name or path.suffix not in {'.md', '.yaml', '.yml'}):
                raise SchemaError('Invalid schema member path')
            size = len(text.encode('utf-8'))
            total += size
            if size > 1024 * 1024 or total > 16 * 1024 * 1024:
                raise SchemaError('Schema package size limit exceeded')
            members.append((name, text))
        if not any(name.endswith('.md') and text.strip() for name, text in members):
            raise SchemaError('A readable Markdown template or example is required')
        members = tuple(sorted(members))
        hashes = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in members}
        digest = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return cls(schema_id, version, members, digest)


@dataclass(frozen=True)
class Field:
    name: str
    kind: str
    required: bool
    target: str | None
    choices: tuple[str, ...] = ()
    reference_key: str | None = None
    reference_attributes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class BodyTemplate:
    class_names: tuple[str, ...]
    required_sections: tuple[str, ...]


@dataclass(frozen=True)
class StructuralGate:
    kind: str
    fields: tuple[str, ...]
    section: str | None = None


@dataclass(frozen=True)
class EntitySchema:
    kind: str
    fields: tuple[Field, ...]
    required_sections: tuple[str, ...]
    uncompiled_rules: tuple[str, ...]
    body_templates: tuple[BodyTemplate, ...] = ()
    structural_gates: tuple[StructuralGate, ...] = ()
    path_policy: PathPolicy | None = None
    schema_digest: str = ""


def _fields(declarations):
    if not isinstance(declarations, list):
        raise SchemaError('Fields must be a list')
    fields, seen = [], set()
    for declaration in declarations:
        if not isinstance(declaration, dict):
            raise SchemaError('Field must be a mapping')
        name, kind = declaration.get('name'), declaration.get('type')
        required, target = declaration.get('required', False), declaration.get('ref_kind')
        if not isinstance(name, str) or not name or name in seen:
            raise SchemaError('Field names must be unique')
        if not isinstance(kind, str) or kind not in {'string', 'list', 'dict', 'integer', 'number', 'boolean'} or type(required) is not bool:
            raise SchemaError('Unsupported field type or invalid required flag')
        if target is not None and (not isinstance(target, str) or not target):
            raise SchemaError('Invalid reference target')
        choices = declaration.get('enum', [])
        if not isinstance(choices, list) or any(not isinstance(x, str) for x in choices) or len(set(choices)) != len(choices):
            raise SchemaError('Unsupported field enum')
        seen.add(name)
        key = declaration.get('reference_key')
        if key is not None and (not isinstance(key, str) or not key or kind != 'list' or not target):
            raise SchemaError('Reference object key requires a list relationship')
        attributes = declaration.get('reference_attributes', {})
        if (not isinstance(attributes, dict) or (attributes and not key)
                or any(not isinstance(attr, str) or not attr or attr == key
                       or not isinstance(typ, str) or typ not in {'string', 'integer', 'number', 'boolean'} for attr, typ in attributes.items())):
            raise SchemaError('Unsupported reference object attributes')
        fields.append(Field(name, kind, required, target, tuple(choices), key, tuple(sorted(attributes.items()))))
    return tuple(fields)


def _string_list(value, label):
    if (not isinstance(value, list) or not value
            or any(not isinstance(item, str) or not item for item in value)
            or len(set(value)) != len(value)):
        raise SchemaError('Invalid ' + label)
    return tuple(value)


def _rule_ids(value):
    if not isinstance(value, list):
        raise SchemaError('Rules must be lists')
    ids = [rule.get('id') for rule in value if isinstance(rule, dict)]
    if len(ids) != len(value) or any(not isinstance(x, str) or not x for x in ids) or len(set(ids)) != len(ids):
        raise SchemaError('Invalid rule declarations')
    return tuple(ids)


def _compile_gates(value, fields, sections):
    if not isinstance(value, list):
        raise SchemaError('Structural gates must be a list')
    result = []
    supported = {'exactly_one', 'nonempty', 'distinct', 'forbid', 'parent_profiles', 'body_reference_order'}
    for item in value:
        if (not isinstance(item, dict) or set(item) - {'kind', 'fields', 'section'}
                or not isinstance(item.get('kind'), str) or item['kind'] not in supported):
            raise SchemaError('Unsupported structural gate')
        kind, names = item['kind'], _string_list(item.get('fields'), 'gate fields')
        if set(names) - set(fields):
            raise SchemaError('Structural gate references undeclared fields')
        section = item.get('section')
        if kind == 'exactly_one':
            if len(names) < 2 or any(fields[name].kind != 'string' or fields[name].required for name in names):
                raise SchemaError('Exactly-one gate requires optional string alternatives')
        elif kind == 'forbid':
            if any(fields[name].required for name in names):
                raise SchemaError('Cannot forbid a required field')
        else:
            if len(names) != 1:
                raise SchemaError('Structural gate requires one field')
            field = fields[names[0]]
            expected = {'nonempty': {'string', 'list'}, 'distinct': {'list'},
                        'parent_profiles': {'string'}, 'body_reference_order': {'list'}}[kind]
            if field.kind not in expected:
                raise SchemaError('Structural gate field type mismatch')
            if kind == 'parent_profiles' and not field.target:
                raise SchemaError('Parent gate requires a reference')
            if kind == 'body_reference_order' and (not field.target or field.reference_key or section not in sections):
                raise SchemaError('Body-order gate requires URI list and a required section')
        if kind != 'body_reference_order' and section is not None:
            raise SchemaError('Section is only valid for body-order gates')
        result.append(StructuralGate(kind, names, section))
    return tuple(result)


def compile_bundle(bundle: SchemaBundle, *, require_executable_rules=False):
    """Compile structural declarations; never claim textual rules are enforced."""
    files = dict(bundle.members)
    shared = _yaml(files['_shared.schema.yaml']) if '_shared.schema.yaml' in files else {}
    common = _fields(shared.get('shared_frontmatter', []))
    shared_rule_ids = _rule_ids(shared.get('shared_rules', []))
    entities = []
    for name, text in bundle.members:
        if not name.endswith('.schema.yaml') or PurePosixPath(name).name.startswith('_'):
            continue
        data = _yaml(text)
        kind = data.get('entity')
        if not isinstance(kind, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', kind):
            raise SchemaError('Invalid entity kind')
        own = _fields(data.get('frontmatter', []))
        exempt = data.get('shared_not_required', [])
        if not isinstance(exempt, list) or any(not isinstance(x, str) for x in exempt) or set(exempt) - {f.name for f in common}:
            raise SchemaError('Unknown shared field exemption')
        # Exemption affects shared requiredness, not its presence or type.
        merged = {f.name: Field(f.name, f.kind, False if f.name in exempt else f.required, f.target, f.choices, f.reference_key, f.reference_attributes) for f in common}
        for field in own:
            if field.name in merged:
                raise SchemaError('Entity field shadows a shared declaration')
            merged[field.name] = field
        class_declaration = data.get('class_name', {})
        if not isinstance(class_declaration, dict):
            raise SchemaError('class_name must be a mapping')
        if 'enum_families' in class_declaration:
            choices = _string_list(class_declaration['enum_families'], 'class enum')
            field = merged.get('class_name')
            if not field or field.kind != 'string' or (field.choices and field.choices != choices):
                raise SchemaError('Class enum requires a compatible class_name string field')
            merged['class_name'] = Field(field.name, field.kind, field.required, field.target, choices, field.reference_key, field.reference_attributes)
        body = data.get('body_sections', {})
        if not isinstance(body, dict):
            raise SchemaError('Body sections must be a mapping')
        sections = body.get('required', [])
        if not isinstance(sections, list) or any(not isinstance(x, str) or not x for x in sections) or len(set(sections)) != len(sections):
            raise SchemaError('Invalid body section declarations')
        templates, seen_classes = [], set()
        raw_templates = body.get('templates', [])
        if not isinstance(raw_templates, list):
            raise SchemaError('Body templates must be a list')
        for item in raw_templates:
            if not isinstance(item, dict) or set(item) != {'class_names', 'required'}:
                raise SchemaError('Invalid body template')
            classes = _string_list(item['class_names'], 'template classes')
            required = _string_list(item['required'], 'template sections')
            if (set(classes) & seen_classes or not set(sections) <= set(required)
                    or [section for section in required if section in sections] != sections):
                raise SchemaError('Overlapping templates or missing base sections')
            field = merged.get('class_name')
            if not field or field.kind != 'string' or (field.choices and not set(classes) <= set(field.choices)):
                raise SchemaError('Body template requires declared class_name choices')
            seen_classes.update(classes)
            templates.append(BodyTemplate(classes, required))
        if templates and merged['class_name'].choices and seen_classes != set(merged['class_name'].choices):
            raise SchemaError('Body templates do not cover all class choices')
        rules, gates = data.get('rules', []), data.get('content_gates', [])
        if not isinstance(rules, list) or not isinstance(gates, list):
            raise SchemaError('Rules must be lists')
        rules = rules + gates
        rule_ids = _rule_ids(rules)
        # Shared prose is policy data, not executable instructions or verified rules.
        uncompiled = rule_ids + tuple('shared:' + name for name in shared_rule_ids)
        from openviking.wiki.path_policy import compile_path_policy
        path_policy = compile_path_policy(data, merged)
        if ('path_regex' in data or 'path_pattern' in data) and path_policy is None:
            # Do not execute arbitrary regex or silently claim layout validation.
            uncompiled += ('schema:path_layout',)
        gates = _compile_gates(data.get('structural_gates', []), merged, sections)
        entities.append(EntitySchema(kind, tuple(merged.values()), tuple(sections), uncompiled, tuple(templates), gates, path_policy, bundle.digest))
    kinds = [e.kind for e in entities]
    if not kinds or len(set(kinds)) != len(kinds):
        raise SchemaError('Executable schema requires unique entity declarations')
    for entity in entities:
        for field in entity.fields:
            if field.target and field.target != 'source' and field.target not in kinds:
                raise SchemaError('Reference target not declared: ' + field.target)
    if require_executable_rules and any(e.uncompiled_rules for e in entities):
        raise SchemaError('Schema rules or layout require executable adapters before strict registration')
    return tuple(entities)


def adapt_sany_profile_example(bundle: SchemaBundle):
    """Produce a new adapter version; never mutate an upstream or frozen package."""
    files = dict(bundle.members)
    name = 'Profile.fewshot.md'
    old = '每条必须是 direct_component_uri 那个件自己的机理；'
    if name not in files or files[name].count(old) != 1:
        raise SchemaError('Expected Profile example conflict changed; inspect upstream before adapting')
    schema = files.get('Profile.schema.yaml', '')
    if 'possible_faults 可跨多个 Component' not in schema:
        raise SchemaError('Current Profile schema does not authorize cross-component faults')
    files[name] = files[name].replace(old, '候选机理可跨多个部件，归属由各 Fault.affected_components 表达；')
    files['adapter.manifest.yaml'] = yaml.safe_dump({
        'adapter': 'sany-profile-example-v1', 'upstream_schema_id': bundle.schema_id,
        'upstream_version': bundle.version, 'upstream_bundle_sha256': bundle.digest,
        'changed_file': name, 'reason': 'Align example with authoritative Profile relation ownership',
    }, allow_unicode=True, sort_keys=True)
    return SchemaBundle.freeze(bundle.schema_id, bundle.version + '+openviking.1', files)


def adapt_sany_structural_gates(bundle: SchemaBundle):
    """Add explicit gates to the audited adapter, preserving all prose as pending.

    This targets one known full bundle. It never interprets arbitrary rule prose
    as executable policy and never mutates the iroot source package.
    """
    if bundle.digest != '83816ecc46b394bd48b83e205498584d4e2acb8afa4995f251040f5bef6f7266':
        raise SchemaError('Sany adapter baseline changed; audit before adapting structural gates')
    files = dict(bundle.members)
    changed = []
    def update(name, change):
        data = _yaml(files[name])
        change(data)
        files[name] = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        changed.append(name)
    def sources(data):
        field = next(field for field in data['shared_frontmatter'] if field['name'] == 'sources')
        field['ref_kind'] = 'source'
    update('_shared.schema.yaml', sources)
    def add_gates(data, gates):
        if 'structural_gates' in data:
            raise SchemaError('Baseline already contains structural gates')
        data['structural_gates'] = gates
    def gate(kind, *fields, **extra):
        return {'kind': kind, 'fields': list(fields), **extra}
    update('Device.schema.yaml', lambda data: add_gates(data, [gate('forbid', 'sources'), gate('nonempty', 'raw_refs')]))
    update('Profile.schema.yaml', lambda data: add_gates(data, [
        gate('exactly_one', 'config_spec_uri', 'device_uri'),
        gate('nonempty', 'possible_faults'), gate('distinct', 'possible_faults'),
        gate('parent_profiles', 'parent_symptom'),
        gate('body_reference_order', 'possible_faults', section='可能失效机理'),
    ]))
    update('Procedure.schema.yaml', lambda data: add_gates(data, [
        gate('nonempty', 'fault_refs'), gate('distinct', 'fault_refs'),
        gate('nonempty', 'target_components'), gate('distinct', 'target_components'),
    ]))
    update('Fault.schema.yaml', lambda data: add_gates(data, [
        gate('nonempty', 'affected_components'), gate('distinct', 'affected_components'),
    ]))
    def asserted(data):
        field = next(field for field in data['frontmatter'] if field['name'] == 'asserted_faults')
        field['reference_key'] = 'fault_uri'
    update('Symptom.schema.yaml', asserted)
    def bom(data):
        field = next(field for field in data['frontmatter'] if field['name'] == 'bom_composition')
        field['reference_key'] = 'component_uri'
        field['reference_attributes'] = {'spec': 'string'}
        add_gates(data, [gate('nonempty', 'device_refs'), gate('distinct', 'device_refs')])
    update('ConfigSpec.schema.yaml', bom)
    files['structural-adapter.manifest.yaml'] = yaml.safe_dump({
        'adapter': 'sany-structural-gates-v1', 'input_bundle_sha256': bundle.digest,
        'input_schema_id': bundle.schema_id, 'input_version': bundle.version,
        'changed_files': changed,
        'scope': 'Explicit structural gates only; all prose rules remain uncompiled',
    }, allow_unicode=True, sort_keys=True)
    adapted = SchemaBundle.freeze(bundle.schema_id, bundle.version + '+structural.1', files)
    compile_bundle(adapted)
    return adapted


def adapt_sany_path_policies(bundle: SchemaBundle):
    """Freeze a new audited path adapter; leave every prose rule pending."""
    if bundle.digest != '6a6f66c824681a465765afdc993a87388a125a259c2915f757c93d53c50e37c0':
        raise SchemaError('Sany path adapter baseline changed; audit before adapting')
    files = dict(bundle.members)
    changed = []
    for name, text in bundle.members:
        if name.endswith('.schema.yaml') and not PurePosixPath(name).name.startswith('_'):
            data = _yaml(text)
            data['path_layout'] = {'policy': 'sany-paths-v1'}
            files[name] = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
            changed.append(name)
    files['path-adapter.manifest.yaml'] = yaml.safe_dump({
        'adapter': 'sany-paths-v1', 'input_bundle_sha256': bundle.digest,
        'input_version': bundle.version, 'changed_files': changed,
        'scope': 'Eight path layouts and local field alignment only; prose/identity uniqueness/relations remain pending',
    }, allow_unicode=True, sort_keys=True)
    adapted = SchemaBundle.freeze(bundle.schema_id, bundle.version + '+paths.1', files)
    compile_bundle(adapted)
    return adapted
