"""Late inference must not publish after cancellation; FIFO spans backends."""

import importlib.util
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def worker(tmp_path, monkeypatch):
    directory = Path(__file__).resolve().parents[2] / "deploy/parser-models"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location("ov_parser_worker_test", directory / "worker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "STATE", tmp_path / "state")
    monkeypatch.setattr(module, "JOBS", tmp_path / "state/jobs")
    monkeypatch.setattr(module, "BACKEND", "mineru")
    return module


def add_job(worker, status="running", backend="mineru", ordinal=1):
    job_id = f"{ordinal:032x}"
    with worker.database() as connection:
        connection.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)",
            (job_id, backend, "auto", "{}", status, ordinal, ordinal, ""),
        )
    directory = worker.JOBS / job_id
    (directory / "input").mkdir(parents=True)
    (directory / "input/input.pdf").write_bytes(b"input")
    (directory / "output").mkdir()
    (directory / "artifact.zip").write_bytes(b"validated")
    return job_id


def test_cancelled_late_result_cannot_publish(worker):
    job_id = add_job(worker)
    worker.cancel(job_id)
    with pytest.raises(BaseException) as exc:
        worker.publish(job_id)
    assert exc.type.__name__ == "CancelledError"
    assert worker.lookup(job_id)["status"] == "cancelled"
    assert not (worker.JOBS / job_id / "artifact.zip").exists()
    assert not (worker.STATE / "mineru.verified").exists()


def test_success_publication_removes_raw_input_and_cancel_does_not_rewrite(worker):
    job_id = add_job(worker)
    worker.publish(job_id)
    assert worker.cancel(job_id)["status"] == "completed"
    assert not (worker.JOBS / job_id / "input").exists()
    assert (worker.JOBS / job_id / "artifact.zip").exists()


def test_fifo_and_cancelled_queue_cleanup(worker):
    first = add_job(worker, "queued", "teleocr", 1)
    second = add_job(worker, "queued", "mineru", 2)
    assert worker.claim("mineru") is None
    worker.cancel(first) if worker.BACKEND == "teleocr" else worker.update(first, "cancelled")
    job, gate = worker.claim("mineru")
    try:
        assert job["id"] == second
        assert worker.claim("mineru") is None
    finally:
        gate.close()


def test_database_connection_is_closed(worker):
    with worker.database() as connection:
        connection.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_queued_cancel_removes_input(worker):
    job_id = add_job(worker, "queued")
    worker.cancel(job_id)
    assert not (worker.JOBS / job_id / "input").exists()


async def test_mineru_cancel_is_forwarded_to_resident_engine(worker, monkeypatch):
    import asyncio

    import httpx

    job_id = add_job(worker)
    worker.cancel(job_id)
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path == "/v1/tiers":
            return httpx.Response(200, json={})
        if request.method == "POST":
            return httpx.Response(200, json={"job_id": "engine-job"})
        return httpx.Response(200, json={"status": "canceled"})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        worker.httpx,
        "AsyncClient",
        lambda **kw: client_type(transport=httpx.MockTransport(handler), **kw),
    )
    with pytest.raises(asyncio.CancelledError):
        await worker.mineru_engine(
            {"id": job_id, "mode": "auto", "options": "{}"}, worker.JOBS / job_id
        )
    assert ("DELETE", "/v1/parse/jobs/engine-job") in seen


def ready_dependencies(worker, monkeypatch):
    env = worker.ROOT / 'env'
    (env / 'bin').mkdir(parents=True)
    (env / 'bin/python').touch()
    (worker.ROOT / 'locks').mkdir()
    (worker.ROOT / 'locks/mineru.requirements.txt').touch()
    (worker.ROOT / 'locks/mineru-models.json').touch()
    monkeypatch.setattr(worker, 'ENV', env)
    async def engine_ready():
        worker.app.state.engine_ready = True
        worker.app.state.engine_checked_at = worker.time.monotonic()
        return True
    monkeypatch.setattr(worker, 'engine_readiness', engine_ready)
    worker.app.state.engine_ready = True
    worker.app.state.engine_checked_at = worker.time.monotonic()


def test_dependencies_without_worker_are_not_ready(worker, monkeypatch):
    ready_dependencies(worker, monkeypatch)
    assert worker.health()['dependencies_ready']
    assert worker.health()['worker_state'] == 'stopped'
    assert not worker.health()['ready_for_jobs']


async def test_dead_queue_loop_rejects_jobs_before_upload(worker, monkeypatch):
    import asyncio
    from fastapi import HTTPException
    ready_dependencies(worker, monkeypatch)
    async def failed():
        raise RuntimeError('private model details must not enter health')
    task = asyncio.create_task(failed())
    await asyncio.sleep(0)
    worker.app.state.worker_task = task
    worker.app.state.recovery_complete = True
    assert worker.health()['worker_state'] == 'failed'
    assert not worker.health()['ready_for_jobs']
    assert 'private' not in str(worker.health())
    with pytest.raises(HTTPException) as exc:
        await worker.submit(file=None, backend='mineru', mode='auto', options='{}')
    assert exc.value.status_code == 503
    assert not worker.JOBS.exists()


async def test_actual_recovery_gate_controls_readiness(worker, monkeypatch):
    import asyncio
    import fcntl
    ready_dependencies(worker, monkeypatch)
    worker.STATE.mkdir()
    with (worker.STATE / 'document.lock').open('a') as gate:
        fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        async with worker.lifespan(worker.app):
            await asyncio.sleep(0)
            assert worker.health()['worker_state'] == 'recovering'
            assert not worker.health()['ready_for_jobs']
            fcntl.flock(gate, fcntl.LOCK_UN)
            async def recovered():
                while not worker.health()['ready_for_jobs']:
                    await asyncio.sleep(.01)
            await asyncio.wait_for(recovered(), 3)
            assert worker.health()['worker_state'] == 'running'
    assert worker.health()['worker_state'] == 'stopped'
    assert not worker.health()['ready_for_jobs']


async def test_lifespan_exception_still_stops_queue_task(worker, monkeypatch):
    import asyncio
    ready_dependencies(worker, monkeypatch)
    async def idle():
        worker.app.state.recovery_complete = True
        await asyncio.Event().wait()
    monkeypatch.setattr(worker, 'work', idle)
    with pytest.raises(ValueError, match='startup consumer'):
        async with worker.lifespan(worker.app):
            await asyncio.sleep(0)
            assert worker.health()['ready_for_jobs']
            raise ValueError('startup consumer')
    assert worker.app.state.worker_task.cancelled()
    assert not worker.health()['ready_for_jobs']
