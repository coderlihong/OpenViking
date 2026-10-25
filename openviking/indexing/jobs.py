"""Authority-owned durable KB jobs and the global parse/write lease.

Lease expiry never proves an in-flight backend write stopped. A lost parser owner
keeps the global slot quarantined until backend recovery is implemented.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from uuid import uuid4

from openviking.indexing.manifest_codec import decode_manifest

KINDS = {'D1', 'D2', 'W1', 'W2'}
STAGES = {'validate', 'parse_write', 'index', 'wiki_extract', 'wiki_validate', 'publish_gate'}
TERMINAL = {'ready', 'draft_ready', 'failed', 'cancelled'}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _ttl(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 < value <= 3600:
        raise ValueError('lease seconds must be finite and in (0, 3600]')
    return float(value)


def _record(row):
    if row is None:
        return None
    result = dict(row)
    for key in ('payload', 'effective_config', 'checkpoint', 'info'):
        value = result[key]
        if isinstance(value, (dict, list)):
            continue
        try:
            result[key] = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            # Manual SQL maintenance may leave plain text in these columns;
            # surface it verbatim instead of wedging the worker.
            result[key] = {'raw': value} if value else {}
    return result


class DurableJobsMixin:
    def init_jobs(self):
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS kb_jobs (
                  job_id TEXT PRIMARY KEY, account TEXT NOT NULL, namespace TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL, kind TEXT NOT NULL, digest TEXT NOT NULL,
                  payload TEXT NOT NULL, effective_config TEXT NOT NULL, principal TEXT NOT NULL,
                  state TEXT NOT NULL, stage TEXT NOT NULL, attempt INTEGER NOT NULL,
                  owner TEXT, fence INTEGER NOT NULL, lease_until REAL NOT NULL,
                  available_at REAL NOT NULL, checkpoint TEXT NOT NULL, info TEXT NOT NULL,
                  created REAL NOT NULL, updated REAL NOT NULL,
                  UNIQUE(account,namespace,idempotency_key));
                CREATE INDEX IF NOT EXISTS kb_jobs_claim ON kb_jobs(state,available_at,created);
                CREATE TABLE IF NOT EXISTS kb_parse_slot (
                  slot INTEGER PRIMARY KEY CHECK(slot=1), job_id TEXT NOT NULL,
                  owner TEXT NOT NULL, fence INTEGER NOT NULL, state TEXT NOT NULL);
            ''')

    def job_create(self, account, namespace, idempotency_key, kind, payload, effective_config, principal):
        if kind not in KINDS:
            raise ValueError('Unknown KB job kind')
        for value in (account, namespace, principal):
            if not isinstance(value, str) or not value or len(value) > 256:
                raise ValueError('Job scope and principal must be non-empty strings')
        if not isinstance(idempotency_key, str) or not re.fullmatch(r'[A-Za-z0-9_.+\-]{1,128}', idempotency_key):
            raise ValueError('Invalid idempotency key')
        if not isinstance(payload, dict) or not isinstance(effective_config, dict):
            raise ValueError('Job payload and effective configuration must be objects')
        body = _json({'kind': kind, 'payload': payload, 'effective_config': effective_config, 'principal': principal})
        if len(body.encode()) > 1024 * 1024:
            raise ValueError('Job input exceeds 1 MiB limit')
        digest = hashlib.sha256(body.encode()).hexdigest()
        now = time.time()
        with self.db() as db:
            old = db.execute('SELECT * FROM kb_jobs WHERE account=? AND namespace=? AND idempotency_key=?',
                             (account, namespace, idempotency_key)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise ValueError('IDEMPOTENCY_CONFLICT: key names different inputs/configuration/principal')
                return _record(old)
            job_id = uuid4().hex
            db.execute('INSERT INTO kb_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (job_id, account, namespace, idempotency_key, kind, digest, _json(payload),
                        _json(effective_config), principal, 'queued', 'validate', 0, None, 0, 0,
                        now, '{}', '{}', now, now))
            return _record(db.execute('SELECT * FROM kb_jobs WHERE job_id=?', (job_id,)).fetchone())

    def job_get(self, account, job_id):
        with self.db() as db:
            return _record(db.execute('SELECT * FROM kb_jobs WHERE account=? AND job_id=?', (account, job_id)).fetchone())

    def job_publication_status(self, job_id, owner, fence):
        """Inspect actual member receipts of the batch bound to this job attempt.

        This proves submitted index members only, not completeness of the body
        tree or a full document-ready contract. Never accepts caller receipts.
        """
        with self.db() as db:
            job = self._job_owner(db, job_id, owner, fence)
            checkpoint = json.loads(job['checkpoint'])
            if job['kind'] not in {'D1', 'D2'} or job['stage'] != 'index':
                raise ValueError('Document publication status requires an index-stage document job')
            batch_id = checkpoint.get('publication_batch_id')
            batch = db.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
            if (batch is None or batch['account'] != job['account']
                    or batch['namespace'] != checkpoint.get('publication_namespace')):
                raise ValueError('Publication batch is not bound to this document job account/namespace')
            rows = db.execute('SELECT b.* FROM builds b JOIN batch_members m '
                              'ON b.generation=m.generation WHERE m.batch=? ORDER BY b.uri',
                              (batch_id,)).fetchall()
            members = []
            for row in rows:
                expected = set(json.loads(row['expected']))
                received = {r[0] for r in db.execute(
                    'SELECT child FROM receipts WHERE generation=?', (row['generation'],))}
                members.append({'uri': row['uri'], 'generation': row['generation'],
                                'state': row['state'], 'expected_count': len(expected),
                                'received_count': len(received & expected),
                                'complete': expected <= received})
            # Hash all real membership observations without growing the durable
            # checkpoint linearly with a large manual's chapter count.
            return {'batch_id': batch_id, 'state': batch['state'],
                    'member_count': len(members),
                    'expected_count': sum(m['expected_count'] for m in members),
                    'received_count': sum(m['received_count'] for m in members),
                    'all_submitted_members_complete': bool(members) and all(m['complete'] for m in members),
                    'membership_sha256': hashlib.sha256(_json(members).encode()).hexdigest(),
                    'document_ready_verified': False}

    @staticmethod
    def _gate_observation(db, batch_id, account, namespace, root_uri):
        """Evaluate all seven joint-publication categories from durable evidence.

        Each category reads its own committed artifact (receipts, lexical rows,
        semantic bindings, snapshot heads); nothing is taken from caller state.
        """
        missing = {name: [] for name in (
            'body_tree', 'images', 'parent_child', 'semantic_levels',
            'bm25', 'metadata', 'version_switch')}
        rows = db.execute('SELECT b.* FROM builds b JOIN batch_members m '
                          'ON b.generation=m.generation WHERE m.batch=? ORDER BY b.uri',
                          (batch_id,)).fetchall()
        if not rows:
            missing['body_tree'].append(root_uri)
        for row in rows:
            expected = set(json.loads(row['expected']))
            received = {r[0] for r in db.execute(
                'SELECT child FROM receipts WHERE generation=?', (row['generation'],))}
            if row['state'] != 'published' or not expected <= received:
                missing['body_tree'].append(row['uri'])
                continue
            uri, generation = row['uri'], row['generation']
            # Images: a body source may declare an immutable image-semantics
            # cache; directories carry none. The cache sha must be present.
            reference = db.execute('SELECT reference FROM sources WHERE generation=?',
                                   (generation,)).fetchone()
            if reference:
                content = json.loads(reference[0])
                if content.get('image_semantics'):
                    pass
                else:
                    declared_images = {child for child in expected if child.startswith('image:')}
                    if declared_images:
                        missing['images'].append(uri)
            # Parent-child: every vector child must recall its bounded parent
            # context, so a lost parent linkage cannot publish silently.
            lexical_rows = db.execute('SELECT child,payload FROM publication_lexical WHERE generation=?',
                                      (generation,)).fetchall()
            for entry in lexical_rows:
                payload = json.loads(entry['payload'])
                if 'parent_chunk_id' in payload and not payload['parent_chunk_id']:
                    missing['parent_child'].append(f"{uri}#{entry['child']}")
            # BM25 and business metadata follow their declared receipt markers;
            # a declaration without its durable row is a hard gap.
            if any(child.startswith('lexical:') for child in expected) and not lexical_rows:
                missing['bm25'].append(uri)
            metadata_row = None
            metadata_ref = db.execute(
                "SELECT 1 FROM receipts WHERE generation=? AND child LIKE 'metadata:%'",
                (generation,)).fetchone()
            if metadata_ref:
                metadata_row = db.execute('SELECT payload FROM publication_metadata WHERE generation=?',
                                          (generation,)).fetchone()
                if metadata_row is None:
                    missing['metadata'].append(uri)
            elif any(child.startswith('metadata:') for child in expected):
                missing['metadata'].append(uri)
            # L0/L1 directory navigation binds its verified sidecar revisions
            # through semantic_sources; a directory member without any binding
            # never carried semantic levels. Declared levels come from the
            # build's own expected members, so an L0-only directory is not
            # forced to fabricate a missing .overview.md.
            semantic = db.execute('SELECT reference FROM semantic_sources WHERE generation=?',
                                  (generation,)).fetchone()
            if semantic is not None:
                binding = json.loads(semantic[0])
                declared_levels = {child.split(':', 1)[1] for child in expected
                                   if child.startswith('semantic:')}
                for level in sorted(declared_levels):
                    if level not in binding or not binding[level].get('sha256'):
                        missing['semantic_levels'].append(f'{uri}:semantic:{level}')
        head = db.execute('SELECT snapshot FROM heads WHERE account=? AND namespace=?',
                          (account, namespace)).fetchone()
        published = db.execute("SELECT count(*) FROM batches WHERE id=? AND state='published'",
                               (batch_id,)).fetchone()[0]
        if not rows:
            return missing
        if not published or not head:
            missing['version_switch'].append(f'{account}/{namespace}')
            return missing
        from openviking.indexing.registry_gc import load_snapshot_manifest

        manifest = load_snapshot_manifest(db, head[0])
        for row in rows:
            item = manifest.get(row['uri'])
            if not item or item.get('generation') != row['generation']:
                missing['version_switch'].append(row['uri'])
        return missing

    def job_publication_ready(self, job_id, owner, fence):
        """Authority-verified joint publication over all seven coverage gates."""
        with self.db() as db:
            job = self._job_owner(db, job_id, owner, fence)
            checkpoint = json.loads(job['checkpoint'])
            if job['kind'] not in {'D1', 'D2'} or job['stage'] != 'index':
                raise ValueError('Document publication status requires an index-stage document job')
            batch_id = checkpoint.get('publication_batch_id')
            batch = db.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
            if (batch is None or batch['account'] != job['account']
                    or batch['namespace'] != checkpoint.get('publication_namespace')):
                raise ValueError('Publication batch is not bound to this document job account/namespace')
            missing = self._gate_observation(db, batch_id, job['account'],
                                             checkpoint.get('publication_namespace'),
                                             checkpoint.get('root_uri'))
            return {'batch_id': batch_id,
                    'verified': not any(missing.values()),
                    'missing': {name: sorted(set(gaps)) for name, gaps in missing.items() if gaps}}

    def job_publication_seal(self, job_id, owner, fence):
        """Fence producer closure and any resulting head switch in one transaction."""
        outcome = {}
        with self.db() as db:
            job = self._job_owner(db, job_id, owner, fence)
            checkpoint = json.loads(job['checkpoint'])
            if job['kind'] not in {'D1', 'D2'} or job['stage'] != 'index':
                raise ValueError('Document publication seal requires an index-stage document job')
            batch_id = checkpoint.get('publication_batch_id')
            batch = db.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
            if (batch is None or batch['account'] != job['account']
                    or batch['namespace'] != checkpoint.get('publication_namespace')):
                raise ValueError('Publication batch is not bound to this document job account/namespace')
            if batch['state'] not in {'building', 'queued', 'published'}:
                raise ValueError('Failed publication batch cannot be sealed')
            db.execute("UPDATE batches SET state='queued' WHERE id=? AND state='building'", (batch_id,))
            self._settle_batch(db, batch_id, outcome=outcome)
        # The publish runs after this transaction closes, so the batch state is
        # read on a fresh transaction: inside the frame it is still 'queued'.
        with self.db(readonly=True) as db:
            state = db.execute('SELECT state FROM batches WHERE id=?', (batch_id,)).fetchone()[0]
        return {'batch_id': batch_id, 'published': bool(outcome.get('published')),
                'state': state}

    def job_list(self, account, namespace=None, limit=100, principal=None):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Job list limit must be between 1 and 1000')
        with self.db() as db:
            rows = db.execute('SELECT * FROM kb_jobs WHERE account=? AND (? IS NULL OR namespace=?) AND (? IS NULL OR principal=?) ORDER BY created DESC,job_id DESC LIMIT ?',
                              (account, namespace, namespace, principal, principal, limit)).fetchall()
            return [_record(row) for row in rows]

    @staticmethod
    def _job_owner(db, job_id, owner, fence):
        row = db.execute('SELECT * FROM kb_jobs WHERE job_id=?', (job_id,)).fetchone()
        if row is None or row['owner'] != owner or row['fence'] != fence or row['lease_until'] <= time.time() or row['state'] != 'running':
            raise ValueError('STALE_OWNER: job lease/fence is not active')
        return row

    @staticmethod
    def _recover_jobs(db, now):
        expired = db.execute("SELECT * FROM kb_jobs WHERE state='running' AND lease_until<=?", (now,)).fetchall()
        for row in expired:
            slot = db.execute('SELECT * FROM kb_parse_slot WHERE job_id=?', (row['job_id'],)).fetchone()
            state = 'write_recovery_required' if slot else 'backoff'
            info = {'code': 'WRITE_RECOVERY_REQUIRED' if slot else 'WORKER_LEASE_EXPIRED'}
            if slot:
                db.execute("UPDATE kb_parse_slot SET state='uncertain' WHERE job_id=?", (row['job_id'],))
            db.execute('UPDATE kb_jobs SET state=?,owner=NULL,fence=fence+1,lease_until=0,available_at=?,info=?,updated=? WHERE job_id=?',
                       (state, now, _json(info), now, row['job_id']))
        return len(expired)

    def job_recover(self):
        with self.db() as db:
            return {'recovered': self._recover_jobs(db, time.time())}

    def job_claim(self, owner, lease_seconds=30, account=None, kinds=None):
        if not isinstance(owner, str) or not owner or len(owner) > 256:
            raise ValueError('Worker owner must be non-empty')
        lease_seconds = _ttl(lease_seconds)
        if kinds is not None and (not isinstance(kinds, list) or not kinds or any(k not in KINDS for k in kinds)):
            raise ValueError('Invalid worker job kinds')
        now = time.time()
        # The claim loop fires around the clock even when nothing is queued, and
        # every poll used to open a BEGIN IMMEDIATE that waited behind whoever
        # held the single writer (measured: 16.6s of lock wait on an idle
        # publication store). A deferred read never blocks and is never blocked,
        # so probe first and pay the write transaction only when there is
        # genuinely something to recover or claim. The probe mirrors the claim
        # predicate exactly, including expired running leases, which
        # _recover_jobs would have to rewind.
        with self.db(readonly=True) as db:
            due = db.execute(
                "SELECT 1 FROM kb_jobs WHERE (state='running' AND lease_until<=?)"
                " OR (state IN ('queued','backoff','written') AND available_at<=?) LIMIT 1",
                (now, now),
            ).fetchone()
        if due is None:
            return None
        with self.db() as db:
            self._recover_jobs(db, now)
            rows = db.execute("SELECT * FROM kb_jobs WHERE state IN ('queued','backoff','written') AND available_at<=? AND (? IS NULL OR account=?) ORDER BY created", (now, account, account)).fetchall()
            row = next((r for r in rows if kinds is None or r['kind'] in kinds), None)
            if row is None:
                return None
            db.execute("UPDATE kb_jobs SET state='running',attempt=attempt+1,owner=?,fence=fence+1,lease_until=?,updated=? WHERE job_id=?",
                       (owner, now + lease_seconds, now, row['job_id']))
            return _record(db.execute('SELECT * FROM kb_jobs WHERE job_id=?', (row['job_id'],)).fetchone())

    def job_heartbeat(self, job_id, owner, fence, lease_seconds=30):
        lease_seconds = _ttl(lease_seconds)
        with self.db() as db:
            self._job_owner(db, job_id, owner, fence)
            db.execute('UPDATE kb_jobs SET lease_until=?,updated=? WHERE job_id=?', (time.time() + lease_seconds, time.time(), job_id))
            return True

    def job_checkpoint(self, job_id, owner, fence, stage, checkpoint, info=None):
        if stage not in STAGES or not isinstance(checkpoint, dict) or (info is not None and not isinstance(info, dict)):
            raise ValueError('Invalid job stage/checkpoint/info')
        if len(_json(checkpoint).encode()) > 1024 * 1024:
            raise ValueError('Checkpoint exceeds 1 MiB')
        with self.db() as db:
            row = self._job_owner(db, job_id, owner, fence)
            slot = db.execute('SELECT 1 FROM kb_parse_slot WHERE job_id=?', (job_id,)).fetchone()
            if slot and stage != 'parse_write':
                raise ValueError('Parse slot must be released before changing stage')
            if stage in {'wiki_extract', 'wiki_validate'} and row['kind'] not in {'W1', 'W2'}:
                raise ValueError('Wiki stages require Wiki jobs')
            sequence = ['validate', 'parse_write', 'index']
            if row['kind'] in {'W1', 'W2'}:
                sequence.extend(['wiki_extract', 'wiki_validate'])
            sequence.append('publish_gate')
            previous, current = sequence.index(row['stage']), sequence.index(stage)
            uri_index_without_parse = row['kind'] == 'D2' and row['stage'] == 'validate' and stage == 'index'
            prepared_wiki = (row['kind'] in {'W1', 'W2'} and row['stage'] == 'validate'
                             and stage == 'wiki_extract' and json.loads(row['payload']).get('build_id'))
            # A wiki job that reached wiki_validate but failed the durable terminal
            # gate (or lost its worker) must be retryable: extract_wiki re-enters
            # wiki_extract to resume/re-run extraction. The terminal gate, not the
            # stage counter, is the authority on receipt validity, so permitting
            # this single rewind does not weaken any integrity check.
            wiki_retry = (row['kind'] in {'W1', 'W2'} and row['stage'] == 'wiki_validate'
                          and stage == 'wiki_extract')
            if ((current < previous and not wiki_retry)
                    or (current > previous + 1 and not (uri_index_without_parse or prepared_wiki))):
                raise ValueError('Job checkpoint cannot skip or rewind stages')
            if stage == 'parse_write' and not slot:
                raise ValueError('Parse stage requires the global parser lease')
            db.execute('UPDATE kb_jobs SET stage=?,checkpoint=?,info=?,updated=? WHERE job_id=?',
                       (stage, _json(checkpoint), _json(info or {}), time.time(), job_id))
            return True

    def job_parse_acquire(self, job_id, owner, fence):
        with self.db() as db:
            row = self._job_owner(db, job_id, owner, fence)
            slot = db.execute('SELECT * FROM kb_parse_slot WHERE slot=1').fetchone()
            if slot:
                return slot['job_id'] == job_id and slot['owner'] == owner and slot['fence'] == fence and slot['state'] == 'active'
            if row['stage'] not in {'validate', 'parse_write'}:
                raise ValueError('Job already passed parse/write stage')
            db.execute('INSERT INTO kb_parse_slot VALUES (1,?,?,?,?)', (job_id, owner, fence, 'active'))
            db.execute("UPDATE kb_jobs SET stage='parse_write',updated=? WHERE job_id=?", (time.time(), job_id))
            return True

    def job_parse_release(self, job_id, owner, fence, outcome, checkpoint=None):
        if outcome not in {'written', 'aborted', 'uncertain'}:
            raise ValueError('Invalid parser outcome')
        if checkpoint is not None and not isinstance(checkpoint, dict):
            raise ValueError('Parser checkpoint must be an object')
        with self.db() as db:
            self._job_owner(db, job_id, owner, fence)
            slot = db.execute('SELECT * FROM kb_parse_slot WHERE slot=1').fetchone()
            if slot is None or (slot['job_id'], slot['owner'], slot['fence']) != (job_id, owner, fence):
                raise ValueError('STALE_OWNER: parse slot does not belong to worker')
            now = time.time()
            if outcome == 'uncertain':
                db.execute("UPDATE kb_parse_slot SET state='uncertain' WHERE slot=1")
                state, stage = 'write_recovery_required', 'parse_write'
            else:
                db.execute('DELETE FROM kb_parse_slot WHERE slot=1')
                state = 'written' if outcome == 'written' else 'backoff'
                stage = 'index' if outcome == 'written' else 'validate'
            info = {'code': 'WRITE_RECOVERY_REQUIRED'} if outcome == 'uncertain' else {}
            db.execute('UPDATE kb_jobs SET state=?,stage=?,checkpoint=?,info=?,owner=NULL,fence=fence+1,lease_until=0,updated=? WHERE job_id=?',
                       (state, stage, _json(checkpoint or {}), _json(info), now, job_id))
            return True

    def job_parse_quarantine_resolve(self, job_id, owner=None):
        """Release an uncertain slot only when the crashed parse never held a
        source write. A live backend write keeps the fence and requires the
        verified write-recovery path instead."""
        with self.db() as db:
            row = db.execute('SELECT * FROM kb_jobs WHERE job_id=?', (job_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown job')
            slot = db.execute('SELECT * FROM kb_parse_slot WHERE slot=1').fetchone()
            if slot is None or slot['job_id'] != job_id or slot['state'] != 'uncertain':
                raise ValueError('Parse slot is not quarantined for this job')
            if db.execute('SELECT 1 FROM source_writes LIMIT 1').fetchone():
                raise ValueError('SOURCE_WRITES_PENDING: verified write recovery is required')
            now = time.time()
            db.execute('DELETE FROM kb_parse_slot WHERE slot=1')
            db.execute("UPDATE kb_jobs SET state='backoff',stage='validate',owner=NULL,fence=fence+1,"
                       "lease_until=0,available_at=?,info=?,updated=? WHERE job_id=?",
                       (now, _json({'code': 'PARSE_QUARANTINE_RESOLVED_NO_PENDING_WRITES'}), now, job_id))
            return _record(db.execute('SELECT * FROM kb_jobs WHERE job_id=?', (job_id,)).fetchone())

    def job_fail(self, job_id, owner, fence, info, retry_seconds=None):
        if not isinstance(info, dict):
            raise ValueError('Failure info must be an object')
        if retry_seconds is not None and (isinstance(retry_seconds, bool) or not isinstance(retry_seconds, (int, float)) or not math.isfinite(retry_seconds) or not 0 <= retry_seconds <= 3600):
            raise ValueError('Retry delay must be finite and between 0 and 3600 seconds')
        with self.db() as db:
            self._job_owner(db, job_id, owner, fence)
            if db.execute('SELECT 1 FROM kb_parse_slot WHERE job_id=?', (job_id,)).fetchone():
                raise ValueError('Release or quarantine parse slot before failing a job')
            state = 'failed' if retry_seconds is None else 'backoff'
            db.execute('UPDATE kb_jobs SET state=?,info=?,available_at=?,owner=NULL,fence=fence+1,lease_until=0,updated=? WHERE job_id=?',
                       (state, _json(info), time.time() + (retry_seconds or 0), time.time(), job_id))
            return True

    def job_cancel(self, account, job_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM kb_jobs WHERE account=? AND job_id=?', (account, job_id)).fetchone()
            if row is None:
                return None
            if row['state'] in TERMINAL:
                return _record(row)
            slot = db.execute('SELECT 1 FROM kb_parse_slot WHERE job_id=?', (job_id,)).fetchone()
            state = 'write_recovery_required' if slot else 'cancelled'
            if slot:
                db.execute("UPDATE kb_parse_slot SET state='uncertain' WHERE job_id=?", (job_id,))
            db.execute('UPDATE kb_jobs SET state=?,owner=NULL,fence=fence+1,lease_until=0,info=?,updated=? WHERE job_id=?',
                       (state, _json({'code': 'CANCEL_REQUESTED'}), time.time(), job_id))
            return _record(db.execute('SELECT * FROM kb_jobs WHERE job_id=?', (job_id,)).fetchone())

    def job_complete(self, job_id, owner, fence, state, receipt=None):
        with self.db() as db:
            row = self._job_owner(db, job_id, owner, fence)
            if db.execute('SELECT 1 FROM kb_parse_slot WHERE job_id=?', (job_id,)).fetchone():
                raise ValueError('Parse slot must be released before completing a job')
            if state == 'ready':
                # The publication gate reads committed receipts/lexical rows/
                # semantic bindings/snapshot heads; an unverified index stage
                # can never claim the document reached its readers.
                checkpoint = json.loads(row['checkpoint'])
                batch_id = checkpoint.get('publication_batch_id')
                batch = db.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
                if (row['kind'] not in {'D1', 'D2'} or row['stage'] != 'index'
                        or batch is None or batch['account'] != row['account']
                        or batch['namespace'] != checkpoint.get('publication_namespace')):
                    raise ValueError('PUBLICATION_GATE_REQUIRED: ready requires an index-stage document job '
                                     'with a bound publication batch')
                missing = self._gate_observation(db, batch_id, row['account'],
                                                 checkpoint.get('publication_namespace'),
                                                 checkpoint.get('root_uri'))
                if any(missing.values()):
                    raise ValueError('PUBLICATION_GATE_REQUIRED: joint coverage gates missing: '
                                     + _json({name: sorted(set(gaps)) for name, gaps in missing.items() if gaps}))
                db.execute("UPDATE kb_jobs SET state='ready',info=?,owner=NULL,fence=fence+1,lease_until=0,updated=? WHERE job_id=?",
                           (_json({'verified': True}), time.time(), job_id))
                return True
            elif state != 'draft_ready' or row['kind'] not in {'W1', 'W2'} or json.loads(row['effective_config']).get('profile') != 'wiki_draft' or row['stage'] != 'wiki_validate':
                raise ValueError('Only private wiki_draft candidates may complete as draft_ready')
            checkpoint = json.loads(row['checkpoint'])
            payload = json.loads(row['payload'])
            config = json.loads(row['effective_config'])
            if (not isinstance(receipt, dict) or checkpoint.get('draft_receipt') != receipt
                    or checkpoint.get('diagnostic_published') is not False
                    or receipt.get('private') is not True or receipt.get('state') != 'draft_ready'
                    or receipt.get('diagnostic_publishable') is not False
                    or receipt.get('build_id') != payload.get('build_id')
                    or receipt.get('plan_digest') != payload.get('plan_digest')):
                raise ValueError('Fixed-plan private candidate receipt required; diagnostic publication forbidden')
            plan = db.execute('SELECT account,namespace,digest,manifest FROM wiki_build_plans WHERE id=?',
                              (payload['build_id'],)).fetchone()
            if plan is None or (plan['account'], plan['namespace'], plan['digest']) != (row['account'], row['namespace'], payload['plan_digest']):
                raise ValueError('Private candidate receipt belongs to another plan or scope')
            manifest = json.loads(plan['manifest'])
            self._wiki_plan_epochs(db, row['account'], manifest)
            revision = db.execute('SELECT revision FROM kb_policy_revisions WHERE account=?',
                                  (row['account'],)).fetchone()
            if receipt.get('authorization_revision') != (revision['revision'] if revision else 0):
                raise ValueError('Private candidate export policy changed before completion')
            drafts = receipt.get('receipts')
            sources = {source['uri'] for source in manifest['sources']}
            covered = {item.get('source_uri') for item in drafts if isinstance(item, dict)}
            failed_sources = receipt.get('failed_sources')
            failed_sections = receipt.get('failed_sections')
            # A section that exhausts its retries records a failure instead of a
            # receipt, so full fixed-plan coverage is receipts ∪ failures, not
            # receipts alone. Current receipts name the failed URIs
            # (failed_sources) for exact reconciliation; a receipt written before
            # that field recorded only per-error counts (failed_sections), so
            # reconcile the uncovered sources against that declared count. The
            # receipt count is bounded by the model-call budget (every receipt
            # costs at least one call), which scales with the configured plan,
            # instead of a fixed small-plan constant that made draft_ready
            # unreachable for a real handbook.
            if isinstance(failed_sections, dict):
                failed_count = sum(v for v in failed_sections.values()
                                   if isinstance(v, int) and v > 0)
            else:
                failed_count = 0
            if isinstance(failed_sources, list):
                accounted = set(failed_sources) <= sources and (covered | set(failed_sources)) == sources
            else:
                accounted = (len(sources) - len(covered)) == failed_count
            receipt_ceiling = (config.get('limits') or {}).get('model_calls')
            if (not isinstance(drafts, list) or not drafts
                    or (type(receipt_ceiling) is int and len(drafts) > receipt_ceiling)
                    or not covered <= sources
                    or not accounted):
                raise ValueError('Private candidate receipt must cover every fixed source')
            seen = set()
            for draft in drafts:
                ref = draft.get('reference', {}) if isinstance(draft, dict) else {}
                key = (draft.get('source_uri'), draft.get('source_chunk')) if isinstance(draft, dict) else (None, None)
                if (key in seen or type(key[1]) is not int or key[1] < 0
                        or draft.get('private') is not True
                        or not isinstance(ref.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', ref['sha256'])
                        or type(ref.get('size')) is not int or ref['size'] <= 0):
                    raise ValueError('Invalid private candidate blob receipt')
                seen.add(key)
            extracted = receipt.get('configuration') or {}
            if (not isinstance(config.get('extractor_version'), str)
                    or not isinstance(config.get('policy_version'), str)
                    or extracted.get('extractor_version') != config.get('extractor_version')
                    or extracted.get('policy_version') != config.get('policy_version')
                    or extracted.get('model_profile') != config.get('model_profile')
                    or extracted.get('limits') != config.get('limits')
                    or extracted.get('profile') != config.get('extraction_profile')):
                raise ValueError('Private candidate effective configuration changed')
            db.execute('UPDATE kb_jobs SET state=?,info=?,owner=NULL,fence=fence+1,lease_until=0,updated=? WHERE job_id=?',
                       (state, _json({'private': True}), time.time(), job_id))
            return True
