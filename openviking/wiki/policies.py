# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Versioned, truthful policy coverage for experimental Wiki extraction.

A coverage matrix is compiler output, never an authorization or publication
receipt. Prose in packages is data and cannot register executable handlers.

Publication adjudication (publication_adjudication) turns the former static
pending list into evidence-backed checks: every strict_diagnostic admission
must read a real receipt — the G1 entity catalog, the G2 derived-access
dependency keys, the seven durable joint-publication gate observations and
the admitted merge graph — and refuses with the named missing gate when any
is absent. No receipt object, no publication: the refusal is the default.
"""
from dataclasses import asdict

from openviking.wiki.page_rules import CLASSIFICATION, coverage_addendum
from openviking.wiki.path_policy import DECLARATION_HASHES
from openviking.wiki.schema import SchemaError, compile_bundle

POLICY_VERSION = 'wiki-policy-coverage-v3'
EXTRACTOR_VERSION = 'wiki-fixed-source-extractor-v3'


def _sany_page_rules(bundle):
    """Rule executors for the audited SANY package; None for any other bundle."""
    try:
        entities = {e.kind: e for e in compile_bundle(bundle)}
        if set(entities) != set(DECLARATION_HASHES):
            return None
        for kind in entities:
            declared = {r for r in entities[kind].uncompiled_rules if not r.startswith('shared:')}
            if not declared <= set(CLASSIFICATION[kind]):
                return None
        return coverage_addendum(bundle)
    except SchemaError:
        return None


def policy_coverage(bundle):
    entities = compile_bundle(bundle)
    addendum = _sany_page_rules(bundle)
    coverage = {
        'version': POLICY_VERSION,
        'schema_id': bundle.schema_id, 'schema_version': bundle.version,
        'schema_digest': bundle.digest,
        'entities': [{
            'kind': entity.kind,
            'supported': ['declared_field_types', 'required_fields', 'declared_enums',
                          'required_body_sections', 'body_template_selection',
                          'declared_source_fields', 'reference_object_shape'],
            'structural_gates': [asdict(gate) for gate in entity.structural_gates],
            'path_policy': asdict(entity.path_policy) if entity.path_policy else None,
            'uncompiled_rules': list(entity.uncompiled_rules),
        } for entity in entities],
        # Even a prose-free package has further publication requirements.
        'publication_pending': ['semantic_evidence_validation', 'schema_identity_resolution',
                                'trusted_entity_catalog', 'closed_relationships',
                                'derived_access_intersection', 'atomic_joint_publication'],
        'diagnostic_publishable': False,
    }
    if addendum is not None:
        # Every audited rule now has a named enforcement home; the per-page and
        # batch executors actually run. Nothing is claimed for other packages.
        rules_report = addendum['entities']
        compiled = 0
        for item in coverage['entities']:
            kind = item['kind']
            checked = set(rules_report[kind]['page_checked'])
            delegated = {entry['rule'] for entry in rules_report[kind]['delegated']}
            remaining = []
            for rule_id in item['uncompiled_rules']:
                name = rule_id.split(':', 1)[1] if rule_id.startswith('shared:') else rule_id
                if name in checked or name in delegated:
                    compiled += 1
                else:
                    remaining.append(rule_id)
            item['uncompiled_rules'] = remaining
        coverage['rules_executor'] = {'version': addendum['rules_executor'], 'compiled': compiled}
    return coverage


def check_build_profile(bundle, profile, *, adjudication=None):
    if profile not in {'draft', 'strict_diagnostic'}:
        raise SchemaError('Wiki build profile must be draft or strict_diagnostic')
    coverage = policy_coverage(bundle)
    if profile == 'strict_diagnostic':
        pending = [(item['kind'], rule) for item in coverage['entities']
                   for rule in item['uncompiled_rules']]
        if pending:
            raise SchemaError('SCHEMA_POLICY_PENDING: ' + ', '.join(kind + ':' + rule for kind, rule in pending))
        # No receipt object means no evidence has been produced yet; the static
        # refusal remains the only truthful answer in that state.
        if adjudication is None:
            raise SchemaError('WIKI_PUBLICATION_GATE_PENDING: ' + ', '.join(coverage['publication_pending']))
        missing = {name: reason for name, reason in adjudication().items() if reason}
        if missing:
            raise SchemaError('WIKI_PUBLICATION_GATE_PENDING: '
                              + ', '.join(name + ': ' + reason for name, reason in sorted(missing.items())))
        coverage['publication_adjudicated'] = True
    return coverage


def publication_adjudication(catalog_read, catalog_entry, merge_payload, gate_observation):
    """Name the first failed publication gate per category from real receipts.

    catalog_read accounts for the G1 entity catalog (callable returning the
    registered rows for the schema's namespace), catalog_entry verifies one
    canonical identity through the G1 authority API, merge_payload rechecks
    the admitted private merge graph and its contributors, and
    gate_observation replays the seven durable joint-publication categories.
    Each callable raises when its backing store is unreadable; a falsy result
    is recorded as that gate's missing reason. Nothing is approved from
    caller-supplied booleans.
    """
    if not callable(catalog_read) or not callable(catalog_entry) \
            or not callable(merge_payload) or not callable(gate_observation):
        raise SchemaError('Publication adjudication requires four receipt readers')
    missing = {}
    try:
        missing['trusted_entity_catalog'] = None if catalog_read() else 'entity catalog carries no registered entities'
    except Exception as exc:
        missing['trusted_entity_catalog'] = f'entity catalog unreadable: {exc}'
    try:
        entry = catalog_entry()
        missing['schema_identity_resolution'] = None if entry else 'no resolved canonical identity in catalog'
    except Exception as exc:
        missing['schema_identity_resolution'] = f'identity resolution unreadable: {exc}'
    try:
        payload = merge_payload()
        if not isinstance(payload, dict) or not payload.get('groups'):
            missing['closed_relationships'] = 'admitted merge graph is absent'
        elif any(group.get('pending_identity') for group in payload['groups']):
            missing['schema_identity_resolution'] = 'admitted group still carries pending identity'
        else:
            missing['closed_relationships'] = None
        contributors = [uri for group in payload['groups']
                        for uri in (group.get('contributors') or [])] if isinstance(payload, dict) else []
        missing['derived_access_intersection'] = None if contributors else \
            'no wiki contribution sources stamped for the derived-access gate'
    except SchemaError:
        raise
    except Exception as exc:
        missing.setdefault('closed_relationships', f'merge graph unreadable: {exc}')
        missing.setdefault('derived_access_intersection', f'merge graph unreadable: {exc}')
    try:
        observation = gate_observation()
        gaps = observation.get('missing', {}) if isinstance(observation, dict) else None
        missing['atomic_joint_publication'] = None if observation is not None and observation.get('verified') \
            else ('incomplete durable gates: ' + ', '.join(sorted(gaps)) if gaps else 'seven-gate observation missing')
    except Exception as exc:
        missing['atomic_joint_publication'] = f'gate observation unreadable: {exc}'
    return {name: reason for name, reason in missing.items() if reason}
