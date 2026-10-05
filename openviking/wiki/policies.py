# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Versioned, truthful policy coverage for experimental Wiki extraction.

A coverage matrix is compiler output, never an authorization or publication
receipt. Prose in packages is data and cannot register executable handlers.
"""
from dataclasses import asdict

from openviking.wiki.page_rules import CLASSIFICATION, coverage_addendum
from openviking.wiki.path_policy import DECLARATION_HASHES
from openviking.wiki.schema import SchemaError, compile_bundle

POLICY_VERSION = 'wiki-policy-coverage-v2'
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


def check_build_profile(bundle, profile):
    if profile not in {'draft', 'strict_diagnostic'}:
        raise SchemaError('Wiki build profile must be draft or strict_diagnostic')
    coverage = policy_coverage(bundle)
    if profile == 'strict_diagnostic':
        pending = [(item['kind'], rule) for item in coverage['entities']
                   for rule in item['uncompiled_rules']]
        if pending:
            raise SchemaError('SCHEMA_POLICY_PENDING: ' + ', '.join(kind + ':' + rule for kind, rule in pending))
        raise SchemaError('WIKI_PUBLICATION_GATE_PENDING: ' + ', '.join(coverage['publication_pending']))
    return coverage
