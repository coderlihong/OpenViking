"""Durable KB job submission, current-identity reads, queue listing and cancellation."""
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from openviking.indexing.publication import configured_store
from openviking.server.auth import get_request_context
from openviking.server.dependencies import get_service
from openviking.server.identity import RequestContext
from openviking.server.models import Response
from openviking.service.kb_job_service import KBJobService
from openviking_cli.exceptions import FailedPreconditionError

router = APIRouter(prefix='/api/v1/kb', tags=['knowledge-durable-jobs'])


class JobPayload(BaseModel):
    model_config = ConfigDict(extra='forbid')
    temp_file_id: StrictStr | None = Field(default=None, min_length=1, max_length=256)
    uri: StrictStr | None = Field(default=None, min_length=1, max_length=4096)
    to: StrictStr | None = None
    parent: StrictStr | None = None
    mode: Literal['vectors_only', 'semantic_and_vectors'] = 'semantic_and_vectors'
    recursive: StrictBool = True
    tags: list[StrictStr] | None = None
    tag_mode: Literal['replace', 'append'] = 'replace'


class JobOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    structure_policy: Literal['bookmarks_preferred'] = 'bookmarks_preferred'
    parse_backend: Literal['local', 'mineru', 'auto', 'teleocr', 'logics'] = 'auto'
    model_mode: Literal['auto', 'text', 'ocr', 'layout'] = 'auto'
    backend_options: dict = Field(default_factory=dict)
    section_target_tokens: StrictInt = Field(default=1024, ge=1, le=65536)
    profile: Literal['document'] = 'document'


class JobRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    namespace_id: StrictStr = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.+\-]+$')
    kind: Literal['D1', 'D2']
    idempotency_key: StrictStr = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.+\-]+$')
    payload: JobPayload
    options: JobOptions = Field(default_factory=JobOptions)

    @model_validator(mode='after')
    def validate_entry(self):
        if self.payload.to is not None and self.payload.parent is not None:
            raise ValueError('to and parent are mutually exclusive')
        if self.kind == 'D1':
            if self.payload.temp_file_id is None or self.payload.uri is not None:
                raise ValueError('D1 requires only an upload temp_file_id input')
        elif (self.payload.uri is None or self.payload.temp_file_id is not None
              or self.payload.to is not None or self.payload.parent is not None):
            raise ValueError('D2 requires only a URI indexing input')
        return self


def get_job_service(request: Request):
    fs, repository = get_service().viking_fs, configured_store()
    if fs is None or repository is None:
        raise FailedPreconditionError('Durable jobs require initialized VikingFS and publication authority')
    return KBJobService(fs, repository, max_source_uris=request.app.state.config.wiki.documents)


# Waiting states are not yet claimed by a worker; they hold a real queue position.
_WAITING = {'queued', 'backoff'}
# Only an explicit authority-verified state may be reported as ready. A written
# artifact is persisted but not yet indexed, and an empty queue is never ready.
_PHASE = {
    'queued': 'queued',
    'backoff': 'retry',
    'written': 'written',
    'failed': 'failed',
    'cancelled': 'cancelled',
    'draft_ready': 'draft_ready',
    'ready': 'ready',
    'write_recovery_required': 'write_recovery_required',
}


def _phase(state, stage):
    """Map durable state/stage onto the UI queue vocabulary.

    ``running`` splits into parsing (validate/parse_write) and indexing (index
    and later). No percentage is invented: progress stays unknown unless the
    checkpoint carries an explicit processed/total pair.
    """
    if state == 'running':
        indexing = {'index', 'wiki_extract', 'wiki_validate', 'publish_gate'}
        return 'indexing' if stage in indexing else 'parsing'
    return _PHASE.get(state, 'unknown')


def job_status(job, *, queue_position=0, queued_total=0):
    """Project a durable job row into the F3 status contract for the UI/SDK.

    The raw durable fields are preserved; the derived fields are additive so the
    existing POST 202 / GET contract keeps its shape.
    """
    checkpoint = job.get('checkpoint') or {}
    info = job.get('info') or {}
    state, stage = job['state'], job['stage']
    processed, total = checkpoint.get('processed_units'), checkpoint.get('total_units')
    known = type(processed) is int and type(total) is int and 0 <= processed <= total and total > 0
    alignment = checkpoint.get('alignment_status') or info.get('alignment_status')
    structure = checkpoint.get('structure_source') or info.get('structure_source')
    fallback = info.get('fallback_strategy') or checkpoint.get('fallback_strategy')
    return {
        **job,
        'task_id': job['job_id'],
        'namespace_id': job['namespace'],
        'document_id': (job['payload'].get('document_id') or job['payload'].get('uri')
                        or job['payload'].get('temp_file_id')),
        'revision': checkpoint.get('revision'),
        'phase': _phase(state, stage),
        'queue_position': queue_position,
        'queued_total': queued_total,
        'queue_position_scope': 'visible_window',
        'processed_units': processed if known else None,
        'total_units': total if known else None,
        'progress_known': known,
        'retryable': state == 'backoff',
        'resume_granularity': checkpoint.get('resume_granularity'),
        'alignment_status': alignment,
        'structure_source': structure,
        'fallback_strategy': fallback,
        'artifact_uri': checkpoint.get('artifact_uri') or checkpoint.get('candidate_uri'),
        'trace_id': job.get('trace_id') or checkpoint.get('trace_id') or info.get('trace_id'),
        'error': info.get('message') if state in {'failed', 'write_recovery_required'} else None,
        'reason_code': info.get('code'),
        'warnings': info.get('warnings') or [],
        'heartbeat': None,
        'lease_expires_at': job.get('lease_until') if state == 'running' else None,
    }


def _queue_positions(jobs):
    """FIFO position per namespace over the waiting jobs, ordered by creation."""
    waiting = {}
    for job in jobs:
        if job['state'] in _WAITING:
            waiting.setdefault(job['namespace'], []).append(job)
    positions, totals = {}, {}
    for namespace, rows in waiting.items():
        rows.sort(key=lambda row: (row['created'], row['job_id']))
        totals[namespace] = len(rows)
        for index, row in enumerate(rows):
            positions[row['job_id']] = index + 1
    return positions, totals


@router.post('/ingestion-jobs', status_code=202)
async def create_job(body: JobRequest, ctx: RequestContext = Depends(get_request_context),
                     jobs: KBJobService = Depends(get_job_service)):
    job = await jobs.create(ctx, namespace_id=body.namespace_id, kind=body.kind,
                            idempotency_key=body.idempotency_key,
                            payload=body.payload.model_dump(exclude_none=True),
                            effective_config=body.options.model_dump())
    peers = await _authorized_jobs(ctx, jobs, namespace_id=job['namespace'], limit=1000)
    positions, totals = _queue_positions(peers)
    return Response(status='ok', result=job_status(
        job, queue_position=positions.get(job['job_id'], 0),
        queued_total=totals.get(job['namespace'], 0)))


async def _authorized_jobs(ctx, jobs, namespace_id=None, limit=100):
    """List the original user's jobs with current resource authorization."""
    return await jobs.list(ctx, namespace_id=namespace_id, limit=limit)


@router.get('/jobs')
async def list_jobs(namespace_id: str | None = Query(default=None, min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.+\-]+$'),
                    limit: int = Query(default=100, ge=1, le=1000),
                    ctx: RequestContext = Depends(get_request_context),
                    jobs: KBJobService = Depends(get_job_service)):
    """List durable jobs with durable queue positions for the tasks/monitoring UI."""
    rows = await _authorized_jobs(ctx, jobs, namespace_id=namespace_id, limit=limit)
    positions, totals = _queue_positions(rows)
    result = [job_status(job, queue_position=positions.get(job['job_id'], 0),
                         queued_total=totals.get(job['namespace'], 0)) for job in rows]
    return Response(status='ok', result=result)


@router.get('/jobs/{job_id}')
async def get_job(job_id: str, ctx: RequestContext = Depends(get_request_context),
                  jobs: KBJobService = Depends(get_job_service)):
    job = await jobs.get(ctx, job_id)
    peers = await _authorized_jobs(ctx, jobs, namespace_id=job['namespace'], limit=1000)
    positions, totals = _queue_positions(peers)
    return Response(status='ok', result=job_status(
        job, queue_position=positions.get(job_id, 0), queued_total=totals.get(job['namespace'], 0)))


@router.post('/jobs/{job_id}/cancel')
async def cancel_job(job_id: str, ctx: RequestContext = Depends(get_request_context),
                     jobs: KBJobService = Depends(get_job_service)):
    job = await jobs.cancel(ctx, job_id)
    return Response(status='ok', result=job_status(job))


@router.post('/jobs/{job_id}/parse-quarantine/resolve')
async def resolve_parse_quarantine(job_id: str, ctx: RequestContext = Depends(get_request_context),
                                   jobs: KBJobService = Depends(get_job_service)):
    """Operator release of a quarantined parse slot; refuses while source writes are pending."""
    job = await jobs.resolve_parse_quarantine(ctx, job_id)
    return Response(status='ok', result=job_status(job))
