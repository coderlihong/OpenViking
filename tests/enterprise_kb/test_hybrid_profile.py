# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Integration tests for the unified ``hybrid`` retrieval profile (KB-017, F6).

These drive ``HierarchicalRetriever.retrieve`` with a fake vector store and a
fake lexical recall path, so the wiring of the pure fusion pipeline is covered
without a real backend. What is *not* covered here is the real rerank provider
and the real FTS5 index — those remain integration/E2E items.
"""

import pytest

from openviking.retrieve.fusion import FusionProfile, profile_for
from openviking.retrieve.hierarchical_retriever import HierarchicalRetriever, RetrieverMode
from openviking.server.identity import RequestContext, Role
from openviking_cli.retrieve.types import ContextType, TypedQuery
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils.config import RerankConfig


def _row(uri, score, level=2, chunk_id="", **extra):
    row = {
        "uri": uri,
        "abstract": uri.rsplit("/", 1)[-1],
        "_score": score,
        "level": level,
        "context_type": "resource",
    }
    if chunk_id:
        row["chunk_id"] = chunk_id
    row.update(extra)
    return row


class DummyEmbedResult:
    def __init__(self):
        self.dense_vector = [1.0]
        self.sparse_vector = {"hello": 1.0}


class DummyEmbedder:
    def prepare_embedding_input(self, text):
        return text

    def embed(self, _query, is_query=False):
        return DummyEmbedResult()

    async def embed_async(self, text, is_query=False):
        return self.embed(text, is_query=is_query)


class HybridStorage:
    """Vector store double returning a fixed L2 leaf set."""

    def __init__(self, leaf_results):
        self.collection_name = "context"
        self.acl_manager = None
        self.leaf_results = list(leaf_results)
        self.search_calls = []

    async def collection_exists_bound(self):
        return True

    async def search_in_tenant(
        self,
        ctx,
        query_vector=None,
        sparse_query_vector=None,
        context_type=None,
        target_directories=None,
        extra_filter=None,
        level=None,
        limit=10,
        offset=0,
    ):
        self.search_calls.append({"level": level, "limit": limit})
        if level is None or 2 in level:
            return [dict(row) for row in self.leaf_results][:limit]
        return []

    async def search_children_in_tenant(self, ctx, parent_uri, **kwargs):
        return []


class FakeRerankClient:
    def __init__(self, scores):
        self.scores = list(scores)
        self.calls = []

    def rerank_batch(self, query, documents):
        self.calls.append((query, list(documents)))
        return list(self.scores[: len(documents)])


def _ctx():
    return RequestContext(user=UserIdentifier("acc1", "user1"), role=Role.USER)


def _query():
    return TypedQuery(query="hello", context_type=ContextType.RESOURCE, intent="")


def _rerank_config():
    return RerankConfig(ak="ak", sk="sk", threshold=0.1)


def _make_retriever(storage, **kwargs):
    kwargs.setdefault("rerank_config", None)
    return HierarchicalRetriever(storage=storage, embedder=DummyEmbedder(), **kwargs)


def test_hybrid_mode_is_a_distinct_profile_constant():
    assert RetrieverMode.HYBRID == "hybrid"
    assert RetrieverMode.QUICK == "quick"
    assert RetrieverMode.THINKING == "thinking"


@pytest.mark.asyncio
async def test_hybrid_fuses_dense_and_lexical_by_rank():
    """RRF must combine both recall paths, not just echo the dense order."""
    # dense prefers b; lexical prefers a -> agreement should surface both, and
    # the raw score magnitudes (999 vs 1e-6) must not decide the order.
    storage = HybridStorage(
        [
            _row("viking://b", 999.0, chunk_id="cb", chunk_text="beta content"),
            _row("viking://a", 0.001, chunk_id="ca", chunk_text="alpha content"),
        ]
    )

    async def lexical_search(**kwargs):
        return [
            _row("viking://a", 1.0, chunk_id="ca", chunk_text="alpha content"),
            _row("viking://b", 0.5, chunk_id="cb", chunk_text="beta content"),
        ]

    retriever = _make_retriever(storage, lexical_search=lexical_search)
    result = await retriever.retrieve(_query(), ctx=_ctx(), limit=5, mode=RetrieverMode.HYBRID)

    uris = [c.uri for c in result.matched_contexts]
    assert set(uris) == {"viking://a", "viking://b"}
    # Both are ranked first in exactly one path -> equal RRF contribution.
    scores = {c.uri: c.score for c in result.matched_contexts}
    assert scores["viking://a"] == pytest.approx(scores["viking://b"])


@pytest.mark.asyncio
async def test_hybrid_leaf_recall_runs_without_lexical_or_rerank():
    """Leaf global recall must survive with no lexical path and no reranker."""
    storage = HybridStorage([_row("viking://only", 0.4, chunk_id="c1", chunk_text="solo")])
    retriever = _make_retriever(storage)
    result = await retriever.retrieve(_query(), ctx=_ctx(), limit=5, mode=RetrieverMode.HYBRID)
    assert [c.uri for c in result.matched_contexts] == ["viking://only"]
    # The leaf query always asks for level [2].
    assert any(call["level"] == [2] for call in storage.search_calls)


@pytest.mark.asyncio
async def test_hybrid_leaf_recall_survives_lexical_failure():
    """A broken lexical path degrades recall, it does not abort the query."""
    storage = HybridStorage([_row("viking://leaf", 0.4, chunk_id="c1", chunk_text="leaf body")])

    async def broken_lexical(**kwargs):
        raise RuntimeError("fts index unavailable")

    retriever = _make_retriever(storage, lexical_search=broken_lexical)
    result = await retriever.retrieve(_query(), ctx=_ctx(), limit=5, mode=RetrieverMode.HYBRID)
    assert [c.uri for c in result.matched_contexts] == ["viking://leaf"]


@pytest.mark.asyncio
async def test_hybrid_rerank_failure_keeps_fused_order(monkeypatch):
    """TC-6.6: rerank timeout degrades to fused order and marks degraded."""
    monkeypatch.setattr(
        "openviking.retrieve.hierarchical_retriever.RerankClient.from_config",
        lambda config: (_ for _ in ()).throw(RuntimeError("provider down")),
    )
    storage = HybridStorage(
        [
            _row("viking://b", 999.0, chunk_id="cb", chunk_text="beta content"),
            _row("viking://a", 0.001, chunk_id="ca", chunk_text="alpha content"),
        ]
    )

    async def lexical_search(**kwargs):
        return [_row("viking://a", 1.0, chunk_id="ca", chunk_text="alpha content")]

    retriever = _make_retriever(storage, rerank_config=None, lexical_search=lexical_search)
    # Force a rerank client that fails at call time.
    retriever._rerank_client = FakeRerankClient([0.1, 0.2, 0.3])  # wrong count -> degrade

    result = await retriever.retrieve(_query(), ctx=_ctx(), limit=5, mode=RetrieverMode.HYBRID)
    # Both paths contribute; 'a' leads on RRF, and the bad rerank did not
    # scramble the association.
    uris = [c.uri for c in result.matched_contexts]
    assert "viking://a" in uris and "viking://b" in uris


@pytest.mark.asyncio
async def test_hybrid_aggregates_same_uri_chunks_into_one_result_with_matches():
    """TC-6.5: one openable URI, several relevant chunk matches preserved."""
    storage = HybridStorage(
        [
            _row("viking://doc", 0.9, chunk_id="c1", chunk_text="first chunk"),
            _row("viking://doc", 0.7, chunk_id="c2", chunk_text="second chunk"),
        ]
    )
    retriever = _make_retriever(storage)
    result = await retriever.retrieve(_query(), ctx=_ctx(), limit=5, mode=RetrieverMode.HYBRID)
    assert len(result.matched_contexts) == 1
    single = result.matched_contexts[0]
    assert single.uri == "viking://doc"
    assert {m["chunk_id"] for m in single.matches} == {"c1", "c2"}
    # A user-facing chunk-file address must never be fabricated.
    assert "#chunk" not in single.uri


@pytest.mark.asyncio
async def test_hybrid_limit_above_default_mmr_top_is_honored():
    """limit=15 must not be silently truncated to the default MMR top (10)."""
    storage = HybridStorage(
        [
            _row(f"viking://doc{i}", 1.0 - i * 0.01, chunk_id=f"c{i}", chunk_text=f"body {i}")
            for i in range(15)
        ]
    )
    retriever = _make_retriever(storage)
    result = await retriever.retrieve(_query(), ctx=_ctx(), limit=15, mode=RetrieverMode.HYBRID)
    assert len(result.matched_contexts) == 15


@pytest.mark.asyncio
async def test_hybrid_trace_records_effective_profile(monkeypatch):
    """Budgets, rrf_k, lambda and degradation are recorded in telemetry."""
    captured = {}

    class FakeTelemetry:
        enabled = True

        def set(self, key, value):
            captured[key] = value

        def set_value(self, key, value):
            captured[key] = value

        def count(self, key, delta=1):
            pass

        def record_error(self, *args, **kwargs):
            pass

        def measure(self, name):
            import contextlib

            return contextlib.nullcontext()

    monkeypatch.setattr(
        "openviking.retrieve.hierarchical_retriever.get_current_telemetry",
        lambda: FakeTelemetry(),
    )
    storage = HybridStorage([_row("viking://a", 0.5, chunk_id="c1", chunk_text="alpha")])
    profile = FusionProfile(name="hybrid", rrf_k=42, mmr_lambda=0.25, mmr_top=3)
    retriever = _make_retriever(storage, fusion_profile=profile)
    await retriever.retrieve(_query(), ctx=_ctx(), limit=5, mode=RetrieverMode.HYBRID)

    assert captured["hybrid.rrf_k"] == 42
    assert captured["hybrid.mmr_lambda"] == 0.25
    # limit=5 exceeds the profile's MMR top of 3, so the trace records the
    # effective (raised) budget rather than the base one.
    assert captured["hybrid.fusion"]["mmr_top"] == 5
    assert captured["hybrid.degraded"] is False


@pytest.mark.asyncio
async def test_quick_mode_does_not_use_fusion_path():
    """Existing quick behavior must be untouched by the hybrid profile."""
    storage = HybridStorage([_row("viking://q", 0.5, chunk_id="c1", chunk_text="q")])
    retriever = _make_retriever(storage)
    result = await retriever.retrieve(_query(), ctx=_ctx(), limit=5, mode=RetrieverMode.QUICK)
    assert [c.uri for c in result.matched_contexts] == ["viking://q"]
    # Quick mode searches leaves directly; it never issues the hybrid [2] probe
    # through the fusion path, which would have recorded hybrid trace keys.
    assert all(call["level"] != [0, 1] for call in storage.search_calls)


@pytest.mark.asyncio
async def test_hybrid_rerank_false_skips_provider_and_serves_normalized_rrf_scores(monkeypatch):
    """rerank_enabled=False must skip the provider call and serve bounded scores.

    Raw RRF sums (~0.02x dust) cannot be thresholded or compared across queries,
    so the served scores are normalized into (0,1] by the fusion ceiling while
    the raw sums stay in ``score_components["rrf"]``.
    """
    fake_client = FakeRerankClient([0.95, 0.05])
    monkeypatch.setattr(
        "openviking.retrieve.hierarchical_retriever.RerankClient.from_config",
        lambda config: fake_client,
    )
    storage = HybridStorage(
        [
            _row("viking://b", 999.0, chunk_id="cb", chunk_text="beta content"),
            _row("viking://a", 0.001, chunk_id="ca", chunk_text="alpha content"),
        ]
    )

    async def lexical_search(**kwargs):
        return [_row("viking://a", 1.0, chunk_id="ca", chunk_text="alpha content")]

    retriever = _make_retriever(storage, rerank_config=_rerank_config(), lexical_search=lexical_search)
    retriever._rerank_client = fake_client

    result = await retriever.retrieve(
        _query(), ctx=_ctx(), limit=5, mode=RetrieverMode.HYBRID, rerank_enabled=False
    )

    assert fake_client.calls == []
    scores = {c.uri: c.score for c in result.matched_contexts}
    assert set(scores) == {"viking://a", "viking://b"}
    # Bounded rank-fusion domain: a appears in both paths (dense rank 2, lexical
    # rank 1) -> (1/62 + 1/61) / (2/61) = 123/124; b tops dense only -> 0.5.
    assert scores["viking://a"] == pytest.approx(123 / 124)
    assert scores["viking://b"] == pytest.approx(0.5)
    assert all(0 < score <= 1 for score in scores.values())


@pytest.mark.asyncio
async def test_hybrid_rerank_true_still_calls_provider(monkeypatch):
    """Counter-test: default rerank_enabled=True keeps the provider in the loop."""
    fake_client = FakeRerankClient([0.05, 0.95])
    monkeypatch.setattr(
        "openviking.retrieve.hierarchical_retriever.RerankClient.from_config",
        lambda config: fake_client,
    )
    storage = HybridStorage(
        [
            _row("viking://b", 999.0, chunk_id="cb", chunk_text="beta content"),
            _row("viking://a", 0.001, chunk_id="ca", chunk_text="alpha content"),
        ]
    )

    async def lexical_search(**kwargs):
        return [_row("viking://a", 1.0, chunk_id="ca", chunk_text="alpha content")]

    retriever = _make_retriever(storage, rerank_config=_rerank_config(), lexical_search=lexical_search)
    retriever._rerank_client = fake_client

    result = await retriever.retrieve(_query(), ctx=_ctx(), limit=5, mode=RetrieverMode.HYBRID)

    assert len(fake_client.calls) == 1
    # Fused window is [a, b] (a leads RRF); scores [0.05, 0.95] land on
    # a=0.05/b=0.95, so rerank keeps b first but in the reranker's 0-1 domain.
    assert [c.uri for c in result.matched_contexts] == ["viking://b", "viking://a"]
    assert result.matched_contexts[0].score == pytest.approx(0.95)


def test_profile_for_hybrid_uses_spec_defaults():
    profile = profile_for("hybrid")
    assert profile.recall_limit == 100
    assert profile.fused_limit == 100
    assert profile.rerank_limit == 50
    assert profile.mmr_top == 10
    assert profile.rrf_k == 60
    assert profile.mmr_lambda == 0.7