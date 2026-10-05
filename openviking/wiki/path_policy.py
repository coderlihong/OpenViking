# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Finite SANY path policies; package prose and regex are never executed."""
from dataclasses import dataclass
import hashlib
import json
import re

from openviking.wiki.schema import SchemaError

VERSION = 'sany-paths-v1'
DECLARATION_HASHES = {
    'Component': '8a40b349de36e84d9f23a3e63c8eb739adc0619aec91268576810295a3f6b130',
    'ConfigSpec': 'ad15594558abc76b06b07b170a298b53b6a6b219d7ecdc58805043c9e4898cf0',
    'DTC': '75d10d05544805e8b4b7278305a289dd045b42a34f577038fc36b73866fd17e1',
    'Device': '773b52659fce9a56232fc5e285ffc4be8dcc07c7b7d86f7a1e70c2555667d21d',
    'Fault': 'b398bacfc1f81811bff60df5dcea87b4f2a21646ac9fd7254320ff6c63d465be',
    'Procedure': 'c7408dfa92973e2dd52e96e413b1edf0406a6b1d51c327d59deaa2282bfcc7d8',
    'Profile': '853d9141f7a674b9f4bb60798e22c29974e50c5448f7c512c51670b706b1b5bf',
    'Symptom': 'e893b6fae1c785b7b32767d5bcbb5cfc9a2286937f7e9e5454d4c5de611581e6',
}
COMPONENTS = frozenset('发动机 多路阀(主阀) 主泵 回转减速机 行走减速机 驾驶室 显示屏 引导轮 张紧装置 散热器 履带总成 回转支承 先导阀1 先导阀2 先导阀3 动臂油缸 斗杆油缸 铲斗油缸 回转接头 其他'.split())
FAULTS = frozenset('ControlFailure ElectricalFailure FluidFailure HydraulicFailure MechanicalFailure StructuralFailure ThermalFailure'.split())
PROCEDURES = frozenset('inspection replacement repair test disassembly'.split())


@dataclass(frozen=True)
class PathPolicy:
    version: str
    declaration_sha256: str


@dataclass(frozen=True)
class PathReceipt:
    rule_id: str
    executor_version: str
    schema_digest: str
    declaration_sha256: str
    entity_uri: str
    namespace_root: str
    entity_sha256: str
    status: str


def compile_path_policy(data, fields):
    declaration = data.get('path_layout')
    if declaration is None:
        return None
    if declaration != {'policy': VERSION}:
        raise SchemaError('Unsupported path policy declaration')
    kind = data['entity']
    sha = hashlib.sha256(json.dumps([data.get('path_pattern'), data.get('path_regex')], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    if DECLARATION_HASHES.get(kind) != sha:
        raise SchemaError('Audited path declaration changed; re-audit before executing')
    required = {'Device': ('series', 'model'), 'ConfigSpec': ('host_code',),
                'DTC': ('class_name', 'machine_model', 'code')}.get(kind, ('class_name',))
    if any(name not in fields or fields[name].kind != 'string' or not fields[name].required for name in required):
        raise SchemaError('Path policy requires declared mandatory string fields')
    return PathPolicy(VERSION, sha)


def execute_path_policy(schema, uri, metadata, markdown, namespace_root):
    from openviking.wiki.validation import source_uri

    if namespace_root is None:
        raise SchemaError('Explicit collection root required for path policy')
    uri = source_uri(uri)
    root = source_uri(namespace_root).rstrip('/')
    if not uri.startswith(root + '/'):
        raise SchemaError('Entity is outside its path policy collection root')
    parts = uri[len(root) + 1:].split('/')
    kind = schema.kind
    leaf = parts[-1]
    named = leaf.endswith('.md') and len(leaf) > 3
    ascii_code = lambda value: bool(re.fullmatch(r'[A-Za-z0-9._-]+', value))
    valid = False
    if kind == 'Component':
        valid = (parts[0] == 'Component' and len(parts) in {3, 4}
                 and parts[1] in COMPONENTS and parts[1] == metadata.get("class_name") and named
                 and (len(parts) == 3 or parts[2] == 'profiles'))
    elif kind == 'ConfigSpec':
        valid = (len(parts) == 3 and parts[0] == kind and ascii_code(parts[1])
                 and leaf == 'index.md' and parts[1] == metadata.get('host_code'))
    elif kind == 'Device':
        valid = (len(parts) == 4 and parts[0] == kind and leaf == 'index.md'
                 and all(ascii_code(p) for p in parts[1:3])
                 and parts[1] == metadata.get('series') and parts[2] == metadata.get('model'))
    elif kind == 'DTC':
        model = metadata.get('machine_model')
        directory = parts[1] if len(parts) == 3 else ''
        prefix = model + '_' if isinstance(model, str) and model else ''
        valid = (len(parts) == 3 and parts[0] == kind and prefix
                 and directory.startswith(prefix) and ascii_code(directory[len(prefix):])
                 and directory == metadata.get('class_name')
                 and named and leaf == str(metadata.get('code')) + '.md')
    elif kind in {'Fault', 'Procedure'}:
        choices = FAULTS if kind == 'Fault' else PROCEDURES
        valid = (len(parts) == 3 and parts[0] == kind and parts[1] in choices
                 and named and parts[1] == metadata.get('class_name'))
    elif kind == 'Symptom':
        valid = len(parts) == 4 and parts[0] == 'Symptom' and parts[1] == metadata.get('class_name') and leaf == 'index.md'
    elif kind == 'Profile':
        valid = len(parts) == 5 and parts[0] == 'Symptom' and parts[1] == metadata.get('class_name') and parts[3] == 'profiles' and named
    if not valid:
        raise SchemaError('Path policy violation: ' + kind)
    return PathReceipt('schema:path_layout', schema.path_policy.version,
                       schema.schema_digest, schema.path_policy.declaration_sha256,
                       uri, root, hashlib.sha256(markdown.encode()).hexdigest(), 'PASS')
