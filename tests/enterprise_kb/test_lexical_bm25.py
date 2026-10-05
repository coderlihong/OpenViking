"""KB-016 real BM25 lexical index: FTS5 provider, tokenizer lock, scoping.

Covers TC-6.1 (dense-disabled lexical recall of Chinese/model/DTC terms),
TC-6.2 preconditions (lexical recall available for later RRF fusion) and
TC-16.8 (lexical version/scoping/receipt gating). No dense or sparse embedder
is involved anywhere in these tests.
"""

import os
from types import SimpleNamespace

import pytest

from openviking.indexing.lexical import (
    FALLBACK_INDEX_VERSION,
    INDEX_VERSION,
    TOKENIZER_VERSION,
    LexicalEntry,
    LexicalIndex,
    SearchScope,
    encode_query,
    entries_from_chunks,
    entries_from_image_records,
    fts5_available,
    index_path,
    lexical_receipt,
    provider_for,
    tokenize,
)
from openviking.indexing.publication import PublicationStore

NAMESPACE = "ns-kb"
ACCOUNT = "acct-kb"


def build_index(tmp_path, entries, namespace=NAMESPACE, account=ACCOUNT):
    index = LexicalIndex(str(tmp_path / "lexical.db"))
    index.build(account, namespace, entries)
    return index


def corpus():
    """Two text children plus one image summary, all derived content only."""
    return [
        LexicalEntry(
            "viking://kb/manual.md",
            "doc-manual",
            "gen-1",
            "child-a",
            "液压泵故障诊断：SY215-C 挖掘机出现液压压力异常时先检查主泵。",
        ),
        LexicalEntry(
            "viking://kb/engine.md",
            "doc-engine",
            "gen-1",
            "child-b",
            "发动机故障码 P0087 表示燃油轨压力过低，需检查喷油器与燃油泵。",
        ),
        LexicalEntry(
            "viking://kb/images/pump.png",
            "doc-image",
            "gen-1",
            "image:0",
            "液压系统原理示意图，展示 SY215-C 主泵与先导泵的连接结构。",
            "image_summary",
        ),
    ]


# ---------------------------------------------------------------- tokenizer lock


def test_tokenizer_version_is_locked_and_reproducible():
    assert TOKENIZER_VERSION == "zh-lexical-v2"
    assert tokenize("液压泵故障") == tokenize("液压泵故障")
    # Repeated source terms must contribute their actual BM25 frequency.
    tokens = tokenize("液压泵故障 液压泵故障")
    assert tokens.count("液压") == 2
    assert encode_query("液压 液压") == encode_query("液压")


def test_model_number_and_dtc_tokens_are_preserved():
    tokens = tokenize("SY215-C 与 P0087 故障")
    assert "sy215-c" in tokens
    assert "sy215" in tokens
    assert "p0087" in tokens
    # CJK runs are segmented into unigrams and bigrams.
    assert "液压" in tokenize("液压泵")
    assert {"液", "压", "泵", "液压", "压泵"} <= set(tokenize("液压泵"))


def test_query_expression_quotes_tokens_and_rejects_empty():
    expression = encode_query("SY215-C")
    assert expression.startswith('"sy215-c"')
    assert " AND " in expression
    with pytest.raises(ValueError):
        encode_query("   ")


def test_native_provider_rejected_for_local_deployment():
    assert provider_for("local") == "local_fts5"
    with pytest.raises(ValueError, match="native"):
        provider_for("native")
    with pytest.raises(ValueError):
        LexicalIndex("/tmp/unused-lexical.db", provider="native")


# ------------------------------------------------- (a) dense-disabled lexical hit


@pytest.mark.skipif(not fts5_available(), reason="SQLite FTS5 unavailable")
def test_dense_disabled_lexical_recall_of_chinese_model_and_dtc(tmp_path):
    """TC-6.1: no dense path is involved; real BM25 must still hit."""
    index = build_index(tmp_path, corpus())
    assert index.provider == "local_fts5"
    assert index.index_version == INDEX_VERSION

    for query, expected_chunk in (
        ("液压", "child-a"),
        ("SY215-C", "child-a"),
        ("P0087", "child-b"),
        ("喷油器", "child-b"),
    ):
        results = index.search(query, ACCOUNT, NAMESPACE)
        assert results, f"lexical recall missed {query!r}"
        assert expected_chunk in {r.chunk_id for r in results}
        top = results[0]
        assert top.provider == "local_fts5"
        assert top.tokenizer_version == TOKENIZER_VERSION
        # Trace carries a lexical score and the index version, not a sparse score.
        assert top.as_dict()["score_components"]["lexical_raw"] is not None


def test_lexical_module_has_no_dense_or_sparse_dependency():
    """Sparse embeddings are explicitly rejected as a BM25 substitute."""
    import ast

    import openviking.indexing.lexical as module

    tree = ast.parse(open(module.__file__, encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = {
        name
        for name in imported
        if any(word in name for word in ("embed", "sparse", "vector", "retrieve"))
    }
    assert not forbidden, f"lexical index must not depend on dense/sparse paths: {forbidden}"


# ---------------------------------------------------- (b) score direction


@pytest.mark.skipif(not fts5_available(), reason="SQLite FTS5 unavailable")
def test_fts_score_direction_is_normalized_higher_is_better(tmp_path):
    index = build_index(
        tmp_path,
        [
            LexicalEntry("viking://kb/s.md", "doc-strong", "g", "c", "液压 液压 液压 液压 泵体故障"),
            LexicalEntry("viking://kb/w.md", "doc-weak", "g", "c", "其他内容，略有液压提及"),
            LexicalEntry("viking://kb/n.md", "doc-none", "g", "c", "完全无关的电气线路"),
        ],
    )
    results = index.search("液压", ACCOUNT, NAMESPACE)
    assert [r.document_id for r in results] == ["doc-strong", "doc-weak"]
    # Native FTS5 bm25() is lower-is-better (negative); the adapter flips it.
    assert all(r.raw_score < 0 for r in results)
    assert all(r.score > 0 for r in results)
    assert results[0].score > results[1].score
    assert results[0].score_normalized == pytest.approx(1.0)
    assert 0.0 < results[1].score_normalized < 1.0
    assert [r.rank for r in results] == [1, 2]


@pytest.mark.skipif(not fts5_available(), reason="SQLite FTS5 unavailable")
def test_portable_fallback_keeps_direction_and_ordering(tmp_path):
    index = build_index(
        tmp_path,
        [
            LexicalEntry("viking://kb/s.md", "doc-strong", "g", "c", "液压 液压 液压 泵体故障"),
            LexicalEntry("viking://kb/w.md", "doc-weak", "g", "c", "略有液压提及"),
        ],
    )
    native = index.search("液压", ACCOUNT, NAMESPACE)
    index._fts5 = False
    index._provider = "portable_bm25"
    fallback = index.search("液压", ACCOUNT, NAMESPACE)
    assert index.index_version == FALLBACK_INDEX_VERSION
    assert [r.document_id for r in fallback] == [r.document_id for r in native]
    assert all(r.score > 0 for r in fallback)
    assert fallback[0].score >= fallback[1].score


# ------------------------------------------------------- (c) scoping + authz


@pytest.mark.skipif(not fts5_available(), reason="SQLite FTS5 unavailable")
def test_authorization_filter_applied_before_ranking(tmp_path):
    """The unauthorized top document must not consume rank 1."""
    index = build_index(
        tmp_path,
        [
            LexicalEntry("viking://kb/secret.md", "doc-secret", "g", "c", "液压 液压 液压 液压"),
            LexicalEntry("viking://kb/public.md", "doc-public", "g", "c", "液压 提及一次"),
        ],
    )
    unfiltered = index.search("液压", ACCOUNT, NAMESPACE)
    assert unfiltered[0].document_id == "doc-secret"

    scope = SearchScope(authorized_uris=frozenset({"viking://kb/public.md"}))
    scoped = index.search("液压", ACCOUNT, NAMESPACE, scope=scope)
    assert [r.document_id for r in scoped] == ["doc-public"]
    # Filter happened before ranking: the survivor is rank 1, not rank 2.
    assert scoped[0].rank == 1
    assert scoped[0].score_normalized == pytest.approx(1.0)

    # A query matching only unauthorized content yields no candidates at all.
    denied = index.search("液压", ACCOUNT, NAMESPACE, scope=SearchScope(authorized_uris=frozenset()))
    assert denied == []


@pytest.mark.skipif(not fts5_available(), reason="SQLite FTS5 unavailable")
def test_namespace_document_and_generation_scoping(tmp_path):
    index = build_index(tmp_path, corpus())
    index.build(
        ACCOUNT,
        "ns-other",
        [LexicalEntry("viking://kb/manual.md", "doc-manual", "gen-1", "child-a", "液压 液压")],
    )

    # Namespace isolation.
    other = index.search("液压", ACCOUNT, "ns-other")
    assert {r.document_id for r in other} == {"doc-manual"}
    assert index.search("液压", ACCOUNT, "ns-missing") == []

    # Account isolation.
    assert index.search("液压", "acct-other", NAMESPACE) == []

    # Document scoping.
    scoped = index.search("液压", ACCOUNT, NAMESPACE, scope=SearchScope(document_ids=("doc-image",)))
    assert {r.document_id for r in scoped} == {"doc-image"}

    # Generation scoping: a stale/unknown generation has no candidates.
    assert index.search("液压", ACCOUNT, NAMESPACE, scope=SearchScope(generations=("gen-2",))) == []
    current = index.search("液压", ACCOUNT, NAMESPACE, scope=SearchScope(generations=("gen-1",)))
    assert current


@pytest.mark.skipif(not fts5_available(), reason="SQLite FTS5 unavailable")
def test_generation_replacement_does_not_mix_old_and_new(tmp_path):
    index = build_index(tmp_path, corpus())
    index.build(
        ACCOUNT,
        NAMESPACE,
        [LexicalEntry("viking://kb/manual.md", "doc-manual", "gen-1", "child-a", "旧内容 齿轮箱")],
        replace_scope=("doc-manual", "gen-1"),
    )
    assert index.generations(ACCOUNT, NAMESPACE, "doc-manual") == ["gen-1"]
    # The replacement wrote the new derived content...
    assert index.search("齿轮箱", ACCOUNT, NAMESPACE) != []
    # ...and the old content for that document generation is gone, not merged.
    scoped = index.search("液压", ACCOUNT, NAMESPACE, scope=SearchScope(document_ids=("doc-manual",)))
    assert scoped == []
    # Only one row per document/chunk identity remains.
    assert index.stats(ACCOUNT, NAMESPACE)["groups"][0]["entries"] >= 1
    assert len(index.generations(ACCOUNT, NAMESPACE, "doc-manual")) == 1


# ------------------------------------------- (d) text + image summary indexing


@pytest.mark.skipif(not fts5_available(), reason="SQLite FTS5 unavailable")
def test_index_builds_from_child_text_and_image_summaries(tmp_path):
    index = build_index(tmp_path, corpus())
    stats = index.stats(ACCOUNT, NAMESPACE)
    kinds = {group["content_kind"] for group in stats["groups"]}
    assert kinds == {"text", "image_summary"}

    # Image summary is reachable by a purely textual query.
    image_hits = index.search(
        "先导泵", ACCOUNT, NAMESPACE, scope=SearchScope(content_kinds=("image_summary",))
    )
    assert [r.chunk_id for r in image_hits] == ["image:0"]
    assert image_hits[0].content_kind == "image_summary"


def test_entries_from_chunks_and_image_records():
    plan = SimpleNamespace(
        source_uri="viking://kb/manual.md",
        document_id="doc-manual",
        content_revision="rev-1",
    )
    entries = entries_from_chunks(
        plan,
        [
            {
                "chunk_id": "child-a",
                "chunk_text": "液压泵 SY215-C",
                "document_id": "doc-manual",
                "content_revision": "rev-1",
                "index_generation": "gen-1",
            }
        ],
    )
    assert entries[0].content_kind == "text"
    assert entries[0].uri == "viking://kb/manual.md"

    images = entries_from_image_records(
        [{"summary": "液压泵结构图", "context_uri": "viking://kb/manual.md"}],
        uri="viking://kb/pump.png",
        document_id="doc-image",
        index_generation="gen-1",
    )
    assert images[0].content_kind == "image_summary"
    assert images[0].uri == "viking://kb/manual.md"


def test_index_path_is_namespace_scoped_and_stable():
    first = index_path("/tmp/lexical", ACCOUNT, NAMESPACE)
    assert first == index_path("/tmp/lexical", ACCOUNT, NAMESPACE)
    assert first != index_path("/tmp/lexical", ACCOUNT, "ns-other")


# --------------------------------------------- generation-based publication gate


def test_lexical_receipt_gates_generation_publication(tmp_path):
    """A missing lexical receipt must block the generation switch (TC-16.8)."""
    store = PublicationStore(str(tmp_path / "publication.db"))
    receipt = lexical_receipt("doc-manual", "gen-1")
    assert receipt == lexical_receipt("doc-manual", "gen-1")
    assert receipt.startswith("lexical:")

    generation = store.begin(
        ACCOUNT, NAMESPACE, "viking://kb/manual.md", "plan", ["child-a", receipt]
    )
    store.seal(generation)
    assert not store.acknowledge(generation, "child-a")
    assert store.state(generation)["state"] == "queued"
    assert not store.capture(ACCOUNT)[1]  # Nothing visible before the lexical receipt.
    assert store.acknowledge(generation, receipt)
    assert store.state(generation)["state"] == "published"
    manifest = store.capture(ACCOUNT)[1]
    assert manifest["viking://kb/manual.md"]["generation"] == generation


def test_lexical_entry_rejects_undeclared_content_kind():
    with pytest.raises(ValueError, match="content_kind"):
        LexicalEntry("viking://kb/a.md", "doc", "gen", "c", "text", "vector")
    with pytest.raises(ValueError, match="nonempty"):
        LexicalEntry("viking://kb/a.md", "doc", "gen", "c", "   ")

@pytest.mark.parametrize("portable", [False, True])
def test_frequency_changes_rank_for_equal_length_documents(tmp_path, portable):
    index = build_index(tmp_path, [
        LexicalEntry("viking://kb/weak.md", "weak", "g", "c", "pump other other other"),
        LexicalEntry("viking://kb/strong.md", "strong", "g", "c", "pump pump pump other"),
    ])
    if portable:
        index._fts5 = False
    hits = index.search("pump", ACCOUNT, NAMESPACE)
    assert [hit.document_id for hit in hits] == ["strong", "weak"]
    assert hits[0].score > hits[1].score


@pytest.mark.parametrize("portable", [False, True])
def test_conjunctive_queries_do_not_return_partial_term_matches(tmp_path, portable):
    index = build_index(tmp_path, [
        LexicalEntry("viking://kb/partial.md", "partial", "g", "c", "pump"),
        LexicalEntry("viking://kb/complete.md", "complete", "g", "c", "pump pressure"),
    ])
    if portable:
        index._fts5 = False
    assert [hit.document_id for hit in index.search("pump pressure", ACCOUNT, NAMESPACE)] == ["complete"]


def test_previous_tokenizer_index_requires_explicit_rebuild(tmp_path):
    import sqlite3
    index = build_index(tmp_path, corpus())
    with sqlite3.connect(index.path) as db:
        db.execute("UPDATE lexical_metadata SET value='zh-lexical-v1' WHERE key='tokenizer_version'")
    with pytest.raises(ValueError, match="LEXICAL_REBUILD_REQUIRED"):
        LexicalIndex(index.path)
    with sqlite3.connect(index.path) as db:
        assert db.execute("SELECT count(*) FROM lexical_rows").fetchone()[0] == len(corpus())
