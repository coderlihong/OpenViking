# SPDX-License-Identifier: AGPL-3.0
"""Operator-only, offline controlled-backend bundles; restore starts quarantined.

This is deliberately not a generic AGFS snapshot adapter. The caller must hold
an operator-owned quiescence context covering identity, vector/ACL and config
writers. Registry and controlled source writers are additionally locked here.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from uuid import uuid4

from openviking.indexing.controlled_write_backend import ControlledLocalWriteBackend
from openviking_cli.exceptions import FailedPreconditionError

VERSION = 'controlled-kb-backup-v1'
REQUIRED = {'registry', 'backend', 'identity', 'model_config', 'vectors_acl'}
VALIDATIONS = ['vector_acl_engine_validation', 'current_identity_acl_validation',
               'model_config_validation', 'published_receipt_validation']


def _hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _regular(path):
    if path.is_symlink() or not path.is_file() or path.absolute() != path.resolve():
        raise FailedPreconditionError('Backup components must contain regular files without symlinks')


def _inventory(directory, *, max_files=100000, max_total_bytes=10 * 1024 ** 3):
    if directory.is_symlink() or directory.absolute() != directory.resolve():
        raise FailedPreconditionError('Backup rejects aliased component directories')
    result = {}
    total = 0
    for path in directory.rglob('*'):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise FailedPreconditionError('Backup rejects symlinks and special files')
        if path.is_file():
            size = path.stat().st_size
            total += size
            if len(result) + 1 > max_files or total > max_total_bytes:
                raise FailedPreconditionError('Component file or byte quota exceeded during inventory')
            result[path.relative_to(directory).as_posix()] = {'size': size, 'sha256': _hash(path)}
    return result


def _snapshot_sqlite(source, destination):
    with sqlite3.connect(f'file:{source}?mode=ro', uri=True) as original, sqlite3.connect(destination) as copy:
        original.backup(copy)
        if copy.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise FailedPreconditionError('SQLite snapshot integrity check failed')
    destination.chmod(0o600)


def _retained_data(directory):
    # HTTP shared raw originals live at viking://upload, mapped account-wise.
    # They must not gain permanent retention merely because a backup ran.
    return {name: value for name, value in _inventory(directory).items()
            if not (len(Path(name).parts) >= 3 and Path(name).parts[0] == 'local'
                    and Path(name).parts[2] == 'upload')}


def _verify_registry_dependencies(db, backend_data):
    from openviking.indexing.publication import PublicationStore
    db.row_factory = sqlite3.Row
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='generation_write_windows'").fetchone():
        if db.execute("SELECT 1 FROM generation_write_windows WHERE state IN ('writing','unknown') LIMIT 1").fetchone():
            raise FailedPreconditionError('Managed vector write recovery required before joint backup/restore')
    # Reuse the real publication/Wiki/job reachability policy without opening
    # another transaction or mutating the registry schema.
    reader = PublicationStore.__new__(PublicationStore)
    for account, digest in reader._protected_blobs(db):
        ControlledLocalWriteBackend.source_path(account, 'viking://resources/_probe')
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise FailedPreconditionError('Registry references an invalid immutable source digest')
        blob = backend_data / 'local' / account / '_system/index_blobs' / (digest + '.bin')
        _regular(blob)
        if _hash(blob) != digest:
            raise FailedPreconditionError('Published/Wiki immutable source dependency is missing or corrupt')
    for row in db.execute("SELECT kind,state,checkpoint FROM kb_jobs WHERE kind IN ('D1','W1')"):
        if row['state'] not in {'ready', 'draft_ready', 'failed', 'cancelled'} and not json.loads(row['checkpoint']).get('materialized'):
            raise FailedPreconditionError('Unmaterialized upload job requires its temporary input; not covered by this bundle')


@contextmanager
def _registry_lock(registry):
    with open(str(registry) + '.authority.lock', 'a+b') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FailedPreconditionError('Stop the live authority before offline joint backup') from exc
        with sqlite3.connect(registry) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                if db.execute('SELECT 1 FROM source_writes LIMIT 1').fetchone():
                    raise FailedPreconditionError('Verified source-write recovery required before joint backup')
                if db.execute('SELECT 1 FROM kb_parse_slot LIMIT 1').fetchone():
                    raise FailedPreconditionError('Global parse/write slot must be clear before joint backup')
                yield db
            finally:
                db.rollback()


def _publish(stage, destination):
    for path in stage.rglob('*'):
        if path.is_file():
            with path.open('rb') as stream:
                os.fsync(stream.fileno())
    for directory in sorted([stage, *(p for p in stage.rglob('*') if p.is_dir())],
                            key=lambda p: len(p.parts), reverse=True):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    if destination.exists() or destination.is_symlink():
        raise FailedPreconditionError('Destination appeared before atomic publication')
    os.rename(stage, destination)
    fd = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def export_bundle(*, registry, backend, identity, model_config, vectors_acl, destination,
                  quiescence, max_total_bytes=10 * 1024 ** 3, max_files=100000):
    """Snapshot only while a trusted operator quiescence context is held.

    ``vectors_acl`` is the offline local vector store containing actual ACL
    records as well as vector data. Its engine-specific semantic validation is
    required after restoration, not inferred from copying bytes successfully.
    """
    if type(backend) is not ControlledLocalWriteBackend or quiescence is None:
        raise FailedPreconditionError('Trusted controlled-backend and joint quiescence capability required')
    if type(max_total_bytes) is not int or max_total_bytes <= 0 or type(max_files) is not int or max_files <= 0:
        raise ValueError('Backup limits must be positive integers')
    registry, identity, model_config = map(lambda p: Path(p).absolute(), (registry, identity, model_config))
    vectors_acl, destination = Path(vectors_acl).absolute(), Path(destination).absolute()
    if destination.exists() or destination.is_symlink() or not destination.parent.is_dir():
        raise FailedPreconditionError('Backup requires a new destination in an existing parent')
    for path in (registry, identity, model_config):
        _regular(path)
    if not vectors_acl.is_dir() or vectors_acl.is_symlink():
        raise FailedPreconditionError('Local vector/ACL snapshot directory is required')
    if any(destination.is_relative_to(root) for root in (backend.root, vectors_acl)):
        raise FailedPreconditionError('Backup destination cannot be inside a source component')
    stage = Path(tempfile.mkdtemp(prefix='.kb-backup-', dir=destination.parent))
    stage.chmod(0o700)
    try:
        with quiescence(), _registry_lock(registry) as registry_db, backend.db() as operations:
            if operations.execute("SELECT 1 FROM write_operations WHERE state='pending' LIMIT 1").fetchone():
                raise FailedPreconditionError('Backend has a nonterminal operation; verified recovery required')
            _verify_registry_dependencies(registry_db, backend.data)
            before = {'identity': _hash(identity), 'model_config': _hash(model_config),
                      'vectors_acl': _inventory(vectors_acl), 'backend_data': _retained_data(backend.data)}
            if not before['vectors_acl']:
                raise FailedPreconditionError('Vector/ACL snapshot is empty; no complete dependency receipt')
            source_files = list(before['vectors_acl'].values()) + list(before['backend_data'].values())
            size_estimate = sum(item['size'] for item in source_files) + identity.stat().st_size + model_config.stat().st_size
            for connection in (registry_db, operations):
                size_estimate += connection.execute('PRAGMA page_size').fetchone()[0] * connection.execute('PRAGMA page_count').fetchone()[0]
            if len(source_files) + 4 > max_files or size_estimate > max_total_bytes:
                raise FailedPreconditionError('Joint backup file or byte quota exceeded before copy')
            for path in (identity, model_config):
                if path.stat().st_size > 1024 * 1024:
                    raise FailedPreconditionError('Identity/model JSON exceeds the supported 1 MiB bound')
                if not isinstance(json.loads(path.read_text()), dict):
                    raise FailedPreconditionError('Identity and model configuration must be JSON objects')
            _snapshot_sqlite(registry, stage / 'registry.sqlite')
            (stage / 'backend').mkdir(mode=0o700)
            _snapshot_sqlite(backend.database, stage / 'backend/operations.db')
            (stage / 'backend/data').mkdir(mode=0o700)
            for name in before['backend_data']:
                target = stage / 'backend/data' / name
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(backend.data / name, target)
            shutil.copytree(vectors_acl, stage / 'vectors_acl')
            shutil.copyfile(identity, stage / 'identity.json')
            shutil.copyfile(model_config, stage / 'model_config.json')
            after = {'identity': _hash(identity), 'model_config': _hash(model_config),
                     'vectors_acl': _inventory(vectors_acl), 'backend_data': _retained_data(backend.data)}
            if after != before:
                raise FailedPreconditionError('Component changed despite the joint quiescence guard')
            files = _inventory(stage)
            copied = {'identity': files['identity.json']['sha256'],
                      'model_config': files['model_config.json']['sha256'],
                      'vectors_acl': {p[len('vectors_acl/'):]: v for p, v in files.items() if p.startswith('vectors_acl/')},
                      'backend_data': {p[len('backend/data/'):]: v for p, v in files.items() if p.startswith('backend/data/')}}
            if copied != before:
                raise FailedPreconditionError('Copied component bytes differ from the quiescent source snapshot')
            if len(files) > max_files or sum(value['size'] for value in files.values()) > max_total_bytes:
                raise FailedPreconditionError('Joint backup file or byte quota exceeded')
            manifest = {'version': VERSION, 'bundle_id': uuid4().hex, 'state': 'snapshot_complete',
                        'backend_identity': backend.identity, 'components': sorted(REQUIRED), 'files': files,
                        'restore_requires': VALIDATIONS}
            (stage / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
            for path in stage.rglob('*'):
                path.chmod(0o700 if path.is_dir() else 0o600)
            _publish(stage, destination)
            return manifest
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def restore_bundle(bundle, destination, *, expected_manifest_sha256,
                   max_total_bytes=10 * 1024 ** 3, max_files=100000):
    """Verify a trusted out-of-band manifest hash and restore into quarantine.

    No registry with a conventional startup filename is released. Operator
    validation of actual engines/ACL/identity/publication receipts is mandatory
    before an application can adopt the quarantined registry.
    """
    bundle, destination = Path(bundle).absolute(), Path(destination).absolute()
    if type(max_total_bytes) is not int or max_total_bytes <= 0 or type(max_files) is not int or max_files <= 0:
        raise ValueError('Restore limits must be positive integers')
    if destination.exists() or destination.is_symlink() or not destination.parent.is_dir():
        raise FailedPreconditionError('Restore requires a new empty destination')
    manifest_path = bundle / 'manifest.json'
    _regular(manifest_path)
    if manifest_path.stat().st_size > 32 * 1024 ** 2:
        raise FailedPreconditionError('Backup manifest exceeds the supported 32 MiB bound')
    if _hash(manifest_path) != expected_manifest_sha256:
        raise FailedPreconditionError('Trusted backup manifest digest mismatch')
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('version') != VERSION or set(manifest.get('components', [])) != REQUIRED or manifest.get('state') != 'snapshot_complete' or manifest.get('restore_requires') != VALIDATIONS:
        raise FailedPreconditionError('Backup lacks required registry/AGFS/identity/ACL/model dependencies')
    actual = _inventory(bundle)
    actual.pop('manifest.json')
    if len(actual) > max_files or sum(item['size'] for item in actual.values()) > max_total_bytes:
        raise FailedPreconditionError('Joint restore file or byte quota exceeded')
    if actual != manifest.get('files'):
        raise FailedPreconditionError('Backup component inventory or digest mismatch')
    required_paths = {'registry.sqlite', 'backend/operations.db', 'identity.json', 'model_config.json'}
    if not required_paths.issubset(actual) or not any(p.startswith('vectors_acl/') for p in actual):
        raise FailedPreconditionError('Backup dependency files are missing')
    stage = Path(tempfile.mkdtemp(prefix='.kb-restore-', dir=destination.parent))
    stage.chmod(0o700)
    try:
        for relative in actual:
            source = bundle / relative
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        # Verify the copied bytes too, so a changing/corrupt source bundle does
        # not slip through the preflight digest scan.
        if _inventory(stage) != actual:
            raise FailedPreconditionError('Backup changed during restore')
        with sqlite3.connect(stage / 'registry.sqlite') as registry:
            if registry.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise FailedPreconditionError('Restored registry is invalid')
            if registry.execute('SELECT 1 FROM source_writes LIMIT 1').fetchone() or registry.execute('SELECT 1 FROM kb_parse_slot LIMIT 1').fetchone():
                raise FailedPreconditionError('Restored registry contains unresolved writers')
            _verify_registry_dependencies(registry, stage / 'backend/data')
        with sqlite3.connect(stage / 'backend/operations.db') as backend:
            if backend.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise FailedPreconditionError('Restored operation journal is invalid')
            identity = backend.execute('SELECT identity FROM backend_identity').fetchone()
            if identity is None or identity[0] != manifest['backend_identity']:
                raise FailedPreconditionError('Restored backend identity mismatch')
            if backend.execute("SELECT 1 FROM write_operations WHERE state='pending' LIMIT 1").fetchone():
                raise FailedPreconditionError('Restored backend contains nonterminal operations')
        (stage / 'registry.sqlite').rename(stage / 'registry.quarantined.sqlite')
        receipt = {'bundle_id': manifest['bundle_id'], 'state': 'restored_pending_validation',
                   'ready': False, 'required_validations': manifest['restore_requires']}
        (stage / 'RESTORE_PENDING.json').write_text(json.dumps(receipt, sort_keys=True, indent=2) + '\n')
        shutil.copyfile(manifest_path, stage / 'backup-manifest.json')
        for path in stage.rglob('*'):
            path.chmod(0o700 if path.is_dir() else 0o600)
        _publish(stage, destination)
        return receipt
    finally:
        if stage.exists():
            shutil.rmtree(stage)
