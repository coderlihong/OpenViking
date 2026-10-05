# SPDX-License-Identifier: AGPL-3.0
"""Server-owned unknown-write recovery; hashes/timeouts never prove terminality.

Adapters and current authorization providers are installed by the coordinator
operator. The worker/public request cannot submit evidence or choose a verifier.
"""
import hashlib
import json
import re
import time
from uuid import uuid4

from openviking_cli.exceptions import ConflictError, FailedPreconditionError, NotFoundError, PermissionDeniedError

RECOVERY_COMMANDS = ('write_recovery_diagnose', 'write_recovery_prepare', 'write_recovery_execute', 'write_recovery_get')


class WriteRecoveryMixin:
    def init_write_recovery(self):
        self._recovery_backends = {}
        self._recovery_authorizer = None
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS source_write_refs (
                  token TEXT PRIMARY KEY, backend TEXT NOT NULL, operation_id TEXT NOT NULL,
                  reference TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS source_recoveries (
                  id TEXT PRIMARY KEY, account TEXT NOT NULL, uri TEXT NOT NULL,
                  actor TEXT NOT NULL, idempotency_key TEXT NOT NULL, snapshot TEXT NOT NULL,
                  targets TEXT NOT NULL, epoch INTEGER NOT NULL, state TEXT NOT NULL,
                  result TEXT, created REAL NOT NULL, UNIQUE(account,uri,idempotency_key));
                CREATE TABLE IF NOT EXISTS source_recovery_scope (
                  recovery TEXT PRIMARY KEY, scope TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS source_recovery_audit (
                  recovery TEXT PRIMARY KEY, account TEXT NOT NULL, uri TEXT NOT NULL,
                  actor TEXT NOT NULL, backend TEXT NOT NULL, operations TEXT NOT NULL,
                  confirmed REAL NOT NULL, sha256 TEXT NOT NULL, old_epoch INTEGER NOT NULL,
                  new_epoch INTEGER NOT NULL, result TEXT NOT NULL);
            ''')

    def configure_write_recovery(self, *, backends, authorizer):
        """Operator-only wiring; intentionally absent from RPC dispatch."""
        self._recovery_backends = dict(backends)
        if any(key != backend.identity for key, backend in self._recovery_backends.items()):
            raise ValueError('Recovery adapter identity mismatch')
        self._recovery_authorizer = authorizer

    def write_recovery_diagnose(self, account, uri):
        with self.db() as db:
            writes = [dict(row) for row in db.execute('SELECT * FROM source_writes WHERE account=? AND uri=? ORDER BY id', (account, uri))]
            refs = {row['token']: dict(row) for row in db.execute('SELECT r.* FROM source_write_refs r JOIN source_writes w ON w.id=r.token WHERE w.account=? AND w.uri=?', (account, uri))}
            supported = bool(writes) and all(w['id'] in refs and refs[w['id']]['backend'] in self._recovery_backends for w in writes) and self._recovery_authorizer is not None
            return {'uri': uri, 'state': 'write_recovery_required' if writes else 'clear',
                    'writes': writes, 'recovery_supported': supported,
                    'reason': None if supported or not writes else 'SERVER_VERIFIED_BACKEND_OR_AUTHORIZATION_UNAVAILABLE'}

    @staticmethod
    def _recovery_scope(db, account, uri):
        roots = [dict(row) for row in db.execute('SELECT namespace,root_uri FROM kb_namespaces WHERE account=?', (account,))
                 if uri == row['root_uri'] or uri.startswith(row['root_uri'] + '/')]
        if len(roots) != 1:
            raise FailedPreconditionError('Recovery source must belong to one registered knowledge namespace')
        revision = db.execute('SELECT revision FROM kb_policy_revisions WHERE account=?', (account,)).fetchone()
        return {**roots[0], 'policy_revision': revision['revision'] if revision else 0}

    def write_recovery_prepare(self, account, uri, actor, tokens, idempotency_key):
        if not isinstance(actor, str) or not actor or len(actor) > 256:
            raise ValueError('Recovery actor must be a bounded server-resolved identity')
        if not isinstance(idempotency_key, str) or not re.fullmatch(r'[A-Za-z0-9_.+\-]{1,128}', idempotency_key):
            raise ValueError('Invalid recovery idempotency key')
        targets = sorted(set(tokens))
        if not targets or len(targets) > 128 or len(targets) != len(tokens):
            raise ValueError('Recovery requires a bounded unique target token set')
        with self.db() as db:
            old = db.execute('SELECT * FROM source_recoveries WHERE account=? AND uri=? AND idempotency_key=?', (account, uri, idempotency_key)).fetchone()
            if old:
                if old['actor'] != actor or json.loads(old['targets']) != targets:
                    raise ConflictError('Recovery idempotency key names another actor or target set')
                return dict(old)
            writes = [dict(row) for row in db.execute('SELECT * FROM source_writes WHERE account=? AND uri=? ORDER BY id', (account, uri))]
            if not set(targets).issubset({row['id'] for row in writes}):
                raise ConflictError('Recovery targets are not current source writers')
            epoch = db.execute('SELECT epoch FROM drafts WHERE account=? AND uri=?', (account, uri)).fetchone()
            scope = self._recovery_scope(db, account, uri)
            recovery = uuid4().hex
            db.execute('INSERT INTO source_recoveries VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (recovery, account, uri, actor, idempotency_key, json.dumps(writes, sort_keys=True), json.dumps(targets), epoch['epoch'], 'prepared', None, time.time()))
            db.execute('INSERT INTO source_recovery_scope VALUES (?,?)', (recovery, json.dumps(scope, sort_keys=True)))
            return dict(db.execute('SELECT * FROM source_recoveries WHERE id=?', (recovery,)).fetchone())

    def write_recovery_get(self, account, recovery):
        with self.db() as db:
            row = db.execute('SELECT * FROM source_recoveries WHERE account=? AND id=?', (account, recovery)).fetchone()
            if row is None:
                raise NotFoundError(recovery, 'Write recovery')
            result = dict(row)
            result['result'] = json.loads(result['result']) if result['result'] else None
            return result

    def _recovery_authorize(self, account, actor, uri):
        if self._recovery_authorizer is None:
            raise FailedPreconditionError('WRITE_RECOVERY_REQUIRED: coordinator current authorization provider unavailable')
        if self._recovery_authorizer(account, actor, uri) is not True:
            raise PermissionDeniedError('Current source write and knowledge-domain manage authorization required')

    def write_recovery_execute(self, account, recovery, actor):
        intent = self.write_recovery_get(account, recovery)
        if intent['actor'] != actor:
            raise PermissionDeniedError('Recovery belongs to another actor')
        self._recovery_authorize(account, actor, intent['uri'])
        if intent['state'] == 'completed':
            return intent['result']
        targets = json.loads(intent['targets'])
        with self.db() as db:
            scope_row = db.execute('SELECT scope FROM source_recovery_scope WHERE recovery=?', (recovery,)).fetchone()
            if scope_row is None:
                raise FailedPreconditionError('Recovery intent lacks a registered authorization scope; prepare with a new key')
            scope = json.loads(scope_row['scope'])
            refs = [dict(row) for row in db.execute('SELECT * FROM source_write_refs WHERE token IN (' + ','.join('?' for _ in targets) + ') ORDER BY token', targets)]
        if len(refs) != len(targets) or len({row['backend'] for row in refs}) != 1:
            raise FailedPreconditionError('WRITE_RECOVERY_REQUIRED: target operation identity unavailable or mixed backends')
        backend_id = refs[0]['backend']
        backend = self._recovery_backends.get(backend_id)
        if backend is None:
            raise FailedPreconditionError('WRITE_RECOVERY_REQUIRED: coordinator has no trusted backend verifier')
        # The adapter queries its own durable operation journal and keeps its
        # mutation fence held through the registry transaction. No request proof.
        with backend.confirm_and_read(account, intent['uri'], [json.loads(row['reference']) for row in refs]) as verified:
            if verified['backend'] != backend_id or verified['account'] != account or verified['uri'] != intent['uri']:
                raise FailedPreconditionError('Server backend evidence identity mismatch')
            raw = verified['content']
            if not isinstance(raw, bytes):
                raise FailedPreconditionError('Server backend did not return verified source bytes')
            sha = hashlib.sha256(raw).hexdigest()
            with self.db() as db:
                self._recovery_authorize(account, actor, intent['uri'])
                current = db.execute('SELECT * FROM source_recoveries WHERE account=? AND id=?', (account, recovery)).fetchone()
                if current['state'] == 'completed':
                    return json.loads(current['result'])
                writes = [dict(row) for row in db.execute('SELECT * FROM source_writes WHERE account=? AND uri=? ORDER BY id', (account, intent['uri']))]
                epoch = db.execute('SELECT epoch FROM drafts WHERE account=? AND uri=?', (account, intent['uri'])).fetchone()['epoch']
                actual_refs = [dict(row) for row in db.execute('SELECT * FROM source_write_refs WHERE token IN (' + ','.join('?' for _ in targets) + ') ORDER BY token', targets)]
                if self._recovery_scope(db, account, intent['uri']) != scope:
                    raise ConflictError('Registered source domain or policy revision changed during recovery')
                if json.dumps(writes, sort_keys=True) != intent['snapshot'] or epoch != intent['epoch'] or actual_refs != refs:
                    raise ConflictError('Source token set, epoch or backend identity changed during verified recovery')
                result = {'recovery_id': recovery, 'uri': intent['uri'], 'state': 'recovered',
                          'sha256': sha, 'old_epoch': epoch, 'new_epoch': epoch + 1,
                          'cleared_tokens': targets, 'rebuild_required': True}
                # Audit insertion must succeed in this same transaction before
                # any token can disappear. No failed build/job is promoted.
                db.execute('INSERT INTO source_recovery_audit VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (recovery, account, intent['uri'], actor, backend_id,
                            json.dumps(verified['operations'], sort_keys=True), time.time(), sha, epoch, epoch + 1, 'recovered'))
                db.execute('UPDATE drafts SET epoch=epoch+1,sha=? WHERE account=? AND uri=?', (sha, account, intent['uri']))
                for token in targets:
                    db.execute('DELETE FROM source_writes WHERE id=? AND account=? AND uri=?', (token, account, intent['uri']))
                    db.execute('DELETE FROM source_write_refs WHERE token=?', (token,))
                db.execute("UPDATE source_recoveries SET state='completed',result=? WHERE id=?", (json.dumps(result), recovery))
                return result
