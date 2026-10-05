# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Explicit frozen business identity rules for private Wiki candidates.

Display names, provisional model IDs and similarities never supply implicit
business identity. This compiler supports declared primitive field tuples;
ontology identity, alias ownership and applicability semantics remain pending.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re

from openviking.wiki.schema import SchemaError, _yaml, compile_bundle

IDENTITY_VERSION = 'wiki-candidate-identity-v2'
POLICY_MEMBER = 'identity.policy.yaml'
_PRESENTATION = {'id', 'title', 'object_name', 'description', 'aliases', 'status',
                 'publication_state', 'validation_state', 'review_state'}


def identity_tuple(*, account_id, namespace_id, kind, schema_id, schema_version, schema_digest):
    """Canonical business identity anchor: (namespace, entity type, schema) tuple.

    Display names and provisional model IDs are deliberately absent so a rebuild
    of the same fixed schema/source resolves to the same canonical entity.
    """
    return {'account_id': account_id, 'namespace_id': namespace_id, 'entity_type': kind,
            'schema_id': schema_id, 'schema_version': schema_version, 'schema_digest': schema_digest}


def alias_ownership(groups):
    """Assign display names to a single canonical entity; collisions stay unowned.

    Aliases are presentation only. A name shared by two distinct canonical IDs is
    never silently merged: it is reported as a collision and left unowned.
    """
    owners = {}
    for group in groups:
        if not group.get('identity', {}).get('canonical_id'):
            continue
        for name in group.get('display_names', ()):
            owners.setdefault(name, []).append(group['candidate_id'])
    return {'aliases': {name: ids[0] for name, ids in owners.items() if len(set(ids)) == 1},
            'collisions': {name: sorted(set(ids)) for name, ids in owners.items() if len(set(ids)) > 1}}


def canonical_json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SchemaError('Candidate values must be finite JSON-compatible data') from exc


@dataclass(frozen=True)
class IdentityRule:
    kind: str
    key_fields: tuple[str, ...]
    scope_fields: tuple[str, ...]
    normalization: str


@dataclass(frozen=True)
class IdentityPolicy:
    version: str | None
    digest: str | None
    rules: tuple[IdentityRule, ...]

    @classmethod
    def compile(cls, bundle):
        text = dict(bundle.members).get(POLICY_MEMBER)
        if text is None:
            return cls(None, None, ())
        data = _yaml(text)
        if (set(data) != {'version', 'policy_version', 'entities'} or type(data['version']) is not int
                or data['version'] != 1 or not isinstance(data['policy_version'], str)
                or not re.fullmatch(r'[A-Za-z0-9_.+\-]{1,128}', data['policy_version'])
                or not isinstance(data['entities'], list) or not data['entities']):
            raise SchemaError('Unsupported identity policy declaration')
        schemas = {schema.kind: schema for schema in compile_bundle(bundle)}
        result, seen = [], set()
        for declaration in data['entities']:
            if (not isinstance(declaration, dict) or set(declaration) != {'kind', 'key_fields', 'scope_fields', 'normalization'}
                    or not isinstance(declaration['kind'], str) or declaration['kind'] not in schemas
                    or declaration['kind'] in seen or not isinstance(declaration['normalization'], str)
                    or declaration['normalization'] not in {'exact', 'casefold_space'}):
                raise SchemaError('Unsupported entity identity rule')
            key, scope = declaration['key_fields'], declaration['scope_fields']
            if (not isinstance(key, list) or not key or not isinstance(scope, list)
                    or any(not isinstance(name, str) or not name for name in key + scope)
                    or len(set(key + scope)) != len(key + scope) or set(key + scope) & _PRESENTATION):
                raise SchemaError('Identity requires distinct business fields; presentation/provisional fields are forbidden')
            fields = {field.name: field for field in schemas[declaration['kind']].fields}
            for name in key + scope:
                if name not in fields or fields[name].target or fields[name].kind not in {'string', 'integer', 'boolean', 'list'}:
                    raise SchemaError('Identity references an unsupported or undeclared business/scope field: ' + name)
                if name in key and fields[name].kind == 'list':
                    raise SchemaError('Business key must be a scalar; list scope preserves declared order')
            result.append(IdentityRule(declaration['kind'], tuple(key), tuple(scope), declaration['normalization']))
            seen.add(declaration['kind'])
        return cls(data['policy_version'], hashlib.sha256(text.encode()).hexdigest(), tuple(result))

    def describe(self):
        from dataclasses import asdict
        return {'compiler_version': IDENTITY_VERSION, 'policy_version': self.version, 'policy_digest': self.digest,
                'rules': [asdict(rule) for rule in self.rules],
                'supported': ['explicit_business_key', 'explicit_applicability_scope', 'account_namespace_type_schema_tuple',
                              'canonical_id_preserved_across_rebuilds', 'exact_or_explicit_casefold_space',
                              'literal_identity_quote_provenance', 'collision_aware_alias_ownership',
                              'expected_revision_human_edit_protection', 'no_default_synonym_merge'],
                'pending': ['semantic_identity_verification', 'ontology_and_applicability_policy',
                            'trusted_entity_catalog'],
                'diagnostic_publishable': False}


def _normalize(value, mode):
    if isinstance(value, str):
        if not value.strip():
            return None
        return re.sub(r'\s+', ' ', value).strip().casefold() if mode == 'casefold_space' else value
    if type(value) in {int, bool}:
        return value
    if isinstance(value, list) and value:
        # Arbitrary nested objects cannot silently become an identity key.
        if any(not isinstance(item, str) or not item.strip() for item in value):
            return None
        return [_normalize(item, mode) for item in value]
    return None


def _quoted(value, evidence, mode):
    values = value if isinstance(value, list) else [value]
    for item in values:
        needle = str(item).lower() if type(item) is bool else str(item)
        needle = _normalize(needle, mode)
        # Identifier boundaries also include code separators: P-001-extra is not P-001.
        pattern = r'(?<![A-Za-z0-9_./+\-])' + re.escape(needle) + r'(?![A-Za-z0-9_./+\-])'
        if not any(isinstance(proof, dict) and isinstance(proof.get('quote'), str)
                   and re.search(pattern, _normalize(proof['quote'], mode) or '') for proof in evidence):
            return False
    return True


def resolve_candidate_identity(policy, bundle, metadata, evidence, *, account_id, namespace_id, kind):
    anchor = identity_tuple(account_id=account_id, namespace_id=namespace_id, kind=kind,
                            schema_id=bundle.schema_id, schema_version=bundle.version, schema_digest=bundle.digest)
    rule = next((rule for rule in policy.rules if rule.kind == kind), None)
    if rule is None:
        return {'state': 'pending_identity', 'reason': 'identity_policy_missing', 'canonical_id': None,
                'identity_tuple': anchor, 'alias_ownership_verified': False}
    values = {}
    for name in rule.key_fields + rule.scope_fields:
        normalized = _normalize(metadata.get(name), rule.normalization)
        if normalized is None:
            return {'state': 'pending_identity', 'reason': 'identity_field_missing:' + name, 'canonical_id': None,
                    'identity_tuple': anchor, 'alias_ownership_verified': False}
        if not _quoted(metadata[name], evidence, rule.normalization):
            return {'state': 'pending_identity', 'reason': 'identity_evidence_missing:' + name, 'canonical_id': None,
                    'identity_tuple': anchor, 'alias_ownership_verified': False}
        values[name] = normalized
    parts = {'account_id': account_id, 'namespace_id': namespace_id, 'kind': kind,
             'schema_id': bundle.schema_id, 'schema_version': bundle.version, 'schema_digest': bundle.digest,
             'identity_compiler': IDENTITY_VERSION, 'identity_policy_version': policy.version,
             'identity_policy_digest': policy.digest,
             'business_key': {name: values[name] for name in rule.key_fields},
             'applicability_scope': {name: values[name] for name in rule.scope_fields}}
    return {'state': 'provisional_business_identity', 'reason': 'semantic_policy_pending',
            'canonical_id': 'candidate-' + hashlib.sha256(canonical_json(parts).encode()).hexdigest(),
            'identity_tuple': anchor, 'components': parts, 'alias_ownership_verified': False}
