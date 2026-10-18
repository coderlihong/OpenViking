"""Authority-owned lexical rows and receipts in the publication transaction."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import time

from openviking.indexing.lexical import TOKENIZER_VERSION, encode, encode_query, tokenize
from openviking.indexing.manifest_codec import decode_manifest

logger = logging.getLogger(__name__)

LEXICAL_COMMANDS = ('lexical_write', 'lexical_search')

FILTER_FIELDS = {
    'uri': 'path', 'business_meta_tokens': 'list<string>', 'search_tags': 'list<string>',
    'account_id': 'string', 'namespace_id': 'string', 'context_type': 'string',
    'level': 'int64', 'document_id': 'string', 'content_revision': 'string',
    'index_generation': 'string', 'created_at': 'date_time', 'updated_at': 'date_time',
}

# One FTS mirror per account: BM25 statistics must never span accounts. The
# mirror is external-content over ``publication_lexical`` (rowid keyed), so it
# stores postings only -- the chunk text stays in the authority table.
_FTS_TOKENIZE = "unicode61 tokenchars '-_/'"
# Marker value for an account whose mirror is missing or partially built.
_INDEX_INCOMPLETE = -1
# Bounded rebuild batch: the single writer is released between batches.
_INDEX_BATCH_ROWS = 20000
_ACCOUNT_EXPR = "json_extract(payload,'$.account_id')"
_WARNED_UNINDEXED: set[str] = set()
# Accounts whose mirror raised a storage error, and when to try it again. A
# transient error must not silence the fast path for the life of the process:
# the whole-scope scan is correct but takes minutes on a tree-scoped corpus.
_MIRROR_RETRY: dict[str, float] = {}
_MIRROR_RETRY_SECONDS = 300.0
# BM25 saturation/length-normalisation, matching the portable ranker.
_BM25_K1 = 1.2
_BM25_B = 0.75


def _under_denied_root(uri: str, denied_roots: set[str]) -> bool:
    """True when ``uri`` is a denied root or lives under one.

    Ancestor-chain lookup against a set: O(depth) instead of O(len(denied_roots)).
    """
    if not denied_roots:
        return False
    if uri in denied_roots:
        return True
    parts = uri.split('/')
    for cut in range(len(parts) - 1, 1, -1):
        if '/'.join(parts[:cut]) in denied_roots:
            return True
    return False


def lexical_fts_table(account: str) -> str:
    """Return the per-account lexical FTS mirror table name."""
    digest = hashlib.sha1(account.encode('utf-8')).hexdigest()[:16]
    return f'publication_lexical_fts_{digest}'


def lexical_vocab_table(account: str) -> str:
    """Return the per-account term/vocabulary view over the mirror.

    Scoring a scoped BM25 needs the query terms' document frequencies and
    per-document term frequencies. ``fts5vocab`` resolves its target table inside
    the schema that owns the virtual table, so a ``temp`` vocab table cannot see
    the ``main`` mirror (it fails with "no such fts5 table"), and creating the
    vocab per query would need a write transaction on a read path. The view
    therefore lives beside the mirror and is created once, next to it.
    """
    digest = hashlib.sha1(account.encode('utf-8')).hexdigest()[:16]
    return f'publication_lexical_vocab_{digest}'


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
            # Index bookkeeping: ``rows`` is the number of rows already mirrored
            # into the account FTS table, or _INDEX_INCOMPLETE while the mirror
            # is missing/partial (searches then use the brute-force scan).
            db.execute('''CREATE TABLE IF NOT EXISTS publication_lexical_index (
                account TEXT PRIMARY KEY, rows INTEGER NOT NULL,
                tokenizer TEXT NOT NULL, updated REAL NOT NULL)''')
            # Scope index: (account, uri, generation, rowid) -> document length.
            # A search resolves the caller's authorized scope through this primary
            # key instead of scanning ``publication_lexical`` (hundreds of MB of
            # payloads) to join uri/generation, which is what made a tree-scoped
            # query take ten minutes. The rowid belongs to the key: one URI has
            # one row per published chunk, and a narrower key would silently keep
            # only a single chunk per URI and return truncated results.
            db.execute('''CREATE TABLE IF NOT EXISTS publication_lexical_scope (
                account TEXT NOT NULL, uri TEXT NOT NULL, generation TEXT NOT NULL,
                rowid INTEGER NOT NULL, doc_len INTEGER NOT NULL,
                PRIMARY KEY(account, uri, generation, rowid)) WITHOUT ROWID''')
            scope_sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='publication_lexical_scope'"
            ).fetchone()
            if scope_sql is not None and 'generation, rowid)' not in (scope_sql[0] or ''):
                # Older scope table keyed by (account, uri, generation) only: it
                # collapses a URI's chunks into one row. Drop it and let the sweep
                # rebuild, so no search ever reads a truncated scope.
                db.execute('DROP TABLE IF EXISTS publication_lexical_scope')
                db.execute('''CREATE TABLE publication_lexical_scope (
                    account TEXT NOT NULL, uri TEXT NOT NULL, generation TEXT NOT NULL,
                    rowid INTEGER NOT NULL, doc_len INTEGER NOT NULL,
                    PRIMARY KEY(account, uri, generation, rowid)) WITHOUT ROWID''')
            if db.execute('SELECT 1 FROM publication_lexical_scope LIMIT 1').fetchone() is None:
                # The scope index is new (or empty) while mirrors may already be
                # marked complete: they cannot answer scope lookups, so mark every
                # account for rebuild. Searches keep using the whole-scope scan
                # until the sweep finishes.
                for row in db.execute(f'SELECT DISTINCT {_ACCOUNT_EXPR} FROM publication_lexical'):
                    if row[0]:
                        db.execute('UPDATE publication_lexical_index SET rows=? WHERE account=?',
                                   (_INDEX_INCOMPLETE, row[0]))
            # A mirror built before the vocabulary view existed cannot rank a
            # scoped query; create the view here so the fast path can serve it
            # without waiting for a rebuild.
            for row in db.execute('SELECT account FROM publication_lexical_index'):
                if row[0]:
                    self._ensure_lexical_vocab(db, row[0])

    # ------------------------------------------------------------------ index

    def _ensure_lexical_vocab(self, db, account):
        """Create the vocabulary view for an existing mirror (upgraded database)."""
        table = lexical_fts_table(account)
        if not self._fts_index_present(db, table):
            return False
        vocab = lexical_vocab_table(account)
        if self._fts_index_present(db, vocab):
            return True
        db.execute(f"CREATE VIRTUAL TABLE {vocab} USING fts5vocab({table}, 'instance')")
        return True

    def _ensure_lexical_fts(self, db, account):
        table = lexical_fts_table(account)
        db.execute(f'''CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING fts5(
            tokens, content='publication_lexical', content_rowid='rowid',
            tokenize="{_FTS_TOKENIZE}")''')
        vocab = lexical_vocab_table(account)
        db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {vocab} "
                   f"USING fts5vocab({table}, 'instance')")
        return table

    @staticmethod
    def _fts_index_present(db, table):
        row = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return row is not None

    @staticmethod
    def _lexical_index_rows(db, account):
        """Number of mirrored rows, or None when the mirror is unusable."""
        row = db.execute('SELECT rows, tokenizer FROM publication_lexical_index WHERE account=?',
                         (account,)).fetchone()
        if row is None or row['tokenizer'] != TOKENIZER_VERSION:
            return None
        rows = int(row['rows'])
        return rows if rows >= 0 else None

    def prune_superseded_lexical(self, older_than_seconds=86400.0, limit=2000):
        """Delete lexical rows for generations that are no longer reachable.

        ``publication_lexical`` is keyed ``(generation, child)`` and immutable, so
        every reindex writes a complete new copy of the text and the previous copy
        is never removed: live, 717,810 rows / 9.1 GB inside a 13.6 GB store, and
        the store only grows with each pass.

        A generation is droppable once it is no longer the *desired* generation of
        its URI, no snapshot a live lease pins still references it, and its build
        is older than ``older_than_seconds``. The lease guard is deliberate and
        total: nothing is pruned while any unexpired query/GC lease exists,
        because a lease is exactly what pins the snapshot a time-travelling reader
        captured.

        The per-account mirror is an FTS5 *external content* table over these
        rows. Its scope index is deleted with the row, which is what makes the
        prune safe: search resolves candidates from ``publication_lexical_scope``
        and only then joins the index, so a surviving posting for a pruned row
        cannot surface.
        """
        if (
            isinstance(older_than_seconds, bool)
            or not isinstance(older_than_seconds, (int, float))
            or not math.isfinite(older_than_seconds)
            or older_than_seconds < 0
        ):
            raise ValueError('Lexical prune age must be finite and nonnegative')
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError('Lexical prune batch must be a positive integer')
        now = time.time()
        cutoff = now - float(older_than_seconds)
        with self.db() as db:
            for table in ('leases', 'gc_leases'):
                if db.execute(f'SELECT 1 FROM {table} WHERE expires>? LIMIT 1', (now,)).fetchone():
                    return 0
            protected = self._protected_generations(db)
            candidates = [
                dict(row)
                for row in db.execute(
                    f'SELECT l.rowid AS rowid, l.generation AS generation, '
                    f'l.tokens AS tokens, {_ACCOUNT_EXPR} AS account '
                    f'FROM publication_lexical l JOIN builds b ON b.generation=l.generation '
                    f'WHERE b.created < ? '
                    f'  AND NOT EXISTS (SELECT 1 FROM desired d WHERE d.generation=l.generation) '
                    f'ORDER BY l.rowid LIMIT ?',
                    (cutoff, limit),
                )
            ]
            victims = [row for row in candidates if row['generation'] not in protected]
            if not victims:
                return 0
            rowids = [row['rowid'] for row in victims]
            marks = ','.join('?' * len(rowids))
            removed_by_account: dict[str, int] = {}
            for row in victims:
                account = row['account'] or ''
                removed_by_account[account] = removed_by_account.get(account, 0) + 1
            # No FTS5 'delete' per row: the mirror is an external-content index
            # with no bulk delete, and one command per row held the single writer
            # for 183s on a 5,000-row batch (live measurement). A stale posting is
            # inert here -- the mirror path resolves candidates from
            # ``publication_lexical_scope`` and only then joins the index, so a row
            # whose scope entry is gone can never surface. The postings are
            # reclaimed the next time the mirror is rebuilt.
            db.execute(f'DELETE FROM publication_lexical_scope WHERE rowid IN ({marks})', rowids)
            db.execute(f'DELETE FROM publication_lexical WHERE rowid IN ({marks})', rowids)
            for account, removed in removed_by_account.items():
                db.execute(
                    'UPDATE publication_lexical_index SET rows=MAX(rows-?,0) '
                    'WHERE account=? AND rows>=0',
                    (removed, account),
                )
        return len(victims)

    def lexical_index_status(self):
        """Report per-account mirror state for operations and the startup sweep."""
        with self.db(readonly=True) as db:
            return [dict(row) for row in db.execute(
                'SELECT account, rows, tokenizer, updated FROM publication_lexical_index ORDER BY account')]

    def _lexical_accounts_due(self, accounts):
        with self.db() as db:
            known = {row[0] for row in db.execute(f'SELECT DISTINCT {_ACCOUNT_EXPR} FROM publication_lexical') if row[0]}
            marked = {row['account']: int(row['rows']) for row in db.execute('SELECT account, rows FROM publication_lexical_index')}
            for account in known - set(marked):
                # Pre-existing rows from before the mirror existed: mark them so
                # the sweep rebuilds instead of searching a partial index.
                db.execute('INSERT OR IGNORE INTO publication_lexical_index VALUES (?,?,?,?)',
                           (account, _INDEX_INCOMPLETE, TOKENIZER_VERSION, time.time()))
                marked[account] = _INDEX_INCOMPLETE
            if accounts is not None:
                wanted = set(accounts)
                return sorted(a for a in marked if a in wanted)
            due = {account for account, rows in marked.items() if rows < 0}
            for account, rows in marked.items():
                if rows >= 0 and not self._fts_index_present(db, lexical_fts_table(account)):
                    db.execute('UPDATE publication_lexical_index SET rows=? WHERE account=?',
                               (_INDEX_INCOMPLETE, account))
                    due.add(account)
            return sorted(due)

    def rebuild_lexical_index(self, accounts=None, batch_rows: int = _INDEX_BATCH_ROWS):
        """(Re)build per-account lexical FTS mirrors from ``publication_lexical``.

        Batched so the single writer is only held for short transactions; the
        final catch-up and the marker flip happen in one transaction, so a
        mirror is either complete or still marked incomplete. Searches fall back
        to the brute-force scan until the marker flips.
        """
        if isinstance(batch_rows, bool) or not isinstance(batch_rows, int) or batch_rows < 1:
            raise ValueError('Lexical rebuild batch must be a positive integer')
        return {account: self._rebuild_lexical_account(account, batch_rows)
                for account in self._lexical_accounts_due(accounts)}

    def _rebuild_lexical_account(self, account, batch_rows):
        table = lexical_fts_table(account)
        vocab = lexical_vocab_table(account)
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO publication_lexical_index VALUES (?,?,?,?)',
                       (account, _INDEX_INCOMPLETE, TOKENIZER_VERSION, time.time()))
            db.execute(f'DROP TABLE IF EXISTS {vocab}')
            db.execute(f'DROP TABLE IF EXISTS {table}')
            db.execute(f'''CREATE VIRTUAL TABLE {table} USING fts5(
                tokens, content='publication_lexical', content_rowid='rowid',
                tokenize="{_FTS_TOKENIZE}")''')
            db.execute(f"CREATE VIRTUAL TABLE {vocab} USING fts5vocab({table}, 'instance')")
            db.execute('DELETE FROM publication_lexical_scope WHERE account=?', (account,))
        watermark = 0
        inserted = 0
        while True:
            with self.db() as db:
                rows = db.execute(
                    f'SELECT rowid, tokens, uri, generation, length(tokens) AS doc_len FROM publication_lexical '
                    f'WHERE rowid>? AND {_ACCOUNT_EXPR}=? ORDER BY rowid LIMIT ?',
                    (watermark, account, batch_rows)).fetchall()
                if not rows:
                    break
                db.executemany(f'INSERT INTO {table}(rowid, tokens) VALUES (?,?)',
                               [(row['rowid'], row['tokens']) for row in rows])
                db.executemany('INSERT OR REPLACE INTO publication_lexical_scope VALUES (?,?,?,?,?)',
                               [(account, row['uri'], row['generation'], row['rowid'], row['doc_len'])
                                for row in rows])
                inserted += len(rows)
                watermark = rows[-1]['rowid']
                if len(rows) < batch_rows:
                    break
        with self.db() as db:
            # Under the write lock: catch up on rows written during the batched
            # pass, then publish the marker only if the mirror is complete.
            while True:
                rows = db.execute(
                    f'SELECT rowid, tokens, uri, generation, length(tokens) AS doc_len FROM publication_lexical '
                    f'WHERE rowid>? AND {_ACCOUNT_EXPR}=? ORDER BY rowid LIMIT ?',
                    (watermark, account, batch_rows)).fetchall()
                if not rows:
                    break
                db.executemany(f'INSERT INTO {table}(rowid, tokens) VALUES (?,?)',
                               [(row['rowid'], row['tokens']) for row in rows])
                db.executemany('INSERT OR REPLACE INTO publication_lexical_scope VALUES (?,?,?,?,?)',
                               [(account, row['uri'], row['generation'], row['rowid'], row['doc_len'])
                                for row in rows])
                inserted += len(rows)
                watermark = rows[-1]['rowid']
                if len(rows) < batch_rows:
                    break
            expected = db.execute(f'SELECT count(*) FROM publication_lexical WHERE {_ACCOUNT_EXPR}=?',
                                  (account,)).fetchone()[0]
            # Count what this rebuild mirrored. ``count(*)`` on an external-content
            # FTS5 table reports the *content* table's rows, so it cannot tell a
            # complete mirror from a partial one; the rows this pass inserted can.
            # Writers cannot add rows while this frame holds the write lock, and
            # ``lexical_write`` skips mirroring while the marker is incomplete, so
            # the catch-up above has seen every row of the account.
            complete = inserted == expected
            db.execute('UPDATE publication_lexical_index SET rows=?, tokenizer=?, updated=? WHERE account=?',
                       (expected if complete else _INDEX_INCOMPLETE, TOKENIZER_VERSION, time.time(), account))
            if complete:
                _WARNED_UNINDEXED.discard(account)
            return {'account': account, 'rows': inserted, 'expected': expected, 'complete': complete}

    # ------------------------------------------------------------------ write

    def lexical_write(self, generation, entries):
        """Materialize every text child and acknowledge only in the same commit.

        The account's FTS mirror is updated in this same transaction, so the
        index can never acknowledge a row it did not index.
        """
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
            account = build['account']
            state = db.execute('SELECT rows FROM publication_lexical_index WHERE account=?', (account,)).fetchone()
            incomplete = state is not None and int(state['rows']) < 0
            table = self._ensure_lexical_fts(db, account)
            inserted = 0
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
                tokens = encode(entry['chunk_text'])
                cursor = db.execute('INSERT OR IGNORE INTO publication_lexical VALUES (?,?,?,?,?,?)',
                                    (generation, entry['chunk_id'], build['uri'], tokens, TOKENIZER_VERSION, canonical))
                if not cursor.rowcount:
                    continue
                inserted += 1
                if not incomplete:
                    # A rebuild in progress marks the account incomplete first, so
                    # this branch cannot double-index a row the rebuild also scans.
                    db.execute(f'INSERT INTO {table}(rowid, tokens) VALUES (?,?)', (cursor.lastrowid, tokens))
                    # ``doc_len`` mirrors SQLite's ``length(tokens)`` (characters) so
                    # scope lookups and the rebuild agree on the ranking inputs.
                    db.execute('INSERT OR REPLACE INTO publication_lexical_scope VALUES (?,?,?,?,?)',
                               (account, build['uri'], generation, cursor.lastrowid, len(tokens)))
            if incomplete:
                db.execute('UPDATE publication_lexical_index SET updated=? WHERE account=?', (time.time(), account))
            elif state is None:
                total = db.execute(f'SELECT count(*) FROM publication_lexical WHERE {_ACCOUNT_EXPR}=?',
                                   (account,)).fetchone()[0]
                rows = inserted if total == inserted else _INDEX_INCOMPLETE
                db.execute('INSERT OR REPLACE INTO publication_lexical_index VALUES (?,?,?,?)',
                           (account, rows, TOKENIZER_VERSION, time.time()))
            else:
                db.execute('UPDATE publication_lexical_index SET rows=rows+?, updated=? WHERE account=?',
                           (inserted, time.time(), account))
            receipt = db.execute('INSERT OR IGNORE INTO receipts VALUES (?,?)', (generation, marker)).rowcount
            return self._receipt_recorded(db, self._build(db, generation), receipt)

    # ----------------------------------------------------------------- search

    def lexical_search(self, account, lease, authorized_uris, query, limit=100, metadata_filter=None):
        """Rank only current-authorized rows in the caller's fixed leased view.

        Visibility (lease snapshots, ACL, tombstones) is resolved before
        ranking: matching runs against the account's FTS mirror joined to the
        leased (uri, generation) scope, so unpublished versions, denied URIs and
        other accounts can never enter the candidate set. BM25 statistics are
        computed over the account's own published lexical rows -- never across
        accounts -- which is the isolation boundary this index maintains.

        Accounts whose mirror is missing or partial (a database written before
        the mirror existed, or a rebuild in flight) fall back to materialising
        the authorized corpus: identical results, whole-scope scan.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError('Lexical limit must be an integer from 1 to 1000')
        if not isinstance(authorized_uris, list) or any(not isinstance(uri, str) for uri in authorized_uris):
            raise ValueError('Explicit authorized source URI list required')
        match = encode_query(query)
        if metadata_filter is not None:
            validate_lexical_filter(metadata_filter)
            from openviking.storage.vectordb.index.cuvs_index import matches_filter
        with self.db(readonly=True) as db:
            held = db.execute('SELECT snapshots FROM leases WHERE id=? AND expires>?', (lease, time.time())).fetchone()
            if not held:
                raise ValueError('Lexical query lease expired')
            manifest = {}
            for snapshot_id in json.loads(held['snapshots']):
                snapshot = db.execute('SELECT account,manifest FROM snapshots WHERE id=?', (snapshot_id,)).fetchone()
                if snapshot is None or snapshot['account'] != account:
                    raise ValueError('Lexical query snapshot account mismatch')
                manifest.update(decode_manifest(snapshot['manifest']))
            denied = [row[0] for row in db.execute('SELECT uri FROM tombstones WHERE account=?', (account,))]
            table = lexical_fts_table(account)
            if (_MIRROR_RETRY.get(account, 0.0) <= time.monotonic()
                    and self._lexical_index_rows(db, account) is not None
                    and self._fts_index_present(db, table)
                    and self._fts_index_present(db, lexical_vocab_table(account))):
                try:
                    indexed = self._lexical_search_indexed(db, account, table, manifest, denied, authorized_uris,
                                                           match, tuple(dict.fromkeys(tokenize(query))),
                                                           limit, metadata_filter)
                except sqlite3.OperationalError as error:
                    # Serving results beats serving speed: fall back to the scan
                    # (correct, slower) and retry the mirror after a cooldown.
                    _MIRROR_RETRY[account] = time.monotonic() + _MIRROR_RETRY_SECONDS
                    logger.warning('Lexical FTS mirror for account %s is unusable (%s); '
                                   'falling back to the whole-scope scan', account, error)
                else:
                    _MIRROR_RETRY.pop(account, None)
                    if indexed is not None:
                        return indexed
            if account not in _WARNED_UNINDEXED:
                _WARNED_UNINDEXED.add(account)
                logger.warning('Lexical FTS mirror for account %s is not ready; '
                               'falling back to the whole-scope scan until rebuild_lexical_index completes', account)
            return self._lexical_search_scan(db, manifest, denied, authorized_uris, match, limit, metadata_filter)

    @staticmethod
    def _lexical_scope(manifest, denied, authorized_uris):
        """Return the (uri, generation) pairs the caller may search.

        The tombstone check walks the URI's own ancestor chain against a set of
        denied roots.  Scanning the denied list per URI instead cost
        O(authorized x denied) string compares -- on the enterprise corpus
        (131,771 authorized URIs against 59,711 tombstones) that was ~8e9
        operations, i.e. the 318-second read transaction measured on a tree
        query, while every SQL statement inside it ran in under a second.
        """
        denied_roots = {root.rstrip("/") for root in denied}
        scope = []
        for uri in sorted(set(authorized_uris)):
            entry = manifest.get(uri)
            if entry is None:
                continue
            if _under_denied_root(uri, denied_roots):
                continue
            scope.append((uri, entry['generation']))
        return scope

    def _lexical_search_indexed(self, db, account, table, manifest, denied, authorized_uris,
                                match, terms, limit, metadata_filter):
        """Exact BM25 over the effective scope, read from the persistent index.

        The mirror supplies the postings; the *statistics* are recomputed on
        every query for exactly the rows the caller may see (lease snapshots,
        ACL, tombstones, metadata filter). That is why an unpublished or denied
        document can never shift a score, and why the corpus is never
        re-tokenized per query: only term postings, document lengths and the
        matching rows are read.
        """
        vocab = lexical_vocab_table(account)
        scope = self._lexical_scope(manifest, denied, authorized_uris)
        if not scope:
            return []
        db.execute('DROP TABLE IF EXISTS temp.query_scope')
        db.execute('CREATE TEMP TABLE query_scope (uri TEXT NOT NULL, generation TEXT NOT NULL, '
                   'PRIMARY KEY(uri, generation)) WITHOUT ROWID')
        db.executemany('INSERT OR IGNORE INTO query_scope VALUES (?,?)', scope)
        db.execute('DROP TABLE IF EXISTS temp.query_rows')
        db.execute('CREATE TEMP TABLE query_rows (rowid INTEGER PRIMARY KEY, doc_len INTEGER NOT NULL)')
        scoped = [(row['rowid'], row['doc_len']) for row in db.execute(
            'SELECT sc.rowid AS rowid, sc.doc_len AS doc_len FROM temp.query_scope s '
            'JOIN publication_lexical_scope sc ON sc.account=? AND sc.uri=s.uri AND sc.generation=s.generation',
            (account,))]
        if not scoped:
            # The scope index for this account has not been rebuilt yet. Answering
            # with nothing would be silently wrong, so the caller falls back to the
            # whole-scope scan until the sweep fills it.
            return None
        if metadata_filter is None:
            db.executemany('INSERT OR IGNORE INTO query_rows(rowid, doc_len) VALUES (?,?)', scoped)
        else:
            from openviking.storage.vectordb.index.cuvs_index import matches_filter
            lengths = dict(scoped)
            rowids = list(lengths)
            kept = []
            for start in range(0, len(rowids), 900):
                chunk = rowids[start:start + 900]
                for row in db.execute('SELECT rowid, payload FROM publication_lexical '
                                      'WHERE rowid IN (%s)' % ','.join('?' * len(chunk)), chunk):
                    if matches_filter(json.loads(row['payload']), metadata_filter, FILTER_FIELDS):
                        kept.append((row['rowid'], lengths[row['rowid']]))
            db.executemany('INSERT OR IGNORE INTO query_rows(rowid, doc_len) VALUES (?,?)', kept)
        stats = db.execute('SELECT count(*) AS docs, coalesce(avg(doc_len), 0) AS avgdl FROM temp.query_rows').fetchone()
        corpus_size = int(stats['docs'])
        if not corpus_size:
            return []
        avgdl = float(stats['avgdl']) or 1.0
        candidates = {row['rowid']: float(row['doc_len']) for row in db.execute(
            f'SELECT r.rowid, r.doc_len FROM temp.query_rows r JOIN {table} m ON m.rowid=r.rowid '
            f'WHERE {table} MATCH ?', (match,))}
        if not candidates:
            return []
        db.execute('DROP TABLE IF EXISTS temp.query_vocab')
        # The mirror and its vocabulary view live in ``main``; a temp-schema name
        # would fail with "no such fts5 table" and silently downgrade every query
        # to the whole-scope scan (minutes on a tree).
        scores: dict[int, float] = {}
        for term in terms:
            postings = {row['rowid']: int(row['tf']) for row in db.execute(
                f'SELECT v.doc AS rowid, count(*) AS tf FROM {vocab} v '
                'JOIN temp.query_rows r ON r.rowid = v.doc WHERE v.term = ? GROUP BY v.doc', (term,))}
            if not postings:
                continue
            document_frequency = len(postings)
            idf = math.log(1.0 + (corpus_size - document_frequency + 0.5) / (document_frequency + 0.5))
            for rowid, doc_len in candidates.items():
                frequency = postings.get(rowid)
                if not frequency:
                    continue
                denominator = frequency + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * doc_len / avgdl)
                scores[rowid] = scores.get(rowid, 0.0) + idf * frequency * (_BM25_K1 + 1.0) / denominator
        if not scores:
            return []
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
        payloads = {}
        rowids = [rowid for rowid, _ in ranked]
        for start in range(0, len(rowids), 900):
            chunk = rowids[start:start + 900]
            for row in db.execute('SELECT rowid, tokenizer, payload FROM publication_lexical '
                                  'WHERE rowid IN (%s)' % ','.join('?' * len(chunk)), chunk):
                if row['tokenizer'] != TOKENIZER_VERSION:
                    raise ValueError('LEXICAL_REBUILD_REQUIRED: snapshot tokenizer is obsolete')
                payloads[row['rowid']] = row['payload']
        return [dict(json.loads(payloads[rowid]), _score=score, score=score, lexical_raw=-score,
                     lexical_provider='authority_fts5', tokenizer_version=TOKENIZER_VERSION)
                for rowid, score in ranked if rowid in payloads]

    def _lexical_search_scan(self, db, manifest, denied, authorized_uris, match, limit, metadata_filter):
        """Whole-scope fallback: identical ranking over a materialised corpus.

        A temporary FTS corpus isolates BM25 statistics to exactly the
        authorized rows. It costs one pass over the authorized scope per call, so
        it is only used while an account mirror is unavailable.
        """
        if metadata_filter is not None:
            from openviking.storage.vectordb.index.cuvs_index import matches_filter
        db.execute('DROP TABLE IF EXISTS temp.query_lexical')
        db.execute(f'CREATE VIRTUAL TABLE temp.query_lexical USING fts5(tokens, payload UNINDEXED, tokenize="{_FTS_TOKENIZE}")')
        for uri, generation in self._lexical_scope(manifest, denied, authorized_uris):
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
        return self._lexical_rows(rows)

    @staticmethod
    def _lexical_rows(rows):
        return [dict(json.loads(row['payload']), _score=-row['raw'], score=-row['raw'], lexical_raw=row['raw'],
                     lexical_provider='authority_fts5_scan', tokenizer_version=TOKENIZER_VERSION)
                for row in rows]
