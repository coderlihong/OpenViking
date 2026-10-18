# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Service-side publication adjudication wired to real registry receipts.

The four readers demanded by ``publication_adjudication`` each read the
authoritative store directly — the G1 entity catalog rows, one canonical
identity matching the pinned schema through the same catalog rows, the sealed
merge state of the collection's snapshot pages (G2 contributor stamps) and the
joint-publication head over the collection's members — so the G3 gate never
accepts a caller-supplied boolean. Reader exceptions surface as that gate's
``unreadable`` reason; nothing here decides on memory state.
"""
from __future__ import annotations

import json

from openviking.indexing.manifest_codec import decode_manifest
from openviking.wiki.policies import publication_adjudication


def _binding(registry, account, collection_id):
    with registry.db() as db:
        row = db.execute('SELECT binding FROM wiki_collections WHERE account=? AND collection_id=?',
                         (account, collection_id)).fetchone()
    if row is None:
        raise ValueError('Wiki collection is not registered: ' + collection_id)
    return json.loads(row['binding'])


def _snapshot_pages(registry, account, namespace, root):
    # Content publication stores resource generations under the account's content
    # namespace 'resources' (kb_job_runtime.begin_batch), not the wiki namespace;
    # wiki pages appear in that head manifest with wiki_contributors stamps.
    with registry.db() as db:
        head = db.execute('SELECT snapshot FROM heads WHERE account=? AND namespace=?',
                          (account, 'resources' if namespace != 'resources' else namespace)).fetchone()
        if head is None:
            return {}
        manifest = db.execute('SELECT manifest FROM snapshots WHERE id=?', (head[0],)).fetchone()
    if manifest is None:
        return {}
    return {uri: item for uri, item in decode_manifest(manifest[0]).items()
            if uri.startswith(root.rstrip('/') + '/')}


def _page_state(pages):
    groups = [{'candidate_uri': uri,
               'kind': (item.get('content', {}).get('kind')
                        or uri.rsplit('/', 1)[-1].rsplit('.', 1)[0]),
               'contributors': list(item.get('content', {}).get('wiki_contributors') or []),
               'pending_identity': None}
              for uri, item in sorted(pages.items())]
    contributors = sorted({source for group in groups for source in group['contributors']})
    return {'groups': groups, 'contributors': contributors}


def service_adjudication(registry, *, account_id, collection_id, bundle):
    """Receipt readers over the registry for one pinned collection schema."""
    def catalog_read():
        rows = registry.wiki_entity_list(account_id, _binding(registry, account_id, collection_id)['namespace_id'])
        if not rows:
            return None
        return [dict(row) for row in rows]

    def catalog_entry():
        namespace = _binding(registry, account_id, collection_id)['namespace_id']
        matched = [row for row in registry.wiki_entity_list(account_id, namespace)
                   if row['schema_id'] == bundle.schema_id and row['schema_version'] == bundle.version
                   and row['schema_digest'] == bundle.digest]
        return dict(matched[0]) if matched else None

    def merge_payload():
        binding = _binding(registry, account_id, collection_id)
        return _page_state(_snapshot_pages(registry, account_id, binding['namespace_id'], binding['target_uri']))

    def gate_observation():
        binding = _binding(registry, account_id, collection_id)
        pages = _snapshot_pages(registry, account_id, binding['namespace_id'], binding['target_uri'])
        return {'verified': bool(pages), 'missing': {} if pages else {'head': ['no published member under the collection root']}}

    return publication_adjudication(catalog_read, catalog_entry, merge_payload, gate_observation)
