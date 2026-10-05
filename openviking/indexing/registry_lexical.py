"""Authority-owned lexical rows and receipts in the publication transaction."""
from __future__ import annotations

import json
import time

from openviking.indexing.lexical import TOKENIZER_VERSION, encode, encode_query

LEXICAL_COMMANDS = ('lexical_write', 'lexical_search')

FILTER_FIELDS = {
    'uri': 'path', 'business_meta_tokens': 'list<string>', 'search_tags': 'list<string>',
    'account_id': 'string', 'namespace_id': 'string', 'context_type': 'string',
    'level': 'int64', 'document_id': 'string', 'content_revision': 'string',
    'index_generation': 'string', 'created_at': 'date_time', 'updated_at': 'date_time',
}


def validate_lexical_filter(predicate, depth=0):
    # Validate before ranking so unsupported constraints cannot be weakened.
    if depth > 32 or not isinstance(predicate, dict):
        raise ValueError('Unsupported lexical metadata predicate')
    op = predicate.get('op')
    if op in {'and', 'or'}:
        if set(predicate) != {'op', 'conds'} or not isinstance(predicate['conds'], list) or not predicate['conds']:
            raise ValueError('Unsupported lexical metadata predicate')
        for child in predicate['conds']:
            validate_lexical_filter(child, depth + 1)
    elif op in {'must', 'must_not'}:
        if (set(predicate) - {'op', 'field', 'conds', 'para'} or predicate.get('field') not in FILTER_FIELDS
                or 'conds' not in predicate):
            raise ValueError('Unsupported lexical metadata field or operator')
        if 'para' in predicate and (predicate['field'] != 'uri' or predicate['para'] not in {'-d=0', '-d=-1'}):
            raise ValueError('Unsupported lexical path predicate')
        if not isinstance(predicate['conds'], list):
            raise ValueError('Lexical membership conditions must be a list')
    elif op == 'contains':
        if (set(predicate) != {'op', 'field', 'substring'} or predicate['field'] not in FILTER_FIELDS
                or not isinstance(predicate['substring'], str)):
            raise ValueError('Unsupported lexical substring predicate')
    elif op in {'range', 'range_out'}:
        if (set(predicate) - {'op', 'field', 'gte', 'gt', 'lte', 'lt'}
                or predicate.get('field') not in FILTER_FIELDS
                or not set(predicate) & {'gte', 'gt', 'lte', 'lt'}):
            raise ValueError('Unsupported lexical range predicate')
        if FILTER_FIELDS[predicate['field']] != 'int64' or any(
                type(predicate[key]) is not int for key in ('gte', 'gt', 'lte', 'lt') if key in predicate):
            raise ValueError('Lexical range requires supported integer bounds; native time conversion remains pending')
    else:
        raise ValueError('Unsupported lexical metadata predicate')


class RegistryLexicalMixin:
    def init_lexical(self):
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS publication_lexical (
                generation TEXT NOT NULL, child TEXT NOT NULL, uri TEXT NOT NULL,
                tokens TEXT NOT NULL, tokenizer TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(generation, child))''')

    def lexical_write(self, generation, entries):
        """Materialize every text child and acknowledge only in the same commit."""
        if not isinstance(entries, list) or not entries:
            raise ValueError('Lexical generation requires nonempty child entries')
        with self.db() as db:
            build = self._build(db, generation)
            marker = 'lexical:' + build['plan']
            expected = set(json.loads(build['expected']))
            if marker not in expected:
                raise ValueError('Lexical receipt was not declared by this generation')
            if build['state'] not in {'building', 'queued'}:
                raise ValueError('Lexical generation is no longer writable')
            ids = [entry.get('chunk_id') for entry in entries]
            metadata_marker = 'metadata:' + build['plan']
            if len(set(ids)) != len(ids) or set(ids) != expected - {marker, metadata_marker}:
                raise ValueError('Lexical children must exactly match the declared vector children')
            metadata = db.execute('SELECT payload FROM publication_metadata WHERE generation=?', (generation,)).fetchone()
            if metadata_marker in expected and metadata is None:
                raise ValueError('Lexical receipt requires its declared immutable metadata projection')
            projection = {}
            if metadata is not None:
                from openviking.indexing.metadata_integration import project_meta
                projection = project_meta(json.loads(metadata['payload']))
            for entry in entries:
                if entry.get('uri') != build['uri'] or not isinstance(entry.get('chunk_text'), str) or not entry['chunk_text'].strip():
                    raise ValueError('Lexical source identity or text is invalid')
                payload = dict(entry, **projection)
                payload.update(index_generation=generation, account_id=build['account'], namespace_id=build['namespace'], level=2, abstract=entry['chunk_text'], context_type='resource')
                canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
                existing = db.execute('SELECT payload,tokenizer FROM publication_lexical WHERE generation=? AND child=?',
                                      (generation, entry['chunk_id'])).fetchone()
                if existing and (existing['payload'] != canonical or existing['tokenizer'] != TOKENIZER_VERSION):
                    raise ValueError('Immutable lexical generation changed; create a new generation')
                db.execute('INSERT OR IGNORE INTO publication_lexical VALUES (?,?,?,?,?,?)',
                           (generation, entry['chunk_id'], build['uri'], encode(entry['chunk_text']), TOKENIZER_VERSION, canonical))
            db.execute('INSERT OR IGNORE INTO receipts VALUES (?,?)', (generation, marker))
            return self._publish_if_complete(db, build)

    def lexical_search(self, account, lease, authorized_uris, query, limit=100, metadata_filter=None):
        """Rank only current-authorized rows in the caller's fixed leased view.

        A temporary FTS corpus also isolates BM25 statistics from denied accounts,
        unpublished versions and denied URIs. This prioritizes correct isolation;
        production corpus-scale performance remains an explicit acceptance gate.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError('Lexical limit must be an integer from 1 to 1000')
        if not isinstance(authorized_uris, list) or any(not isinstance(uri, str) for uri in authorized_uris):
            raise ValueError('Explicit authorized source URI list required')
        match = encode_query(query)
        if metadata_filter is not None:
            validate_lexical_filter(metadata_filter)
            from openviking.storage.vectordb.index.cuvs_index import matches_filter
        with self.db() as db:
            held = db.execute('SELECT snapshots FROM leases WHERE id=? AND expires>?', (lease, time.time())).fetchone()
            if not held:
                raise ValueError('Lexical query lease expired')
            manifest = {}
            for snapshot_id in json.loads(held['snapshots']):
                snapshot = db.execute('SELECT account,manifest FROM snapshots WHERE id=?', (snapshot_id,)).fetchone()
                if snapshot is None or snapshot['account'] != account:
                    raise ValueError('Lexical query snapshot account mismatch')
                manifest.update(json.loads(snapshot['manifest']))
            denied = [row[0] for row in db.execute('SELECT uri FROM tombstones WHERE account=?', (account,))]
            db.execute('CREATE VIRTUAL TABLE temp.query_lexical USING fts5(tokens, payload UNINDEXED, tokenize="unicode61 tokenchars \'-_/\'")')
            for uri in sorted(set(authorized_uris)):
                if uri not in manifest or any(uri == root or uri.startswith(root.rstrip('/') + '/') for root in denied):
                    continue
                generation = manifest[uri]['generation']
                rows = db.execute('SELECT tokens,tokenizer,payload FROM publication_lexical WHERE generation=? AND uri=? ORDER BY child',
                                  (generation, uri)).fetchall()
                for row in rows:
                    if row['tokenizer'] != TOKENIZER_VERSION:
                        raise ValueError('LEXICAL_REBUILD_REQUIRED: snapshot tokenizer is obsolete')
                    if metadata_filter is not None and not matches_filter(json.loads(row['payload']), metadata_filter, FILTER_FIELDS):
                        continue
                    db.execute('INSERT INTO query_lexical(tokens,payload) VALUES (?,?)', (row['tokens'], row['payload']))
            rows = db.execute('SELECT payload,bm25(query_lexical) AS raw FROM query_lexical WHERE query_lexical MATCH ? ORDER BY raw,rowid LIMIT ?',
                              (match, limit)).fetchall()
            return [dict(json.loads(row['payload']), _score=-row['raw'], score=-row['raw'], lexical_raw=row['raw'], lexical_provider='authority_fts5', tokenizer_version=TOKENIZER_VERSION)
                    for row in rows]
