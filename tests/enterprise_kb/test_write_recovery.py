"""Server-query recovery against real controlled local writes and SQLite CAS."""
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sqlite3
import threading
import time

import pytest

from test_content_publication import environment as environment
from test_index_plan import embedding as embedding
from openviking.indexing.controlled_write_backend import ControlledLocalWriteBackend
from openviking.indexing.publication import PublicationStore
from openviking.indexing.registry_authority import RegistryAuthority, AuthorityRepository
from openviking_cli.exceptions import ConflictError, FailedPreconditionError, PermissionDeniedError

URI = 'viking://resources/raw/document.md'


def _late_writer(directory, reference, ready, release, output):
    backend = ControlledLocalWriteBackend(directory)
    ready.set()
    release.wait(15)
    try:
        backend.commit_operation(reference, b'late old content')
    except FailedPreconditionError:
        output.put('fenced')
    else:
        output.put('written')


def _crashing_writer(directory, registry_db, reference, stage, ready):
    """Worker inside a real source_write fence whose process dies by SIGKILL:
    neither finish_write nor uncertain_write runs, so the durable registry row
    stays 'writing' — the exact TC-13.16 inflight crash window."""
    from openviking.indexing.publication import PublicationStore
    registry = PublicationStore(registry_db)
    token = registry.begin_write('a', URI, backend_ref=reference)
    ready.set()
    if stage == 'before-commit':
        # Parent kills here; the backend operation remains pending.
        while True:
            time.sleep(1)
    ControlledLocalWriteBackend(directory).commit_operation(reference, b'inflight bytes')
    if stage == 'after-commit-pre-finish':
        while True:
            time.sleep(1)


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / 'controlled'
    root.mkdir(mode=0o700)
    backend = ControlledLocalWriteBackend(root)
    source = backend.path(backend.source_path('a', URI))
    source.parent.mkdir(parents=True)
    source.write_bytes(b'current content')
    registry = PublicationStore(str(tmp_path / 'registry.db'))
    registry.namespace_register('a', 'raw', 'viking://resources/raw')
    registry.configure_write_recovery(backends={backend.identity: backend}, authorizer=lambda account, actor, uri: account == 'a' and actor == 'admin' and uri == URI)
    return registry, backend


def _uncertain(registry, backend, *, complete=True):
    ref = backend.prepare_source_write('a', URI)
    token = registry.begin_write('a', URI, backend_ref=ref)
    if complete:
        backend.commit_operation(ref, b'confirmed content')
    registry.uncertain_write(token)
    return token, ref


def _intent(registry, tokens, key='recover'):
    return registry.write_recovery_prepare('a', URI, 'admin', tokens, key)


def test_terminal_server_query_recovery_is_atomic_audited_and_idempotent(setup):
    registry, backend = setup
    token, ref = _uncertain(registry, backend)
    intent = _intent(registry, [token])
    result = registry.write_recovery_execute('a', intent['id'], 'admin')
    assert result['cleared_tokens'] == [token] and result['rebuild_required'] is True
    assert registry.pending_writes('a', URI) == []
    assert registry.observe_draft('a', URI) == result['new_epoch']
    assert registry.write_recovery_execute('a', intent['id'], 'admin') == result
    with registry.db() as db:
        assert db.execute('SELECT count(*) FROM source_recovery_audit').fetchone()[0] == 1
        audit = db.execute('SELECT * FROM source_recovery_audit').fetchone()
        assert audit['actor'] == 'admin' and audit['backend'] == backend.identity
        assert json.loads(audit['operations'])[0]['state'] == 'terminal'
    with pytest.raises(FailedPreconditionError, match='cannot write again'):
        backend.commit_operation(ref, b'replayed stale content')


def test_pending_backend_operation_and_fake_identity_never_unlock(setup):
    registry, backend = setup
    token, ref = _uncertain(registry, backend, complete=False)
    intent = _intent(registry, [token])
    with pytest.raises(FailedPreconditionError, match='not terminal'):
        registry.write_recovery_execute('a', intent['id'], 'admin')
    assert registry.pending_writes('a', URI)
    # Even a worker fabricating a bound operation ID cannot manufacture a
    # server-side durable terminal record in the configured adapter.
    fake = dict(ref, operation_id='client-claimed-completed')
    forged = registry.begin_write('a', URI, backend_ref=fake)
    registry.uncertain_write(forged)
    intent = _intent(registry, [forged], key='forged')
    with pytest.raises(FailedPreconditionError, match='not terminal'):
        registry.write_recovery_execute('a', intent['id'], 'admin')
    assert len(registry.pending_writes('a', URI)) == 2


def test_plain_agfs_missing_server_capability_and_client_extra_proof_fail_closed(setup):
    registry, backend = setup
    token = registry.begin_write('a', URI)
    registry.uncertain_write(token)
    assert registry.write_recovery_diagnose('a', URI)['recovery_supported'] is False
    intent = _intent(registry, [token])
    with pytest.raises(FailedPreconditionError, match='identity unavailable'):
        registry.write_recovery_execute('a', intent['id'], 'admin')
    with pytest.raises(TypeError):
        registry.write_recovery_execute('a', intent['id'], 'admin', proof={'completed': True})
    assert registry.pending_writes('a', URI)


def test_new_writer_cas_audit_failure_and_authorization_revocation_keep_all_tokens(setup):
    registry, backend = setup
    first, _ = _uncertain(registry, backend)
    intent = _intent(registry, [first])
    second = registry.begin_write('a', URI)
    with pytest.raises(ConflictError, match='token set'):
        registry.write_recovery_execute('a', intent['id'], 'admin')
    assert {row['id'] for row in registry.pending_writes('a', URI)} == {first, second}
    # Recover only the verified first token, while preserving an unrelated one.
    intent = _intent(registry, [first], key='audit')
    with registry.db() as db:
        db.execute("CREATE TRIGGER reject_recovery_audit BEFORE INSERT ON source_recovery_audit BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
    with pytest.raises(sqlite3.IntegrityError, match='audit unavailable'):
        registry.write_recovery_execute('a', intent['id'], 'admin')
    assert len(registry.pending_writes('a', URI)) == 2
    with registry.db() as db:
        db.execute('DROP TRIGGER reject_recovery_audit')
    checks = []
    def revoke(account, actor, uri):
        checks.append(1)
        return len(checks) == 1
    registry.configure_write_recovery(backends={backend.identity: backend}, authorizer=revoke)
    with pytest.raises(PermissionDeniedError):
        registry.write_recovery_execute('a', intent['id'], 'admin')
    assert len(registry.pending_writes('a', URI)) == 2
    registry.configure_write_recovery(backends={backend.identity: backend}, authorizer=lambda *args: True)
    registry.write_recovery_execute('a', intent['id'], 'admin')
    assert [row['id'] for row in registry.pending_writes('a', URI)] == [second]


def test_restarted_backend_fences_delayed_independent_writer_before_recovery(setup):
    registry, backend = setup
    token, reference = _uncertain(registry, backend, complete=False)
    context = multiprocessing.get_context('spawn')
    ready, release, output = context.Event(), context.Event(), context.Queue()
    writer = context.Process(target=_late_writer, args=(str(backend.root), reference, ready, release, output))
    writer.start()
    try:
        assert ready.wait(15)
        # Both recovery registry and adapter reopen their persisted state while
        # the old writer process still exists, paused before its backend commit.
        restarted = ControlledLocalWriteBackend(backend.root)
        restarted.fence_operation(reference)
        reopened = PublicationStore(registry.path)
        reopened.configure_write_recovery(backends={restarted.identity: restarted}, authorizer=lambda *args: True)
        result = reopened.write_recovery_execute('a', _intent(reopened, [token])['id'], 'admin')
        assert result['state'] == 'recovered'
        # A new source version uses a new backend operation. Resuming the old
        # worker cannot overwrite it, even after registry/adapter reopen.
        fresh = restarted.prepare_source_write('a', URI)
        fresh_token = reopened.begin_write('a', URI, backend_ref=fresh)
        restarted.commit_operation(fresh, b'new rebuilt content')
        reopened.finish_write(fresh_token)
        release.set()
        assert output.get(timeout=15) == 'fenced'
        writer.join(15)
        assert writer.exitcode == 0
        assert restarted.path(restarted.source_path('a', URI)).read_bytes() == b'new rebuilt content'
    finally:
        release.set()
        if writer.is_alive():
            writer.terminate()
        writer.join(15)


async def test_actual_vikingfs_records_controlled_backend_operation(environment, embedding, tmp_path):
    backend = ControlledLocalWriteBackend(tmp_path / 'controlled-vfs')
    e = environment
    e.fs._async_agfs = backend
    await e.fs.write(URI, 'actual source', ctx=e.ctx)
    assert await e.fs.read_file(URI, ctx=e.ctx) == 'actual source'
    assert not e.store.pending_writes('acct', URI)
    with backend.db() as db:
        record = db.execute('SELECT * FROM write_operations').fetchone()
        assert record['state'] == 'terminal' and record['account'] == 'acct' and record['uri'] == URI


def test_crash_mid_fence_with_pending_backend_operation_is_diagnosed_then_recoverable(setup, tmp_path):
    """TC-13.16, crash window 1: SIGKILL inside the fenced await before the
    backend committed. The durable registry row stays 'writing' (no uncertain
    handler ran); recovery must fail closed on the pending operation and then
    succeed once the operator fences it."""
    registry, backend = setup
    reference = backend.prepare_source_write('a', URI)
    context = multiprocessing.get_context('spawn')
    ready, output = context.Event(), context.Queue()
    writer = context.Process(target=_crashing_writer,
                             args=(str(backend.root), str(tmp_path / 'registry.db'), reference, 'before-commit', ready))
    writer.start()
    try:
        assert ready.wait(15)
        rows = None
        db = sqlite3.connect(str(tmp_path / 'registry.db'))
        try:
            db.execute('PRAGMA busy_timeout=10000')
            rows = db.execute("SELECT state FROM source_writes WHERE uri=?", (URI,)).fetchall()
        finally:
            db.close()
        assert [row[0] for row in rows] == ['writing'], rows
        writer.kill()
        writer.join(15)
        assert writer.exitcode == -signal.SIGKILL
    finally:
        if writer.is_alive():
            writer.terminate()
        writer.join(15)
    assert len(registry.pending_writes('a', URI)) == 1
    assert registry.pending_writes('a', URI)[0]['state'] == 'writing'
    diagnostic = registry.write_recovery_diagnose('a', URI)
    assert diagnostic['recovery_supported'] is True
    intent = _intent(registry, [registry.pending_writes('a', URI)[0]['id']], key='inflight-crash')
    with pytest.raises(FailedPreconditionError, match='not terminal'):
        registry.write_recovery_execute('a', intent['id'], 'admin')
    backend.fence_operation(reference)
    result = registry.write_recovery_execute('a', intent['id'], 'admin')
    assert result['state'] == 'recovered' and result['cleared_tokens']
    assert registry.pending_writes('a', URI) == []
    with backend.db() as db:
        assert db.execute("SELECT state FROM write_operations WHERE id=?", (reference['operation_id'],)).fetchone()[0] == 'fenced'


def test_crash_after_backend_commit_before_finish_recovers_verified_bytes(setup, tmp_path):
    """TC-13.16, crash window 2: SIGKILL after the backend durably committed
    but before finish_write. The stale 'writing' registry row must clear with
    the verified bytes; a restarted reader sees the committed content."""
    registry, backend = setup
    source = backend.path(backend.source_path('a', URI))
    reference = backend.prepare_source_write('a', URI)
    context = multiprocessing.get_context('spawn')
    ready, output = context.Event(), context.Queue()
    writer = context.Process(target=_crashing_writer,
                             args=(str(backend.root), str(tmp_path / 'registry.db'), reference, 'after-commit-pre-finish', ready))
    writer.start()
    try:
        assert ready.wait(15)
        db = sqlite3.connect(str(tmp_path / 'registry.db'))
        try:
            db.execute('PRAGMA busy_timeout=10000')
            while True:
                states = {row[0] for row in db.execute("SELECT state FROM source_writes WHERE uri=?", (URI,)).fetchall()}
                if states <= {'writing'}:
                    break
                time.sleep(0.05)
        finally:
            db.close()
        operations = sqlite3.connect(str(backend.root / 'operations.db'))
        try:
            operations.execute('PRAGMA busy_timeout=10000')
            while True:
                row = operations.execute("SELECT state FROM write_operations WHERE id=?", (reference['operation_id'],)).fetchone()
                if row and row[0] == 'terminal':
                    break
                time.sleep(0.05)
        finally:
            operations.close()
        writer.kill()
        writer.join(15)
        assert writer.exitcode == -signal.SIGKILL
    finally:
        if writer.is_alive():
            writer.terminate()
        writer.join(15)
    assert [row['state'] for row in registry.pending_writes('a', URI)] == ['writing']
    token = registry.pending_writes('a', URI)[0]['id']
    intent = _intent(registry, [token], key='committed-crash')
    result = registry.write_recovery_execute('a', intent['id'], 'admin')
    assert result['state'] == 'recovered' and result['sha256'] == hashlib.sha256(b'inflight bytes').hexdigest()
    assert source.read_bytes() == b'inflight bytes'
    assert registry.pending_writes('a', URI) == []
    # Post-recovery the source is writable again through a fresh one-shot op.
    fresh = backend.prepare_source_write('a', URI)
    fresh_token = registry.begin_write('a', URI, backend_ref=fresh)
    backend.commit_operation(fresh, b'rebuilt after crash recovery')
    registry.finish_write(fresh_token)
    assert registry.pending_writes('a', URI) == []


def _serve_recovery(endpoint, database, token_file, backend_dir, authorization_file, ready):
    backend = ControlledLocalWriteBackend(backend_dir)
    def authorize(account, actor, uri):
        policy = json.loads(Path(authorization_file).read_text())
        return policy == {'account': account, 'actor': actor, 'uri': uri, 'write': True, 'manage': True}
    with RegistryAuthority(endpoint, database, token_file,
                           recovery_backends={backend.identity: backend}, recovery_authorizer=authorize) as server:
        ready.set()
        server.serve_forever(poll_interval=0.02)


@pytest.fixture
async def remote_api(environment, embedding, tmp_path, monkeypatch):
    import tempfile
    from dataclasses import replace
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from test_wiki_catalog import register_domains
    from openviking.server.auth import get_request_context
    from openviking.server.identity import Role
    from openviking.server.models import ERROR_CODE_TO_HTTP_STATUS
    from openviking.server.routers import write_recovery
    from openviking_cli.exceptions import OpenVikingError
    e = environment
    context = [replace(e.ctx, role=Role.ADMIN)]
    backend = ControlledLocalWriteBackend(tmp_path / 'controlled-http')
    authorization = tmp_path / 'server-authorization.json'
    authorization.write_text(json.dumps({'account':'acct', 'actor':'user', 'uri':URI, 'write':True, 'manage':True}))
    ctx = multiprocessing.get_context('spawn')
    ready = ctx.Event()
    with tempfile.TemporaryDirectory(prefix='ov-rec-', dir='/tmp') as private:
        tokenfile = Path(private) / 'token'
        tokenfile.write_text('x' * 64)
        tokenfile.chmod(0o600)
        endpoint = str(Path(private).resolve() / 's')
        database = str(tmp_path / 'authority-recovery.db')
        process = ctx.Process(target=_serve_recovery, args=(endpoint, database, str(tokenfile), str(backend.root), str(authorization), ready))
        process.start()
        assert ready.wait(15)
        try:
            embedding.chunking.publication_path = ''
            embedding.chunking.publication_authority_socket = endpoint
            embedding.chunking.publication_authority_token_file = str(tokenfile)
            e.store = AuthorityRepository(endpoint, str(tokenfile))
            e.fs._async_agfs = backend
            await register_domains(e)
            app = FastAPI()
            app.include_router(write_recovery.router)
            app.dependency_overrides[get_request_context] = lambda: context[0]
            monkeypatch.setattr(write_recovery, 'get_service', lambda: SimpleNamespace(viking_fs=e.fs))
            @app.exception_handler(OpenVikingError)
            async def errors(request, exc):
                return JSONResponse(status_code=ERROR_CODE_TO_HTTP_STATUS.get(exc.code, 500), content={
                    'status':'error','error':{'code':exc.code,'message':exc.message,'details':exc.details}})
            yield app, e, backend, context, authorization
        finally:
            process.terminate()
            process.join(15)


async def test_real_authority_http_uses_server_query_and_rejects_client_proofs(remote_api):
    import httpx
    app, e, backend, context, authorization = remote_api
    ref = backend.prepare_source_write('acct', URI)
    token = e.store.begin_write('acct', URI, backend_ref=ref)
    backend.commit_operation(ref, b'confirmed current bytes')
    e.store.uncertain_write(token)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        diagnostic = await client.post('/api/v1/kb/write-recovery/diagnostics', json={'uri':URI})
        assert diagnostic.status_code == 200 and diagnostic.json()['result']['recovery_supported'] is True
        request = {'uri':URI, 'tokens':[token], 'idempotency_key':'http'}
        assert (await client.post('/api/v1/kb/write-recovery/recoveries', json={**request,'proof':{'completed':True}})).status_code == 422
        assert e.store.pending_writes('acct', URI)
        first = await client.post('/api/v1/kb/write-recovery/recoveries', json=request)
        assert first.status_code == 200, first.text
        result = first.json()['result']
        assert result['state'] == 'recovered' and result['rebuild_required'] is True
        assert (await client.post('/api/v1/kb/write-recovery/recoveries', json=request)).json()['result'] == result
        fetched = await client.get('/api/v1/kb/write-recovery/recoveries/' + result['recovery_id'])
        assert fetched.status_code == 200 and fetched.json()['result']['state'] == 'completed'
        assert e.store.pending_writes('acct', URI) == []


async def test_real_authority_current_server_authorization_can_revoke_recovery(remote_api):
    import httpx
    app, e, backend, context, authorization = remote_api
    ref = backend.prepare_source_write('acct', URI)
    token = e.store.begin_write('acct', URI, backend_ref=ref)
    backend.commit_operation(ref, b'confirmed current bytes')
    e.store.uncertain_write(token)
    # Worker request context still permits source/domain access; the authority's
    # own current provider has revoked domain management and must refuse.
    authorization.write_text(json.dumps({'account':'acct','actor':'user','uri':URI,'write':True,'manage':False}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/api/v1/kb/write-recovery/recoveries', json={'uri':URI,'tokens':[token],'idempotency_key':'revoked'})
        assert response.status_code == 403
        assert e.store.pending_writes('acct', URI)
        # Cross-account scope cannot retrieve the first account's intent.
        with pytest.raises(__import__('openviking_cli.exceptions', fromlist=['NotFoundError']).NotFoundError) as missing:
            e.store.write_recovery_get('other', e.store.write_recovery_prepare('acct', URI, 'user', [token], 'private')['id'])
        assert 'not found' in str(missing.value).lower()


def test_recovery_requires_registered_source_domain_and_pins_domain_revision(setup):
    registry, backend = setup
    token, _ = _uncertain(registry, backend)
    with pytest.raises(FailedPreconditionError, match='registered knowledge namespace'):
        registry.write_recovery_prepare('a', 'viking://resources/raw-sibling/doc.md', 'admin',
                                        [registry.begin_write('a', 'viking://resources/raw-sibling/doc.md')], 'unregistered')
    intent = _intent(registry, [token])
    # A real registered-domain policy update makes the prepared intent stale.
    registry.namespace_register('a', 'other', 'viking://resources/other')
    with pytest.raises(ConflictError, match='policy revision changed'):
        registry.write_recovery_execute('a', intent['id'], 'admin')
    assert [row['id'] for row in registry.pending_writes('a', URI)] == [token]
    with registry.db() as db:
        assert db.execute('SELECT count(*) FROM source_recovery_audit').fetchone()[0] == 0
    result = registry.write_recovery_execute('a', _intent(registry, [token], key='new-policy')['id'], 'admin')
    assert result['cleared_tokens'] == [token]


def test_recovery_intents_cannot_cross_source_account_actor_or_operation_scope(setup):
    from openviking_cli.exceptions import NotFoundError
    registry, backend = setup
    token, ref = _uncertain(registry, backend)
    other_uri = 'viking://resources/raw/other.md'
    for account, uri in [('other', URI), ('a', other_uri)]:
        with pytest.raises(ValueError, match='bound to this account'):
            registry.begin_write(account, uri, backend_ref=ref)
        with pytest.raises(ConflictError, match='current source writers'):
            registry.write_recovery_prepare(account, uri, 'admin', [token], 'cross-scope')
    intent = _intent(registry, [token])
    with pytest.raises(NotFoundError):
        registry.write_recovery_execute('other', intent['id'], 'admin')
    with pytest.raises(PermissionDeniedError, match='another actor'):
        registry.write_recovery_execute('a', intent['id'], 'other-admin')
    with pytest.raises(ConflictError, match='another actor'):
        registry.write_recovery_prepare('a', URI, 'other-admin', [token], 'recover')
    assert [row['id'] for row in registry.pending_writes('a', URI)] == [token]


def test_competing_intents_only_clear_a_writer_once_and_completed_replay_rechecks_acl(setup):
    registry, backend = setup
    token, _ = _uncertain(registry, backend)
    first, second = _intent(registry, [token], 'first'), _intent(registry, [token], 'second')
    barrier = threading.Barrier(2)
    outcomes = []
    def execute(intent):
        barrier.wait(timeout=10)
        try:
            outcomes.append(registry.write_recovery_execute('a', intent['id'], 'admin'))
        except (FailedPreconditionError, ConflictError) as exc:
            outcomes.append(exc)
    workers = [threading.Thread(target=execute, args=(intent,)) for intent in [first, second]]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(15)
        assert not worker.is_alive()
    successes = [result for result in outcomes if isinstance(result, dict)]
    assert len(successes) == 1
    assert len(outcomes) == 2 and isinstance(next(result for result in outcomes if not isinstance(result, dict)), (FailedPreconditionError, ConflictError))
    with registry.db() as db:
        assert db.execute('SELECT count(*) FROM source_recovery_audit').fetchone()[0] == 1
    registry.configure_write_recovery(backends={backend.identity: backend}, authorizer=lambda *args: False)
    with pytest.raises(PermissionDeniedError):
        registry.write_recovery_execute('a', successes[0]['recovery_id'], 'admin')


def test_audit_failure_rolls_back_epoch_hash_intent_and_fences(setup):
    registry, backend = setup
    token, _ = _uncertain(registry, backend)
    intent = _intent(registry, [token])
    with registry.db() as db:
        before = dict(db.execute('SELECT * FROM drafts WHERE account=? AND uri=?', ('a', URI)).fetchone())
        db.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON source_recovery_audit BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='injected audit failure'):
        registry.write_recovery_execute('a', intent['id'], 'admin')
    with registry.db() as db:
        after = dict(db.execute('SELECT * FROM drafts WHERE account=? AND uri=?', ('a', URI)).fetchone())
        assert after == before
        assert db.execute('SELECT count(*) FROM source_write_refs WHERE token=?', (token,)).fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM source_recovery_audit').fetchone()[0] == 0
    assert registry.write_recovery_get('a', intent['id'])['state'] == 'prepared'
    assert registry.pending_writes('a', URI)[0]['id'] == token


def test_domain_revision_changed_inside_backend_verification_fails_final_cas(setup):
    from contextlib import contextmanager
    registry, backend = setup
    token, _ = _uncertain(registry, backend)
    intent = _intent(registry, [token])
    class ChangedDomainBackend:
        identity = backend.identity
        @contextmanager
        def confirm_and_read(self, *args):
            with backend.confirm_and_read(*args) as verified:
                registry.namespace_register('a', 'during-verification', 'viking://resources/new-domain')
                yield verified
    registry.configure_write_recovery(backends={backend.identity: ChangedDomainBackend()}, authorizer=lambda *args: True)
    with pytest.raises(ConflictError, match='policy revision changed'):
        registry.write_recovery_execute('a', intent['id'], 'admin')
    assert registry.write_recovery_get('a', intent['id'])['state'] == 'prepared'
    assert registry.pending_writes('a', URI)[0]['id'] == token


def test_terminal_backend_record_without_readable_source_cannot_release_fence(setup):
    registry, backend = setup
    token, _ = _uncertain(registry, backend)
    backend.path(backend.source_path('a', URI)).unlink()
    intent = _intent(registry, [token])
    with pytest.raises(FailedPreconditionError, match='source bytes are unavailable'):
        registry.write_recovery_execute('a', intent['id'], 'admin')
    assert registry.pending_writes('a', URI)[0]['id'] == token
    assert registry.write_recovery_get('a', intent['id'])['state'] == 'prepared'
