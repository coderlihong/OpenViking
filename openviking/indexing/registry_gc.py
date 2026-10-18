# SPDX-License-Identifier: AGPL-3.0
"""Generation write windows and fail-closed physical GC intents.

Awaited adapter returns confirm a write window ended, not remote index deletion
visibility. No supported production delete/fence capability is installed here;
physical execution stays blocked rather than accepting a worker Boolean proof.
"""
import json
import math
import sqlite3
import time
from uuid import uuid4

from openviking.indexing.manifest_codec import decode_manifest
from openviking.storage.vector_ids import publication_member_id
from openviking_cli.exceptions import ConflictError, FailedPreconditionError, NotFoundError

GC_COMMANDS = ('generation_write_begin', 'generation_write_confirm', 'generation_write_unknown',
               'generation_writes', 'physical_gc_prepare', 'physical_gc_get', 'physical_gc_execute')

# Snapshot rows are append-only (publication only ever INSERTs them), so the
# {uri: generation} index derived from a snapshot manifest is immutable and safe
# to memoize.  Without this, generation_write_begin re-decoded an ~11MB gzip
# manifest for *every* managed write (i.e. per embedded item) while holding the
# single writer lock, which is what put multi-second lock waits in front of
# every other writer during a reindex (measured 0.6-10s lock waits).
_SNAPSHOT_MANIFEST_CACHE: dict[str, dict] = {}
_SNAPSHOT_GENERATIONS_CACHE: dict[str, dict[str, str]] = {}
_SNAPSHOT_MANIFEST_CACHE_MAX = 2


def _cache_snapshot(snapshot, manifest):
    """Memoize one snapshot's manifest together with its {uri: generation} index.

    Both caches are keyed by snapshot id and must stay in step, so they are
    filled from the same call.
    """
    if len(_SNAPSHOT_MANIFEST_CACHE) >= _SNAPSHOT_MANIFEST_CACHE_MAX and snapshot not in _SNAPSHOT_MANIFEST_CACHE:
        _SNAPSHOT_MANIFEST_CACHE.clear()
        _SNAPSHOT_GENERATIONS_CACHE.clear()
    _SNAPSHOT_MANIFEST_CACHE[snapshot] = manifest
    index = {}
    for uri, item in manifest.items():
        if isinstance(item, dict) and item.get('generation'):
            index[uri] = item['generation']
    if len(_SNAPSHOT_GENERATIONS_CACHE) >= _SNAPSHOT_MANIFEST_CACHE_MAX and snapshot not in _SNAPSHOT_GENERATIONS_CACHE:
        _SNAPSHOT_GENERATIONS_CACHE.clear()
    _SNAPSHOT_GENERATIONS_CACHE[snapshot] = index
    return index


def record_snapshot_manifest(snapshot, manifest):
    """Seed the snapshot caches with a manifest the caller has just published.

    Every publish stores a *new* snapshot id, so a reader deriving the manifest
    itself would miss the cache almost every time and gunzip + parse a ~13MB
    blob (~154ms of GIL-bound CPU per call, once per file during reindex).  The
    publisher still holds the exact mapping it wrote, so hand it over instead.
    """
    _cache_snapshot(snapshot, manifest)


def _snapshot_manifest(db, snapshot):
    """Decoded manifest of one snapshot.

    Snapshot rows are append-only, so the decoded value is immutable and can be
    memoized across calls.  Callers must treat the returned mapping as
    read-only (copy before mutating an entry).
    """
    cached = _SNAPSHOT_MANIFEST_CACHE.get(snapshot)
    if cached is not None:
        return cached
    row = db.execute('SELECT manifest FROM snapshots WHERE id=?', (snapshot,)).fetchone()
    if row is None:
        raise FailedPreconditionError('Pinned snapshot dependency is missing')
    manifest = decode_manifest(row[0])
    _cache_snapshot(snapshot, manifest)
    return manifest


def _snapshot_generation_index(db, snapshot):
    """Immutable {uri: generation} view of one snapshot manifest."""
    cached = _SNAPSHOT_GENERATIONS_CACHE.get(snapshot)
    if cached is not None:
        return cached
    return _cache_snapshot(snapshot, _snapshot_manifest(db, snapshot))


def _tombstone_covers(db, account, uri):
    """True when uri or one of its ancestors has an active tombstone.

    The previous implementation scanned every tombstone of the account (59,713
    rows / ~21ms) and this runs on every source read, including from the event
    loop; primary-key lookups along the URI's ancestor chain answer the same
    question with a couple of dozen index seeks.
    """
    candidates = {uri, uri.rstrip('/')}
    trimmed = uri.rstrip('/')
    while '/' in trimmed:
        trimmed = trimmed.rsplit('/', 1)[0]
        if not trimmed:
            break
        candidates.add(trimmed)
        candidates.add(trimmed + '/')
    candidates.discard('')
    placeholders = ','.join('?' * len(candidates))
    row = db.execute(
        f'SELECT 1 FROM tombstones WHERE account=? AND uri IN ({placeholders}) LIMIT 1',
        (account, *candidates),
    ).fetchone()
    return row is not None


def _under_tombstone(uri, denied):
    """Set-based form of :func:`_tombstone_covers` for many URIs at once.

    ``denied`` is the account's tombstone URIs as a set; testing one URI costs a
    handful of hash lookups instead of a scan over every tombstone.
    """
    if uri in denied or uri.rstrip('/') in denied:
        return True
    trimmed = uri.rstrip('/')
    while '/' in trimmed:
        trimmed = trimmed.rsplit('/', 1)[0]
        if not trimmed:
            break
        if trimmed in denied or (trimmed + '/') in denied:
            return True
    return False


def _seconds(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError('GC duration must be finite and nonnegative')
    return value


class RegistryGCMixin:
    def checkpoint_wal(self, mode="TRUNCATE"):
        """Copy the WAL back into the database and truncate it.

        A write-ahead log that nothing checkpoints grows without bound: live it
        reached 2.5 GB against a 37 GB store, and every write transaction had to
        append to it while readers walked it.  ``TRUNCATE`` also returns the file
        to zero bytes, so the next burst starts from an empty log.
        """
        if mode not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
            raise ValueError("Unsupported WAL checkpoint mode")
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            row = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
            return tuple(row) if row else ()
        finally:
            conn.close()

    def prune_generation_write_windows(self, older_than_seconds=86400.0, limit=20000):
        """Drop confirmed managed-write windows nothing can still need.

        ``generation_write_begin`` inserts one row per managed write and no code
        ever deleted them: live, 1.19M rows / 545 MB inside a 37 GB store, all
        unreachable once their generation published.  ``writing``/``unknown``
        rows stay -- those are exactly what blocks GC for a generation.
        """
        _seconds(older_than_seconds)
        cutoff = time.time() - float(older_than_seconds)
        with self.db() as db:
            removed = db.execute(
                "DELETE FROM generation_write_windows WHERE token IN ("
                "  SELECT token FROM generation_write_windows "
                "  WHERE state='confirmed' AND created < ? ORDER BY created LIMIT ?)",
                (cutoff, int(limit)),
            ).rowcount
        return removed or 0

    def init_physical_gc(self):
        with self.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS generation_write_windows (
                token TEXT PRIMARY KEY, account TEXT NOT NULL, backend TEXT NOT NULL,
                records TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
                completed REAL);
              CREATE TABLE IF NOT EXISTS generation_gc_fences (
                generation TEXT PRIMARY KEY, intent TEXT NOT NULL, created REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS physical_gc_intents (
                id TEXT PRIMARY KEY, account TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                manifest TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
                UNIQUE(account,idempotency_key));
            ''')

    @staticmethod
    def _protected_generations(db):
        snapshots = {row[0] for row in db.execute('SELECT snapshot FROM heads')}
        for table in ('leases', 'gc_leases'):
            for row in db.execute(f'SELECT snapshots FROM {table} WHERE expires>?', (time.time(),)):
                snapshots.update(json.loads(row[0]))
        protected = set()
        for snapshot in snapshots:
            protected.update(_snapshot_generation_index(db, snapshot).values())
        return protected

    def generation_write_begin(self, account, backend, records):
        """Register final bound/merged payloads immediately before adapter await."""
        if not isinstance(backend, str) or not backend or len(backend) > 1024:
            raise ValueError('A bounded backend identity is required')
        if not isinstance(records, list) or not 1 <= len(records) <= 10000:
            raise ValueError('A bounded managed write batch is required')
        bound = []
        protected = None
        with self.db() as db:
            for fields in records:
                if not isinstance(fields, dict) or fields.get('account_id') != account:
                    raise ValueError('Managed write account mismatch')
                generation = fields.get('index_generation')
                row = self._build(db, generation)
                if (row['account'], row['uri'], row['plan']) != (account, fields.get('uri'), fields.get('index_plan_id')):
                    raise ValueError('Managed write generation identity mismatch')
                child = publication_member_id(fields)
                if child not in json.loads(row['expected']):
                    raise ValueError('Managed write member was not declared')
                if db.execute('SELECT 1 FROM generation_gc_fences WHERE generation=?', (generation,)).fetchone():
                    raise FailedPreconditionError('GENERATION_GC_FENCED: retired generation cannot receive writes')
                desired = db.execute('SELECT generation FROM desired WHERE account=? AND namespace=? AND uri=?',
                                     (account, row['namespace'], row['uri'])).fetchone()
                if row['state'] in {'building', 'queued'}:
                    live = desired and desired[0] == generation
                elif row['state'] == 'published':
                    if protected is None:
                        protected = self._protected_generations(db)
                    head = db.execute('SELECT s.id FROM heads h JOIN snapshots s ON s.id=h.snapshot WHERE h.account=? AND h.namespace=?',
                                      (account, row['namespace'])).fetchone()
                    live = (bool(head) and _snapshot_generation_index(db, head[0]).get(row['uri']) == generation) or generation in protected
                else:
                    live = False
                if not live:
                    raise FailedPreconditionError('Managed generation was superseded or retired before actual write')
                bound.append({'generation': generation, 'child': child, 'uri': row['uri']})
            if len({(item['generation'], item['child']) for item in bound}) != len(bound):
                raise ValueError('Duplicate managed write member')
            token = uuid4().hex
            db.execute("INSERT INTO generation_write_windows VALUES (?,?,?,?,'writing',?,NULL)",
                       (token, account, backend, json.dumps(bound, sort_keys=True), time.time()))
            return token

    def generation_write_confirm(self, account, token, backend):
        with self.db() as db:
            row = db.execute('SELECT * FROM generation_write_windows WHERE token=? AND account=?', (token, account)).fetchone()
            if row is None or row['backend'] != backend:
                raise ConflictError('Managed write token/backend identity mismatch')
            if row['state'] != 'writing':
                raise FailedPreconditionError('Only an awaited live write may confirm; unknown writes require backend recovery')
            db.execute("UPDATE generation_write_windows SET state='confirmed',completed=? WHERE token=?", (time.time(), token))
            return True

    def generation_write_unknown(self, account, token, backend):
        with self.db() as db:
            row = db.execute('SELECT * FROM generation_write_windows WHERE token=? AND account=?', (token, account)).fetchone()
            if row is None or row['backend'] != backend:
                raise ConflictError('Managed write token/backend identity mismatch')
            if row['state'] == 'writing':
                db.execute("UPDATE generation_write_windows SET state='unknown' WHERE token=?", (token,))
            return True

    def generation_writes(self, account):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM generation_write_windows WHERE account=? AND state IN ('writing','unknown') ORDER BY created", (account,))]

    @staticmethod
    def _gc_assert_no_unknown_writer(db, account):
        if db.execute('SELECT 1 FROM source_writes WHERE account=? LIMIT 1', (account,)).fetchone():
            raise FailedPreconditionError('WRITE_RECOVERY_REQUIRED: source writer protects account assets')
        if db.execute("SELECT 1 FROM generation_write_windows WHERE account=? AND state IN ('writing','unknown') LIMIT 1", (account,)).fetchone():
            raise FailedPreconditionError('VECTOR_WRITE_RECOVERY_REQUIRED: managed writer protects account assets')
        if db.execute('SELECT 1 FROM kb_parse_slot p JOIN kb_jobs j ON j.job_id=p.job_id WHERE j.account=? LIMIT 1', (account,)).fetchone():
            raise FailedPreconditionError('Global parse/write slot protects account assets')

    def physical_gc_prepare(self, account, generations, idempotency_key, grace_seconds=3600):
        import re
        _seconds(grace_seconds)
        if not isinstance(idempotency_key, str) or not re.fullmatch(r'[A-Za-z0-9_.+\-]{1,128}', idempotency_key):
            raise ValueError('Invalid physical GC idempotency key')
        if not isinstance(generations, list) or not 1 <= len(generations) <= 1000 or any(not isinstance(item, str) for item in generations) or len(set(generations)) != len(generations):
            raise ValueError('A bounded unique generation set is required')
        generations = sorted(generations)
        with self.db() as db:
            previous = db.execute('SELECT * FROM physical_gc_intents WHERE account=? AND idempotency_key=?', (account, idempotency_key)).fetchone()
            if previous:
                if sorted(item['generation'] for item in json.loads(previous['manifest'])['generations']) != generations:
                    raise ConflictError('Physical GC idempotency key refers to different generations')
                return dict(previous)
            self._gc_assert_no_unknown_writer(db, account)
            protected_generations = self._protected_generations(db)
            protected_blobs = self._protected_blobs(db)
            manifest = {'account': account, 'generations': [], 'delete_blobs': [],
                        'required_receipts': ['all_managed_writers_terminal', 'permanent_generation_write_fence',
                                              'vector_and_meta_absence_visible', 'lexical_absence', 'blob_absence',
                                              'current_reachability_cas', 'transactional_audit']}
            blobs = set()
            for generation in generations:
                row = self._build(db, generation)
                if row['account'] != account:
                    raise ConflictError('Physical GC generation account mismatch')
                if generation in protected_generations or row['state'] not in {'published','failed','superseded'} or time.time() - row['created'] < grace_seconds:
                    raise FailedPreconditionError('Generation is current, leased, in flight or within grace period')
                if db.execute('SELECT 1 FROM generation_gc_fences WHERE generation=?', (generation,)).fetchone():
                    raise ConflictError('Generation already belongs to another physical GC intent')
                source = db.execute('SELECT reference FROM sources WHERE generation=?', (generation,)).fetchone()
                semantic = db.execute('SELECT reference FROM semantic_sources WHERE generation=?', (generation,)).fetchone()
                if source:
                    reference = json.loads(source[0])
                    blobs.add(reference['sha256'])
                    if reference.get('image_semantics'):
                        blobs.add(reference['image_semantics']['sha256'])
                if semantic:
                    blobs.update(ref['sha256'] for ref in json.loads(semantic[0]).values())
                manifest['generations'].append({'generation': generation, 'uri': row['uri'], 'namespace': row['namespace'],
                                                'plan': row['plan'], 'members': json.loads(row['expected']),
                                                'lexical_rows': db.execute('SELECT count(*) FROM publication_lexical WHERE generation=?', (generation,)).fetchone()[0],
                                                'metadata_rows': db.execute('SELECT count(*) FROM publication_metadata WHERE generation=?', (generation,)).fetchone()[0]})
            manifest['delete_blobs'] = sorted(sha for sha in blobs if (account, sha) not in protected_blobs)
            intent = uuid4().hex
            now = time.time()
            db.execute("INSERT INTO physical_gc_intents VALUES (?,?,?,?,'capability_required',?)",
                       (intent, account, idempotency_key, json.dumps(manifest, sort_keys=True), now))
            for generation in generations:
                db.execute('INSERT INTO generation_gc_fences VALUES (?,?,?)', (generation, intent, now))
            return dict(db.execute('SELECT * FROM physical_gc_intents WHERE id=?', (intent,)).fetchone())

    def physical_gc_get(self, account, intent):
        with self.db() as db:
            row = db.execute('SELECT * FROM physical_gc_intents WHERE id=? AND account=?', (intent, account)).fetchone()
            if row is None:
                raise NotFoundError(intent, 'Physical GC intent')
            return dict(row)

    def physical_gc_execute(self, account, intent):
        # No backend currently supplies a durable all-writer fence together
        # with deletion visibility. Explicitly refuse; a Boolean callback or
        # request proof must never manufacture an all-index release receipt.
        saved = self.physical_gc_get(account, intent)
        manifest = json.loads(saved['manifest'])
        with self.db() as db:
            self._gc_assert_no_unknown_writer(db, account)
            protected_generations = self._protected_generations(db)
            protected_blobs = self._protected_blobs(db)
            if any(item['generation'] in protected_generations for item in manifest['generations']):
                raise ConflictError('Current reachability changed after physical GC intent')
            if any((account, sha) in protected_blobs for sha in manifest['delete_blobs']):
                raise ConflictError('A physical GC blob was referenced again after intent preparation')
            for item in manifest['generations']:
                fence = db.execute('SELECT intent FROM generation_gc_fences WHERE generation=?', (item['generation'],)).fetchone()
                if fence is None or fence['intent'] != intent:
                    raise ConflictError('Physical GC generation fence ownership changed')
        raise FailedPreconditionError('PHYSICAL_GC_CAPABILITY_REQUIRED: trusted backend generation fence and delete-visibility verifier unavailable')
