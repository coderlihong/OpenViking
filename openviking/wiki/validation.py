# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Generic Markdown entity checks, not a complete diagnostic publication gate.

ReferenceResolver must perform authorized, revision-bound reads. Path existence
or planned names are insufficient. This module never fetches untrusted links.
Natural-language, ontology identity and applicability rules remain separate.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

from openviking.wiki.schema import EntitySchema, SchemaError, _yaml


@dataclass(frozen=True)
class Reference:
    uri: str
    expected_kind: str | None
    field: str


@dataclass(frozen=True)
class Draft:
    kind: str
    uri: str
    content_hash: str
    references: tuple[Reference, ...]
    uncompiled_rules: tuple[str, ...]
    policy_receipts: tuple = ()


@dataclass(frozen=True)
class ResolvedReference:
    kind: str | None
    account_id: str
    namespace_id: str
    revision: str
    authorized: bool
    has_content: bool
    entity_validated: bool = False
    validation_schema_hash: str | None = None


ReferenceResolver = Callable[[str], Awaitable[ResolvedReference]]


def source_uri(uri):
    if not isinstance(uri, str) or not uri.startswith('viking://resources/') or any(ord(c) < 32 for c in uri):
        raise SchemaError('Reference must be a Viking URI')
    parsed = urlsplit(uri)
    try:
        decoded = unquote(parsed.path, errors="strict")
    except UnicodeDecodeError as exc:
        raise SchemaError("Invalid URI encoding") from exc
    if (parsed.scheme != 'viking' or parsed.netloc != 'resources' or parsed.query or parsed.fragment
            or '\\' in decoded or any(ord(c) < 32 for c in decoded)
            or any(p in {'.', '..', ''} for p in decoded.split('/')[1:])):
        raise SchemaError('Reference must be a canonical resource URI without query or fragment')
    if decoded != parsed.path and any(x in parsed.path.lower() for x in ('%2f', '%5c', '%25')):
        raise SchemaError('Encoded separators or repeated encoding are unsupported')
    return "viking://resources" + decoded


def inspect_entity(schema: EntitySchema, uri: str, markdown: str, *, reference_keys=None, namespace_root=None):
    """Inspect structure and enumerate dependencies without declaring publication ready.

    reference_keys explicitly maps an object-list reference field to its URI key;
    e.g. asserted_faults -> fault_uri. Unspecified object references are rejected.
    """
    import hashlib
    uri = source_uri(uri)
    if not isinstance(markdown, str) or len(markdown.encode()) > 1024 * 1024:
        raise SchemaError('Invalid or oversized entity Markdown')
    lines = markdown.replace('\r\n', '\n').splitlines()
    if not lines or lines[0] != '---':
        raise SchemaError('Entity YAML frontmatter required')
    try:
        end = lines.index('---', 1)
    except ValueError as exc:
        raise SchemaError('Unterminated frontmatter') from exc
    metadata = _yaml('\n'.join(lines[1:end]))
    fields = {f.name: f for f in schema.fields}
    if set(metadata) - set(fields):
        raise SchemaError('Undeclared entity fields: ' + ', '.join(sorted(set(metadata) - set(fields))))
    references = []
    compiled_keys = {f.name: f.reference_key for f in schema.fields if f.reference_key}
    reference_keys = reference_keys or {}
    if any(name in compiled_keys and compiled_keys[name] != key for name, key in reference_keys.items()):
        raise SchemaError('Reference key conflicts with fixed schema')
    reference_keys = {**compiled_keys, **reference_keys}
    for gate in schema.structural_gates:
        if gate.kind == 'forbid' and set(gate.fields) & set(metadata):
            raise SchemaError('Forbidden entity field: ' + ', '.join(gate.fields))
    if any(not isinstance(key, str) or not key for key in reference_keys.values()):
        raise SchemaError('Invalid reference object key')
    if set(reference_keys) - {f.name for f in schema.fields if f.target and f.kind == 'list'}:
        raise SchemaError('Reference object keys require a declared list relationship')
    for name, field in fields.items():
        if name not in metadata:
            if field.required:
                raise SchemaError('Missing required field: ' + name)
            continue
        value = metadata[name]
        valid = {'string': isinstance(value, str), 'list': isinstance(value, list), 'dict': isinstance(value, dict),
                 'integer': type(value) is int, 'number': type(value) in {int, float}, 'boolean': type(value) is bool}[field.kind]
        if not valid or (field.kind == 'number' and type(value) is float and not math.isfinite(value)):
            raise SchemaError('Invalid field type: ' + name)
        if field.required and isinstance(value, str) and not value.strip():
            raise SchemaError('Empty required field: ' + name)
        if field.choices and (not isinstance(value, str) or value not in field.choices):
            raise SchemaError('Invalid enum value: ' + name)
        if not field.target:
            continue
        if field.kind not in {'string', 'list'}:
            raise SchemaError('Reference field requires a supported string/list adapter: ' + name)
        values = value if field.kind == 'list' else [value]
        for target in values:
            if name in reference_keys:
                key = reference_keys[name]
                attributes = dict(field.reference_attributes)
                if not isinstance(target, dict) or set(target) != {key, *attributes}:
                    raise SchemaError('Invalid object reference: ' + name)
                for attr, kind in attributes.items():
                    item = target[attr]
                    valid = {'string': isinstance(item, str) and bool(item.strip()),
                             'integer': type(item) is int, 'number': type(item) in {int, float},
                             'boolean': type(item) is bool}[kind]
                    if not valid or (kind == 'number' and type(item) is float and not math.isfinite(item)):
                        raise SchemaError('Invalid reference attribute: ' + name + '.' + attr)
                target = target[key]
            references.append(Reference(source_uri(target), field.target, name))
    if not any(reference.expected_kind == 'source' for reference in references):
        raise SchemaError('Entity requires at least one declared source reference')
    tokens = MarkdownIt('commonmark').parse('\n'.join(lines[end + 1:]))
    headings = [tokens[i + 1].content for i, token in enumerate(tokens)
                if token.type == 'heading_open' and token.tag == 'h2']
    required_sections = schema.required_sections
    if schema.body_templates:
        selected = [template for template in schema.body_templates if metadata.get('class_name') in template.class_names]
        if len(selected) != 1:
            raise SchemaError('No body template for entity class_name')
        required_sections = selected[0].required_sections
    if any(headings.count(section) != 1 for section in required_sections):
        raise SchemaError('Missing or duplicate required body section')
    positions = [headings.index(section) for section in required_sections]
    if positions != sorted(positions):
        raise SchemaError('Required body sections are out of order')
    for token in tokens:
        for child in token.children or ():
            if child.type == 'link_open':
                link = child.attrGet('href') or ''
                if link.startswith('viking:'):
                    references.append(Reference(source_uri(link), None, 'body'))
    _structural_gates(schema, uri, metadata, tokens)
    receipts = ()
    if schema.path_policy:
        from openviking.wiki.path_policy import execute_path_policy
        receipts = (execute_path_policy(schema, uri, metadata, markdown, namespace_root),)
    return Draft(schema.kind, uri, hashlib.sha256(markdown.encode()).hexdigest(),
                 tuple(dict.fromkeys(references)), schema.uncompiled_rules, receipts)


def _structural_gates(schema, uri, metadata, tokens):
    """Evaluate explicit structural declarations; prose/domain rules stay pending."""
    import json
    for gate in schema.structural_gates:
        if gate.kind == 'forbid':
            continue  # Checked before type/reference handling.
        values = [metadata.get(field) for field in gate.fields]
        if gate.kind == 'exactly_one':
            if sum(isinstance(value, str) and bool(value.strip()) for value in values) != 1:
                raise SchemaError('Exactly one anchor required: ' + ', '.join(gate.fields))
        elif gate.kind == 'nonempty':
            if not values[0] or (isinstance(values[0], str) and not values[0].strip()):
                raise SchemaError('Nonempty field required: ' + gate.fields[0])
        elif gate.kind == 'distinct':
            try:
                signatures = [json.dumps(value, sort_keys=True, allow_nan=False) for value in (values[0] or [])]
            except (TypeError, ValueError) as exc:
                raise SchemaError('Unsupported distinct-list value') from exc
            if len(signatures) != len(set(signatures)):
                raise SchemaError('Duplicate list entries: ' + gate.fields[0])
        elif gate.kind == 'parent_profiles':
            parent = source_uri(values[0])
            prefix = parent.removesuffix('/index.md') + '/profiles/'
            suffix = uri.removeprefix(prefix)
            if not parent.endswith('/index.md') or not uri.startswith(prefix) or '/' in suffix or not suffix.endswith('.md'):
                raise SchemaError('Entity is outside its referenced parent profiles directory')
        elif gate.kind == 'body_reference_order':
            expected = [source_uri(value) for value in (values[0] or [])]
            actual, inside = [], False
            for index, token in enumerate(tokens):
                if token.type == 'heading_open' and token.tag == 'h2':
                    inside = tokens[index + 1].content == gate.section
                if inside:
                    for child in token.children or ():
                        if child.type == 'link_open' and (child.attrGet('href') or '').startswith('viking:'):
                            link = source_uri(child.attrGet('href'))
                            if link in expected and link not in actual:
                                actual.append(link)
            if actual != expected:
                raise SchemaError('Body reference order does not match: ' + gate.fields[0])


async def resolve_references(draft: Draft, resolver: ReferenceResolver, *, account_id, namespace_id, schema_digest=None):
    """Validate references via a trusted adapter; preserve dependency revisions.

    Source links may be in another authorized namespace. Entity edges must stay
    in this collection's account/namespace. Returned revisions are dependencies,
    not sufficient proof for final publication without later CAS/ACL rechecks.
    """
    resolved = {}
    for reference in draft.references:
        target = await resolver(reference.uri)
        if not isinstance(target, ResolvedReference) or not target.authorized or not target.has_content or not isinstance(target.revision, str) or not target.revision.strip():
            raise SchemaError('Reference cannot be resolved to authorized actual content')
        if target.account_id != account_id:
            raise SchemaError('Reference account mismatch')
        if not isinstance(target.namespace_id, str) or not target.namespace_id.strip():
            raise SchemaError('Reference namespace is not registered')
        if reference.expected_kind and reference.expected_kind != 'source':
            if not target.entity_validated or not schema_digest or target.validation_schema_hash != schema_digest:
                raise SchemaError('Entity target lacks validation for the fixed schema')
            if target.kind != reference.expected_kind or target.account_id != account_id or target.namespace_id != namespace_id:
                raise SchemaError('Entity relation target type or collection mismatch')
        previous = resolved.get(reference.uri)
        if previous is not None and previous != target:
            raise SchemaError('Reference revision changed during validation')
        resolved[reference.uri] = target
    return tuple(sorted(resolved.items()))
