# SPDX-License-Identifier: AGPL-3.0
"""Generation write windows and fail-closed physical GC intents.

Awaited adapter returns confirm a write window ended, not remote index deletion
visibility. No supported production delete/fence capability is installed here;
physical execution stays blocked rather than accepting a worker Boolean proof.
"""
import json
import math
import time
from uuid import uuid4

from openviking.storage.vector_ids import publication_member_id
from openviking_cli.exceptions import ConflictError, FailedPreconditionError, NotFoundError

GC_COMMANDS = ('generation_write_begin', 'generation_write_confirm', 'generation_write_unknown',
               'generation_writes', 'physical_gc_prepare', 'physical_gc_get', 'physical_gc_execute')


def _seconds(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError('GC duration must be finite and nonnegative')
    return value


class RegistryGCMixin:
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
            row = db.execute('SELECT manifest FROM snapshots WHERE id=?', (snapshot,)).fetchone()
            if row is None:
                raise FailedPreconditionError('Pinned snapshot dependency is missing')
            protected.update(item['generation'] for item in json.loads(row[0]).values())
        return protected

    def generation_write_begin(self, account, backend, records):
        """Register final bound/merged payloads immediately before adapter await."""
        if not isinstance(backend, str) or not backend or len(backend) > 1024:
            raise ValueError('A bounded backend identity is required')
        if not isinstance(records, list) or not 1 <= len(records) <= 10000:
            raise ValueError('A bounded managed write batch is required')
        bound = []
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
                    head = db.execute('SELECT s.manifest FROM heads h JOIN snapshots s ON s.id=h.snapshot WHERE h.account=? AND h.namespace=?',
                                      (account, row['namespace'])).fetchone()
                    live = (head and json.loads(head[0]).get(row['uri'], {}).get('generation') == generation) or generation in self._protected_generations(db)
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
