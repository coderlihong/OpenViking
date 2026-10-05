"""Read-only inventory of the referenced iroot schema bundle, not a Wiki validator.

Run with this repository's Python environment. No inputs are copied or modified.
The digest covers every Markdown/YAML member, including vocab and fewshot files.
"""
import argparse
import hashlib
import json
from pathlib import Path

import yaml


def inventory(directory):
    root = Path(directory).resolve(strict=True)
    hashes, entities = {}, {}
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.suffix not in {'.md', '.yaml', '.yml'}:
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('Schema bundle cannot include symlink members')
        name = path.relative_to(root).as_posix()
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        if path.name.endswith('.schema.yaml') and not path.name.startswith('_'):
            data = yaml.safe_load(path.read_text())
            kind = data['entity']
            if kind in entities:
                raise ValueError('Duplicate entity declaration: ' + kind)
            entities[kind] = {
                'schema_file': name,
                'markdown_example': kind + '.fewshot.md' if (root / (kind + '.fewshot.md')).is_file() else None,
                'path_pattern': data.get('path_pattern'),
                'fields': [{k: f[k] for k in ('name', 'type', 'required', 'ref_kind') if k in f}
                           for f in data.get('frontmatter', [])],
                'shared_not_required': data.get('shared_not_required', []),
                'body_sections': data.get('body_sections'),
                'rule_ids': [r['id'] for r in data.get('rules', [])],
            }
    if not entities:
        raise ValueError('No entity schema declarations found')
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    shared = yaml.safe_load((root / '_shared.schema.yaml').read_text())
    return {'source_directory': str(root), 'scope': 'Offline metadata inventory only; no runtime validation or schema registration',
            'declared_version': shared.get('version'), 'bundle_sha256': digest,
            'member_sha256': hashes, 'entities': entities}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory')
    args = parser.parse_args()
    print(json.dumps(inventory(args.directory), ensure_ascii=False, indent=2))
