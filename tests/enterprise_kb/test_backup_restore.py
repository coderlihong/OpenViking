"""Controlled offline bundles, real SQLite metadata and fail-closed quarantine."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile

import pytest

from openviking.indexing.backup_restore import export_bundle, restore_bundle
from openviking.indexing.controlled_write_backend import ControlledLocalWriteBackend
from openviking.indexing.publication import PublicationStore
from openviking.indexing.registry_authority import RegistryAuthority
from openviking_cli.exceptions import FailedPreconditionError


@pytest.fixture
def components(tmp_path):
    registry = PublicationStore(str(tmp_path / 'registry.db'))
    registry.namespace_register('acct', 'raw', 'viking://resources/raw')
    backend = ControlledLocalWriteBackend(tmp_path / 'agfs')
    ref = backend.prepare_source_write('acct', 'viking://resources/raw/document.md')
    backend.commit_operation(ref, b'persisted source')
    identity = tmp_path / 'identity.json'
    identity.write_text(json.dumps({'accounts': {'acct': {'users': {'user': {'groups': ['private']}}}}}))
    config = tmp_path / 'models.json'
    config.write_text(json.dumps({'embedding': {'model': 'test-embedding', 'dimension': 3}}))
    vectors = tmp_path / 'vectors'
    vectors.mkdir()
    (vectors / 'records.json').write_text(json.dumps({'acl': {'uri': 'viking://resources/raw', 'mode': 'restricted', 'read': ['user:user']},
                                                     'vectors': [{'uri': 'viking://resources/raw/document.md', 'embedding': [1, 0, 0]}]}))
    @contextmanager
    def stopped_component_writers():
        # All components in this isolated fixture are offline and only this
        # test mutates them. This does not model a production vendor snapshot.
        yield
    return dict(registry=registry.path, backend=backend, identity=identity,
                model_config=config, vectors_acl=vectors, quiescence=stopped_component_writers)


def export(components, tmp_path, **kwargs):
    destination = tmp_path / 'backup'
    result = export_bundle(**components, destination=destination, **kwargs)
    digest = hashlib.sha256((destination / 'manifest.json').read_bytes()).hexdigest()
    return result, destination, digest


def test_full_dependency_bytes_restored_but_registry_quarantined(components, tmp_path):
    registry = PublicationStore(components['registry'])
    registry.job_create('acct', 'raw', 'uri-job', 'D2', {'uri': 'viking://resources/raw/document.md'}, {}, 'user')
    manifest, bundle, digest = export(components, tmp_path)
    restored = tmp_path / 'restored'
    result = restore_bundle(bundle, restored, expected_manifest_sha256=digest)
    assert result['ready'] is False and result['state'] == 'restored_pending_validation'
    assert not (restored / 'registry.sqlite').exists()
    assert (restored / 'RESTORE_PENDING.json').exists()
    with pytest.raises(FailedPreconditionError, match='RESTORE_PENDING'):
        PublicationStore(str(restored / 'registry.quarantined.sqlite'))
    with tempfile.TemporaryDirectory(prefix='ov-restore-', dir='/tmp') as private:
        token = Path(private) / 'token'
        token.write_text('x' * 64)
        token.chmod(0o600)
        with pytest.raises(FailedPreconditionError, match='RESTORE_PENDING'):
            RegistryAuthority(str(Path(private).resolve() / 's'), str(restored / 'registry.quarantined.sqlite'), str(token))
    with sqlite3.connect(restored / 'registry.quarantined.sqlite') as reopened:
        assert reopened.execute('SELECT root_uri FROM kb_namespaces WHERE account=? AND namespace=?', ('acct', 'raw')).fetchone()[0] == 'viking://resources/raw'
        assert json.loads(reopened.execute('SELECT payload FROM kb_jobs WHERE account=?', ('acct',)).fetchone()[0])['uri'] == 'viking://resources/raw/document.md'
    backend = ControlledLocalWriteBackend(restored / 'backend')
    assert backend.identity == manifest['backend_identity']
    assert backend.path(backend.source_path('acct', 'viking://resources/raw/document.md')).read_bytes() == b'persisted source'
    assert (restored / 'identity.json').read_bytes() == Path(components['identity']).read_bytes()
    assert (restored / 'model_config.json').read_bytes() == Path(components['model_config']).read_bytes()
    assert (restored / 'vectors_acl/records.json').read_bytes() == (components['vectors_acl'] / 'records.json').read_bytes()
    assert (restored / 'identity.json').stat().st_mode & 0o077 == 0


@pytest.mark.parametrize('mutation', ['missing_identity', 'wrong_bytes', 'extra_file', 'symlink', 'bad_manifest'])
def test_restore_corruption_missing_dependency_and_alias_do_not_leave_startable_tree(components, tmp_path, mutation):
    _, bundle, digest = export(components, tmp_path)
    if mutation == 'missing_identity':
        (bundle / 'identity.json').unlink()
    elif mutation == 'wrong_bytes':
        (bundle / 'model_config.json').write_text('{}')
    elif mutation == 'extra_file':
        (bundle / 'untracked').write_text('unexpected')
    elif mutation == 'symlink':
        (bundle / 'identity.json').unlink()
        (bundle / 'identity.json').symlink_to(components['identity'])
    else:
        (bundle / 'manifest.json').write_text('{}')
    restored = tmp_path / 'restored'
    with pytest.raises(FailedPreconditionError):
        restore_bundle(bundle, restored, expected_manifest_sha256=digest)
    assert not restored.exists()
    assert not list(tmp_path.glob('.kb-restore-*'))


def test_active_authority_and_uncertain_source_prevent_joint_snapshot(components, tmp_path):
    # An independent authority owns the same lifetime lock in production; this
    # actual server instance is enough to demonstrate flock exclusion here.
    with tempfile.TemporaryDirectory(prefix='ov-bak-', dir='/tmp') as private:
        token = Path(private) / 'token'
        token.write_text('x' * 64)
        token.chmod(0o600)
        with RegistryAuthority(str(Path(private).resolve() / 's'), components['registry'], str(token)):
            with pytest.raises(FailedPreconditionError, match='live authority'):
                export(components, tmp_path)
    assert not (tmp_path / 'backup').exists()
    registry = PublicationStore(components['registry'])
    token = registry.begin_write('acct', 'viking://resources/raw/document.md')
    registry.uncertain_write(token)
    with pytest.raises(FailedPreconditionError, match='source-write recovery'):
        export(components, tmp_path)
    assert not (tmp_path / 'backup').exists()


def test_backend_pending_and_unmaterialized_upload_require_recovery_or_input(components, tmp_path):
    backend = components['backend']
    ref = backend.prepare_source_write('acct', 'viking://resources/raw/next.md')
    with pytest.raises(FailedPreconditionError, match='nonterminal operation'):
        export(components, tmp_path)
    backend.fence_operation(ref)
    registry = PublicationStore(components['registry'])
    registry.job_create('acct', 'raw', 'upload', 'D1', {'temp_file_id': 'not-archived'}, {}, 'user')
    with pytest.raises(FailedPreconditionError, match='temporary input'):
        export(components, tmp_path)
    assert not (tmp_path / 'backup').exists()


def test_missing_wiki_immutable_blob_and_bounded_backup_fail_closed(components, tmp_path):
    registry = PublicationStore(components['registry'])
    missing_sha = hashlib.sha256(b'missing schema body').hexdigest()
    with registry.db() as db:
        db.execute('INSERT INTO wiki_schemas VALUES (?,?,?,?,?,?)', ('acct', 'schema', 'v1', missing_sha,
                                                                  json.dumps({'sha256': missing_sha}), 'registered'))
    with pytest.raises(FailedPreconditionError):
        export(components, tmp_path)
    blob = components['backend'].data / 'local/acct/_system/index_blobs' / (missing_sha + '.bin')
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b'missing schema body')
    with pytest.raises(FailedPreconditionError, match='quota'):
        export(components, tmp_path, max_total_bytes=1)
    assert not (tmp_path / 'backup').exists()
    assert not list(tmp_path.glob('.kb-backup-*'))
    export(components, tmp_path)


def test_generic_agfs_and_absent_quiescence_not_accepted(components, tmp_path):
    with pytest.raises(FailedPreconditionError, match='capability'):
        export_bundle(**{**components, 'backend': object()}, destination=tmp_path / 'backup')
    with pytest.raises(FailedPreconditionError, match='capability'):
        export_bundle(**{**components, 'quiescence': None}, destination=tmp_path / 'backup')


def test_raw_shared_originals_are_not_permanently_archived(components, tmp_path):
    raw = components['backend'].data / 'local/acct/upload/bucket/token/content'
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b'original private upload')
    _, bundle, digest = export(components, tmp_path)
    assert raw.exists()
    assert not (bundle / 'backend/data/local/acct/upload').exists()
    restored = tmp_path / 'restored'
    restore_bundle(bundle, restored, expected_manifest_sha256=digest)
    assert not (restored / 'backend/data/local/acct/upload').exists()


def test_component_mutation_despite_operator_guard_rejects_snapshot(components, tmp_path, monkeypatch):
    import openviking.indexing.backup_restore as module
    original = module._snapshot_sqlite
    def changing_snapshot(source, destination):
        original(source, destination)
        Path(components['model_config']).write_text(json.dumps({'embedding': {'model': 'changed'}}))
    monkeypatch.setattr(module, '_snapshot_sqlite', changing_snapshot)
    with pytest.raises(FailedPreconditionError, match='changed despite'):
        export(components, tmp_path)
    assert not (tmp_path / 'backup').exists()
    assert not list(tmp_path.glob('.kb-backup-*'))


def test_restore_quota_and_weakened_validation_manifest_fail_closed(components, tmp_path):
    _, bundle, digest = export(components, tmp_path)
    with pytest.raises(FailedPreconditionError, match='quota'):
        restore_bundle(bundle, tmp_path / 'restored', expected_manifest_sha256=digest, max_total_bytes=1)
    manifest = json.loads((bundle / 'manifest.json').read_text())
    manifest['restore_requires'] = []
    (bundle / 'manifest.json').write_text(json.dumps(manifest))
    changed_digest = hashlib.sha256((bundle / 'manifest.json').read_bytes()).hexdigest()
    with pytest.raises(FailedPreconditionError, match='dependencies'):
        restore_bundle(bundle, tmp_path / 'restored', expected_manifest_sha256=changed_digest)
    assert not (tmp_path / 'restored').exists()


def _independent_source_writer(registry_path, backend_dir, ready, release, blocked, resume, done):
    ready.set()
    if not release.wait(15):
        return
    try:
        registry = PublicationStore(registry_path)
    except RuntimeError:
        # Lifetime ownership rejects a direct worker immediately. A separate
        # operator signal explicitly permits a new operation after the export.
        blocked.set()
        if not resume.wait(15):
            return
        registry = PublicationStore(registry_path)
    backend = ControlledLocalWriteBackend(backend_dir)
    uri = 'viking://resources/raw/document.md'
    reference = backend.prepare_source_write('acct', uri)
    token = registry.begin_write('acct', uri, backend_ref=reference)
    backend.commit_operation(reference, b'new content after snapshot')
    registry.finish_write(token)
    done.set()


def test_joint_locks_hold_independent_source_writer_until_snapshot_finishes(components, tmp_path, monkeypatch):
    import multiprocessing
    import openviking.indexing.backup_restore as module
    context = multiprocessing.get_context('spawn')
    ready, release, blocked, resume, done = [context.Event() for _ in range(5)]
    writer = context.Process(target=_independent_source_writer,
                             args=(components['registry'], str(components['backend'].root), ready, release, blocked, resume, done))
    writer.start()
    try:
        assert ready.wait(15)
        original = module._snapshot_sqlite
        def snapshot_while_writer_attempts(source, destination):
            release.set()
            assert blocked.wait(10)
            assert not done.is_set()
            original(source, destination)
            assert not done.is_set()
        monkeypatch.setattr(module, '_snapshot_sqlite', snapshot_while_writer_attempts)
        _, bundle, digest = export(components, tmp_path)
        resume.set()
        assert done.wait(15)
        writer.join(15)
        assert writer.exitcode == 0
        source_path = components['backend'].source_path('acct', 'viking://resources/raw/document.md')
        assert components['backend'].path(source_path).read_bytes() == b'new content after snapshot'
        restored = tmp_path / 'restored'
        restore_bundle(bundle, restored, expected_manifest_sha256=digest)
        reopened = ControlledLocalWriteBackend(restored / 'backend')
        assert reopened.path(source_path).read_bytes() == b'persisted source'
    finally:
        release.set()
        resume.set()
        if writer.is_alive():
            writer.terminate()
        writer.join(15)


def test_unknown_managed_vector_window_blocks_joint_backup(components, tmp_path):
    registry = PublicationStore(components['registry'])
    uri = 'viking://resources/raw/document.md'
    generation = registry.begin('acct', 'raw', uri, 'p', ['c'])
    token = registry.generation_write_begin('acct', 'adapter-boundary', [{
        'account_id': 'acct', 'uri': uri, 'index_generation': generation,
        'index_plan_id': 'p', 'chunk_id': 'c', 'level': 2,
    }])
    registry.generation_write_unknown('acct', token, 'adapter-boundary')
    with pytest.raises(FailedPreconditionError, match='Managed vector write recovery'):
        export(components, tmp_path)
    assert not (tmp_path / 'backup').exists()
