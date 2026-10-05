"""Build/audit the known Sany structural adapter without modifying iroot sources.

Only structural examples are checked; references are not production-resolved.
"""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

from openviking.wiki.schema import SchemaBundle, adapt_sany_structural_gates, compile_bundle
from openviking.wiki.validation import inspect_entity


def build(input_zip, output_dir):
    with zipfile.ZipFile(input_zip) as archive:
        members = [info for info in archive.infolist() if not info.is_dir()]
        if (len(members) > 256 or len({info.filename for info in members}) != len(members)
                or any(info.file_size > 1024 * 1024 for info in members)
                or sum(info.file_size for info in members) > 16 * 1024 * 1024):
            raise ValueError('Invalid schema archive size/member count')
        source = SchemaBundle.freeze('sany', '2+openviking.1',
                                     {info.filename: archive.read(info).decode('utf-8') for info in members})
    adapted = adapt_sany_structural_gates(source)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_zip = output_dir / 'sany-schema-adapter-I026.zip'
    with zipfile.ZipFile(output_zip, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, text in adapted.members:
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 4, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, text.encode('utf-8'))
    root = 'viking://resources/sany'
    profile_uri = root + '/Symptom/动力与发动机/发动机冒白烟/profiles/国四和新欧五.md'
    try:
        original_profile = next(schema for schema in compile_bundle(source) if schema.kind == 'Profile')
        inspect_entity(original_profile, profile_uri,
                       dict(source.members)['Profile.fewshot.md'].replace('viking://resources/<ns>', root))
        original_replay = {'structure': 'PASS'}
    except Exception as exc:
        original_replay = {'structure': 'REJECTED', 'error': str(exc)}
    results = []
    for schema in compile_bundle(adapted):
        text = dict(adapted.members).get(schema.kind + '.fewshot.md', '').replace('viking://resources/<ns>', root)
        uri = (root + '/Symptom/动力与发动机/发动机冒白烟/profiles/国四和新欧五.md'
               if schema.kind == 'Profile' else root + '/' + schema.kind + '/example.md')
        try:
            draft = inspect_entity(schema, uri, text)
            result = {'structure': 'PASS', 'references': len(draft.references)}
        except Exception as exc:
            result = {'structure': 'REJECTED', 'error': str(exc)}
        results.append({'kind': schema.kind, **result,
                        'explicit_gates': len(schema.structural_gates),
                        'pending_checks': list(schema.uncompiled_rules),
                        'body_templates': len(schema.body_templates)})
    try:
        compile_bundle(adapted, require_executable_rules=True)
        strict = 'PASS'
    except Exception as exc:
        strict = 'REJECTED: ' + str(exc)
    audit = {
        'run_id': 'I026-schema-adapter', 'status': 'PARTIAL_IMPLEMENTATION',
        'input_digest': source.digest, 'adapter_digest': adapted.digest, 'version': adapted.version,
        'input_profile_direct_replay': original_replay,
        'zip_sha256': hashlib.sha256(output_zip.read_bytes()).hexdigest(),
        'entities': results, 'strict_compilation': strict,
        'exclusions': ['References only enumerated, not resolved to real production entities/sources',
                       'Path layouts, all prose policies, semantic evidence/identity/domain constraints not executed',
                       'No extraction, namespace/export authorization or publication'],
        'dtc_blocker': 'Upstream DTC fewshot source kb://chapters/813927429744789 is not a Viking URI; needs an explicit migration/mapping, not an invented source path',
    }
    (output_dir / 'wiki-schema-I026-2026-10-04.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n')
    return audit


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input_zip', type=Path)
    parser.add_argument('output_dir', type=Path)
    args = parser.parse_args()
    result = build(args.input_zip, args.output_dir)
    print(json.dumps({'adapter_digest': result['adapter_digest'], 'strict_compilation': result['strict_compilation']}, ensure_ascii=False))
