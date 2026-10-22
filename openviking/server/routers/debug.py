# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Debug endpoints for OpenViking HTTP Server.

Provides debug API for system diagnostics.
- /api/v1/debug/health - Quick health check
- /api/v1/debug/vector/scroll - Paginated vector records
- /api/v1/debug/vector/count - Count vector records
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from openviking.core.path_variables import resolve_path_variables
from openviking.core.uri_validation import validate_request_viking_uri
from openviking.server.auth import get_request_context
from openviking.server.dependencies import get_service
from openviking.server.identity import RequestContext
from openviking.server.models import Response
from openviking.server.responses import error_response
from openviking.storage.vikingdb_manager import VikingDBManagerProxy

router = APIRouter(prefix="/api/v1/debug", tags=["debug"])


@router.get("/health")
async def debug_health(
    _ctx: RequestContext = Depends(get_request_context),
):
    """Quick health check."""
    service = get_service()
    is_healthy = service.debug.is_healthy()
    return Response(status="ok", result={"healthy": is_healthy})


@router.get("/vector/scroll")
async def debug_vector_scroll(
    limit: int = Query(100, ge=1, le=1000),
    cursor: Optional[str] = None,
    uri: Optional[str] = None,
    output_fields: Optional[str] = None,
    _ctx: RequestContext = Depends(get_request_context),
):
    """Get paginated vector records with tenant isolation."""
    service = get_service()
    if not service.vikingdb_manager:
        return error_response(
            code="NO_VECTOR_DB", message="Vector DB not initialized"
        )

    proxy = VikingDBManagerProxy(service.vikingdb_manager, _ctx)

    filter_expr = None
    if uri:
        # Resolve path variables before using URI
        uri = validate_request_viking_uri(resolve_path_variables(uri), _ctx)
        filter_expr = {"op": "must", "field": "uri", "conds": [uri]}

    fields = [f for f in (output_fields or "").split(",") if f] or None
    records, next_cursor = await proxy.scroll(
        filter=filter_expr, limit=limit, cursor=cursor, output_fields=fields
    )

    return Response(status="ok", result={"records": records, "next_cursor": next_cursor})


@router.get("/vector/scan")
async def debug_vector_scan(
    prefix: str = Query(...),
    level: Optional[int] = None,
    limit: int = Query(500, ge=1, le=2000),
    cursor: Optional[str] = None,
    _ctx: RequestContext = Depends(get_request_context),
):
    """Scan records by URI prefix and level with engine-side filters."""
    from openviking.storage.expr import And, In, PathScope

    service = get_service()
    proxy = VikingDBManagerProxy(service.vikingdb_manager, _ctx)
    filters = [PathScope("uri", prefix, depth=-1)]
    if level is not None:
        filters.append(In("level", [level]))
    records, next_cursor = await proxy.scroll(
        filter=And(filters) if len(filters) > 1 else filters[0],
        limit=limit,
        cursor=cursor,
        output_fields=[
            "id", "uri", "level", "context_type", "name", "abstract", "document_id",
            "type", "parent_uri", "importance", "created_at", "updated_at",
            "chunk_id", "chunk_ordinal", "search_tags", "owner_user_id", "account_id",
        ],
    )
    return Response(status="ok", result={"records": records, "next_cursor": next_cursor})


@router.get("/vector/records")
async def debug_vector_records(
    uri: str = Query(...),
    limit: int = Query(100, ge=1, le=1000),
    output_fields: Optional[str] = None,
    _ctx: RequestContext = Depends(get_request_context),
):
    """Return full vector records for one URI (diagnostics)."""
    service = get_service()
    proxy = VikingDBManagerProxy(service.vikingdb_manager, _ctx)
    fields = [f for f in (output_fields or "").split(",") if f] or None
    records, _ = await proxy.scroll(
        filter={"op": "must", "field": "uri", "conds": [uri]},
        limit=limit,
        output_fields=fields,
    )
    return Response(status="ok", result={"records": records})


@router.get("/vector/count")
async def debug_vector_count(
    filter: Optional[str] = None,
    uri: Optional[str] = None,
    _ctx: RequestContext = Depends(get_request_context),
):
    """Get count of vector records with tenant isolation."""
    import json

    service = get_service()
    if not service.vikingdb_manager:
        return error_response(
            code="NO_VECTOR_DB", message="Vector DB not initialized"
        )

    proxy = VikingDBManagerProxy(service.vikingdb_manager, _ctx)

    filter_expr = None
    if filter:
        try:
            filter_expr = json.loads(filter)
        except json.JSONDecodeError:
            return error_response(
                code="INVALID_FILTER", message="Invalid filter JSON"
            )

    if uri:
        # Resolve path variables before using URI
        uri = validate_request_viking_uri(resolve_path_variables(uri), _ctx)
        uri_filter = {"op": "must", "field": "uri", "conds": [uri]}
        if filter_expr:
            # For combining filters, we should use And from expr, but for simplicity, let's use RawDSL for now
            from openviking.storage.expr import And, RawDSL

            if isinstance(filter_expr, dict):
                filter_expr = RawDSL(filter_expr)
            uri_filter = RawDSL(uri_filter)
            filter_expr = And([filter_expr, uri_filter])
        else:
            filter_expr = uri_filter

    count = await proxy.count(filter=filter_expr)
    return Response(status="ok", result={"count": count})
