"""Authenticated, durable parser jobs with a single shared document inference slot.

Model packages live in isolated environments mounted at /models/envs. The HTTP
process never imports their incompatible torch/vLLM versions. Raw inputs are
removed after a validated result is ready; failed jobs have a bounded TTL.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import secrets
import shutil
import signal
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pymupdf
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pdf_image_preservation import preserve_pdf_images, annotate_artifact
from model_artifact import (
    BACKEND_MODES,
    package_document,
    safe_extract,
    validate_document,
    validate_options,
)

ROOT = Path(os.environ.get("PARSER_ROOT", "/models"))
BACKEND = os.environ.get("PARSER_BACKEND", "logics")
STATE = ROOT / "state"
JOBS = STATE / "jobs"
KEY_FILE = ROOT / "secrets/parser.key"
MAX_UPLOAD = int(os.environ.get("PARSER_MAX_UPLOAD_BYTES", str(1024**3)))
TIMEOUT = int(os.environ.get("PARSER_JOB_TIMEOUT", "7200"))
TTL = int(os.environ.get("PARSER_ARTIFACT_TTL", "86400"))
SOURCE = ROOT / "source" / BACKEND
ENV = ROOT / "envs" / BACKEND
WEIGHTS = ROOT / "weights" / BACKEND
ENGINE_URL = os.environ.get("PARSER_MINERU_ENGINE", "http://172.17.0.1:9110")


@contextlib.contextmanager
def database():
    STATE.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(STATE / "jobs.sqlite", timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, backend TEXT, mode TEXT, options TEXT, status TEXT, created REAL, updated REAL, error TEXT)"
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    if "error" not in columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN error TEXT")
    connection.commit()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def update(job_id, status, error=""):
    with database() as connection:
        connection.execute(
            "UPDATE jobs SET status=?,updated=?,error=? WHERE id=?",
            (status, time.time(), error[:4000], job_id),
        )


def claim(backend):
    """Hold an OS lock until inference finishes, across all backend containers."""
    STATE.mkdir(parents=True, exist_ok=True)
    lock = (STATE / "document.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM jobs WHERE status='queued' ORDER BY created,id LIMIT 1"
        ).fetchone()
        if row is None or row["backend"] != backend:
            lock.close()
            return None
        connection.execute(
            "UPDATE jobs SET status='running',updated=? WHERE id=? AND status='queued'",
            (time.time(), row["id"]),
        )
    return dict(row), lock


def command(job, directory):
    mode = job["mode"]
    options = validate_options(job["backend"], mode, json.loads(job["options"]))
    python = str(ENV / "bin/python")
    output = directory / "output"
    input_pdf = directory / "input/input.pdf"
    if BACKEND == "teleocr":
        return [
            python,
            str(SOURCE / "infer.py"),
            "--image_sub_path",
            str(input_pdf.parent),
            "--result_save_path",
            str(output),
            "--use_async",
            "--override",
            f"model_path={WEIGHTS}",
            "BACKEND=vllm-async-engine",
            f"LAYOUT_MODE={options['layout_mode']}",
            "GPU_MEMORY_UTILIZATION=0.18",
            "MAX_MODEL_LEN=16384",
            "MAX_PIXELS=4194304",
            "PDF_TOOLS_WORKER_MAX_NUM=2",
        ]
    if BACKEND == "logics":
        args = [
            python,
            str(SOURCE / "inference_v3.py"),
            "--model_path",
            str(WEIGHTS),
            "--input_path",
            str(input_pdf),
            "--output_dir",
            str(output),
            "--window_size",
            str(options["window_size"]),
            "--max_model_len",
            str(options["max_model_len"]),
            "--gpu_memory_utilization",
            "0.18",
        ]
        if options["enforce_eager"]:
            args.append("--enforce_eager")
        return args
    return None


async def mineru_engine(job, directory):
    """Drive the resident MinerU api-server; returns the unpacked output dir."""
    output = directory / "output"
    input_pdf = directory / "input/input.pdf"
    headers = {}
    engine_key = os.environ.get("PARSER_MINERU_ENGINE_KEY", "")
    if engine_key:
        headers["Authorization"] = f"Bearer {engine_key}"
    async with httpx.AsyncClient(base_url=ENGINE_URL, timeout=60) as client:
        (await client.get("/v1/tiers", headers=headers)).raise_for_status()
        job_response = await client.post(
            "/v1/parse/jobs",
            headers=headers,
            json={
                "files": [{"source": {"type": "local", "path": str(input_pdf)}}],
                "tier": validate_options("mineru", job["mode"], json.loads(job["options"]))[
                    "quality_tier"
                ],
                "ocr_mode": {"auto": "auto", "text": "txt", "ocr": "ocr"}[
                    job["mode"]
                ],
                "output_formats": ["zip"],
            },
        )
        job_response.raise_for_status()
        engine_job = job_response.json()
        deadline = time.monotonic() + TIMEOUT
        try:
            while True:
                with database() as connection:
                    local_status = connection.execute(
                        "SELECT status FROM jobs WHERE id=?", (job["id"],)
                    ).fetchone()[0]
                if local_status == "cancelled":
                    raise asyncio.CancelledError()
                status_response = await client.get(
                    f"/v1/parse/jobs/{engine_job['job_id']}", headers=headers
                )
                status_response.raise_for_status()
                state = status_response.json()
                if state["status"] == "partial":
                    raise ValueError("MinerU engine returned partial document output")
                if state["status"] == "completed":
                    break
                if state["status"] in {"failed", "canceled"}:
                    raise RuntimeError(f"MinerU engine job {state['status']}")
                if time.monotonic() > deadline:
                    raise TimeoutError("MinerU engine job deadline exceeded")
                await asyncio.sleep(0.5)
            result_file = state["files"][0]["output_files"]["zip"]
            zip_response = await client.get(
                f"/v1/files/{result_file['file_id']}/content", headers=headers
            )
            zip_response.raise_for_status()
        except BaseException:
            # Cancellation must reach the resident engine, not only the wrapper DB.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(
                    asyncio.shield(
                        client.delete(f"/v1/parse/jobs/{engine_job['job_id']}", headers=headers)
                    ),
                    5,
                )
            raise
    (output / "output.zip").write_bytes(zip_response.content)
    safe_extract(output / "output.zip", output / "unpacked")
    return output / "unpacked"


def snapshot_revision(backend):
    snapshot = json.loads((WEIGHTS / "official-snapshot.json").read_text())
    files = snapshot.get("Data", {}).get("Files") or []
    if files:
        return files[0]["Revision"]
    return snapshot["sha"]


def validate_page_records(records, pages):
    """Counts alone cannot distinguish duplicate pages from complete coverage."""
    if not isinstance(records, list) or len(records) != pages:
        raise ValueError("Incomplete parser page coverage")
    ids = [record.get("page_idx") if isinstance(record, dict) else None for record in records]
    if (any(isinstance(index, bool) or not isinstance(index, int) for index in ids) or
            ids != list(range(pages))):
        raise ValueError("Parser page identities are missing, duplicated or out of order")
    return ids


def normalize(job, directory, assets=None):
    output = directory / "output"
    with pymupdf.open(directory / "input/input.pdf") as pdf:
        pages = len(pdf)
    if BACKEND == "mineru":
        markdown = (assets / "markdown.md").read_text()
        middle = json.loads((assets / "middle_json.json").read_text())
        page_ids = validate_page_records(middle.get("pages", middle.get("pdf_info", [])), pages)
        processed = len(page_ids)
        revision = json.loads((ROOT / "locks/mineru-models.json").read_text())["revision"]
    elif BACKEND == "teleocr":
        assets = output / "input"
        markdown = (assets / "input.md").read_text()
        middle = json.loads((assets / "input_middle.json").read_text())
        page_ids = validate_page_records(middle["pdf_info"], pages)
        processed = len(page_ids)
        revision = snapshot_revision(BACKEND)
    else:
        assets = output
        result = json.loads((output / "result.json").read_text())
        if result.get("complete_generation") is not True:
            raise ValueError("Logics generation truncated")
        page_ids = [page for window in result["windows"] for page in window["pages"]]
        if any(isinstance(index, bool) or not isinstance(index, int) for index in page_ids) or page_ids != list(range(pages)):
            raise ValueError("Logics windows do not cover the whole document in order")
        processed = len(page_ids)
        markdown = (output / "markdown.md").read_text()
        revision = snapshot_revision(BACKEND)
    version = (ROOT / f"locks/{BACKEND}.commit.txt").read_text().strip()
    preservation = None
    # Every accepted parser job is a PDF conversion, so all backends must keep
    # the source-visible placements the model may drop from its markdown.
    if (directory / "input/input.pdf").is_file():
        markdown, preservation = preserve_pdf_images(directory / "input/input.pdf", assets, markdown)
    artifact = directory / "artifact.zip"
    package_document(
        markdown,
        assets,
        artifact,
        backend=BACKEND,
        version=version,
        model_revision=revision,
        mode=job["mode"],
        options=json.loads(job["options"]),
        input_pages=pages,
        processed_pages=processed,
        page_ids=page_ids,
    )
    if preservation is not None:
        annotate_artifact(artifact, preservation)
    checked = directory / "validated"
    safe_extract(artifact, checked)
    validate_document(
        checked,
        backend=BACKEND,
        mode=job["mode"],
        options=json.loads(job["options"]),
        expected_pages=pages,
    )
    shutil.rmtree(checked)
    return artifact


def publish(job_id):
    """A cancelled job must never be promoted by a late model result."""
    with database() as connection:
        changed = connection.execute(
            "UPDATE jobs SET status='completed',updated=?,error='' WHERE id=? AND status='running'",
            (time.time(), job_id),
        ).rowcount
    if not changed:
        (JOBS / job_id / "artifact.zip").unlink(missing_ok=True)
        raise asyncio.CancelledError()
    (STATE / f"{BACKEND}.verified").write_text(job_id)
    shutil.rmtree(JOBS / job_id / "input", ignore_errors=True)
    shutil.rmtree(JOBS / job_id / "output", ignore_errors=True)


async def execute(job):
    directory = JOBS / job["id"]
    (directory / "output").mkdir(exist_ok=True)
    process = None
    try:
        environment = dict(os.environ)
        environment.update(
            {
                "TMPDIR": str(directory / "temporary"),
                "MINERU_HOME": str(ROOT / "mineru-home"),
                "MINERU_MODEL_SOURCE": "modelscope",
                "TOKENIZERS_PARALLELISM": "false",
            }
        )
        (directory / "temporary").mkdir(exist_ok=True)
        if command(job, directory) is None:
            # MinerU runs as a resident engine; drive it over HTTP instead of spawning a CLI.
            await asyncio.to_thread(normalize, job, directory, await mineru_engine(job, directory))
            publish(job["id"])
            return
        with (directory / "worker.log").open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                *command(job, directory),
                cwd=SOURCE,
                env=environment,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            started = time.monotonic()
            while process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), timeout=0.5)
                except TimeoutError:
                    pass
                with database() as connection:
                    status = connection.execute(
                        "SELECT status FROM jobs WHERE id=?", (job["id"],)
                    ).fetchone()[0]
                if status == "cancelled":
                    raise asyncio.CancelledError()
                if time.monotonic() - started > TIMEOUT:
                    raise TimeoutError("Model job deadline exceeded")
        if process.returncode != 0:
            raise RuntimeError(
                f"Model process exited {process.returncode}; inspect protected worker.log"
            )
        await asyncio.to_thread(normalize, job, directory)
        publish(job["id"])
    except asyncio.CancelledError:
        update(job["id"], "cancelled")
        shutil.rmtree(directory / "input", ignore_errors=True)
        (directory / "artifact.zip").unlink(missing_ok=True)
        raise
    except Exception as exc:
        with database() as connection:
            connection.execute(
                "UPDATE jobs SET status='failed',updated=?,error=? WHERE id=? AND status='running'",
                (time.time(), f"{type(exc).__name__}: {exc}"[:4000], job["id"]),
            )
    finally:
        if process and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 10)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        shutil.rmtree(directory / "temporary", ignore_errors=True)


async def work():
    # Acquiring the shared gate first prevents recovery from modifying a live peer.
    while True:
        STATE.mkdir(parents=True, exist_ok=True)
        with (STATE / "document.lock").open("a") as gate:
            try:
                fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with database() as connection:
                    connection.execute(
                        "UPDATE jobs SET status='failed',error='Worker interrupted; source retained for bounded retry',updated=? WHERE backend=? AND status='running'",
                        (time.time(), BACKEND),
                    )
                app.state.recovery_complete = True
                break
            except BlockingIOError:
                pass
        await asyncio.sleep(1)
    while True:
        claimed = claim(BACKEND) if health()["engine_ready"] else None
        if claimed:
            job, gate = claimed
            try:
                try:
                    await execute(job)
                except asyncio.CancelledError:
                    # A user cancelled one job; continue processing subsequent jobs.
                    if asyncio.current_task().cancelling():
                        raise
            finally:
                gate.close()
        # TTL cleanup is bounded to terminal jobs; queued/running inputs are protected.
        with database() as connection:
            expired = connection.execute(
                "SELECT id FROM jobs WHERE backend=? AND status IN ('completed','failed','cancelled') AND updated<?",
                (BACKEND, time.time() - TTL),
            ).fetchall()
            for row in expired:
                shutil.rmtree(JOBS / row["id"], ignore_errors=True)
                connection.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
        await asyncio.sleep(0.5)


async def engine_readiness():
    """Check the resident engine, without exposing auth, paths, or exceptions."""
    if BACKEND != "mineru":
        return True
    ready = False
    headers = {}
    key = os.environ.get("PARSER_MINERU_ENGINE_KEY", "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        async with httpx.AsyncClient(base_url=ENGINE_URL, timeout=2) as client:
            response = await client.get("/v1/tiers", headers=headers)
            response.raise_for_status()
            body = response.json()
            ready = isinstance(body, dict) and isinstance(body.get("data"), list) and any(
                isinstance(tier, dict) and tier.get("id") == "standard" and tier.get("current_model")
                for tier in body["data"]
            )
    except (httpx.HTTPError, ValueError, TypeError):
        pass
    app.state.engine_ready = bool(ready)
    app.state.engine_checked_at = time.monotonic()
    return bool(ready)


async def monitor_engine():
    while True:
        await engine_readiness()
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app):
    JOBS.mkdir(parents=True, exist_ok=True)
    app.state.recovery_complete = False
    app.state.engine_ready = False
    app.state.engine_checked_at = 0
    engine_task = asyncio.create_task(monitor_engine())
    app.state.engine_task = engine_task
    task = asyncio.create_task(work())
    app.state.worker_task = task
    try:
        yield
    finally:
        task.cancel()
        engine_task.cancel()
        for pending in (task, engine_task):
            with contextlib.suppress(asyncio.CancelledError):
                await pending


app = FastAPI(lifespan=lifespan)


def authenticate(request: Request):
    if not KEY_FILE.is_file():
        raise HTTPException(503, "Parser key not configured")
    expected = "Bearer " + KEY_FILE.read_text().strip()
    if not secrets.compare_digest(request.headers.get("Authorization", ""), expected):
        raise HTTPException(401, "Parser authentication required")


def lookup(job_id):
    if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
        raise HTTPException(404, "Job not found")
    with database() as connection:
        row = connection.execute(
            "SELECT * FROM jobs WHERE id=? AND backend=?", (job_id, BACKEND)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Job not found")
    return dict(row)


def health():
    ready = (ENV / "bin/python").is_file() and (
        ROOT / f"locks/{BACKEND}.requirements.txt"
    ).is_file()
    if BACKEND in {"logics", "teleocr"}:
        ready = ready and (WEIGHTS / "official-snapshot.json").is_file()
    else:
        ready = ready and (ROOT / "locks/mineru-models.json").is_file()
    task = getattr(app.state, "worker_task", None)
    if task is None or task.cancelled():
        worker_state = "stopped"
    elif task.done():
        # Consume the task exception without exposing model paths or credentials.
        worker_state = "failed" if task.exception() is not None else "stopped"
    else:
        worker_state = "running" if getattr(app.state, "recovery_complete", False) else "recovering"
    engine_ready = BACKEND != "mineru" or (
        getattr(app.state, "engine_ready", False) and
        time.monotonic() - getattr(app.state, "engine_checked_at", 0) < 5
    )
    return {
        "backend": BACKEND,
        "dependencies_ready": ready,
        "engine_ready": bool(engine_ready),
        "engine_state": ("reachable" if engine_ready else "unavailable") if BACKEND == "mineru" else "on_demand",
        "worker_state": worker_state,
        "ready_for_jobs": ready and engine_ready and worker_state == "running",
        "model_verified": (STATE / f"{BACKEND}.verified").exists(),
    }


@app.get("/health")
async def live_health():
    await engine_readiness()
    return health()


@app.get("/v1/capabilities", dependencies=[Depends(authenticate)])
async def capabilities():
    await engine_readiness()
    return {
        "artifact_version": 1,
        "backend": BACKEND,
        "modes": sorted(BACKEND_MODES[BACKEND]),
        "resume_granularity": "document",
        **health(),
    }


@app.post("/v1/jobs", status_code=202, dependencies=[Depends(authenticate)])
async def submit(
    file: UploadFile = File(...),
    backend: str = Form(...),
    mode: str = Form("auto"),
    options: str = Form("{}"),
):
    if backend != BACKEND:
        raise HTTPException(422, "Backend not served here")
    try:
        parsed = json.loads(options)
        if not isinstance(parsed, dict):
            raise ValueError("Options must be an object")
        parsed = validate_options(backend, mode, parsed)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    await engine_readiness()
    if not health()["ready_for_jobs"]:
        raise HTTPException(503, "Model dependencies or weights are not ready")
    job_id = uuid.uuid4().hex
    directory = JOBS / job_id
    (directory / "input").mkdir(parents=True)
    try:
        total = 0
        with (directory / "input/input.pdf").open("wb") as output:
            while data := await file.read(1024 * 1024):
                total += len(data)
                if total > MAX_UPLOAD:
                    raise HTTPException(413, "PDF upload exceeds limit")
                output.write(data)
        with pymupdf.open(directory / "input/input.pdf") as pdf:
            if pdf.needs_pass or len(pdf) < 1:
                raise HTTPException(422, "Encrypted or empty PDF")
        with database() as connection:
            now = time.time()
            connection.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)",
                (job_id, backend, mode, json.dumps(parsed), "queued", now, now, ""),
            )
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    finally:
        await file.close()
    return {"job_id": job_id, "status": "queued", "backend": backend}


@app.get("/v1/jobs/{job_id}", dependencies=[Depends(authenticate)])
def status(job_id: str):
    return {key: value for key, value in lookup(job_id).items() if key not in {"options"}}


@app.delete("/v1/jobs/{job_id}", dependencies=[Depends(authenticate)])
def cancel(job_id: str):
    lookup(job_id)
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        prior = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        changed = connection.execute(
            "UPDATE jobs SET status='cancelled',updated=? WHERE id=? AND status IN ('queued','running')",
            (time.time(), job_id),
        ).rowcount
    if changed and prior == "queued":
        shutil.rmtree(JOBS / job_id / "input", ignore_errors=True)
    return {"status": lookup(job_id)["status"]}


@app.get("/v1/jobs/{job_id}/artifact", dependencies=[Depends(authenticate)])
def artifact(job_id: str):
    if lookup(job_id)["status"] != "completed":
        raise HTTPException(409, "Artifact is not complete")
    return FileResponse(JOBS / job_id / "artifact.zip", media_type="application/zip")
