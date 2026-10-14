# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
Search Service for OpenViking.

Provides semantic search operations: search, find.
"""

import asyncio
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

from openviking.server.identity import RequestContext
from openviking.indexing.publication import pin_query_snapshot, query_snapshot
from openviking.storage.viking_fs import VikingFS
from openviking.utils.image_search import (
    image_bytes_to_data_uri,
    is_data_image_uri,
    is_http_url,
    is_viking_uri,
)
from openviking_cli.exceptions import InvalidArgumentError, NotInitializedError
from openviking_cli.utils import get_logger

if TYPE_CHECKING:
    from openviking.session import Session

logger = get_logger(__name__)


def _ensure_non_empty_query(
    query: str,
    image_url: Optional[str] = None,
    filter: Optional[Dict] = None,
) -> None:
    """Reject a request that gives the search nothing to work with.

    A filter is an acceptable substitute for a query: the result set is then
    fully determined by the filter, which is exactly what an exact-match lookup
    (e.g. by a tag carrying an external id) needs. Without either one the call
    would return an arbitrary slice of the whole store.
    """
    if query.strip() or image_url or filter:
        return
    raise InvalidArgumentError(
        "Search query or image_url must not be empty unless a filter is provided."
    )


def _ensure_image_url_form(image_url: Optional[str]) -> None:
    """Reject a malformed image reference before anything is read or leased."""
    if image_url and not (
        is_viking_uri(image_url) or is_data_image_uri(image_url) or is_http_url(image_url)
    ):
        raise InvalidArgumentError(
            "image_url must be a data:image base64 URI, http(s) URL, or viking:// URI."
        )


class SearchService:
    """Semantic search service."""

    def __init__(self, viking_fs: Optional[VikingFS] = None):
        self._viking_fs = viking_fs

    def set_viking_fs(self, viking_fs: VikingFS) -> None:
        """Set VikingFS instance (for deferred initialization)."""
        self._viking_fs = viking_fs

    def _ensure_initialized(self) -> VikingFS:
        """Ensure VikingFS is initialized."""
        if not self._viking_fs:
            raise NotInitializedError("VikingFS")
        return self._viking_fs

    def is_intent_enabled(self) -> bool:
        """Whether search uses session context for LLM intent analysis.

        When false, callers should skip session.load / get_context_for_search:
        VikingFS.search ignores session_info and searches with the raw query.
        Default is True (matches RetrievalConfig) when config is unset.
        """
        if not self._viking_fs or self._viking_fs.retrieval_config is None:
            return True
        return bool(self._viking_fs.retrieval_config.enable_intent)

    async def _resolve_image_url(
        self,
        image_url: Optional[str],
        ctx: RequestContext,
    ) -> Optional[str]:
        _ensure_image_url_form(image_url)
        if not image_url:
            return None
        if is_viking_uri(image_url):
            viking_fs = self._ensure_initialized()
            content = await viking_fs.read_file_bytes(image_url, ctx=ctx)
            return image_bytes_to_data_uri(content, image_url)
        return image_url

    @query_snapshot
    async def search(
        self,
        query: str,
        ctx: RequestContext,
        target_uri: Union[str, List[str]] = "",
        session: Optional["Session"] = None,
        limit: int = 10,
        score_threshold: Optional[float] = None,
        filter: Optional[Dict] = None,
        level: Optional[List[int]] = None,
        image_url: Optional[str] = None,
        meta_filters: Optional[List[Dict]] = None,
        meta_hints: Optional[List[Dict]] = None,
        rerank_enabled: bool = True,
    ) -> Any:
        """Complex search with session context.

        Args:
            query: Query string
            target_uri: Target directory URI(s), supports str or List[str]
            session: Session object for context
            limit: Max results
            score_threshold: Score threshold
            filter: Metadata filters
            level: Filter by level (0=abstract, 1=overview, 2=file)
            rerank_enabled: False skips the rerank provider call

        Returns:
            FindResult
        """
        # ``search`` has no filter-only branch downstream (VikingFS.search embeds the
        # query), so the gate here must not accept what that layer would reject.
        _ensure_non_empty_query(query, image_url)
        _ensure_image_url_form(image_url)
        viking_fs = self._ensure_initialized()
        await asyncio.to_thread(pin_query_snapshot, ctx)
        resolved_image_url = await self._resolve_image_url(image_url, ctx)
        metadata_trace = None
        if meta_filters or meta_hints:
            from openviking.retrieve.metadata_query import resolve_metadata
            metadata_filter, metadata_trace = await resolve_metadata(viking_fs, ctx, target_uri, meta_filters or [], meta_hints or [])
            filter = {"op": "and", "conds": [filter, metadata_filter]} if filter else metadata_filter

        session_info = None
        # Intent off: session_info is unused by VikingFS — skip the archive/message scan.
        if session is not None and self.is_intent_enabled() and not resolved_image_url:
            session_info = await session.get_context_for_search(query)

        result = await viking_fs.search(
            query=query,
            ctx=ctx,
            target_uri=target_uri,
            session_info=session_info,
            limit=limit,
            score_threshold=score_threshold,
            filter=filter,
            level=level,
            image_url=resolved_image_url,
            rerank_enabled=rerank_enabled,
        )
        if metadata_trace is not None:
            from openviking.retrieve.metadata_query import MetadataResult
            return MetadataResult(result, metadata_trace)
        return result

    @query_snapshot
    async def find(
        self,
        query: str,
        ctx: RequestContext,
        target_uri: Union[str, List[str]] = "",
        limit: int = 10,
        score_threshold: Optional[float] = None,
        filter: Optional[Dict] = None,
        level: Optional[List[int]] = None,
        image_url: Optional[str] = None,
        meta_filters: Optional[List[Dict]] = None,
        meta_hints: Optional[List[Dict]] = None,
        rerank_enabled: bool = True,
    ) -> Any:
        """Semantic search without session context.

        Args:
            query: Query string
            target_uri: Target directory URI(s), supports str or List[str]
            limit: Max results
            score_threshold: Score threshold
            filter: Metadata filters
            level: Filter by level (0=abstract, 1=overview, 2=file)
            rerank_enabled: False skips the rerank provider call

        Returns:
            FindResult
        """
        _ensure_non_empty_query(query, image_url, filter or meta_filters)
        _ensure_image_url_form(image_url)
        viking_fs = self._ensure_initialized()
        await asyncio.to_thread(pin_query_snapshot, ctx)
        resolved_image_url = await self._resolve_image_url(image_url, ctx)
        metadata_trace = None
        if meta_filters or meta_hints:
            from openviking.retrieve.metadata_query import resolve_metadata
            metadata_filter, metadata_trace = await resolve_metadata(viking_fs, ctx, target_uri, meta_filters or [], meta_hints or [])
            filter = {"op": "and", "conds": [filter, metadata_filter]} if filter else metadata_filter
        result = await viking_fs.find(
            query=query,
            ctx=ctx,
            target_uri=target_uri,
            limit=limit,
            score_threshold=score_threshold,
            filter=filter,
            level=level,
            image_url=resolved_image_url,
            rerank_enabled=rerank_enabled,
        )
        if metadata_trace is not None:
            from openviking.retrieve.metadata_query import MetadataResult
            return MetadataResult(result, metadata_trace)
        return result
