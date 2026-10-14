# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Catalogue keyword-search endpoint, decoupled from the search surface."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from openviking.retrieve.index_navigation import search_navigation_indexes
from openviking.server.auth import get_request_context
from openviking.server.dependencies import get_service
from openviking.server.identity import RequestContext
from openviking.server.models import Response
from openviking.server.search_request import resolve_uri_or_uris
from openviking.server.telemetry import run_operation
from openviking.telemetry import TelemetryRequest

router = APIRouter(prefix="/api/v1/index", tags=["index"])


class IndexSearchRequest(BaseModel):
    """Request model for catalogue keyword search."""

    model_config = ConfigDict(extra="forbid")

    uri: str
    query: str
    limit: int = Field(default=20, ge=1, le=200)
    case_insensitive: bool = False
    level_limit: int = Field(default=10, ge=0, le=64)
    include_content: bool = False
    telemetry: TelemetryRequest = False


@router.post("/search")
async def index_search(
    request: IndexSearchRequest,
    _ctx: RequestContext = Depends(get_request_context),
):
    """Keyword search over generated directory catalogues under a URI."""
    service = get_service()
    resolved_uri = resolve_uri_or_uris(request.uri, _ctx)
    execution = await run_operation(
        operation="index.search",
        telemetry=request.telemetry,
        fn=lambda: search_navigation_indexes(
            viking_fs=service.viking_fs,
            ctx=_ctx,
            uri=resolved_uri,
            query=request.query,
            limit=request.limit,
            case_insensitive=request.case_insensitive,
            level_limit=request.level_limit,
            include_content=request.include_content,
        ),
    )
    return Response(
        status="ok",
        result=execution.result,
        telemetry=execution.telemetry,
    ).model_dump(exclude_none=True)
