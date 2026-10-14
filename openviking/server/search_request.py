# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Shared resolvers for the search request surface.

Both the HTTP router (``openviking.server.routers.search``) and the MCP
endpoint (``openviking.server.mcp_endpoint``) expose ``find``/``search``/
``grep``/``glob``. Keeping the request-normalization logic here — rather than
in the router — is what stops the two surfaces from drifting apart.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Sequence, Union

from openviking.core.path_variables import resolve_path_variables
from openviking.core.uri_validation import validate_request_viking_uri
from openviking.server.identity import RequestContext
from openviking.utils.image_search import is_viking_uri
from openviking.utils.search_filters import (
    SearchContextTypeInput,
    merge_search_filter,
)
from openviking.utils.tags import build_search_tags_filter
from openviking_cli.exceptions import InvalidArgumentError

TimeField = Literal["updated_at", "created_at"]


def resolve_search_limit(limit: int, node_limit: Optional[int]) -> int:
    """``node_limit`` is the legacy alias for ``limit`` and wins when set."""
    return node_limit if node_limit is not None else limit


def resolve_search_filter(
    request_filter: Optional[Dict[str, Any]],
    context_type: Optional[SearchContextTypeInput],
    since: Optional[str],
    until: Optional[str],
    time_field: Optional[TimeField],
    tags: Optional[List[str]],
) -> Optional[Dict[str, Any]]:
    """Merge the public search shortcuts into one metadata filter tree."""
    try:
        merged = merge_search_filter(
            request_filter,
            context_type=context_type,
            since=since,
            until=until,
            time_field=time_field,
        )
        tag_filter = build_search_tags_filter(tags)
        if not tag_filter:
            return merged
        if merged:
            if tag_filter.get("op") == "and" and isinstance(tag_filter.get("conds"), list):
                return {"op": "and", "conds": [merged, *tag_filter["conds"]]}
            return {"op": "and", "conds": [merged, tag_filter]}
        return tag_filter
    except ValueError as exc:
        raise InvalidArgumentError(str(exc)) from exc


def resolve_uri_or_uris(
    uri: Union[str, List[str]], ctx: RequestContext
) -> Union[str, List[str]]:
    """Resolve path variables in a single URI or list of URIs."""
    if isinstance(uri, list):
        return [validate_request_viking_uri(resolve_path_variables(u), ctx) for u in uri]
    if not uri:
        return ""
    return validate_request_viking_uri(resolve_path_variables(uri), ctx)


def resolve_uri_list(uris: Sequence[str], ctx: RequestContext) -> List[str]:
    return [validate_request_viking_uri(resolve_path_variables(uri), ctx) for uri in uris]


def resolve_image_url(image_url: Optional[str], ctx: RequestContext) -> Optional[str]:
    """Validate a viking:// image query; pass data: and http(s): through verbatim."""
    if not image_url:
        return image_url
    resolved = resolve_path_variables(image_url)
    if is_viking_uri(resolved):
        return validate_request_viking_uri(resolved, ctx, field_name="image_url")
    return resolved


def filter_hits_by_score(hits: Any, floor: Optional[float]) -> List[Any]:
    """Keep hits whose final score is >= ``floor``; ``None`` keeps everything.

    The hybrid fusion recall does not consume ``score_threshold`` itself, so
    the vector surfaces apply it to the final (post-rerank) score here. Rows
    may be serialized dicts (HTTP) or objects with a ``score`` attribute
    (MCP); a missing or unusable score counts as 0 and is dropped.
    """
    if floor is None:
        return list(hits or [])
    kept = []
    for row in hits or []:
        raw = row.get("score") if isinstance(row, dict) else getattr(row, "score", None)
        try:
            score = float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            score = 0.0
        if score >= floor:
            kept.append(row)
    return kept