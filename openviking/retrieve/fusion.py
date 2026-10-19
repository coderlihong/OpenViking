# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Pure fusion primitives for the unified retrieval profile (KB-017, F6).

This module implements the deterministic, network-free half of the unified
pipeline described by the enterprise KB spec, section 7.1::

    parallel recall -> RRF fusion + chunk dedup -> rerank -> MMR selection
    -> parent expansion -> URI aggregation

Everything here is pure: given the same inputs it returns the same output, so
the ordering contract (RRF rank-position math, stable tie-breaks, rerank
degradation, MMR normalization) is unit-testable without a backend.

Design constraints taken directly from the spec:

- RRF consumes *rank positions*, never raw scores. Scores coming from dense
  text space, dense image space and BM25 are not on a comparable scale, so
  adding them would silently privilege whichever space has the widest range.
- Ties break deterministically (fused score, first-seen order, URI, chunk id).
- Rerank failures degrade to the fused ordering and are marked ``degraded``;
  when the profile requires strict rerank the failure is raised instead.
- MMR runs at the *final* selection stage, after rerank, so a later rerank
  pass cannot re-collapse diversity. Relevance and redundancy are both
  normalized to ``[0, 1]`` before blending.
- Cross-space redundancy uses unified text/summary *token* similarity
  (token/Jaccard, borrowing the WeKnora ``internal/reranking/mmr.go`` idea).
  It is explicitly not a cross-space vector distance.

The lexical tokenizer is imported from the BM25 adapter so that similarity
uses the same version-locked segmentation as the inverted index.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:  # pragma: no cover - exercised implicitly through every MMR call
    from openviking.indexing.lexical import tokenize as _lexical_tokenize
except Exception:  # pragma: no cover - defensive fallback if the adapter moves
    _lexical_tokenize = None

DEFAULT_RRF_K = 60
DEFAULT_MMR_LAMBDA = 0.7
DEFAULT_RECALL_LIMIT = 100
DEFAULT_FUSED_LIMIT = 100
DEFAULT_RERANK_LIMIT = 50
DEFAULT_MMR_TOP = 10

PROFILE_QUICK = "quick"
PROFILE_THINKING = "thinking"
PROFILE_HYBRID = "hybrid"
SUPPORTED_PROFILES = (PROFILE_QUICK, PROFILE_THINKING, PROFILE_HYBRID)


class FusionError(RuntimeError):
    """Base class for fusion failures."""


class RerankRequiredError(FusionError):
    """Raised when a strict profile cannot obtain valid rerank scores."""


# --------------------------------------------------------------------------- #
# Profile configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FusionProfile:
    """Candidate budgets and fusion knobs for one retrieval profile.

    Defaults match the spec: each recall path 100, fused 100, rerank 50,
    MMR top 10, RRF constant 60, MMR lambda 0.7. All values are recorded in the
    trace so an effective configuration can be audited per query.
    """

    name: str = PROFILE_HYBRID
    recall_limit: int = DEFAULT_RECALL_LIMIT
    fused_limit: int = DEFAULT_FUSED_LIMIT
    rerank_limit: int = DEFAULT_RERANK_LIMIT
    mmr_top: int = DEFAULT_MMR_TOP
    rrf_k: int = DEFAULT_RRF_K
    mmr_lambda: float = DEFAULT_MMR_LAMBDA
    strict_rerank: bool = False
    parent_token_budget: int = 0

    def __post_init__(self) -> None:
        if self.name not in SUPPORTED_PROFILES:
            raise ValueError(
                f"Unknown retrieval profile {self.name!r}; "
                f"expected one of {list(SUPPORTED_PROFILES)}"
            )
        for label, value in (
            ("recall_limit", self.recall_limit),
            ("fused_limit", self.fused_limit),
            ("rerank_limit", self.rerank_limit),
            ("mmr_top", self.mmr_top),
        ):
            if value < 1:
                raise ValueError(f"FusionProfile.{label} must be >= 1, got {value}")
        if self.rrf_k < 1:
            raise ValueError(f"FusionProfile.rrf_k must be >= 1, got {self.rrf_k}")
        if not 0.0 <= self.mmr_lambda <= 1.0:
            raise ValueError(f"FusionProfile.mmr_lambda must be in [0, 1], got {self.mmr_lambda}")
        if self.parent_token_budget < 0:
            raise ValueError("FusionProfile.parent_token_budget must be >= 0")

    def as_trace(self) -> Dict[str, Any]:
        """Serializable view of the effective configuration (spec: record trace)."""
        return {
            "profile": self.name,
            "recall_limit": self.recall_limit,
            "fused_limit": self.fused_limit,
            "rerank_limit": self.rerank_limit,
            "mmr_top": self.mmr_top,
            "rrf_k": self.rrf_k,
            "mmr_lambda": self.mmr_lambda,
            "strict_rerank": self.strict_rerank,
            "parent_token_budget": self.parent_token_budget,
        }

    def with_row_budget(self, limit: int) -> "FusionProfile":
        """Return a profile whose stage budgets can carry ``limit`` rows.

        Any per-stage cap below the requested row count silently truncates a
        larger request (MMR top defaults to 10), so those caps are raised to
        ``limit``; caps already above it stay untouched.
        """
        smallest = min(self.recall_limit, self.fused_limit, self.rerank_limit, self.mmr_top)
        if limit <= smallest:
            return self
        return replace(
            self,
            recall_limit=max(self.recall_limit, limit),
            fused_limit=max(self.fused_limit, limit),
            rerank_limit=max(self.rerank_limit, limit),
            mmr_top=max(self.mmr_top, limit),
        )


def profile_for(name: str, **overrides: Any) -> FusionProfile:
    """Build a profile by name, applying explicit budget overrides."""
    return FusionProfile(name=name, **overrides)


# --------------------------------------------------------------------------- #
# Candidate model
# --------------------------------------------------------------------------- #


@dataclass
class Candidate:
    """One recallable unit flowing through the fusion pipeline.

    ``uri`` is the *original* citation URI (the thing a client opens with the
    existing Viking content read API); ``chunk_id`` identifies the specific
    child chunk. ``namespace_id`` is carried through untouched so provenance
    survives every stage.
    """

    uri: str
    chunk_id: str = ""
    parent_chunk_id: str = ""
    level: int = 2
    context_type: str = "resource"
    abstract: str = ""
    chunk_text: str = ""
    parent_text: str = ""
    namespace_id: str = ""
    type: str = ""
    score: float = 0.0
    rank: int = 0
    score_components: Dict[str, float] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def identity(self) -> Tuple[str, str]:
        """Dedup key: the same URI may legitimately hold many chunks."""
        return (self.uri, self.chunk_id)

    @property
    def text_for_similarity(self) -> str:
        """Unified text/summary used for redundancy (never a space-specific vector)."""
        return (self.chunk_text or self.abstract or "").strip()

    def copy(self, **changes: Any) -> "Candidate":
        return replace(self, **changes)

    def as_dict(self) -> Dict[str, Any]:
        """Render back to the dict shape the retriever/vector layer expects."""
        data: Dict[str, Any] = {
            "uri": self.uri,
            "level": self.level,
            "context_type": self.context_type,
            "abstract": self.abstract,
            "_score": self.score,
            "_final_score": self.score,
            "score_components": dict(self.score_components),
        }
        if self.chunk_id:
            data["chunk_id"] = self.chunk_id
        if self.parent_chunk_id:
            data["parent_chunk_id"] = self.parent_chunk_id
        if self.chunk_text:
            data["chunk_text"] = self.chunk_text
        if self.parent_text:
            data["parent_text"] = self.parent_text
        if self.namespace_id:
            data["namespace_id"] = self.namespace_id
        if self.type:
            data["type"] = self.type
        data.update(self.extra)
        return data


def candidate_from_result(
    result: Mapping[str, Any],
    *,
    rank: int = 0,
    path_score: Optional[float] = None,
) -> Candidate:
    """Adapt a vector/lexical backend row into a :class:`Candidate`."""
    score = result.get("_score", 0.0)
    if path_score is not None:
        score = path_score
    score_components: Dict[str, float] = dict(result.get("score_components") or {})
    extra = {
        key: value
        for key, value in result.items()
        if key
        not in {
            "uri",
            "chunk_id",
            "parent_chunk_id",
            "chunk_text",
            "parent_text",
            "level",
            "context_type",
            "abstract",
            "namespace_id",
            "type",
            "_score",
            "_final_score",
            "score_components",
        }
    }
    return Candidate(
        uri=str(result.get("uri", "")),
        chunk_id=str(result.get("chunk_id", "") or ""),
        parent_chunk_id=str(result.get("parent_chunk_id", "") or ""),
        level=int(result.get("level", 2) or 2),
        context_type=str(result.get("context_type", "resource") or "resource"),
        abstract=str(result.get("abstract", "") or ""),
        chunk_text=str(result.get("chunk_text", "") or ""),
        parent_text=str(result.get("parent_text", "") or ""),
        namespace_id=str(result.get("namespace_id", "") or ""),
        type=str(result.get("type", "") or ""),
        score=float(score) if _is_finite(score) else 0.0,
        rank=rank,
        score_components=score_components,
        extra=extra,
    )


def _is_finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# RRF fusion
# --------------------------------------------------------------------------- #


def reciprocal_rank_fusion(
    ranked_paths: Mapping[str, Sequence[Candidate]],
    *,
    k: int = DEFAULT_RRF_K,
    limit: Optional[int] = None,
) -> List[Candidate]:
    """Fuse ranked lists using Reciprocal Rank Fusion over rank positions.

    ``score = sum over paths of 1 / (k + rank)`` where ``rank`` is the 1-based
    position inside that path. Raw scores are never added across paths, which
    is the whole point of RRF: a BM25 score and a cosine similarity are not
    comparable quantities.

    Candidates are deduplicated by ``(uri, chunk_id)``; a candidate appearing in
    several paths accumulates contributions from each. Ordering is fully
    deterministic: fused score desc, then first-seen order, then URI, then
    chunk id.
    """
    if k < 1:
        raise ValueError(f"RRF constant must be >= 1, got {k}")

    fused_scores: Dict[Tuple[str, str], float] = {}
    per_path: Dict[Tuple[str, str], Dict[str, float]] = {}
    first_seen: Dict[Tuple[str, str], int] = {}
    winners: Dict[Tuple[str, str], Candidate] = {}
    counter = 0

    # Iterate paths in sorted name order so fusion does not depend on the
    # caller's dict insertion order.
    for path_name in sorted(ranked_paths):
        seen_in_path: set = set()
        for position, candidate in enumerate(ranked_paths[path_name], start=1):
            key = candidate.identity
            if not candidate.uri or key in seen_in_path:
                continue
            seen_in_path.add(key)
            contribution = 1.0 / (k + position)
            fused_scores[key] = fused_scores.get(key, 0.0) + contribution
            per_path.setdefault(key, {})[path_name] = contribution
            if key not in first_seen:
                first_seen[key] = counter
                counter += 1
            # Keep the richest record for the key (first path wins on ties).
            existing = winners.get(key)
            if existing is None or len(candidate.text_for_similarity) > len(
                existing.text_for_similarity
            ):
                merged = candidate.copy()
                merged.extra = {**candidate.extra, **(existing.extra if existing else {})}
                winners[key] = merged

    ordered = sorted(
        fused_scores,
        key=lambda key: (
            -fused_scores[key],
            first_seen[key],
            key[0],
            key[1],
        ),
    )

    results: List[Candidate] = []
    for index, key in enumerate(ordered):
        base = winners[key]
        components = dict(base.score_components)
        for path_name, value in sorted(per_path[key].items()):
            components[f"rrf_{path_name}"] = value
        components["rrf"] = fused_scores[key]
        results.append(
            base.copy(
                score=fused_scores[key],
                rank=index + 1,
                score_components=components,
            )
        )
    if limit is not None:
        results = results[:limit]
    return results


def normalize_fused_scores(
    candidates: Sequence[Candidate],
    *,
    path_count: int,
    k: int = DEFAULT_RRF_K,
) -> List[Candidate]:
    """Scale raw RRF sums into ``(0, 1]`` so rank-only scores stay usable.

    Raw ``sum(1/(k+rank))`` lives in a dust domain (~0.016-0.033 for k=60) that
    no consumer can threshold and no operator can compare across queries. Dividing
    by the theoretical maximum -- every non-empty path ranking the candidate
    first, ``path_count / (k + 1)`` -- keeps the fused ordering exactly (positive
    scaling) while bounding the score: topping every path maps to 1.0, and a
    single-source rank-1 hit with two paths maps to 0.5. The raw sum stays in
    ``score_components["rrf"]`` for debugging.

    This is a rank-position domain, not a query-relevance domain: only the
    reranker measures relevance. Call it whenever the pipeline will serve fused
    scores (rerank unavailable or not requested).
    """
    if not candidates or path_count <= 0 or k < 1:
        return list(candidates)
    ceiling = path_count / (k + 1)
    if ceiling <= 0:
        return list(candidates)
    return [
        candidate.copy(score=candidate.score / ceiling) if _is_finite(candidate.score) else candidate
        for candidate in candidates
    ]


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #


def normalize_scores(scores: Sequence[float]) -> List[float]:
    """Min-max normalize into ``[0, 1]`` so relevance and redundancy compare.

    Degenerate input (empty, all equal, non-finite) maps to ``1.0`` for every
    element: all candidates are equally relevant, so with ``lambda < 1`` the
    MMR step is then driven purely by redundancy.
    """
    values = [float(s) if _is_finite(s) else 0.0 for s in scores]
    if not values:
        return []
    low = min(values)
    high = max(values)
    if high <= low:
        return [1.0] * len(values)
    spread = high - low
    return [(value - low) / spread for value in values]


# --------------------------------------------------------------------------- #
# MMR (final selection stage)
# --------------------------------------------------------------------------- #


def token_set(text: str) -> frozenset:
    """Unified token set for redundancy: version-locked lexical tokenization.

    Falls back to a small local tokenizer only if the BM25 adapter is
    unavailable, so fusion stays importable in isolation.
    """
    if not text:
        return frozenset()
    if _lexical_tokenize is not None:
        return frozenset(_lexical_tokenize(text))
    lowered = text.lower()
    return frozenset(part for part in lowered.replace("\n", " ").split(" ") if part)


def jaccard_similarity(left: str, right: str) -> float:
    """Token/Jaccard similarity in ``[0, 1]`` (WeKnora MMR token idea)."""
    left_tokens = token_set(left)
    right_tokens = token_set(right)
    if not left_tokens or not right_tokens:
        return 0.0
    union = left_tokens | right_tokens
    if not union:
        return 0.0
    return len(left_tokens & right_tokens) / len(union)


def mmr_select(
    candidates: Sequence[Candidate],
    *,
    lambda_: float = DEFAULT_MMR_LAMBDA,
    top_k: int = DEFAULT_MMR_TOP,
    relevance: Optional[Sequence[float]] = None,
    similarity: Callable[[Candidate, Candidate], float] = lambda a, b: jaccard_similarity(
        a.text_for_similarity, b.text_for_similarity
    ),
) -> List[Candidate]:
    """Maximal Marginal Relevance selection over the final candidate set.

    ``MMR = lambda * rel_norm(c) - (1 - lambda) * max_sim(c, selected)``.

    Both terms live in ``[0, 1]``: relevance is min-max normalized from the
    incoming order (fused or reranked score), redundancy is token/Jaccard over
    unified text/summary. ``lambda=1`` reproduces pure relevance order;
    ``lambda=0`` picks the most diverse set. Ties fall back to input order,
    which is already deterministic.
    """
    if not 0.0 <= lambda_ <= 1.0:
        raise ValueError(f"MMR lambda must be in [0, 1], got {lambda_}")
    if top_k < 1:
        raise ValueError(f"MMR top_k must be >= 1, got {top_k}")
    if not candidates:
        return []

    if relevance is None:
        raw = [candidate.score for candidate in candidates]
    else:
        if len(relevance) != len(candidates):
            raise ValueError(
                f"MMR relevance length {len(relevance)} does not match "
                f"candidate count {len(candidates)}"
            )
        raw = list(relevance)
    normalized = normalize_scores(raw)

    remaining = list(range(len(candidates)))
    selected: List[Candidate] = []
    selected_indices: List[int] = []

    while remaining and len(selected) < top_k:
        best_position = 0
        best_score = -math.inf
        for position, index in enumerate(remaining):
            redundancy = 0.0
            for chosen in selected_indices:
                redundancy = max(redundancy, similarity(candidates[index], candidates[chosen]))
            score = lambda_ * normalized[index] - (1.0 - lambda_) * redundancy
            if score > best_score:
                best_score = score
                best_position = position
        chosen_index = remaining.pop(best_position)
        selected_indices.append(chosen_index)
        selected.append(candidates[chosen_index].copy(rank=len(selected) + 1))
    return selected


# --------------------------------------------------------------------------- #
# Rerank with model-count validation and degradation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RerankOutcome:
    """Result of a rerank attempt, including the degradation decision."""

    candidates: Tuple[Candidate, ...]
    applied: bool
    degraded: bool
    reason: str = ""

    def as_trace(self) -> Dict[str, Any]:
        return {
            "rerank_applied": self.applied,
            "degraded": self.degraded,
            "rerank_reason": self.reason,
        }


def apply_rerank(
    candidates: Sequence[Candidate],
    *,
    rerank: Optional[Callable[[Sequence[Candidate]], Optional[Sequence[float]]]],
    profile: FusionProfile,
    limit: Optional[int] = None,
) -> RerankOutcome:
    """Rerank the fused candidates, degrading deterministically on failure.

    ``rerank`` receives the candidate window and must return one score per
    candidate in the same order, or ``None`` to signal unavailability. A wrong
    count is rejected rather than zipped positionally, which is exactly the
    failure mode TC-6.6 forbids ("cannot mis-associate candidates").

    On failure: strict profiles raise :class:`RerankRequiredError`; permissive
    profiles keep the fused ordering and set ``degraded=True``.
    """
    window_limit = profile.rerank_limit if limit is None else limit
    window = list(candidates[:window_limit])
    remainder = list(candidates[window_limit:])
    if not window:
        return RerankOutcome(candidates=tuple(candidates), applied=False, degraded=False)

    if rerank is None:
        # Rerank was never requested for this profile (e.g. no client
        # configured). That is not a degradation, it is the configured path.
        return RerankOutcome(candidates=tuple(candidates), applied=False, degraded=False)

    failure_reason = ""
    scores: Optional[Sequence[float]] = None
    try:
        scores = rerank(window)
    except Exception as exc:  # noqa: BLE001 - degrade on any provider failure
        failure_reason = f"rerank failed: {exc}"
    else:
        if scores is None:
            failure_reason = "rerank unavailable"
        elif len(scores) != len(window):
            failure_reason = f"rerank returned {len(scores)} scores for {len(window)} candidates"
            scores = None

    if scores is None:
        if profile.strict_rerank:
            raise RerankRequiredError(failure_reason or "rerank unavailable")
        return RerankOutcome(
            candidates=tuple(candidates),
            applied=False,
            degraded=True,
            reason=failure_reason,
        )

    rescored: List[Candidate] = []
    for index, (candidate, score) in enumerate(zip(window, scores, strict=True)):
        value = float(score) if _is_finite(score) else candidate.score
        components = dict(candidate.score_components)
        components["rerank"] = value
        components["fused"] = candidate.score
        rescored.append(
            candidate.copy(score=value, rank=index + 1, score_components=components)
        )
    # Deterministic re-sort: rerank score desc, then fused order, then identity.
    rescored.sort(key=lambda c: (-c.score, c.rank, c.uri, c.chunk_id))
    for index, candidate in enumerate(rescored):
        rescored[index] = candidate.copy(rank=index + 1)
    return RerankOutcome(
        candidates=tuple(rescored + remainder),
        applied=True,
        degraded=False,
    )


# --------------------------------------------------------------------------- #
# Parent expansion
# --------------------------------------------------------------------------- #


def expand_parents(
    candidates: Sequence[Candidate],
    *,
    budget_tokens: int = 0,
    estimate: Optional[Callable[[str], int]] = None,
) -> List[Candidate]:
    """Attach parent context in rank order while staying inside a token budget.

    ``budget_tokens <= 0`` disables expansion. Candidates without parent text
    pass through unchanged. Greedy in rank order so the outcome is
    deterministic and the highest-ranked hit always keeps its parent.
    """
    if budget_tokens <= 0:
        return list(candidates)
    if estimate is None:  # pragma: no cover - retriever always injects this
        estimate = lambda text: len(text)  # noqa: E731

    remaining = budget_tokens
    expanded: List[Candidate] = []
    for candidate in candidates:
        parent_text = candidate.parent_text
        if not parent_text:
            expanded.append(candidate)
            continue
        cost = estimate(parent_text)
        if cost <= remaining:
            remaining -= cost
            expanded.append(
                candidate.copy(
                    extra={**candidate.extra, "parent_expanded": True},
                )
            )
        else:
            expanded.append(candidate.copy(parent_text=""))
    return expanded


# --------------------------------------------------------------------------- #
# URI aggregation + provenance
# --------------------------------------------------------------------------- #


def aggregate_by_uri(candidates: Sequence[Candidate]) -> List[Dict[str, Any]]:
    """Aggregate chunk hits by original URI while keeping every match.

    The spec requires a client to open ``source_uri`` with the existing content
    API and still see the relevant chunks, so grouping must not collapse
    ``matches[]``. Groups keep first-seen order (already rank-ordered) and the
    group score is the best member score.
    """
    groups: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for candidate in candidates:
        if not candidate.uri:
            continue
        group = groups.get(candidate.uri)
        if group is None:
            group = {
                "source_uri": candidate.uri,
                "namespace_id": candidate.namespace_id,
                "level": candidate.level,
                "context_type": candidate.context_type,
                "snippet": candidate.abstract or candidate.chunk_text,
                "score": candidate.score,
                "score_components": dict(candidate.score_components),
                "matches": [],
            }
            groups[candidate.uri] = group
            order.append(candidate.uri)
        elif candidate.namespace_id and not group["namespace_id"]:
            group["namespace_id"] = candidate.namespace_id
        if candidate.chunk_id:
            group["matches"].append(
                {
                    "chunk_id": candidate.chunk_id,
                    "parent_chunk_id": candidate.parent_chunk_id,
                    "text": candidate.chunk_text or candidate.abstract,
                    "parent_text": candidate.parent_text,
                    "score": candidate.score,
                    "score_components": dict(candidate.score_components),
                }
            )
        if candidate.score > group["score"]:
            group["score"] = candidate.score
            group["snippet"] = candidate.abstract or candidate.chunk_text
    return [groups[uri] for uri in order]


# --------------------------------------------------------------------------- #
# Unified pipeline
# --------------------------------------------------------------------------- #


@dataclass
class FusionResult:
    """Output of :func:`run_fusion`, with the effective config for the trace."""

    selected: List[Candidate]
    fused: List[Candidate]
    aggregated: List[Dict[str, Any]]
    trace: Dict[str, Any]


def run_fusion(
    ranked_paths: Mapping[str, Sequence[Candidate]],
    *,
    profile: FusionProfile,
    rerank: Optional[Callable[[Sequence[Candidate]], Optional[Sequence[float]]]] = None,
    lambda_: Optional[float] = None,
    estimate: Optional[Callable[[str], int]] = None,
    apply_mmr: bool = True,
) -> FusionResult:
    """Run the whole pure pipeline: RRF -> rerank -> MMR -> expand -> aggregate.

    This is the single orchestration point used by the retriever's hybrid
    profile. It performs no I/O and no network calls; the rerank callable is
    injected by the caller.
    """
    per_path = {
        name: list(paths)[: profile.recall_limit] for name, paths in ranked_paths.items()
    }
    fused = reciprocal_rank_fusion(per_path, k=profile.rrf_k, limit=profile.fused_limit)
    # Without a reranker the served score would be raw RRF dust (~0.02x), which
    # no threshold can consume; rescale it into a bounded rank-position domain.
    # When the reranker applies it overwrites these scores with real relevance.
    fused = normalize_fused_scores(
        fused,
        path_count=sum(1 for path in per_path.values() if path),
        k=profile.rrf_k,
    )

    outcome = apply_rerank(fused, rerank=rerank, profile=profile)
    ranked = list(outcome.candidates)

    effective_lambda = profile.mmr_lambda if lambda_ is None else lambda_
    if apply_mmr:
        selected = mmr_select(ranked, lambda_=effective_lambda, top_k=profile.mmr_top)
    else:
        selected = [c.copy(rank=index + 1) for index, c in enumerate(ranked[: profile.mmr_top])]

    expanded = expand_parents(
        selected,
        budget_tokens=profile.parent_token_budget,
        estimate=estimate,
    )

    trace: Dict[str, Any] = {
        "fusion": profile.as_trace(),
        "rrf_k": profile.rrf_k,
        "mmr_lambda": effective_lambda,
        "mmr_applied": apply_mmr,
        "paths": {name: len(path) for name, path in sorted(per_path.items())},
        "fused_count": len(fused),
        "selected_count": len(selected),
        # What domain the served ``score`` is in: the reranker's 0-1 relevance,
        # or the normalized rank-fusion scale (order-preserving, not relevance).
        "score_domain": "rerank" if outcome.applied else "normalized_rrf",
    }
    trace.update(outcome.as_trace())

    return FusionResult(
        selected=expanded,
        fused=fused,
        aggregated=aggregate_by_uri(expanded),
        trace=trace,
    )


def to_candidate_list(results: Iterable[Mapping[str, Any]]) -> List[Candidate]:
    """Adapt a sequence of backend rows, preserving their incoming order."""
    return [candidate_from_result(result, rank=index + 1) for index, result in enumerate(results)]


__all__ = [
    "DEFAULT_MMR_LAMBDA",
    "DEFAULT_MMR_TOP",
    "DEFAULT_RECALL_LIMIT",
    "DEFAULT_RERANK_LIMIT",
    "DEFAULT_RRF_K",
    "DEFAULT_FUSED_LIMIT",
    "PROFILE_HYBRID",
    "PROFILE_QUICK",
    "PROFILE_THINKING",
    "SUPPORTED_PROFILES",
    "Candidate",
    "FusionError",
    "FusionProfile",
    "FusionResult",
    "RerankOutcome",
    "RerankRequiredError",
    "aggregate_by_uri",
    "apply_rerank",
    "candidate_from_result",
    "expand_parents",
    "jaccard_similarity",
    "mmr_select",
    "normalize_fused_scores",
    "normalize_scores",
    "profile_for",
    "reciprocal_rank_fusion",
    "run_fusion",
    "to_candidate_list",
    "token_set",
]