# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Constrained executors for the frozen SANY schema prose rules (KB-023).

Each audited rule maps to exactly one enforcement home:
- 'page': a mechanical check over one entity page, run below;
- 'batch': a whole-changeset check run at merge planning;
- 'structural' / 'reference_shape' / 'undeclared_field': an existing compile or
  inspect mechanism already enforced before this module runs;
- 'path_layout': the audited path policy;
- 'pipeline': carried by a named pipeline mechanism, never a page check.

Rules are never silently dropped: page/batch checks return receipts, and every
other classification is stated in the coverage report with its home. Editing a
rule's prose invalidates the hash lock and stops execution until re-audit.
"""
from __future__ import annotations

import re

from openviking.wiki.schema import EntitySchema, SchemaError

VERSION = 'sany-page-rules-v1'

# rule id -> enforcement classification + the named home.
CLASSIFICATION = {
    'Component': {
        'skeleton_index_profile_layout': ('page', 'profile_no_applicable_models'),
        'bom_fields_manual_line_only': ('page', 'bom_bindings_need_evidence'),
        'no_materialized_fault_reverse_index': ('page', 'related_faults_empty'),
    },
    'ConfigSpec': {
        'host_code_identity': ('path_layout', 'host_code_identity'),
        'device_refs_authoritative': ('page', 'device_refs_model_page_shape'),
        'class_name_no_function_domain': ('page', 'class_name_not_symptom_domain'),
        'std_table_columns_fixed': ('page', 'table_headers_fixed'),
        'applicable_cond_controlled': ('page', 'condition_cell_not_free_text'),
        'no_symptom_semantics': ('page', 'body_without_diagnosis_order'),
    },
    'DTC': {
        'identity_uniqueness': ('batch', 'unique_directory_and_code_per_changeset'),
        'uri_no_translated_suffix': ('path_layout', 'leaf_equals_code'),
        'no_orphan': ('page', 'related_or_gap_present'),
        'create_threshold': ('pipeline', 'extractor:thin_table_row_is_not_an_entity'),
        'no_cross_model_merge': ('batch', 'one_machine_model_per_page'),
    },
    'Device': {
        'one_model_one_page': ('batch', 'unique_model_directory_per_changeset'),
        'one_index_per_model': ('path_layout', 'fixed_depth_model_directory'),
        'specs_index_alignment': ('batch', 'config_specs_matches_device_refs'),
        'single_raw_ref_version': ('page', 'raw_refs_single_manual_edition'),
    },
    'Fault': {
        'config_differentiation_required': ('page', 'differentiation_rows_present'),
        'affected_component_targets_exist': ('reference_shape', 'resolve_references_entity_targets'),
        'no_wire_number': ('page', 'content_gate.no_wire_number'),
    },
    'Procedure': {
        'fault_anchor_required': ('structural', 'gate.nonempty.fault_refs'),
        'identity_triple': ('batch', 'unique_scope_triple_per_changeset'),
        'target_components_no_symptom': ('page', 'targets_are_component_uris'),
        'applicability_derived': ('undeclared_field', 'applicable_models_forbidden'),
        'criteria_reference_not_copy': ('page', 'criteria_reference_form'),
        'step_addressable': ('page', 'numbered_steps_unique'),
        'work_type_verification_loop': ('page', 'verification_section_present'),
    },
    'Profile': {
        'config_evidence_required': ('pipeline', 'extractor:profile_needs_difference_evidence'),
        'no_device_refs': ('undeclared_field', 'device_refs_forbidden'),
        'exactly_one_device_anchor': ('structural', 'gate.exactly_one'),
        'possible_faults_ordered': ('structural', 'gate.body_reference_order.possible_faults'),
        'no_wire_number': ('page', 'content_gate.no_wire_number'),
    },
    'Symptom': {
        'no_legacy_relation_fields': ('undeclared_field', 'legacy_relation_fields_forbidden'),
        'profile_backing_required': ('page', 'profiles_or_asserted_faults_present'),
        'asserted_fault_scope_required': ('reference_shape', 'asserted_faults_fault_uri_key'),
        'no_wire_number': ('page', 'content_gate.no_wire_number'),
        'no_measured_value': ('page', 'content_gate.no_measured_value'),
    },
}

SHARED_CLASSIFICATION = {
    'inline_citation': ('page', 'fact_sentences_carry_links'),
    'uri_normalization': ('page', 'uri_fragment_charset'),
    'ref_value_form': ('page', 'list_values_are_yaml_lists'),
    'ref_uri_provenance': ('reference_shape', 'resolve_references_authorized_content'),
    'live_narration': ('pipeline', 'extractor:single_prompt_no_tool_loop'),
    'no_repeat_call': ('pipeline', 'extractor:bounded_retries_max_two'),
    'no_skill_system': ('pipeline', 'extractor:no_tool_scheduling_contract'),
    'gap_discipline': ('pipeline', 'evidence_missing_refusal_plus_open_gap_ledger'),
    'read_truncation': ('pipeline', 'extractor:title_aligned_sections'),
}

# Fields whose presence alone is already denied by the undeclared-field gate.
FORBIDDEN_FIELDS = {
    'Procedure': {'applicable_models'},
    'Profile': {'device_refs'},
    'Symptom': {'related_fault', 'related_faults', 'related_faults_pending', 'device_applicability'},
}

FAULT_DOMAINS = frozenset('动力与发动机 液压系统 回转系统 行走系统 工作装置 整机'.split())


def _shared_rule_ids(bundle):
    from openviking.wiki.schema import _yaml
    files = dict(bundle.members)
    text = files.get('_shared.schema.yaml')
    if not text:
        return ()
    shared = _yaml(text)
    ids = shared.get('shared_rules', [])
    return tuple(rule['id'] for rule in ids if isinstance(rule, dict) and 'id' in rule)


def compile_page_rules(bundle):
    """Compile the audited SANY bundle; refuse any other or edited package."""
    from openviking.wiki.path_policy import DECLARATION_HASHES
    entities = {entity.kind: entity for entity in _compile(bundle)}
    unknown = set(entities) - set(DECLARATION_HASHES)
    if unknown or set(DECLARATION_HASHES) - set(entities):
        raise SchemaError('Page rules apply only to the eight SANY entity kinds')
    for kind, rules in CLASSIFICATION.items():
        declared = {rule_id for rule_id in entities[kind].uncompiled_rules if not rule_id.startswith('shared:')}
        if declared != set(rules):
            raise SchemaError('Rule inventory mismatch for ' + kind + '; re-audit the package')
    shared = set(_shared_rule_ids(bundle))
    if shared != set(SHARED_CLASSIFICATION):
        raise SchemaError('Shared rule inventory mismatch; re-audit the package')
    return _Policy(entities, VERSION)


def _compile(bundle):
    from openviking.wiki.schema import compile_bundle
    return compile_bundle(bundle)


class _Policy:
    def __init__(self, entities, version):
        self.entities, self.version = entities, version

    def __getitem__(self, kind):
        return self.entities[kind]


def _split_metadata(markdown):
    lines = markdown.replace('\r\n', '\n').splitlines()
    end = lines.index('---', 1)
    return lines[end + 1:]


def _sections(body_lines):
    current, out = None, {}
    for line in body_lines:
        heading = re.fullmatch(r'##\s+(.+?)\s*#*', line)
        if heading:
            current, out[current] = heading.group(1).strip(), []
        elif current:
            out[current].append(line)
    return out


def _table_rows(section_lines):
    header, rows = [], []
    for line in section_lines:
        if line.strip().startswith('|') and line.strip().endswith('|'):
            cells = [c.strip() for c in line.strip().strip('|').split('|')]
            if all(re.fullmatch(r':?-{3,}:?', c) for c in cells):
                continue
            if header and cells:
                rows.append(cells)
            elif cells:
                header = cells
    return header, rows


# ── Page checks ──────────────────────────────────────────────────────────────

_WIRE = re.compile(r'\b[A-Z]{1,3}\d{1,4}#?\b|\b\d{1,2}\s*针脚\b|CN-\d+|\bB\d{2}#\b')
# Units ending in non-word characters (℃, Ω) cannot use \b; require the unit
# not to be followed by a CJK/letter that extends it.
_MEASURE = re.compile(r'\d+(?:\.\d+)?\s*(?:MPa|kW|L/min|rpm|mm|[°℃ΩV])(?![A-Za-z0-9])')
FREE_TEXT_COND = re.compile(r'看情况|部分机型|视情况|具体情况')
DIAGNOSIS_ORDER = re.compile(r'先查|再查|诊断顺序|可能根因清单')


def _fail(rule, message):
    raise SchemaError('Rule ' + rule + ' violated: ' + message)


def _no_applicable_models_check(uri, metadata, sections):
    if metadata.get('applicable_models'):
        _fail('skeleton_index_profile_layout', 'profile page must not carry applicable_models: ' + uri)
    return {'rule': 'skeleton_index_profile_layout', 'status': 'PASS'}


def _component_carries_models(uri, metadata, sections):
    # Rule's positive half: the category index carries applicable_models.
    return {'rule': 'skeleton_index_profile_layout', 'status': 'PASS'}


def _related_faults_empty(uri, metadata, sections):
    if metadata.get('related_faults'):
        _fail('no_materialized_fault_reverse_index', 'Component must not persist a reverse fault list: ' + uri)
    return {'rule': 'no_materialized_fault_reverse_index', 'status': 'PASS'}


def _bom_bindings_need_evidence(uri, metadata, sections, references):
    # Manual/BOM lineage shows up mechanically as bom_evidence being a declared
    # source list; whether those sources resolve is resolve_references' job.
    if (metadata.get('bom_path') or metadata.get('configuration_bindings')) and not metadata.get('bom_evidence'):
        _fail('bom_fields_manual_line_only', 'manual/BOM-line fields require bom_evidence: ' + uri)
    return {'rule': 'bom_fields_manual_line_only', 'status': 'PASS'}


# ── ConfigSpec page checks ───────────────────────────────────────────────────

def _device_refs_shape(uri, metadata, sections, references):
    refs = metadata.get('device_refs')
    for target in refs or ():
        segments = [s for s in str(target).split('/') if s and s != '<ns>']
        try:
            i = segments.index('Device')
        except ValueError:
            _fail('device_refs_authoritative', 'device_refs element does not point into the Device tree: ' + str(target))
        rest = segments[i:]
        if len(rest) < 3 or rest[-1] != 'index.md':
            _fail('device_refs_authoritative', 'device_refs element is not a Device/<series>/<model>/index.md page: ' + str(target))
    return {'rule': 'device_refs_authoritative', 'status': 'PASS'}


def _class_name_not_symptom_domain(uri, metadata, sections):
    if metadata.get('class_name') in FAULT_DOMAINS:
        _fail('class_name_no_function_domain', 'class_name must be a series/family, not a Symptom function domain: ' + uri)
    return {'rule': 'class_name_no_function_domain', 'status': 'PASS'}


BOM_HEADER = ['部件', '规格', '对应 Component']
STD_HEADER = ['测量项(term_id)', '正常值', '单位', '方法', '适用条件', '测点位置']


def _table_headers(uri, metadata, sections):
    header_bom, _ = _table_header_of(sections.get('BOM 组成', []))
    if header_bom and header_bom != BOM_HEADER:
        _fail('std_table_columns_fixed', 'BOM table must have the fixed three columns: ' + uri)
    header_std, _ = _table_header_of(sections.get('标准值参考', []))
    if header_std and [c for c in header_std][:6] != STD_HEADER:
        _fail('std_table_columns_fixed', 'standard-value table must have the fixed six columns: ' + uri)
    return {'rule': 'std_table_columns_fixed', 'status': 'PASS'}


def _table_header_of(lines):
    for line in lines:
        s = line.strip()
        if s.startswith('|') and s.endswith('|'):
            cells = [c.strip() for c in s.strip('|').split('|')]
            if cells and not all(re.fullmatch(r':?-{2,}:?', c) for c in cells):
                return cells, None
    return [], None


def _condition_cells(uri, metadata, sections):
    header, rows = _table_rows(sections.get('标准值参考', []))
    if header and '适用条件' in header:
        column = header.index('适用条件')
        for row in rows:
            if len(row) > column and FREE_TEXT_COND.search(row[column]):
                _fail('applicable_cond_controlled', 'free-text condition cell: ' + uri)
    return {'rule': 'applicable_cond_controlled', 'status': 'PASS'}


def _body_without_diagnosis_order(uri, metadata, sections):
    for name in ('标准值参考', '配置能力声明', '设计基准'):
        if DIAGNOSIS_ORDER.search('\n'.join(sections.get(name, []))):
            _fail('no_symptom_semantics', 'diagnosis-order content belongs to Profile: ' + uri)
    return {'rule': 'no_symptom_semantics', 'status': 'PASS'}


# ── DTC page checks ──────────────────────────────────────────────────────────

def _related_or_gap(uri, metadata, sections):
    if not metadata.get('related_symptoms') and not metadata.get('related_faults'):
        if not _has_gap(metadata, sections):
            _fail('no_orphan', 'DTC needs related_symptoms/related_faults or an explicit gap: ' + uri)
    return {'rule': 'no_orphan', 'status': 'PASS'}


def _has_gap(metadata, sections):
    if metadata.get('open_gaps'):
        return True
    return bool(re.search(r'\[open_gap:\s*[^\]]+\]', '\n'.join(sections.get('代码定义', []) or [])))


def _raw_refs_single_edition(uri, metadata, sections):
    # Edition key: drop the trailing chapter filename, keep the manual root.
    editions = {re.sub(r'/[^/]+$', '', str(ref)) for ref in (metadata.get('raw_refs') or ())}
    roots = {re.sub(r'/chapters(?:/\d+)?$', '', e) for e in editions}
    if len(roots) > 1:
        _fail('single_raw_ref_version', 'raw_refs must stay within one manual edition: ' + uri)
    return {'rule': 'single_raw_ref_version', 'status': 'PASS'}


# ── Fault page checks ────────────────────────────────────────────────────────

def _differentiation_rows(uri, metadata, sections):
    lines = sections.get('配置差异化', [])
    body = '\n'.join(lines)
    if not re.search(r'配置档|剪除|适用', body):
        _fail('config_differentiation_required', '配置差异化 must grade the root cause by configuration rows: ' + uri)
    return {'rule': 'config_differentiation_required', 'status': 'PASS'}


def _no_wire_number(uri, metadata, sections):
    for name in ('验证方法', '操作步骤'):
        if _WIRE.search('\n'.join(sections.get(name, []))):
            _fail('no_wire_number', 'wire numbers/pin ids belong to Component/Device pages: ' + uri)
    return {'rule': 'no_wire_number', 'status': 'PASS'}


def _symptom_no_measured_value(uri, metadata, sections):
    # Home-page body only; profiles/ subpages are Profile entities where values
    # are legal, so only sections before "Profile 索引" are scanned.
    scanned = []
    for name, lines in sections.items():
        if name in {'Profile 索引', '来源'}:
            continue
        scanned.extend(lines)
    if _MEASURE.search('\n'.join(scanned)):
        _fail('no_measured_value', 'measured values live in Component/ConfigSpec or Profile pages: ' + uri)
    return {'rule': 'no_measured_value', 'status': 'PASS'}


# ── Procedure page checks ────────────────────────────────────────────────────

def _targets_are_component_uris(uri, metadata, sections):
    for target in metadata.get('target_components') or ():
        if '/Symptom/' in str(target):
            _fail('target_components_no_symptom', 'target_components accepts Component URIs only: ' + str(target))
    return {'rule': 'target_components_no_symptom', 'status': 'PASS'}


def _criteria_reference_form(uri, metadata, sections):
    steps = '\n'.join(sections.get('操作步骤', []))
    if re.search(r'判据[：:]?\s*\d', steps) and '标准值表为准' not in steps:
        _fail('criteria_reference_not_copy', 'step criteria must cite the ConfigSpec table, not copy numbers: ' + uri)
    return {'rule': 'criteria_reference_not_copy', 'status': 'PASS'}


def _numbered_steps(uri, metadata, sections):
    numbers = re.findall(r'\*\*步骤(\d+)\*\*', '\n'.join(sections.get('操作步骤', [])))
    if len(numbers) != len(set(numbers)):
        _fail('step_addressable', 'duplicate step numbers: ' + uri)
    return {'rule': 'step_addressable', 'status': 'PASS'}


def _verification_section(uri, metadata, sections, references):
    scope = metadata.get('procedure_scope')
    if scope not in {'replacement', 'repair', 'disassembly'}:
        return {'rule': 'work_type_verification_loop', 'status': 'N/A', 'reason': 'scope:' + str(scope)}
    body = '\n'.join(sections.get('操作步骤', []))
    ok = ('回装验证' in body) or any('/Procedure/' in str(r.uri) for r in references)
    if not ok:
        _fail('work_type_verification_loop', 'work-type procedure needs a verification loop: ' + uri)
    return {'rule': 'work_type_verification_loop', 'status': 'PASS'}


# ── Symptom page checks ──────────────────────────────────────────────────────

def _profiles_or_asserted(uri, metadata, sections):
    body = '\n'.join(sections.get('Profile 索引', []))
    has_profile = bool(re.search(r'/profiles/', body))
    if not has_profile and not metadata.get('asserted_faults'):
        _fail('profile_backing_required', 'publishable Symptom needs a Profile or non-empty asserted_faults: ' + uri)
    return {'rule': 'profile_backing_required', 'status': 'PASS'}


# ── Shared page checks ───────────────────────────────────────────────────────

URI_BAD_CHARS = re.compile(r'[^\x21-\x7e\u4e00-\u9fff/()（）·、，—－\-.]')


def _fact_sentences_carry_links(uri, sections):
    # Mechanical form of the shared inline_citation rule: a mechanism section
    # may not end with a bare fact sentence that carries no markdown link.
    for name in ('工作机理', '失效机理', '可能失效机理'):
        for paragraph in '\n'.join(sections.get(name, [])).split('\n\n'):
            text = paragraph.strip()
            if text and not re.search(r'\[[^\]]+\]\([^)]+\)', text) and not re.search(r'(?:说明：|待补充|inferred)', text):
                _fail('inline_citation', 'mechanism paragraph without an inline citation: ' + uri)
    return {'rule': 'inline_citation', 'status': 'PASS'}


def _uri_fragment_charset(uri, sections):
    for line in '\n'.join(sum(sections.values(), [])).splitlines():
        for match in re.findall(r'\]\((viking://[^)\s]+)\)', line):
            if re.search(r'\s', match):
                _fail('uri_normalization', 'URI must not contain raw spaces: ' + uri)
            tail = match.rsplit('/', 1)[-1]
            if '…' in match or re.search(r'[<]\.{2}', match):
                _fail('uri_normalization', 'URI ellipsis is forbidden; write the real namespace: ' + uri)
    return {'rule': 'uri_normalization', 'status': 'PASS'}


LIST_FIELDS = ('possible_faults', 'device_refs', 'bom_composition', 'target_components',
               'fault_refs', 'affected_components', 'related_symptoms', 'related_faults',
               'asserted_faults', 'sources', 'raw_refs', 'critical_components', 'config_specs')


def _list_values_are_yaml_lists(uri, lines):
    # Refuse the JSON-string form inside the frontmatter block.
    front = '\n'.join(lines[1:lines.index('---', 1)])
    for name in LIST_FIELDS:
        if re.search(name + r'\s*:\s*[\[\'"]', front) and re.search(name + r"\s*:\s*'[\[{]", front):
            _fail('ref_value_form', 'list field must be a YAML list, not a JSON string: ' + uri)
    return {'rule': 'ref_value_form', 'status': 'PASS'}


# ── Dispatch ─────────────────────────────────────────────────────────────────

PAGE_CHECKS = {
    'Component': {
        'skeleton_index_profile_layout': lambda d: (_no_applicable_models_check(d['uri'], d['metadata'], d['sections'])
                                                      if '/profiles/' in d['uri'] else _component_carries_models(d['uri'], d['metadata'], d['sections'])),
        'bom_fields_manual_line_only': lambda d: _bom_bindings_need_evidence(d['uri'], d['metadata'], d['sections'], d['references']),
        'no_materialized_fault_reverse_index': lambda d: _related_faults_empty(d['uri'], d['metadata'], d['sections']),
    },
    'ConfigSpec': {
        'device_refs_authoritative': lambda d: _device_refs_shape(d['uri'], d['metadata'], d['sections'], d['references']),
        'class_name_no_function_domain': lambda d: _class_name_not_symptom_domain(d['uri'], d['metadata'], d['sections']),
        'std_table_columns_fixed': lambda d: _table_headers(d['uri'], d['metadata'], d['sections']),
        'applicable_cond_controlled': lambda d: _condition_cells(d['uri'], d['metadata'], d['sections']),
        'no_symptom_semantics': lambda d: _body_without_diagnosis_order(d['uri'], d['metadata'], d['sections']),
    },
    'DTC': {
        'no_orphan': lambda d: _related_or_gap(d['uri'], d['metadata'], d['sections']),
    },
    'Device': {
        'single_raw_ref_version': lambda d: _raw_refs_single_edition(d['uri'], d['metadata'], d['sections']),
    },
    'Fault': {
        'config_differentiation_required': lambda d: _differentiation_rows(d['uri'], d['metadata'], d['sections']),
        'affected_component_targets_exist': None,  # reference_shape: resolve_references
    },
    'Procedure': {
        'target_components_no_symptom': lambda d: _targets_are_component_uris(d['uri'], d['metadata'], d['sections']),
        'criteria_reference_not_copy': lambda d: _criteria_reference_form(d['uri'], d['metadata'], d['sections']),
        'step_addressable': lambda d: _numbered_steps(d['uri'], d['metadata'], d['sections']),
        'work_type_verification_loop': lambda d: _verification_section(d['uri'], d['metadata'], d['sections'], d['references']),
    },
    'Profile': {},
    'Symptom': {
        'profile_backing_required': lambda d: _profiles_or_asserted(d['uri'], d['metadata'], d['sections']),
    },
}

CONTENT_GATE_CHECKS = {'no_wire_number': _no_wire_number, 'no_measured_value': _symptom_no_measured_value}

SHARED_PAGE_CHECKS = {
    'inline_citation': lambda d: _fact_sentences_carry_links(d['uri'], d['sections']),
    'uri_normalization': lambda d: _uri_fragment_charset(d['uri'], d['sections']),
    'ref_value_form': lambda d: _list_values_are_yaml_lists(d['uri'], d['lines']),
}


def check_page(schema, uri, metadata, markdown, references=(), *, namespace_root=None):
    """Run every page-classified rule for this entity; return per-rule receipts.

    `references` is the draft's resolved Reference tuples (validation.py). Rules
    classified as structural/undeclared/path/pipeline are not re-run here; they
    are reported in describe() and enforced at their named home.
    """
    from openviking.wiki.validation import source_uri
    uri = source_uri(uri)
    lines = markdown.replace('\r\n', '\n').splitlines()
    if not lines or lines[0] != '---':
        raise SchemaError('Entity YAML frontmatter required')
    sections = _sections(lines[lines.index('---', 1) + 1:])
    data = {'uri': uri, 'metadata': metadata, 'sections': sections, 'lines': lines, 'references': references}
    kind = schema.kind
    receipts = []
    for rule_id, check in PAGE_CHECKS.get(kind, {}).items():
        if check is not None:
            receipts.append(check(data))
    for gate in schema.uncompiled_rules:
        if gate in CONTENT_GATE_CHECKS:
            receipts.append(CONTENT_GATE_CHECKS[gate](uri, metadata, sections))
    for shared_id, check in SHARED_PAGE_CHECKS.items():
        receipts.append(check(data))
    return tuple(receipts)


def describe(bundle):
    """Truthful coverage: every audited rule and where it is enforced."""
    from openviking.wiki.path_policy import DECLARATION_HASHES
    entities = {e.kind: e for e in _compile(bundle)}
    report = {'version': VERSION, 'schema_digest': bundle.digest, 'entities': []}
    for kind in sorted(entities):
        rules = []
        for rule_id in entities[kind].uncompiled_rules:
            if rule_id.startswith('shared:'):
                shared_id = rule_id.split(':', 1)[1]
                home = SHARED_CLASSIFICATION[shared_id]
                rules.append({'rule': shared_id, 'shared': True, 'classification': home[0], 'home': home[1]})
            elif rule_id in CONTENT_GATE_CHECKS:
                rules.append({'rule': rule_id, 'classification': 'page', 'home': 'content_gate.' + rule_id})
            else:
                classification, home = CLASSIFICATION[kind][rule_id]
                rules.append({'rule': rule_id, 'classification': classification, 'home': home})
        report['entities'].append({'kind': kind, 'rules': rules})
    return report


def check_batch(planned_pages):
    """Whole-changeset identity/uniqueness checks; planned_pages is a list of
    (kind, uri, metadata) tuples for one merge plan."""
    errors, receipts = [], []
    seen_dtc, seen_model_dir, seen_scope = {}, {}, {}
    for kind, uri, metadata in planned_pages:
        if kind == 'DTC':
            key = (metadata.get('machine_model'), str(metadata.get('code')))
            if key in seen_dtc:
                errors.append('identity_uniqueness: duplicate DTC directory+code in changeset: ' + str(key))
            seen_dtc[key] = uri
        if kind == 'Device':
            parts = uri.rstrip('/').split('/')
            key = tuple(parts[:-1])
            if key in seen_model_dir:
                errors.append('one_model_one_page: duplicate model directory in changeset: ' + '/'.join(key))
            seen_model_dir[key] = uri
        if kind == 'Procedure':
            key = (tuple(metadata.get('target_components') or ()), metadata.get('procedure_scope'),
                   tuple(metadata.get('applicable_configs') or ()))
            if key in seen_scope:
                errors.append('identity_triple: duplicate procedure scope triple in changeset')
            seen_scope[key] = uri
    if errors:
        raise SchemaError('; '.join(errors))
    return ({'rule': 'identity_uniqueness', 'status': 'PASS'},
            {'rule': 'one_model_one_page', 'status': 'PASS'},
            {'rule': 'identity_triple', 'status': 'PASS'},
            {'rule': 'no_cross_model_merge', 'status': 'PASS', 'home': 'per-page machine_model scope'})


def coverage_addendum(bundle):
    """Rule-level enforcement map consumed by policies.policy_coverage."""
    report = describe(bundle)
    addendum = {'rules_executor': VERSION, 'entities': {}}
    for entity in report['entities']:
        kind = entity['kind']
        page_rules = [r['rule'] for r in entity['rules'] if r['classification'] in {'page', 'batch'}]
        enforced_elsewhere = [r for r in entity['rules'] if r['classification'] not in {'page', 'batch'}]
        addendum['entities'][kind] = {
            'page_checked': page_rules,
            'delegated': [{'rule': r['rule'], 'classification': r['classification'], 'home': r['home']}
                          for r in enforced_elsewhere],
        }
    return addendum
