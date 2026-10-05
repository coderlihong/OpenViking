# SPDX-License-Identifier: AGPL-3.0
"""Opt-in local backend with durable one-shot operations and writer fencing.

This is a controlled local capability, not a claim that RAGFS/remote AGFS has an
operation-status API. Every writer of protected source paths must use this
adapter. The operator owns its private directory and coordinator registration.
"""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from uuid import uuid4

from openviking_cli.exceptions import FailedPreconditionError

_active = ContextVar('controlled_source_operation', default=None)


class ControlledLocalWriteBackend:
    def __init__(self, directory):
        self.root = Path(directory).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.stat().st_mode & 0o077:
            raise ValueError('Controlled backend requires a private directory (0700)')
        self.data = self.root / 'data'
        self.data.mkdir(mode=0o700, exist_ok=True)
        self.database = self.root / 'operations.db'
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS backend_identity (identity TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS write_operations (
                  id TEXT PRIMARY KEY, account TEXT NOT NULL, uri TEXT NOT NULL,
                  path TEXT NOT NULL, state TEXT NOT NULL, sha256 TEXT, confirmed REAL);
            ''')
            # executescript ends its transaction; reacquire serialization before
            # choosing the one persisted backend identity across processes.
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT identity FROM backend_identity').fetchone()
            self.identity = old['identity'] if old else 'controlled-local:' + uuid4().hex
            if old is None:
                db.execute('INSERT INTO backend_identity VALUES (?)', (self.identity,))
        self.database.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def path(self, value):
        if not isinstance(value, str) or not value.startswith('/') or '..' in value.split('/'):
            raise ValueError('Invalid controlled backend path')
        lexical = self.data / value.lstrip('/')
        resolved = lexical.resolve()
        if not resolved.is_relative_to(self.data) or resolved != lexical:
            raise ValueError('Controlled backend rejects escaping paths and symlink aliases')
        return resolved

    @staticmethod
    def source_path(account, uri):
        if not account or '/' in account or account in {'.', '..'} or not uri.startswith('viking://resources/'):
            raise ValueError('Controlled recovery only supports canonical resource file URIs')
        parts = uri[len('viking://'):].split('/')
        if any(not part or part in {'.', '..'} or len(part.encode()) > 255 for part in parts):
            raise ValueError('Controlled recovery requires unshortened canonical URI components')
        return '/local/' + account + '/' + '/'.join(parts)

    def prepare_source_write(self, account, uri):
        path = self.source_path(account, uri)
        self.path(path)
        operation = uuid4().hex
        with self.db() as db:
            db.execute("INSERT INTO write_operations VALUES (?,?,?,?,'pending',NULL,NULL)", (operation, account, uri, path))
        return {'backend': self.identity, 'operation_id': operation, 'account': account, 'uri': uri}

    @contextmanager
    def activate_source_write(self, reference):
        if reference.get('backend') != self.identity:
            raise ValueError('Controlled source operation belongs to another backend')
        token = _active.set(reference)
        try:
            yield
        finally:
            _active.reset(token)

    def commit_operation(self, reference, content):
        if reference.get('backend') != self.identity or not isinstance(content, bytes):
            raise ValueError('Invalid controlled write operation')
        with self.db() as db:
            row = db.execute('SELECT * FROM write_operations WHERE id=?', (reference['operation_id'],)).fetchone()
            if row is None or (row['account'], row['uri']) != (reference['account'], reference['uri']):
                raise FailedPreconditionError('Unknown backend operation identity')
            if row['state'] != 'pending':
                raise FailedPreconditionError('Backend operation is terminal or fenced; it cannot write again')
            destination = self.path(row['path'])
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as staging:
                temporary = Path(staging.name)
                staging.write(content)
                staging.flush()
                os.fsync(staging.fileno())
            try:
                # Mutation and terminal state are protected by this backend
                # transaction. Crash after rename leaves pending, never a fake
                # terminal receipt; fencing can then safely reject old replay.
                os.replace(temporary, destination)
                fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                db.execute("UPDATE write_operations SET state='terminal',sha256=?,confirmed=? WHERE id=?",
                           (hashlib.sha256(content).hexdigest(), time.time(), reference['operation_id']))
            finally:
                temporary.unlink(missing_ok=True)

    def fence_operation(self, reference):
        """Backend operator action; not exposed as a worker/client recovery proof."""
        if reference.get('backend') != self.identity:
            raise ValueError('Backend identity mismatch')
        with self.db() as db:
            row = db.execute('SELECT * FROM write_operations WHERE id=?', (reference['operation_id'],)).fetchone()
            if row is None or (row['account'], row['uri']) != (reference['account'], reference['uri']):
                raise FailedPreconditionError('Unknown backend operation identity')
            if row['state'] == 'pending':
                db.execute("UPDATE write_operations SET state='fenced',confirmed=? WHERE id=?", (time.time(), reference['operation_id']))

    @contextmanager
    def confirm_and_read(self, account, uri, references):
        # Keep the backend mutation lock through the coordinator CAS. This is
        # more than a generic pathlock: every protected write validates its
        # one-shot operation state while holding this exact durable lock.
        with self.db() as db:
            operations = []
            for ref in references:
                if ref.get('backend') != self.identity or (ref.get('account'), ref.get('uri')) != (account, uri):
                    raise FailedPreconditionError('Server backend reference scope mismatch')
                row = db.execute('SELECT * FROM write_operations WHERE id=?', (ref['operation_id'],)).fetchone()
                if row is None or (row['account'], row['uri']) != (account, uri) or row['state'] not in {'terminal', 'fenced'}:
                    raise FailedPreconditionError('WRITE_RECOVERY_REQUIRED: backend operation is not terminal or fenced')
                operations.append({'operation_id': row['id'], 'state': row['state'], 'confirmed': row['confirmed']})
            try:
                raw = self.path(self.source_path(account, uri)).read_bytes()
            except OSError as exc:
                raise FailedPreconditionError(
                    'WRITE_RECOVERY_REQUIRED: verified source bytes are unavailable'
                ) from exc
            yield {'backend': self.identity, 'account': account, 'uri': uri, 'operations': operations, 'content': raw}

    async def write(self, path, data, **kwargs):
        reference = _active.get()
        if reference is not None:
            if path != self.source_path(reference['account'], reference['uri']):
                raise FailedPreconditionError('Backend write path differs from bound source operation')
            await asyncio.to_thread(self.commit_operation, reference, bytes(data))
        else:
            with self.db() as db:
                if db.execute('SELECT 1 FROM write_operations WHERE path=?', (path,)).fetchone():
                    raise FailedPreconditionError('Protected source writes must use a bound controlled operation')
                target = self.path(path)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                target.write_bytes(bytes(data))
        return path

    async def read(self, path, offset=0, size=-1, **kwargs):
        raw = self.path(path).read_bytes()
        return raw[offset:] if size == -1 else raw[offset:offset + size]

    async def stat(self, path, **kwargs):
        target = self.path(path)
        if not target.exists():
            raise FileNotFoundError(path)
        return {'isDir': target.is_dir(), 'size': target.stat().st_size}

    async def mkdir(self, path, **kwargs):
        self.path(path).mkdir(mode=0o700, parents=True, exist_ok=True)
