# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Unit tests for the pure RRF/rerank/MMR fusion pipeline (KB-017, F6).

These tests are backend-free on purpose: the whole point of
``openviking.retrieve.fusion`` being pure is that the ordering contract can be
proven without a vector store, a rerank provider or a network.
"""

import pytest

from openviking.retrieve import fusion as fx
from openviking.retrieve.fusion import (
    Candidate,
    FusionProfile,
    RerankRequiredError,
    aggregate_by_uri,
    apply_rerank,
    expand_parents,
    jaccard_similarity,
    mmr_select,
    normalize_scores,
    profile_for,
    reciprocal_rank_fusion,
    run_fusion,
)


def _c(uri, chunk_id="", text="", score=0.0, **kw):
    return Candidate(uri=uri, chunk_id=chunk_id, chunk_text=text, score=score, **kw)


# --------------------------------------------------------------------------- #
# RRF: rank positions, not raw scores
# --------------------------------------------------------------------------- #


def test_rrf_uses_rank_positions_not_raw_scores():
    """A huge raw score in one space must not outrank rank agreement."""
    dense = [
        _c("viking://a", "c1", score=999.0),
        _c("viking://b", "c2", score=998.0),
        _c("viking://c", "c3", score=0.001),
    ]
    lexical = [
        _c("viking://c", "c3", score=1e-9),
        _c("viking://a", "c1", score=1e-9),
        _c("viking://b", "c2", score=1e-9),
    ]
    fused = reciprocal_rank_fusion({"dense": dense, "lexical": lexical}, k=60)
    # c1: 1/61 + 1/62, c2: 1/62 + 1/63, c3: 1/63 + 1/61
    scores = {c.chunk_id: c.score for c in fused}
    assert scores["c1"] == pytest.approx(1 / 61 + 1 / 62)
    assert scores["c3"] == pytest.approx(1 / 63 + 1 / 61)
    assert scores["c2"] == pytest.approx(1 / 62 + 1 / 63)
    # The raw 999.0 and 1e-9 magnitudes are irrelevant; only positions matter.
    assert [c.chunk_id for c in fused] == ["c1", "c3", "c2"]


def test_rrf_score_is_rank_reciprocal_sum_for_multiple_paths():
    paths = {
        "dense": [_c("viking://x", "c1"), _c("viking://y", "c2")],
        "lexical": [_c("viking://x", "c1")],
    }
    fused = reciprocal_rank_fusion(paths, k=60)
    by_id = {c.chunk_id: c for c in fused}
    assert by_id["c1"].score == pytest.approx(1 / 61 + 1 / 61)
    assert by_id["c2"].score == pytest.approx(1 / 62)
    assert by_id["c1"].score_components["rrf_dense"] == pytest.approx(1 / 61)
    assert by_id["c1"].score_components["rrf_lexical"] == pytest.approx(1 / 61)


def test_rrf_constant_is_configurable():
    paths = {"dense": [_c("viking://x", "c1")]}
    assert reciprocal_rank_fusion(paths, k=1)[0].score == pytest.approx(1 / 2)
    assert reciprocal_rank_fusion(paths, k=10)[0].score == pytest.approx(1 / 11)
    with pytest.raises(ValueError):
        reciprocal_rank_fusion(paths, k=0)


def test_rrf_dedups_same_uri_different_chunks():
    """Same URI with distinct chunks are distinct candidates, not duplicates."""
    paths = {
        "dense": [_c("viking://doc", "c1"), _c("viking://doc", "c2")],
    }
    fused = reciprocal_rank_fusion(paths)
    assert len(fused) == 2
    assert {c.chunk_id for c in fused} == {"c1", "c2"}


def test_rrf_is_deterministic_across_path_insertion_order():
    a = [_c("viking://1", "c1"), _c("viking://2", "c2"), _c("viking://3", "c3")]
    b = [_c("viking://3", "c3"), _c("viking://2", "c2"), _c("viking://1", "c1")]
    forward = reciprocal_rank_fusion({"dense": a, "lexical": b})
    reverse = reciprocal_rank_fusion({"lexical": b, "dense": a})
    assert [(c.uri, c.chunk_id) for c in forward] == [(c.uri, c.chunk_id) for c in reverse]


# --------------------------------------------------------------------------- #
# Stable tie-breaking
# --------------------------------------------------------------------------- #


def test_rrf_tie_break_is_stable_and_first_seen_order():
    # All three appear once at rank 1 in their own path -> identical RRF score.
    paths = {
        "dense": [_c("viking://b", "c1")],
        "lexical": [_c("viking://a", "c1")],
        "image": [_c("viking://a", "c2")],
    }
    first = reciprocal_rank_fusion(paths)
    second = reciprocal_rank_fusion(paths)
    order = [(c.uri, c.chunk_id) for c in first]
    scores = {c.score for c in first}
    assert len(scores) == 1  # genuinely tied
    # Ties resolve to first-seen order, and paths are iterated in sorted name
    # order, so the result is identical run to run and insertion-order free.
    assert order == [(c.uri, c.chunk_id) for c in second]
    assert order == [("viking://b", "c1"), ("viking://a", "c2"), ("viking://a", "c1")]

    reinserted = reciprocal_rank_fusion(
        {"image": paths["image"], "lexical": paths["lexical"], "dense": paths["dense"]}
    )
    assert [(c.uri, c.chunk_id) for c in reinserted] == order


def test_mmr_tie_break_follows_input_order():
    cands = [_c("viking://a", "c1", score=1.0), _c("viking://b", "c2", score=1.0)]
    picked = mmr_select(cands, lambda_=1.0, top_k=2)
    assert [c.uri for c in picked] == ["viking://a", "viking://b"]


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #


def test_normalize_scores_maps_to_unit_range():
    assert normalize_scores([1.0, 2.0, 3.0]) == [0.0, 0.5, 1.0]
    assert normalize_scores([]) == []
    # Degenerate ranges collapse to 1.0 so MMR is driven by redundancy alone.
    assert normalize_scores([5.0, 5.0]) == [1.0, 1.0]
    assert normalize_scores([float("nan"), float("inf")]) == [1.0, 1.0]


def test_mmr_lambda_extremes():
    """lambda=1 is pure relevance; lambda=0 is pure diversity."""
    cands = [
        _c("viking://a", "c1", text="alpha beta gamma", score=1.0),
        _c("viking://b", "c2", text="alpha beta gamma", score=0.9),
        _c("viking://c", "c3", text="delta epsilon zeta", score=0.1),
    ]
    pure_relevance = mmr_select(cands, lambda_=1.0, top_k=3)
    assert [c.uri for c in pure_relevance] == ["viking://a", "viking://b", "viking://c"]

    pure_diversity = mmr_select(cands, lambda_=0.0, top_k=2)
    # First pick is the input head (all zero relevance), second must be the
    # maximally dissimilar one, not the near-duplicate b.
    assert [c.uri for c in pure_diversity] == ["viking://a", "viking://c"]


def test_mmr_rejects_out_of_range_lambda():
    with pytest.raises(ValueError):
        mmr_select([_c("viking://a")], lambda_=1.5, top_k=1)
    with pytest.raises(ValueError):
        mmr_select([_c("viking://a")], lambda_=-0.1, top_k=1)


def test_mmr_relevance_length_must_match():
    with pytest.raises(ValueError, match="length"):
        mmr_select([_c("viking://a"), _c("viking://b")], relevance=[1.0], top_k=2)


# --------------------------------------------------------------------------- #
# MMR improves diversity vs no MMR
# --------------------------------------------------------------------------- #


def test_mmr_runs_last_and_improves_diversity_over_no_mmr():
    """Constructed set: three near-duplicates outrank one distinct document."""
    dup = "充电系统故障排查 步骤 检查保险丝 检查继电器"
    cands = [
        _c("viking://dup1", "c1", text=dup, score=1.00),
        _c("viking://dup2", "c2", text=dup + " 补充", score=0.99),
        _c("viking://dup3", "c3", text=dup, score=0.98),
        _c("viking://distinct", "c4", text="液压泵压力异常 排查 溢流阀 主安全阀", score=0.95),
    ]
    no_mmr = [c.copy(rank=i + 1) for i, c in enumerate(cands)][:2]
    with_mmr = mmr_select(cands, lambda_=0.3, top_k=2)

    def _mean_pairwise_diversity(selection):
        pairs = [
            jaccard_similarity(a.text_for_similarity, b.text_for_similarity)
            for i, a in enumerate(selection)
            for b in selection[i + 1 :]
        ]
        return 1.0 - (sum(pairs) / len(pairs))

    assert _mean_pairwise_diversity(with_mmr) > _mean_pairwise_diversity(no_mmr)
    assert "viking://distinct" in [c.uri for c in with_mmr]


def test_mmr_applied_after_rerank_not_before():
    """Rerank must not be able to re-collapse the diversity MMR just produced."""
    dup = "同一段重复文本 内容 内容"
    cands = [
        _c("viking://a", "c1", text=dup, score=0.1),
        _c("viking://b", "c2", text=dup, score=0.2),
        _c("viking://c", "c3", text="完全不同的另一段内容", score=0.3),
    ]
    # A rerank that loves near-duplicates and promotes both a and b.
    outcome = apply_rerank(
        cands,
        rerank=lambda window: [0.9, 0.8, 0.1],
        profile=profile_for("hybrid", mmr_top=2),
    )
    assert outcome.applied is True
    # Post-rerank order is a, b, c — MMR then has to pull c back in.
    assert [c.uri for c in outcome.candidates] == ["viking://a", "viking://b", "viking://c"]
    selected = mmr_select(list(outcome.candidates), lambda_=0.0, top_k=2)
    assert [c.uri for c in selected] == ["viking://a", "viking://c"]


# --------------------------------------------------------------------------- #
# Cross-space redundancy is token/Jaccard, not vector math
# --------------------------------------------------------------------------- #


def test_jaccard_similarity_is_token_based():
    assert jaccard_similarity("alpha beta", "alpha beta") == 1.0
    assert jaccard_similarity("alpha beta", "gamma delta") == 0.0
    assert jaccard_similarity("", "alpha") == 0.0
    assert 0.0 < jaccard_similarity("alpha beta gamma", "alpha beta delta") < 1.0


def test_mmr_similarity_is_overridable_but_default_is_token_jaccard():
    """Default redundancy uses text tokens; scores alone must not drive it."""
    a = _c("viking://a", "c1", text="alpha", score=0.9)
    b = _c("viking://b", "c2", text="alpha", score=0.9)
    c = _c("viking://c", "c3", text="omega", score=0.9)
    # Equal relevance: only the near-duplicate pair should be penalized, so the
    # second pick is the token-disjoint document regardless of scores.
    selected = mmr_select([a, b, c], lambda_=0.7, top_k=2)
    assert [x.uri for x in selected] == ["viking://a", "viking://c"]


# --------------------------------------------------------------------------- #
# Rerank model-count validation + degradation
# --------------------------------------------------------------------------- #


def test_rerank_count_mismatch_degrades_to_fused_order():
    cands = [_c("viking://a", "c1", score=0.3), _c("viking://b", "c2", score=0.2)]
    outcome = apply_rerank(
        cands,
        rerank=lambda window: [0.9],  # wrong count
        profile=profile_for("hybrid"),
    )
    assert outcome.applied is False
    assert outcome.degraded is True
    assert "2" in outcome.reason and "1" in outcome.reason
    # Ordering is the incoming fused order, not a positional mis-association.
    assert [c.uri for c in outcome.candidates] == ["viking://a", "viking://b"]


def test_rerank_exception_degrades_to_fused_order():
    cands = [_c("viking://a", "c1", score=0.3), _c("viking://b", "c2", score=0.2)]

    def boom(_window):
        raise TimeoutError("rerank timed out")

    outcome = apply_rerank(cands, rerank=boom, profile=profile_for("hybrid"))
    assert outcome.degraded is True
    assert outcome.applied is False
    assert "timed out" in outcome.reason
    assert [c.uri for c in outcome.candidates] == ["viking://a", "viking://b"]


def test_strict_rerank_failure_raises_instead_of_degrading():
    cands = [_c("viking://a", "c1", score=0.3)]
    with pytest.raises(RerankRequiredError):
        apply_rerank(
            cands,
            rerank=lambda window: None,
            profile=profile_for("hybrid", strict_rerank=True),
        )
    with pytest.raises(RerankRequiredError):
        apply_rerank(
            cands,
            rerank=lambda window: [0.1, 0.2],
            profile=profile_for("hybrid", strict_rerank=True),
        )


def test_rerank_absent_is_not_a_degradation():
    cands = [_c("viking://a", "c1", score=0.3)]
    outcome = apply_rerank(cands, rerank=None, profile=profile_for("hybrid"))
    assert outcome.applied is False
    assert outcome.degraded is False


def test_rerank_success_resorts_by_rerank_score():
    cands = [
        _c("viking://a", "c1", score=0.9),
        _c("viking://b", "c2", score=0.1),
    ]
    outcome = apply_rerank(cands, rerank=lambda w: [0.05, 0.95], profile=profile_for("hybrid"))
    assert outcome.applied is True
    assert [c.uri for c in outcome.candidates] == ["viking://b", "viking://a"]
    assert outcome.candidates[0].score_components["rerank"] == 0.95


def test_rerank_window_respects_rerank_limit_and_keeps_remainder():
    cands = [_c(f"viking://{i}", f"c{i}", score=float(i)) for i in range(5)]
    seen = {}

    def rerank(window):
        seen["n"] = len(window)
        return [1.0] * len(window)

    outcome = apply_rerank(
        cands,
        rerank=rerank,
        profile=profile_for("hybrid", rerank_limit=2),
    )
    assert seen["n"] == 2
    assert len(outcome.candidates) == 5


# --------------------------------------------------------------------------- #
# Parent expansion
# --------------------------------------------------------------------------- #


def test_parent_expansion_respects_token_budget_in_rank_order():
    cands = [
        _c("viking://a", "c1", parent_text="one two three"),
        _c("viking://b", "c2", parent_text="four five six"),
    ]
    estimate = lambda text: len(text.split())  # noqa: E731
    expanded = expand_parents(cands, budget_tokens=3, estimate=estimate)
    assert expanded[0].parent_text == "one two three"
    # Budget exhausted -> later parent dropped, candidate itself retained.
    assert expanded[1].parent_text == ""
    assert len(expanded) == 2


def test_parent_expansion_disabled_when_budget_zero():
    cands = [_c("viking://a", "c1", parent_text="keep me")]
    assert expand_parents(cands, budget_tokens=0)[0].parent_text == "keep me"


# --------------------------------------------------------------------------- #
# URI aggregation keeps every chunk match
# --------------------------------------------------------------------------- #


def test_aggregate_by_uri_keeps_multiple_chunk_matches():
    cands = [
        _c("viking://doc", "c1", text="first chunk", score=0.9),
        _c("viking://doc", "c2", text="second chunk", score=0.7),
        _c("viking://other", "c3", text="other", score=0.5),
    ]
    groups = aggregate_by_uri(cands)
    assert [g["source_uri"] for g in groups] == ["viking://doc", "viking://other"]
    doc = groups[0]
    assert doc["score"] == 0.9
    assert [m["chunk_id"] for m in doc["matches"]] == ["c1", "c2"]
    assert [m["text"] for m in doc["matches"]] == ["first chunk", "second chunk"]
    assert [m["chunk_id"] for m in groups[1]["matches"]] == ["c3"]
    # A chunk-less candidate still produces a readable, openable group.
    chunkless = aggregate_by_uri([_c("viking://bare", text="body", score=0.4)])
    assert chunkless[0]["source_uri"] == "viking://bare"
    assert chunkless[0]["matches"] == []
    assert chunkless[0]["snippet"] == "body"


def test_aggregate_by_uri_preserves_namespace_id():
    cands = [_c("viking://doc", "c1", namespace_id="ns-42", text="x")]
    groups = aggregate_by_uri(cands)
    assert groups[0]["namespace_id"] == "ns-42"


# --------------------------------------------------------------------------- #
# Profile configuration + end-to-end pipeline
# --------------------------------------------------------------------------- #


def test_profile_defaults_match_spec_and_are_recorded_in_trace():
    profile = profile_for("hybrid")
    assert (profile.recall_limit, profile.fused_limit) == (100, 100)
    assert profile.rerank_limit == 50
    assert profile.mmr_top == 10
    assert profile.rrf_k == 60
    assert profile.mmr_lambda == 0.7
    trace = profile.as_trace()
    assert trace["rrf_k"] == 60
    assert trace["mmr_lambda"] == 0.7


def test_profile_rejects_unknown_name_and_bad_values():
    with pytest.raises(ValueError):
        FusionProfile(name="turbo")
    with pytest.raises(ValueError):
        FusionProfile(name="hybrid", rrf_k=0)
    with pytest.raises(ValueError):
        FusionProfile(name="hybrid", mmr_lambda=1.2)
    with pytest.raises(ValueError):
        FusionProfile(name="hybrid", mmr_top=0)


def test_run_fusion_records_degraded_and_effective_config():
    paths = {"dense": [_c("viking://a", "c1", text="alpha", score=0.9)]}
    result = run_fusion(
        paths,
        profile=profile_for("hybrid", mmr_top=1),
        rerank=lambda window: (_ for _ in ()).throw(RuntimeError("provider down")),
    )
    assert result.trace["degraded"] is True
    assert result.trace["rerank_applied"] is False
    assert result.trace["fusion"]["profile"] == "hybrid"
    assert result.trace["paths"] == {"dense": 1}
    assert [c.uri for c in result.selected] == ["viking://a"]


def test_run_fusion_is_deterministic_end_to_end():
    paths = {
        "dense": [_c("viking://a", "c1", text="x"), _c("viking://b", "c2", text="y")],
        "lexical": [_c("viking://b", "c2", text="y"), _c("viking://a", "c1", text="x")],
    }
    kwargs = dict(profile=profile_for("hybrid"), rerank=lambda w: [0.5] * len(w))
    first = run_fusion(paths, **kwargs)
    second = run_fusion(paths, **kwargs)
    assert [c.identity for c in first.selected] == [c.identity for c in second.selected]
    assert first.trace == second.trace


def test_run_fusion_applies_recall_and_fused_budgets():
    dense = [_c(f"viking://{i}", f"c{i}", text=f"t{i}") for i in range(10)]
    lexical = [_c(f"viking://l{i}", f"lc{i}", text=f"lt{i}") for i in range(10)]
    result = run_fusion(
        {"dense": dense, "lexical": lexical},
        profile=profile_for("hybrid", recall_limit=3, fused_limit=4, mmr_top=2),
    )
    assert result.trace["paths"] == {"dense": 3, "lexical": 3}
    assert result.trace["fused_count"] == 4
    assert len(result.selected) == 2


def test_with_row_budget_only_raises_caps_below_the_requested_limit():
    profile = profile_for("hybrid")
    assert profile.with_row_budget(5) is profile  # every cap already >= limit
    slight = profile.with_row_budget(20)
    assert slight.mmr_top == 20  # the only cap below the request
    assert (slight.recall_limit, slight.fused_limit, slight.rerank_limit) == (100, 100, 50)
    wide = profile.with_row_budget(120)
    assert (wide.recall_limit, wide.fused_limit, wide.rerank_limit, wide.mmr_top) == (
        120,
        120,
        120,
        120,
    )


def test_run_fusion_can_exceed_default_mmr_top_when_budget_is_raised():
    dense = [_c(f"viking://d{i}", f"c{i}", text=f"distinct text {i}") for i in range(15)]
    default = run_fusion({"dense": dense}, profile=profile_for("hybrid"))
    assert len(default.selected) == 10  # default MMR top caps the row count
    raised = run_fusion({"dense": dense}, profile=profile_for("hybrid").with_row_budget(15))
    assert len(raised.selected) == 15


def test_candidate_roundtrip_keeps_provenance_fields():
    result = {
        "uri": "viking://doc",
        "chunk_id": "c1",
        "parent_chunk_id": "p1",
        "namespace_id": "ns1",
        "chunk_text": "body",
        "abstract": "sum",
        "level": 2,
        "context_type": "resource",
        "_score": 0.42,
    }
    candidate = fx.candidate_from_result(result, rank=1)
    assert candidate.namespace_id == "ns1"
    assert candidate.parent_chunk_id == "p1"
    restored = candidate.as_dict()
    assert restored["uri"] == "viking://doc"
    assert restored["chunk_id"] == "c1"
    assert restored["parent_chunk_id"] == "p1"
    assert restored["namespace_id"] == "ns1"
    assert restored["_final_score"] == 0.42
    assert "score_components" in restored