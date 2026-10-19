# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
Hierarchical retriever for OpenViking.

Implements directory-based hierarchical retrieval with recursive search
and rerank-based relevance scoring.
"""

import asyncio
import heapq
import logging
import math
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from openviking.core.context import ContextLevel
from openviking.core.retrieval_targets import default_target_directories
from openviking.indexing.record_meta import meta_view, provenance_view
from openviking.models.embedder.base import EmbedResult, embed_compat
from openviking.models.rerank import RerankClient
from openviking.retrieve.fusion import (
    PROFILE_HYBRID,
    Candidate,
    FusionProfile,
    candidate_from_result,
    profile_for,
    run_fusion,
)
from openviking.retrieve.memory_lifecycle import hotness_score
from openviking.retrieve.retrieval_stats import get_stats_collector
from openviking.server.identity import RequestContext
from openviking.storage.abstract_overview import AbstractOverviewFormatError, body_for_preview
from openviking.storage.expr import FilterExpr
from openviking.storage.vikingdb_manager import VikingDBManager, VikingDBManagerProxy
from openviking.telemetry import get_current_telemetry
from openviking.utils.tags import normalize_search_tags
from openviking.utils.time_utils import parse_iso_datetime
from openviking.utils.token_estimation import (
    estimate_text_tokens,
    truncate_text_to_token_budget,
)
from openviking_cli.exceptions import InvalidArgumentError
from openviking_cli.retrieve.types import (
    ContextType,
    MatchedContext,
    QueryResult,
    TypedQuery,
)
from openviking_cli.utils.config import RerankConfig, RetrievalConfig
from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)

# The dense leaf pool is fused by rank position, so a pool owned by one record
# type starves every other kind of document: with 406k image vectors in the live
# corpus, the whole-tree top-100 was images only, and the text document that
# actually matched the query got no dense contribution at all (its fused score
# was exactly one lexical term, tying the image's single dense term). Over-fetch
# and cap the share of any one type before fusion.
DENSE_POOL_OVERFETCH = 4
_DENSE_POOL_MAX_TYPE_SHARE = 0.5


def diversify_by_type(
    results: List[Dict[str, Any]],
    limit: int,
    *,
    max_share: float = _DENSE_POOL_MAX_TYPE_SHARE,
) -> List[Dict[str, Any]]:
    """Cap one record type's share of a recall pool, keeping the pool size.

    ``type`` groups recall rows by modality (``image`` vs text). Rows beyond a
    type's share are held back rather than dropped, and are used to refill the
    pool in their original order once diversified rows run out -- so a
    single-modality pool (every other deployment) comes back byte-identical,
    and only a mixed pool changes.
    """
    if limit <= 0 or len(results) <= limit:
        return results
    cap = max(1, int(limit * max_share))
    kept: List[Dict[str, Any]] = []
    held: List[Dict[str, Any]] = []
    per_type: Dict[str, int] = {}
    for result in results:
        kind = str(result.get("type") or "")
        if per_type.get(kind, 0) < cap:
            per_type[kind] = per_type.get(kind, 0) + 1
            kept.append(result)
        else:
            held.append(result)
        if len(kept) >= limit:
            break
    if len(kept) < limit:
        kept.extend(held[: limit - len(kept)])
    return kept


class RetrieverMode(str):
    THINKING = "thinking"
    QUICK = "quick"
    # Unified fusion profile (KB-017). It is an explicit opt-in: it is never
    # inferred from whether a reranker happens to be configured.
    HYBRID = "hybrid"


class HierarchicalRetriever:
    """Hierarchical retriever with dense and sparse vector support."""

    MAX_CONVERGENCE_ROUNDS = 3  # Stop after multiple rounds with unchanged topk
    DIRECTORY_DOMINANCE_RATIO = 1.2  # Directory score must exceed max child score
    GLOBAL_SEARCH_TOPK = 10  # Global retrieval count (more candidates = better rerank precision)
    MAX_PARALLEL_CHILD_SEARCHES = 4  # Limit per-request fan-out against remote vector stores
    LEVEL_URI_SUFFIX = {0: ".abstract.md", 1: ".overview.md"}

    def __init__(
        self,
        storage: VikingDBManager,
        embedder: Optional[Any],
        rerank_config: Optional[RerankConfig] = None,
        retrieval_config: Optional[RetrievalConfig] = None,
        fusion_profile: Optional[FusionProfile] = None,
        lexical_search: Optional[
            Callable[..., Awaitable[List[Dict[str, Any]]]]
        ] = None,
    ):
        """Initialize hierarchical retriever with rerank_config.

        Args:
            storage: VikingVectorIndexBackend instance
            embedder: Embedder instance (supports dense/sparse/hybrid)
            rerank_config: Rerank configuration (optional, will fallback to vector search only)
            retrieval_config: Retrieval ranking configuration.
            fusion_profile: Budgets/knobs for the unified ``hybrid`` profile. It is
                only consulted when ``mode == RetrieverMode.HYBRID``, so quick and
                thinking behavior is unaffected.
            lexical_search: Optional async callable implementing the BM25 recall
                path for the hybrid profile. It must return ranked candidate dicts
                (higher-is-better normalized scores). Kept as an injected
                interface so the retriever does not own a second search API.
        """
        self.vector_store = storage
        self.embedder = embedder
        self.rerank_config = rerank_config
        self.fusion_profile = fusion_profile or profile_for(PROFILE_HYBRID)
        self._lexical_search = lexical_search
        self.rerank_max_input_tokens = rerank_config.max_input_tokens if rerank_config else 0
        self.retrieval_config = retrieval_config or RetrievalConfig()
        self.hotness_alpha = self.retrieval_config.hotness_alpha
        self.score_propagation_alpha = self.retrieval_config.score_propagation_alpha

        # Use rerank threshold if available, otherwise use a default
        self.threshold = rerank_config.threshold if rerank_config else 0

        # Initialize rerank client — all providers go through unified dispatch
        if rerank_config and rerank_config.is_available():
            self._rerank_client = RerankClient.from_config(rerank_config)
            provider = rerank_config._effective_provider()
            logger.info(
                f"[HierarchicalRetriever] Rerank enabled (provider={provider}), threshold={self.threshold}"
            )
        else:
            self._rerank_client = None
            logger.info(
                f"[HierarchicalRetriever] Rerank not configured, using vector search only with threshold={self.threshold}"
            )

    async def retrieve(
        self,
        query: TypedQuery,
        ctx: RequestContext,
        limit: int = 5,
        mode: Optional[RetrieverMode] = None,
        score_threshold: Optional[float] = None,
        score_gte: bool = False,
        scope_dsl: Optional[FilterExpr | Dict[str, Any]] = None,
        level: Optional[List[int]] = None,
        rerank_enabled: bool = True,
    ) -> QueryResult:
        """
        Execute hierarchical retrieval.

        Args:
            ctx: Request context used for tenant and permission filtering
            score_threshold: Custom score threshold (overrides config)
            score_gte: True uses >=, False uses >
            scope_dsl: Additional scope constraints passed from public find/search filter
            level: Optional result level filter (0=L0, 1=L1, 2=L2)
            rerank_enabled: False skips the rerank provider call and ranks by
                fused order; scores then come from the RRF fusion, not the
                reranker's 0-1 relevance domain.
        """
        t0 = time.monotonic()
        telemetry = get_current_telemetry()
        effective_threshold = self._resolve_threshold(score_threshold)
        image_query = bool(getattr(query, "image_query", False))
        if mode is None:
            mode = (RetrieverMode.HYBRID if self._lexical_search is not None else
                    RetrieverMode.QUICK if not self._rerank_client else RetrieverMode.THINKING)
        if mode == RetrieverMode.HYBRID and level is not None and 2 not in level:
            mode = RetrieverMode.QUICK
        if image_query:
            mode = RetrieverMode.QUICK
            if level is None:
                level = [2]

        # 创建 proxy 包装器，绑定当前 ctx
        vector_proxy = VikingDBManagerProxy(self.vector_store, ctx)

        target_dirs = [d for d in (query.target_directories or []) if d]

        dense_collection_exists = await vector_proxy.collection_exists_bound()
        if not dense_collection_exists and not (mode == RetrieverMode.HYBRID and self._lexical_search is not None):
            logger.warning(
                "[RecursiveSearch] Collection %s does not exist",
                vector_proxy.collection_name,
            )
            return QueryResult(
                query=query,
                matched_contexts=[],
                searched_directories=[],
            )

        # Generate query vectors once to avoid duplicate embedding calls
        query_vector = None
        sparse_query_vector = None
        if self.embedder:
            if image_query and not getattr(self.embedder, "supports_multimodal", False):
                raise InvalidArgumentError("Image search requires a multimodal embedding model.")
            with telemetry.measure("search.embed_query"):
                embedding_input = getattr(query, "embedding_input", None) or query.query
                result: EmbedResult = await embed_compat(
                    self.embedder,
                    embedding_input,
                    is_query=True,
                )
                query_vector = result.dense_vector
                sparse_query_vector = result.sparse_vector

        # Step 1: Determine starting directories based on explicit target dirs.
        if target_dirs:
            root_uris = target_dirs
        else:
            root_uris = default_target_directories(ctx, context_type=query.context_type)

        context_type = query.context_type.value if query.context_type else None
        if image_query and context_type is None:
            context_type = ContextType.RESOURCE.value

        if mode == RetrieverMode.HYBRID:
            return await self._retrieve_hybrid(
                query=query,
                ctx=ctx,
                vector_proxy=vector_proxy,
                query_vector=query_vector,
                sparse_query_vector=sparse_query_vector,
                context_type=context_type,
                target_dirs=target_dirs,
                scope_dsl=scope_dsl,
                level=level,
                limit=limit,
                root_uris=root_uris,
                t0=t0,
                rerank_enabled=rerank_enabled,
            )

        if mode == RetrieverMode.QUICK:
            search_limit = (
                max(limit * 5, 50) if image_query else max(limit, self.GLOBAL_SEARCH_TOPK)
            )
            with telemetry.measure("search.vector_retrieval"):
                quick_results = await vector_proxy.search_in_tenant(
                    query_vector=query_vector,
                    sparse_query_vector=sparse_query_vector,
                    context_type=context_type,
                    target_directories=target_dirs,
                    extra_filter=scope_dsl,
                    level=level,
                    limit=search_limit,
                )
            telemetry.count("vector.searches", 1)
            telemetry.count("vector.scored", len(quick_results))
            telemetry.count("vector.scanned", len(quick_results))

            collected_by_uri: Dict[str, Dict[str, Any]] = {}
            for result in quick_results:
                uri = result.get("uri", "")
                if not uri:
                    continue

                score = self._finite_score(result.get("_score", 0.0))
                if not self._passes_threshold(score, effective_threshold, score_gte):
                    continue

                candidate = dict(result)
                candidate["_score"] = score
                candidate["_final_score"] = score

                previous = collected_by_uri.get(self._candidate_identity(candidate))
                if previous is None or score > previous.get("_final_score", 0.0):
                    collected_by_uri[self._candidate_identity(candidate)] = candidate

            candidates = sorted(
                collected_by_uri.values(),
                key=lambda x: x.get("_final_score", 0.0),
                reverse=True,
            )
            apply_hotness = False
            rerank_used = False
        else:
            # Step 2: Global vector search to supplement starting points
            with telemetry.measure("search.vector_retrieval"):
                global_results = await vector_proxy.search_in_tenant(
                    query_vector=query_vector,
                    sparse_query_vector=sparse_query_vector,
                    context_type=context_type,
                    target_directories=target_dirs,
                    extra_filter=scope_dsl,
                    level=[0, 1],
                    limit=max(limit, self.GLOBAL_SEARCH_TOPK),
                )
            telemetry.count("vector.searches", 1)
            telemetry.count("vector.scored", len(global_results))
            telemetry.count("vector.scanned", len(global_results))

            leaf_results: List[Dict[str, Any]] = []
            # Navigation can miss relevant leaves even when ACL is disabled.
            # The bound tenant proxy still applies the same scope/authorization.
            if level is None or 2 in level:
                leaf_results = await vector_proxy.search_in_tenant(
                    query_vector=query_vector,
                    sparse_query_vector=sparse_query_vector,
                    context_type=context_type,
                    target_directories=target_dirs,
                    extra_filter=scope_dsl,
                    level=[2],
                    limit=max(limit, self.GLOBAL_SEARCH_TOPK),
                )
                telemetry.count("vector.searches", 1)
                telemetry.count("vector.scored", len(leaf_results))
                telemetry.count("vector.scanned", len(leaf_results))
                if self._rerank_client and mode == RetrieverMode.THINKING and leaf_results:
                    leaf_scores = await self._rerank_scores(
                        query.query,
                        [str(result.get("abstract", "")) for result in leaf_results],
                        [self._finite_score(result.get("_score", 0.0)) for result in leaf_results],
                    )
                    leaf_results = [
                        {**result, "_score": score}
                        for result, score in zip(leaf_results, leaf_scores, strict=True)
                    ]

            # Debug: Print all URIs in global_results
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"[retrieve] target_dirs: {target_dirs}")
                logger.debug(f"[retrieve] root_uris: {root_uris}")
                logger.debug(f"[retrieve] scope_dsl: {scope_dsl}")
                logger.debug(
                    f"[retrieve] Step 2 completed, global_results contains {len(global_results)} items:"
                )
                for i, r in enumerate(global_results):
                    uri = r.get("uri", "UNKNOWN_URI")
                    score = r.get("_score", 0.0)
                    result_level = r.get("level", "UNKNOWN_LEVEL")
                    account_id = r.get("account_id", "UNKNOWN_ACCOUNT_ID")
                    logger.debug(
                        f"  [{i}] URI: {uri}, score: {score:.4f}, level: {result_level}, account_id: {account_id}"
                    )

            # Step 3: Pick recursive entry points from directory hits and explicit roots.
            directory_scores = [self._finite_score(r.get("_score", 0.0)) for r in global_results]
            if self._rerank_client and mode == RetrieverMode.THINKING:
                directory_scores = await self._rerank_scores(
                    query.query,
                    [str(r.get("abstract", "")) for r in global_results],
                    directory_scores,
                )

            starting_points = []
            seen_starting_uris = set()
            for result, score in zip(global_results, directory_scores, strict=True):
                uri = result.get("uri", "")
                if not uri or uri in seen_starting_uris:
                    continue
                starting_points.append((uri, score))
                seen_starting_uris.add(uri)

            for uri in root_uris:
                if uri not in seen_starting_uris:
                    starting_points.append((uri, 0.0))
                    seen_starting_uris.add(uri)

            # Add directory hits to the result pool only when explicitly requested.
            initial_candidates = list(leaf_results)
            if level is not None:
                for result, score in zip(global_results, directory_scores, strict=True):
                    if result.get("level", 2) not in level:
                        continue
                    candidate = dict(result)
                    candidate["_score"] = score
                    initial_candidates.append(candidate)

            # Step 4: Recursive search
            with telemetry.measure("search.vector_retrieval"):
                candidates = await self._recursive_search(
                    vector_proxy=vector_proxy,
                    query=query.query,
                    query_vector=query_vector,
                    sparse_query_vector=sparse_query_vector,
                    starting_points=starting_points,
                    limit=limit,
                    mode=mode,
                    threshold=effective_threshold,
                    score_gte=score_gte,
                    context_type=context_type,
                    target_dirs=target_dirs,
                    scope_dsl=scope_dsl,
                    initial_candidates=initial_candidates,
                    level=level,
                )
            apply_hotness = True
            rerank_used = self._rerank_client is not None and mode == RetrieverMode.THINKING

        # Step 6: Convert results
        matched = await self._convert_to_matched_contexts(
            candidates,
            ctx=ctx,
            apply_hotness=apply_hotness,
        )
        final = matched[:limit]

        elapsed_ms = (time.monotonic() - t0) * 1000
        get_stats_collector().record_query(
            context_type=context_type or "unknown",
            result_count=len(final),
            scores=[m.score for m in final],
            latency_ms=elapsed_ms,
            rerank_used=rerank_used,
        )

        return QueryResult(
            query=query,
            matched_contexts=final,
            searched_directories=root_uris,
        )

    async def _retrieve_hybrid(
        self,
        *,
        query: TypedQuery,
        ctx: RequestContext,
        vector_proxy: VikingDBManagerProxy,
        query_vector: Optional[List[float]],
        sparse_query_vector: Optional[Dict[str, float]],
        context_type: Optional[str],
        target_dirs: List[str],
        scope_dsl: Optional[FilterExpr | Dict[str, Any]],
        level: Optional[List[int]],
        limit: int,
        root_uris: List[str],
        t0: float,
        rerank_enabled: bool = True,
    ) -> QueryResult:
        """Unified pipeline: recall -> RRF -> rerank -> MMR -> expand -> aggregate.

        The leaf/global L2 recall below is always executed regardless of whether
        navigation, rerank or ACL are enabled, so the leaf fallback can never be
        silently dropped by a config toggle (spec 7.1 / TC-6.4).
        """
        profile = self.fusion_profile.with_row_budget(limit)
        telemetry = get_current_telemetry()
        stage_started = time.monotonic()

        recall_limit = max(profile.recall_limit, limit)
        # Leaf global recall — independent of navigation and rerank toggles.
        leaf_results = []
        with telemetry.measure("search.vector_retrieval"):
            dense_searched = query_vector is not None and await vector_proxy.collection_exists_bound()
            if dense_searched:
                leaf_results = await vector_proxy.search_in_tenant(
                    query_vector=query_vector,
                    sparse_query_vector=sparse_query_vector,
                    context_type=context_type,
                    target_directories=target_dirs,
                    extra_filter=scope_dsl,
                    level=[2] if level is None else [lv for lv in level if lv == 2] or [2],
                    limit=recall_limit * DENSE_POOL_OVERFETCH,
                )
                leaf_results = diversify_by_type(leaf_results, recall_limit)
        vector_done = time.monotonic()
        telemetry.count("vector.searches", int(dense_searched))
        telemetry.count("vector.scored", len(leaf_results))
        telemetry.count("vector.scanned", len(leaf_results))

        ranked_paths: Dict[str, List[Candidate]] = {
            "dense": [
                candidate_from_result(result, rank=index + 1)
                for index, result in enumerate(leaf_results)
            ]
        }

        lexical_count = 0
        if self._lexical_search is not None:
            try:
                lexical_rows = await self._lexical_search(
                    query=query,
                    ctx=ctx,
                    context_type=context_type,
                    target_dirs=target_dirs,
                    scope_dsl=scope_dsl,
                    limit=recall_limit,
                )
            except Exception as exc:  # noqa: BLE001 - lexical path is a recall path
                logger.warning("[HybridRetrieval] Lexical recall failed: %s", exc)
                telemetry.record_error("hybrid.lexical_recall", "LEXICAL_RECALL_FAILED", str(exc))
            else:
                if lexical_rows:
                    lexical_count = len(lexical_rows)
                    ranked_paths["lexical"] = [
                        candidate_from_result(result, rank=index + 1)
                        for index, result in enumerate(lexical_rows)
                    ]
        lexical_done = time.monotonic()

        # The pipeline is pure CPU work plus one blocking provider call; run it off
        # the event loop rather than stalling concurrent requests on rerank.
        fusion_reranker = self._build_fusion_reranker(query.query) if rerank_enabled else None
        fusion_result = await asyncio.to_thread(
            run_fusion,
            ranked_paths,
            profile=profile,
            rerank=fusion_reranker,
            estimate=estimate_text_tokens,
        )

        fusion_done = time.monotonic()

        for key, value in fusion_result.trace.items():
            telemetry.set(f"hybrid.{key}", value)
        telemetry.set("hybrid.rerank_requested", rerank_enabled)
        rerank_applied = bool(fusion_result.trace.get("rerank_applied"))
        rerank_degraded = bool(rerank_enabled and not rerank_applied)
        if rerank_degraded:
            # Silent degradation served rank-only RRF scores for a whole
            # acceptance run: the fused score depends only on rank position, so
            # an unrelated query tied with a matching one and no counter or log
            # line said so. Rerank is the only query-dependent scoring stage.
            degrade_reason = str(fusion_result.trace.get("rerank_reason") or "reranker unavailable")
            logger.warning(
                "[HybridRetrieval] Rerank degraded, serving rank-only fused order: %s. "
                "Scores are normalized rank-fusion values in (0,1] (order-preserving, "
                "not query relevance).",
                degrade_reason,
            )
            telemetry.record_error("hybrid.rerank", "RERANK_DEGRADED", degrade_reason)

        candidates = [candidate.as_dict() for candidate in fusion_result.selected]
        matched = await self._convert_to_matched_contexts(candidates, ctx=ctx)
        final = matched[:limit]
        convert_done = time.monotonic()

        stage_total = convert_done - stage_started
        if stage_total >= 5.0:
            # Tree-scoped finds can take minutes; keep a stage breakdown so the
            # cost centre is visible without a profiler (see round-14 notes).
            logger.warning(
                "[HybridRetrieval] slow find %.1fs: vector=%.1fs rows=%d lexical=%.1fs rows=%d "
                "fusion=%.1fs convert=%.1fs candidates=%d target_dirs=%s rerank=%s",
                stage_total,
                vector_done - stage_started,
                len(leaf_results),
                lexical_done - vector_done,
                lexical_count,
                fusion_done - lexical_done,
                convert_done - fusion_done,
                len(candidates),
                target_dirs,
                rerank_enabled,
            )

        elapsed_ms = (time.monotonic() - t0) * 1000
        get_stats_collector().record_query(
            context_type=context_type or "unknown",
            result_count=len(final),
            scores=[m.score for m in final],
            latency_ms=elapsed_ms,
            rerank_used=rerank_applied,
            # The observer's "Rerank Fallback" counter is what an operator reads
            # to decide whether ranking is trustworthy; leaving it at 0 while
            # every query silently served rank-only order hid the outage.
            rerank_fallback=rerank_degraded,
        )

        return QueryResult(
            query=query,
            matched_contexts=final,
            searched_directories=root_uris,
        )

    def _build_fusion_reranker(
        self, rerank_query: str
    ) -> Optional[Callable[[List[Candidate]], Optional[List[float]]]]:
        """Adapt the sync rerank client into the pure fusion rerank callable.

        Returning ``None`` when no client is configured means "rerank not
        requested", which is not a degradation. A configured client that throws
        or returns a wrong count *is* a degradation, handled inside
        :func:`openviking.retrieve.fusion.apply_rerank`.
        """
        if not self._rerank_client:
            return None

        max_input_tokens = self.rerank_max_input_tokens

        def _rerank(window: List[Candidate]) -> Optional[List[float]]:
            documents = []
            kept: List[int] = []
            for index, candidate in enumerate(window):
                text = candidate.text_for_similarity
                if not text.strip():
                    continue
                if max_input_tokens > 0:
                    text = truncate_text_to_token_budget(text, max_input_tokens)
                documents.append(text)
                kept.append(index)
            if not documents:
                return None
            scores = self._rerank_client.rerank_batch(rerank_query, documents)
            if scores is None:
                return None
            if len(scores) != len(documents):
                raise ValueError(
                    f"rerank returned {len(scores)} scores for {len(documents)} documents"
                )
            filled = [window[index].score for index in range(len(window))]
            for index, score in zip(kept, scores, strict=True):
                filled[index] = self._finite_score(score, window[index].score)
            return filled

        return _rerank

    def _resolve_threshold(self, threshold: Optional[float]) -> float:
        resolved = threshold if threshold is not None else self.threshold
        return resolved if resolved is not None else 0.0

    @staticmethod
    def _finite_score(value: Any, default: float = 0.0) -> float:
        try:
            score = float(value)
        except (TypeError, ValueError):
            return default
        return score if math.isfinite(score) else default

    @staticmethod
    def _passes_threshold(score: float, threshold: float, score_gte: bool) -> bool:
        if score_gte:
            return score >= threshold
        return score > threshold

    async def _rerank_scores(
        self,
        query: str,
        documents: List[str],
        fallback_scores: List[float],
    ) -> List[float]:
        """Return rerank scores or fall back to vector scores."""
        if not self._rerank_client or not documents:
            return fallback_scores

        rerank_query = query
        rerank_documents = [
            (index, document) for index, document in enumerate(documents) if document.strip()
        ]
        if not rerank_documents:
            return fallback_scores

        if self.rerank_max_input_tokens > 0:
            max_query_tokens = self.rerank_max_input_tokens * 3 // 4
            if estimate_text_tokens(query) > max_query_tokens:
                rerank_query = truncate_text_to_token_budget(query, max_query_tokens)
            document_tokens = self.rerank_max_input_tokens - estimate_text_tokens(rerank_query)
            rerank_documents = [
                (index, truncate_text_to_token_budget(document, document_tokens))
                for index, document in rerank_documents
            ]

        try:
            scores = await asyncio.to_thread(
                self._rerank_client.rerank_batch,
                rerank_query,
                [document for _, document in rerank_documents],
            )
        except Exception as e:
            logger.warning(
                "[HierarchicalRetriever] Rerank failed, fallback to vector scores: %s", e
            )
            return fallback_scores

        if not scores or len(scores) != len(rerank_documents):
            logger.warning(
                "[HierarchicalRetriever] Invalid rerank result, fallback to vector scores"
            )
            return fallback_scores

        normalized_scores = list(fallback_scores)
        for score, (index, _) in zip(scores, rerank_documents, strict=True):
            normalized_scores[index] = self._finite_score(score, fallback_scores[index])
        return normalized_scores

    async def _recursive_search(
        self,
        vector_proxy: VikingDBManagerProxy,
        query: str,
        query_vector: Optional[List[float]],
        sparse_query_vector: Optional[Dict[str, float]],
        starting_points: List[Tuple[str, float]],
        limit: int,
        mode: str,
        threshold: Optional[float] = None,
        score_gte: bool = False,
        context_type: Optional[str] = None,
        target_dirs: Optional[List[str]] = None,
        scope_dsl: Optional[FilterExpr | Dict[str, Any]] = None,
        initial_candidates: Optional[List[Dict[str, Any]]] = None,
        level: Optional[List[int]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Recursive search with directory priority return and score propagation.

        Args:
            threshold: Score threshold
            score_gte: True uses >=, False uses >
            grep_patterns: Keyword match patterns
            scope_dsl: Additional scope constraints from public find/search filter
        """
        effective_threshold = self._resolve_threshold(threshold)

        sparse_query_vector = sparse_query_vector or None

        collected_by_uri: Dict[str, Dict[str, Any]] = {}
        dir_queue: List[tuple] = []  # Priority queue: (-score, uri)
        visited: set = set()
        prev_topk_uris: set = set()
        prev_pool_size = 0
        convergence_rounds = 0
        stagnant_rounds = 0

        # Add initial candidates that match the requested level.
        if initial_candidates:
            for r in initial_candidates:
                uri = r.get("uri", "")
                if not uri:
                    continue
                if level is None or r.get("level", 2) in level:
                    score = self._finite_score(r.get("_score", 0.0))
                    if not self._passes_threshold(score, effective_threshold, score_gte):
                        logger.debug(
                            f"[RecursiveSearch] Initial candidate URI {uri} score {score:.4f} did not pass threshold {effective_threshold}"
                        )
                        continue
                    r["_final_score"] = score
                    collected_by_uri[self._candidate_identity(r)] = r
                    logger.debug(
                        f"[RecursiveSearch] Added initial candidate: {uri} (score: {score:.4f})"
                    )

        alpha = self.score_propagation_alpha

        # Initialize: process starting points
        for uri, score in starting_points:
            heapq.heappush(dir_queue, (-score, uri))

        async def search_children(current_uri: str) -> List[Dict[str, Any]]:
            return await vector_proxy.search_children_in_tenant(
                parent_uri=current_uri,
                query_vector=query_vector,
                sparse_query_vector=sparse_query_vector,  # Pass sparse vector
                context_type=context_type,
                target_directories=target_dirs,
                extra_filter=scope_dsl,
                limit=max(limit * 2, 20),
            )

        parallelism = max(1, self.MAX_PARALLEL_CHILD_SEARCHES)

        while dir_queue:
            batch: List[Tuple[str, float]] = []
            while dir_queue and len(batch) < parallelism:
                temp_score, current_uri = heapq.heappop(dir_queue)
                current_score = -temp_score
                if current_uri in visited:
                    continue
                visited.add(current_uri)
                logger.info(f"[RecursiveSearch] Entering URI: {current_uri}")
                batch.append((current_uri, current_score))

            if not batch:
                continue

            batch_results = await asyncio.gather(
                *(search_children(current_uri) for current_uri, _ in batch)
            )

            telemetry = get_current_telemetry()
            for (_, current_score), results in zip(batch, batch_results, strict=True):
                telemetry.count("vector.searches", 1)
                telemetry.count("vector.scored", len(results))
                telemetry.count("vector.scanned", len(results))

                if not results:
                    continue

                query_scores = [self._finite_score(r.get("_score", 0.0)) for r in results]
                if self._rerank_client and mode == RetrieverMode.THINKING:
                    documents = [str(r.get("abstract", "")) for r in results]
                    query_scores = await self._rerank_scores(query, documents, query_scores)

                for r, score in zip(results, query_scores, strict=True):
                    uri = r.get("uri", "")
                    final_score = (
                        alpha * score + (1 - alpha) * current_score if current_score else score
                    )

                    if not self._passes_threshold(final_score, effective_threshold, score_gte):
                        logger.debug(
                            f"[RecursiveSearch] URI {uri} score {final_score} did not pass threshold {effective_threshold}"
                        )
                        continue

                    telemetry.count("vector.passed", 1)
                    if level is None or r.get("level", 2) in level:
                        # Deduplicate by URI and keep the highest-scored candidate.
                        previous = collected_by_uri.get(self._candidate_identity(r))
                        if previous is None or final_score > previous.get("_final_score", 0):
                            r["_final_score"] = final_score
                            collected_by_uri[self._candidate_identity(r)] = r
                            logger.debug(
                                "[RecursiveSearch] Updated URI: %s candidate score to %.4f",
                                uri,
                                final_score,
                            )

                    # Only recurse into directories (L0/L1). L2 files are terminal hits.
                    if uri not in visited and r.get("level", 2) != 2:
                        heapq.heappush(dir_queue, (-final_score, uri))

            # Convergence check after each parallel expansion round.
            current_topk = sorted(
                collected_by_uri.values(),
                key=lambda x: x.get("_final_score", 0),
                reverse=True,
            )[:limit]
            current_topk_uris = {self._candidate_identity(c) for c in current_topk}
            current_pool_size = len(collected_by_uri)

            if current_topk_uris == prev_topk_uris and len(current_topk_uris) >= limit:
                convergence_rounds += 1

                if convergence_rounds >= self.MAX_CONVERGENCE_ROUNDS:
                    break
            elif current_pool_size == prev_pool_size:
                stagnant_rounds += 1

                if stagnant_rounds >= self.MAX_CONVERGENCE_ROUNDS:
                    break
            else:
                convergence_rounds = 0
                stagnant_rounds = 0
                prev_topk_uris = current_topk_uris
                prev_pool_size = current_pool_size

        collected = sorted(
            collected_by_uri.values(),
            key=lambda x: x.get("_final_score", 0),
            reverse=True,
        )
        return collected[:limit]

    @staticmethod
    def _candidate_identity(candidate):
        return (candidate.get("uri", ""), candidate.get("level", 2), candidate.get("chunk_id", ""))

    async def _convert_to_matched_contexts(
        self,
        candidates: List[Dict[str, Any]],
        ctx: RequestContext,
        apply_hotness: bool = True,
    ) -> List[MatchedContext]:
        """Convert candidate results to MatchedContext list.

        Blends semantic similarity with a hotness score derived from
        ``active_count`` and ``updated_at`` when configured. The blend weight
        is controlled by ``retrieval.hotness_alpha`` (0 disables the boost).
        """
        results = []
        for c in candidates:
            # Fix: clamp inf/nan scores from vector search (#inf-score)
            semantic_score = self._finite_score(c.get("_final_score", c.get("_score", 0.0)))

            alpha = self.hotness_alpha
            if apply_hotness and alpha > 0:
                updated_at_raw = c.get("updated_at")
                if isinstance(updated_at_raw, str):
                    try:
                        updated_at_val = parse_iso_datetime(updated_at_raw)
                    except (ValueError, TypeError):
                        updated_at_val = None
                elif isinstance(updated_at_raw, datetime):
                    updated_at_val = updated_at_raw
                else:
                    updated_at_val = None

                h_score = hotness_score(
                    active_count=c.get("active_count", 0),
                    updated_at=updated_at_val,
                )
                final_score = (1 - alpha) * semantic_score + alpha * h_score
            else:
                final_score = semantic_score
            if not math.isfinite(final_score):
                final_score = 0.0
            level = c.get("level", 2)
            display_uri = self._append_level_suffix(c.get("uri", ""), level)
            abstract = c.get("abstract", "")
            if level in {ContextLevel.ABSTRACT, ContextLevel.OVERVIEW}:
                # New records persist body-only rerank scalars, but imported or
                # legacy indexes may still contain the full OKF document. Keep
                # the public find/search preview contract body-only at its final
                # conversion boundary. L2 user Markdown is intentionally left
                # untouched, including ordinary YAML frontmatter.
                try:
                    abstract = body_for_preview(abstract)
                except AbstractOverviewFormatError as exc:
                    logger.warning("Malformed sidecar in retrieval result %s: %s", display_uri, exc)
                    abstract = ""

            results.append(
                MatchedContext(
                    uri=display_uri,
                    context_type=ContextType(c["context_type"])
                    if c.get("context_type")
                    else ContextType.RESOURCE,
                    level=level,
                    abstract=abstract,
                    category=c.get("category", ""),
                    score=final_score,
                    search_tags=normalize_search_tags(c.get("search_tags"), discard_invalid=True),
                    type=str(c.get("type", "") or ""),
                    # The record's own declaration of what it is and where it
                    # came from, read back from the columns it was written to.
                    meta=meta_view(c.get("business_meta_payload")),
                    provenance=provenance_view(c),
                )
            )

        # Aggregate readable sources only after preserving individual child hits.
        grouped = {}
        for result, candidate in zip(results, candidates, strict=True):
            key = (result.uri, result.level)
            if candidate.get("chunk_id"):
                result.matches.append(
                    {
                        "chunk_id": candidate["chunk_id"],
                        "parent_chunk_id": candidate.get("parent_chunk_id", ""),
                        "text": candidate.get("chunk_text", result.abstract),
                        "parent_text": candidate.get("parent_text", ""),
                        "content_revision": candidate.get("content_revision", ""),
                        "image_context_uri": candidate.get("image_context_uri", ""),
                        "image_context_hash": candidate.get("image_context_hash", ""),
                        "score": result.score,
                    }
                )
            previous = grouped.get(key)
            if previous is None:
                grouped[key] = result
            else:
                previous.matches.extend(result.matches)
                # A directory summary row declares no business meta, so a group
                # that leads with it still has to answer with what its children
                # cite -- otherwise the record's own provenance is dropped by
                # whichever chunk happened to rank first.
                if not previous.meta:
                    previous.meta = result.meta
                if not previous.provenance:
                    previous.provenance = result.provenance
                if result.score > previous.score:
                    previous.score = result.score
                    previous.abstract = result.abstract
        results = list(grouped.values())
        # Re-sort by blended score so hotness boost can change ranking
        results.sort(key=lambda x: x.score, reverse=True)
        return results

    @classmethod
    def _append_level_suffix(cls, uri: str, level: int) -> str:
        """Return user-facing URI with L0/L1 suffix reconstructed by level."""
        suffix = cls.LEVEL_URI_SUFFIX.get(level)
        if not uri or not suffix:
            return uri
        if uri.endswith(f"/{suffix}"):
            return uri
        if uri.endswith("/.abstract.md") or uri.endswith("/.overview.md"):
            return uri
        if uri.endswith("/") and not uri.endswith("://"):
            uri = uri.rstrip("/")
        return f"{uri}/{suffix}"
