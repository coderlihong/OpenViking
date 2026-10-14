# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Content endpoints for OpenViking HTTP Server."""

import base64
import binascii
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import Response as FastAPIResponse
from pydantic import BaseModel, ConfigDict, model_validator

from openviking.core.directory_index import authored_index_collision
from openviking.core.namespace import (
    is_hidden_by_actor_peer_view,
    may_include_hidden_actor_peers,
    resolve_uri,
)
from openviking.core.path_variables import resolve_path_variables
from openviking.core.uri_validation import validate_request_viking_uri
from openviking.pyagfs.exceptions import AGFSClientError, AGFSNotFoundError
from openviking.resource.processing_mode import (
    DEFAULT_PROCESSING_MODE,
    WriteProcessingMode,
)
from openviking.server.auth import (
    get_request_context,
    require_role,
)
from openviking.server.dependencies import get_service
from openviking.server.error_mapping import map_exception
from openviking.server.identity import RequestContext, Role
from openviking.server.models import Response
from openviking.server.telemetry import run_operation
from openviking.storage.vector_ids import is_vector_record_id
from openviking.telemetry import TelemetryRequest
from openviking_cli.exceptions import InvalidArgumentError, NotFoundError, PermissionDeniedError
from openviking_cli.utils import get_logger

logger = get_logger(__name__)


class WriteContentRequest(BaseModel):
    """Request to write, append, or create text or base64-encoded binary content."""

    model_config = ConfigDict(extra="forbid")

    uri: str
    content: str | None = None
    content_base64: str | None = None
    mode: str = "replace"
    wait: bool = False
    timeout: float | None = None
    telemetry: TelemetryRequest = False
    processing_mode: WriteProcessingMode = DEFAULT_PROCESSING_MODE
    tags: list[str] | None = None
    tag_mode: Literal["replace", "append"] = "replace"


class BatchWritePrecondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["create_if_absent", "replace_if_hash"]
    base_hash: str | None = None

    @model_validator(mode="after")
    def validate_hash_shape(self) -> "BatchWritePrecondition":
        if self.kind == "replace_if_hash" and not self.base_hash:
            raise ValueError("base_hash is required for replace_if_hash")
        if self.kind == "create_if_absent" and self.base_hash is not None:
            raise ValueError("base_hash is not allowed for create_if_absent")
        return self


class BatchWriteOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str
    content: str | None = None
    content_base64: str | None = None
    mode: Literal["replace", "append", "create", "upsert"] = "replace"

    @model_validator(mode="after")
    def validate_content_shape(self) -> "BatchWriteOperation":
        if (self.content is None) == (self.content_base64 is None):
            raise ValueError("exactly one of content or content_base64 is required")
        return self


class BatchWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root_uri: str
    operations: list[BatchWriteOperation]
    wait: bool = True
    timeout: float | None = None
    telemetry: TelemetryRequest = False


class SetTagsRequest(BaseModel):
    """Request to set explicit k=v retrieval tags metadata for a file or directory."""

    model_config = ConfigDict(extra="forbid")

    uri: str
    tags: list[str]
    mode: str = "replace"
    recursive: bool = False
    telemetry: TelemetryRequest = False


class ReindexRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str
    mode: str = "vectors_only"
    wait: bool = True
    dry_run: bool = False
    recursive: bool = True
    tags: list[str] | None = None
    tag_mode: str = "replace"


class NormalizeCitationsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str
    recursive: bool = True


router = APIRouter(prefix="/api/v1/content", tags=["content"])


def _resolve_payload(uri: str, content: str | None, content_base64: str | None) -> str | bytes:
    """Return the write body, requiring exactly one of the two payload fields."""

    if content is not None:
        if content_base64 is not None:
            raise InvalidArgumentError("exactly one of content or content_base64 is required")
        return content
    if content_base64 is None:
        raise InvalidArgumentError("exactly one of content or content_base64 is required")
    try:
        return base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidArgumentError(f"content_base64 is invalid: {uri}") from exc


def _reserve_navigation_index(uri: str, content: str | bytes) -> None:
    """Refuse an authored body at ``{dir}/index.md``, the catalogue's name.

    The navigation index is what an agent greps to list a directory, so a chapter
    body that takes the name leaves that directory with no catalogue -- and the
    indexer will not overwrite prose to get one. Corpora uploaded file-by-file hit
    this because upstream docs use ``index.md`` for a section's landing page.
    Pointing at ``{dir}/{dir}.md`` gives the caller the name the parser itself
    produces for a branch chapter.
    """

    canonical = authored_index_collision(uri, content)
    if canonical is None:
        return
    raise InvalidArgumentError(
        f"{uri} is reserved for the generated directory catalogue. "
        f"Write this chapter body to {canonical} instead.",
        details={"uri": uri, "write_to": canonical},
    )


def _authorize_reindex_uri(uri: str, ctx: RequestContext) -> str:
    """Allow users to reindex only their own private namespace."""
    if ctx.role != Role.USER:
        return uri

    target = resolve_uri(uri)
    if (
        target.scope != "user"
        or target.owner_user_id != ctx.user.user_id
        or is_hidden_by_actor_peer_view(uri, ctx)
        or may_include_hidden_actor_peers(uri, ctx)
    ):
        raise PermissionDeniedError(
            "USER can only reindex their own user namespace.",
            resource=uri,
        )
    return uri


@router.get("/read")
async def read(
    uri: str = Query(..., description="Viking URI"),
    offset: int = Query(0, description="Starting line number (0-indexed)"),
    limit: int = Query(-1, description="Number of lines to read, -1 means read to end"),
    raw: bool = Query(False, description="Return raw stored content without memory-field cleanup"),
    _ctx: RequestContext = Depends(get_request_context),
):
    """Read file content (L2)."""
    service = get_service()
    # If the argument is a raw 32-hex vector record id, skip URI validation
    # (id lookup + access check happens inside VikingFS.read_file).
    if is_vector_record_id(uri):
        resolved = uri
    else:
        resolved = validate_request_viking_uri(resolve_path_variables(uri), _ctx)
    try:
        if raw:
            result = await service.fs.read(resolved, ctx=_ctx, offset=offset, limit=limit)
        else:
            result = await service.fs.read_visible(resolved, ctx=_ctx, offset=offset, limit=limit)
    except AGFSNotFoundError:
        raise NotFoundError(uri, "file")
    except AGFSClientError as e:
        mapped = map_exception(e, resource=uri, resource_type="file")
        if mapped is not None:
            raise mapped from e
        raise

    return Response(status="ok", result=result)


@router.get("/abstract")
async def abstract(
    uri: str = Query(..., description="Viking URI"),
    _ctx: RequestContext = Depends(get_request_context),
):
    """Read L0 abstract."""
    service = get_service()
    uri = validate_request_viking_uri(resolve_path_variables(uri), _ctx)
    try:
        result = await service.fs.abstract(uri, ctx=_ctx)
    except AGFSNotFoundError:
        raise NotFoundError(uri, "file")
    except AGFSClientError as e:
        mapped = map_exception(e, resource=uri, resource_type="file")
        if mapped is not None:
            raise mapped from e
        raise
    return Response(status="ok", result=result)


@router.get("/overview")
async def overview(
    uri: str = Query(..., description="Viking URI"),
    _ctx: RequestContext = Depends(get_request_context),
):
    """Read L1 overview."""
    service = get_service()
    uri = validate_request_viking_uri(resolve_path_variables(uri), _ctx)
    try:
        result = await service.fs.overview(uri, ctx=_ctx)
    except AGFSNotFoundError:
        raise NotFoundError(uri, "file")
    except AGFSClientError as e:
        mapped = map_exception(e, resource=uri, resource_type="file")
        if mapped is not None:
            raise mapped from e
        raise
    return Response(status="ok", result=result)


@router.get("/download")
async def download(
    uri: str = Query(..., description="Viking URI"),
    _ctx: RequestContext = Depends(get_request_context),
):
    """Download file as raw bytes (for images, binaries, etc.)."""
    service = get_service()
    uri = validate_request_viking_uri(resolve_path_variables(uri), _ctx)
    try:
        content = await service.fs.read_file_bytes(uri, ctx=_ctx)
    except AGFSNotFoundError:
        raise NotFoundError(uri, "file")
    except AGFSClientError as e:
        mapped = map_exception(e, resource=uri, resource_type="file")
        if mapped is not None:
            raise mapped from e
        raise

    # Try to get filename from stat
    filename = "download"
    try:
        stat = await service.fs.stat(uri, ctx=_ctx, skip_count=True)
        if stat and "name" in stat:
            filename = stat["name"]
    except Exception:
        pass
    filename = quote(filename)
    return FastAPIResponse(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@router.post("/write")
async def write(
    request: WriteContentRequest = Body(...),
    _ctx: RequestContext = Depends(get_request_context),
):
    """Write text or base64-encoded binary content to a file and refresh semantics/vectors."""
    uri = validate_request_viking_uri(resolve_path_variables(request.uri), _ctx)
    content = _resolve_payload(uri, request.content, request.content_base64)
    _reserve_navigation_index(uri, content)
    service = get_service()
    execution = await run_operation(
        operation="content.write",
        telemetry=request.telemetry,
        fn=lambda: service.fs.write(
            uri=uri,
            content=content,
            ctx=_ctx,
            mode=request.mode,
            wait=request.wait,
            timeout=request.timeout,
            processing_mode=request.processing_mode,
            tags=request.tags,
            tag_mode=request.tag_mode,
        ),
    )
    return Response(
        status="ok",
        result=execution.result,
        telemetry=execution.telemetry,
    ).model_dump(exclude_none=True)


@router.post("/batch-write")
async def batch_write(
    request: BatchWriteRequest = Body(...),
    _ctx: RequestContext = Depends(get_request_context),
):
    """Apply file writes and refresh their indexes once after the batch is written."""
    service = get_service()
    root_uri = validate_request_viking_uri(resolve_path_variables(request.root_uri), _ctx)
    operations = [operation.model_dump(exclude_none=True) for operation in request.operations]
    for operation in operations:
        uri = validate_request_viking_uri(resolve_path_variables(operation["uri"]), _ctx)
        operation["uri"] = uri
        payload = _resolve_payload(uri, operation.get("content"), operation.get("content_base64"))
        _reserve_navigation_index(uri, payload)
    execution = await run_operation(
        operation="content.batch_write",
        telemetry=request.telemetry,
        fn=lambda: service.fs.batch_write(
            root_uri=root_uri,
            operations=operations,
            ctx=_ctx,
            wait=request.wait,
            timeout=request.timeout,
        ),
    )
    return Response(
        status="ok",
        result=execution.result,
        telemetry=execution.telemetry,
    ).model_dump(exclude_none=True)


@router.post("/set_tags")
async def set_tags(
    request: SetTagsRequest = Body(...),
    _ctx: RequestContext = Depends(get_request_context),
):
    """Set explicit k=v retrieval tags metadata for a file or directory."""
    service = get_service()
    uri = validate_request_viking_uri(resolve_path_variables(request.uri), _ctx)
    execution = await run_operation(
        operation="content.set_tags",
        telemetry=request.telemetry,
        fn=lambda: service.fs.set_tags(
            uri=uri,
            tags=request.tags,
            mode=request.mode,
            recursive=request.recursive,
            ctx=_ctx,
        ),
    )
    return Response(
        status="ok",
        result=execution.result,
        telemetry=execution.telemetry,
    ).model_dump(exclude_none=True)


@router.post("/reindex")
async def reindex(
    body: ReindexRequest = Body(...),
    ctx: RequestContext = require_role(Role.ROOT, Role.ADMIN, Role.USER),
):
    """Reindex semantic/vector artifacts for a URI-scoped maintenance target."""
    if body.dry_run and body.mode != "prune_orphans":
        raise InvalidArgumentError("dry_run is only supported for prune_orphans reindex mode.")
    uri = validate_request_viking_uri(resolve_path_variables(body.uri), ctx)
    uri = _authorize_reindex_uri(uri, ctx)
    service = get_service()
    reindex_kwargs = {
        "uri": uri,
        "mode": body.mode,
        "wait": body.wait,
        "dry_run": body.dry_run,
        "ctx": ctx,
    }
    if not body.recursive:
        reindex_kwargs["recursive"] = False
    if body.tags is not None:
        reindex_kwargs["tags"] = body.tags
        reindex_kwargs["tag_mode"] = body.tag_mode
    result = await service.reindex(
        **reindex_kwargs,
    )
    return Response(status="ok", result=result)


@router.post("/normalize_citations")
async def normalize_citations(
    body: NormalizeCitationsRequest = Body(...),
    ctx: RequestContext = require_role(Role.ROOT, Role.ADMIN, Role.USER),
):
    """Complete figure citations (vikingsrc pointers + figcaption bodies).

    Runs BEFORE reindex: the vector phase archives the published markdown, so
    captions must already be in it. Accepts a single markdown URI or a
    directory URI walked recursively.
    """
    uri = validate_request_viking_uri(resolve_path_variables(body.uri), ctx)
    uri = _authorize_reindex_uri(uri, ctx)
    from openviking.indexing.citation_stage import normalize_citations

    result = await normalize_citations(uri, ctx=ctx, recursive=body.recursive)
    return Response(status="ok", result=result)
