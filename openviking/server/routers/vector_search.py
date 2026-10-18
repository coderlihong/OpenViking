# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Semantic retrieval endpoint (vector + hybrid fusion), decoupled from search."""

import asyncio
import math
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from openviking.retrieve.context_assembler import DEFAULT_LIMIT
from openviking.server.auth import get_request_context
from openviking.server.dependencies import get_service
from openviking.server.identity import RequestContext
from openviking.server.models import Response
from openviking.server.search_request import (
    TimeField,
    filter_hits_by_score,
    resolve_image_url,
    resolve_search_filter,
    resolve_uri_or_uris,
)
from openviking.server.telemetry import run_operation
from openviking.telemetry import TelemetryRequest
from openviking.utils.search_filters import _resolve_levels
from openviking_cli.utils.config.concurrency_config import concurrency_config

router = APIRouter(prefix="/api/v1/vector", tags=["vector"])


def _sanitize_floats(obj: Any) -> Any:
    """Recursively replace inf/nan with 0.0 to ensure JSON compliance."""
    if isinstance(obj, float):
        if math.isinf(obj) or math.isnan(obj):
            return 0.0
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_floats(v) for v in obj]
    return obj


async def _inline_read_content(
    result: Any,
    *,
    service: Any,
    ctx: RequestContext,
) -> Any:
    """Attach visible file content to ranked hits when it can be read."""
    if not isinstance(result, dict):
        return result

    hits = [
        hit
        for category in ("memories", "resources", "skills")
        for hit in result.get(category, [])
        if isinstance(hit, dict) and isinstance(hit.get("uri"), str)
    ]
    semaphore = asyncio.Semaphore(concurrency_config().max_request_concurrency)

    async def _read(hit: Dict[str, Any]) -> None:
        async with semaphore:
            try:
                hit["content"] = await service.fs.read_visible(hit["uri"], ctx=ctx)
            except Exception:
                pass

    await asyncio.gather(*(_read(hit) for hit in hits))
    return result


class VectorSearchRequest(BaseModel):
    """Request model for semantic search without session context.

    Mirrors the ``find`` recall surface; session intent, query expansion and
    context assembly are deliberately absent.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = ""
    image_url: Optional[str] = None
    target_uri: Union[str, List[str]] = ""
    context_type: Optional[Union[str, List[str]]] = None
    limit: int = DEFAULT_LIMIT
    score_threshold: Optional[float] = None
    filter: Optional[Dict[str, Any]] = None
    meta_filters: Optional[List[Dict[str, Any]]] = Field(default=None, max_length=16)
    meta_hints: Optional[List[Dict[str, Any]]] = Field(default=None, max_length=16)
    tags: Optional[List[str]] = None
    since: Optional[str] = None
    until: Optional[str] = None
    time_field: Optional[TimeField] = None
    level: Optional[Union[int, str, List[int]]] = None
    include_provenance: bool = False
    read_content: bool = False
    rerank: bool = True
    telemetry: TelemetryRequest = False


@router.post("/search")
async def vector_search(
    request: VectorSearchRequest,
    _ctx: RequestContext = Depends(get_request_context),
):
    """Semantic search over the vector + BM25 hybrid fusion recall."""
    service = get_service()
    effective_filter = resolve_search_filter(
        request.filter,
        request.context_type,
        request.since,
        request.until,
        request.time_field,
        request.tags,
    )
    resolved_target_uri = resolve_uri_or_uris(request.target_uri, _ctx)
    resolved_image_url = resolve_image_url(request.image_url, _ctx)
    execution = await run_operation(
        operation="vector.search",
        telemetry=request.telemetry,
        fn=lambda: service.search.find(
            query=request.query,
            ctx=_ctx,
            target_uri=resolved_target_uri,
            limit=request.limit,
            score_threshold=request.score_threshold,
            filter=effective_filter,
            **(
                {"meta_filters": request.meta_filters, "meta_hints": request.meta_hints}
                if request.meta_filters or request.meta_hints
                else {}
            ),
            level=_resolve_levels(request.level) or None,
            image_url=resolved_image_url,
            rerank_enabled=request.rerank,
        ),
    )
    result = execution.result
    if hasattr(result, "to_dict"):
        result = result.to_dict(include_provenance=request.include_provenance)
    if isinstance(result, dict) and request.score_threshold is not None:
        total = 0
        for category in ("memories", "resources", "skills"):
            kept = filter_hits_by_score(result.get(category), request.score_threshold)
            result[category] = kept
            total += len(kept)
        result["total"] = total
    if request.read_content:
        result = await _inline_read_content(result, service=service, ctx=_ctx)
    result = _sanitize_floats(result)
    return Response(
        status="ok",
        result=result,
        telemetry=execution.telemetry,
    ).model_dump(exclude_none=True)
