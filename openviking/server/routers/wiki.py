# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Experimental Wiki configuration and immutable source preparation endpoints."""
import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from openviking.indexing.publication import configured_store
from openviking.server.auth import get_request_context
from openviking.server.dependencies import get_service
from openviking.server.identity import RequestContext
from openviking.server.models import Response
from openviking.server.temp_upload_store import TempUploadStore
from openviking.wiki.build_plan import WikiBuildPlanner
from openviking.wiki.catalog import WikiCatalog
from openviking.wiki.schema import SchemaBundle, SchemaError
from openviking.service.wiki_job_service import WikiJobService
from openviking.server.routers.kb_jobs import job_status
from openviking_cli.exceptions import FailedPreconditionError, InvalidArgumentError, ProcessingError

router = APIRouter(prefix='/api/v1/kb/wiki', tags=['wiki-experimental'])


class SchemaRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_id: StrictStr = Field(min_length=1, max_length=128)
    version: StrictStr = Field(min_length=1, max_length=128)
    files: dict[StrictStr, StrictStr] = Field(min_length=1, max_length=256)


class CollectionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    collection_id: StrictStr
    namespace_id: StrictStr
    target_uri: StrictStr
    raw_sources_prefix: StrictStr
    schema_id: StrictStr
    schema_version: StrictStr


class CollectionDomainMigrationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_binding_digest: StrictStr = Field(pattern=r'^[0-9a-f]{64}$')


class BuildRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_uris: list[StrictStr] = Field(min_length=1, max_length=2048)
    collection_id: StrictStr
    recursive: StrictBool = True
    idempotency_key: StrictStr = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.+\-]+$')


class IngestionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    temp_file_id: StrictStr = Field(min_length=1, max_length=256)
    collection_id: StrictStr
    update_policy: Literal['fail', 'replace'] = 'fail'
    idempotency_key: StrictStr = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.+\-]+$')


class ExecuteBuildRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_plan_digest: StrictStr = Field(pattern=r'^[0-9a-f]{64}$')
    idempotency_key: StrictStr = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.+\-]+$')
    profile: Literal['draft', 'strict_diagnostic'] = 'draft'


class MergeReference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sha256: StrictStr = Field(pattern=r'^[0-9a-f]{64}$')
    size: int = Field(ge=0)


class PublishMergeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reference: MergeReference
    revision: StrictStr = Field(pattern=r'^[0-9a-f]{64}$')


def get_catalog():
    fs = get_service().viking_fs
    registry = configured_store()
    if fs is None or registry is None:
        raise FailedPreconditionError('Wiki configuration requires initialized VikingFS and embedding.chunking.publication_path')
    return WikiCatalog(fs, registry=registry)


def get_planner():
    fs = get_service().viking_fs
    registry = configured_store()
    if fs is None or registry is None:
        raise FailedPreconditionError('Wiki build requires initialized VikingFS and embedding.chunking.publication_path')
    return WikiBuildPlanner(fs, registry=registry)


def get_temp_upload_store(request: Request) -> TempUploadStore:
    return TempUploadStore.build(request.app.state.config)


def get_wiki_jobs(planner: WikiBuildPlanner = Depends(get_planner)):
    return WikiJobService(planner)


def get_merger(planner: WikiBuildPlanner = Depends(get_planner)):
    from openviking.wiki.extraction import WikiDraftExtractor
    from openviking.wiki.merge import PrivateWikiMerger
    return PrivateWikiMerger(WikiDraftExtractor(planner))


@router.post('/merges/publish')
async def publish_private_merge(body: PublishMergeRequest,
                                ctx: RequestContext = Depends(get_request_context),
                                merger=Depends(get_merger)):
    # The referenced blob's own digest is its true revision; the caller-named
    # revision carries the CAS claim and maps to 409 on mismatch, never 400.
    receipt = {'reference': body.reference.model_dump(), 'revision': body.reference.sha256,
               'state': 'private_merge_ready', 'private': True}
    try:
        result = await merger.publish(ctx, receipt, expected_revision=body.revision)
    except SchemaError as exc:
        raise InvalidArgumentError(str(exc)) from exc
    return Response(status='ok', result=result)


@router.post('/builds/{build_id}/execute', status_code=202)
async def execute_build(build_id: str, body: ExecuteBuildRequest,
                        ctx: RequestContext = Depends(get_request_context),
                        jobs: WikiJobService = Depends(get_wiki_jobs)):
    try:
        job = await jobs.create(ctx, build_id, expected_plan_digest=body.expected_plan_digest,
                                idempotency_key=body.idempotency_key, profile=body.profile)
    except SchemaError as exc:
        raise InvalidArgumentError(str(exc)) from exc
    return Response(status='ok', result=job_status(job))


@router.get('/jobs/{job_id}/drafts/{draft_index}')
async def read_private_draft(job_id: str, draft_index: int,
                             ctx: RequestContext = Depends(get_request_context),
                             jobs: WikiJobService = Depends(get_wiki_jobs)):
    try:
        result = await jobs.read_draft(ctx, job_id, draft_index)
    except SchemaError as exc:
        raise InvalidArgumentError(str(exc)) from exc
    return Response(status='ok', result=result)


@router.post('/schemas')
async def register_schema(body: SchemaRequest, ctx: RequestContext = Depends(get_request_context), catalog: WikiCatalog = Depends(get_catalog)):
    catalog._admin(ctx)
    try:
        bundle = SchemaBundle.freeze(body.schema_id, body.version, body.files)
        result = await catalog.register_schema(ctx, bundle)
    except SchemaError as exc:
        raise InvalidArgumentError(str(exc)) from exc
    return Response(status='ok', result=result)


@router.get('/schemas/{schema_id}/{version}')
async def get_schema(schema_id: str, version: str, ctx: RequestContext = Depends(get_request_context), catalog: WikiCatalog = Depends(get_catalog)):
    try:
        result = await catalog.describe_schema(ctx, schema_id, version)
    except SchemaError as exc:
        raise InvalidArgumentError(str(exc)) from exc
    return Response(status='ok', result=result)


@router.post('/collections')
async def register_collection(body: CollectionRequest, ctx: RequestContext = Depends(get_request_context), catalog: WikiCatalog = Depends(get_catalog)):
    try:
        binding = await catalog.register_collection(ctx, **body.model_dump())
    except SchemaError as exc:
        raise InvalidArgumentError(str(exc)) from exc
    return Response(status='ok', result=asdict(binding))


@router.get('/collections/{collection_id}')
async def get_collection(collection_id: str, ctx: RequestContext = Depends(get_request_context), catalog: WikiCatalog = Depends(get_catalog)):
    try:
        binding = await catalog.get_collection(ctx, collection_id)
    except SchemaError as exc:
        raise InvalidArgumentError(str(exc)) from exc
    result = asdict(binding)
    _, result['capability_state'] = await catalog._read_schema(ctx, binding.schema_id, binding.schema_version)
    await catalog.fs._ensure_access(binding.target_uri, ctx)
    return Response(status='ok', result=result)


@router.get('/collections/{collection_id}/migration-inspection')
async def inspect_collection_migration(collection_id: str, ctx: RequestContext = Depends(get_request_context),
                                       catalog: WikiCatalog = Depends(get_catalog)):
    try:
        result = await catalog.inspect_collection_migration(ctx, collection_id)
    except SchemaError as exc:
        raise InvalidArgumentError(str(exc)) from exc
    return Response(status='ok', result=result)


@router.post('/collections/{collection_id}/migrate-domains')
async def migrate_collection_domains(collection_id: str, body: CollectionDomainMigrationRequest,
                                     ctx: RequestContext = Depends(get_request_context), catalog: WikiCatalog = Depends(get_catalog)):
    try:
        binding = await catalog.migrate_collection(ctx, collection_id,
                                                  expected_binding_digest=body.expected_binding_digest)
    except SchemaError as exc:
        raise InvalidArgumentError(str(exc)) from exc
    return Response(status='ok', result=asdict(binding))


@router.post('/builds')
async def prepare_build(body: BuildRequest, ctx: RequestContext = Depends(get_request_context), planner: WikiBuildPlanner = Depends(get_planner)):
    try:
        plan = await planner.prepare_sources(
            ctx, body.collection_id, body.source_uris,
            input_kind='uri', idempotency_key=body.idempotency_key, recursive=body.recursive)
    except SchemaError as exc:
        raise InvalidArgumentError(str(exc)) from exc
    return Response(status='ok', result=asdict(plan))


@router.post('/ingestions')
async def prepare_ingestion(
    body: IngestionRequest,
    ctx: RequestContext = Depends(get_request_context),
    planner: WikiBuildPlanner = Depends(get_planner),
    store: TempUploadStore = Depends(get_temp_upload_store),
):
    resolved = await store.resolve_for_consume(body.temp_file_id, ctx)
    try:
        filename = resolved.original_filename
        if not filename:
            raise InvalidArgumentError('Uploaded Wiki source requires an original file name')
        suffix = Path(filename).suffix.lower()
        if suffix == '.md':
            # Read at most one byte beyond the capture limit, even if the temporary
            # file grows after a stat. A rejected upload must never be materialized.
            content = await asyncio.to_thread(
                _read_bounded_upload, resolved.local_path, planner.limits.document_bytes)
            try:
                plan = await planner.prepare_upload(
                    ctx, body.collection_id, filename, content,
                    update_policy=body.update_policy, idempotency_key=body.idempotency_key)
            except SchemaError as exc:
                raise InvalidArgumentError(str(exc)) from exc
        elif suffix == '.pdf' or suffix in {'.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx'}:
            source_hash, source_size = await asyncio.to_thread(_hash_upload, resolved.local_path)
            try:
                plan = await planner.prepare_materialized_upload(
                    ctx, body.collection_id, filename,
                    materialize=_document_materializer(resolved, ctx),
                    idempotency_key=body.idempotency_key,
                    source_sha256=source_hash, source_size=source_size,
                    update_policy=body.update_policy)
            except SchemaError as exc:
                raise InvalidArgumentError(str(exc)) from exc
        else:
            raise InvalidArgumentError('Wiki upload must be Markdown or a PDF/Office document')
    finally:
        await resolved.cleanup()
    return Response(status='ok', result=asdict(plan))


def _document_materializer(resolved, ctx):
    """W1 PDF/Office stage: run the shared document parser under ``root_uri``."""
    async def materialize(root_uri: str):
        result = await get_service().resources._execute_resource_ingestion(
            path=resolved.local_path, ctx=ctx, defer_post_processing=False,
            to=root_uri, to_is_directory=True, build_index=False, summarize=False,
            manage_watch=False, allow_local_path_resolution=True,
            source_name=resolved.original_filename, internal_task=True)
        if result.get('status') == 'error' or not result.get('root_uri'):
            raise ProcessingError('Resource parser/materializer did not complete')
    return materialize


def _read_bounded_upload(path: str, limit: int) -> bytes:
    with Path(path).open('rb') as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise InvalidArgumentError('Wiki upload document size limit exceeded')
    return content


def _hash_upload(path: str) -> tuple[str, int]:
    """Bind the parser input to a fixed identity without loading a PDF into RAM."""
    import hashlib

    digest, size = hashlib.sha256(), 0
    with Path(path).open('rb') as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
            size += len(block)
    if not size:
        raise InvalidArgumentError('Wiki upload document is empty')
    return digest.hexdigest(), size


@router.get('/builds/{build_id}')
async def get_build(build_id: str, ctx: RequestContext = Depends(get_request_context),
                    planner: WikiBuildPlanner = Depends(get_planner)):
    """Read a stored preparation plan under current collection/source ACLs.

    This is not a job status endpoint or an export authorization receipt.
    """
    try:
        plan = await planner.get(ctx, build_id)
    except SchemaError as exc:
        raise InvalidArgumentError(str(exc)) from exc
    return Response(status='ok', result=asdict(plan))
