"""KB-013: real parent/child write + recall (F4/F9, TC-4.1..4.7, TC-9.2).

These tests prove, without a live vector backend, the contracts the spec makes
about parent/child chunks:

- one parent owns MANY children and each child gets a DISTINCT primary key;
- a distant (tail) fact is recallable and is not dropped just because it shares
  a URI with a head hit;
- parent expansion respects a token budget;
- every chunk cites the ORIGINAL source URI (no position/version burden);
- a superseded generation is excluded from the visible set;
- the D2 reindex path for existing Markdown calls no PDF/Office parser, does
  not rewrite the source, and does not need ``_toc.json`` to re-split chapters.

The real backend end-to-end write/recall is explicitly NOT_RUN (see the KB-013
evidence JSON); these tests use the shared plan, the deterministic record-id
derivation, the publication registry and the pure fusion primitives.
"""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tokenizers import Tokenizer, models, pre_tokenizers

from openviking.indexing.enqueue import enqueue_text_plan
from openviking.indexing.plan import (
    build_plan,
    children_by_parent,
    expand_parent_budget,
    plan_resource,
)
from openviking.indexing.publication import PublicationStore
from openviking.models.embedder.base import EmbedResult
from openviking.storage.collection_schemas import TextEmbeddingHandler
from openviking.storage.queuefs.embedding_msg import EmbeddingMsg
from openviking.storage.vector_ids import vector_record_id_for_data
from openviking_cli.utils.config.embedding_config import EmbeddingConfig

URI = "viking://resources/raw/维修手册.md"
# Three distinctive fact markers far apart in the same document: head/mid/tail.
# They are short enough to stay inside one child even when the tokenizer splits
# at a character boundary, so a missing marker means a genuinely dropped chunk.
HEAD_FACT = "液压泵型号为 SY215"
MID_FACT = "回油滤芯更换周期为 500 小时"
TAIL_FACT = "最终诊断码为 DTC-9137"


@pytest.fixture
def embedding(tmp_path):
    alphabet = pre_tokenizers.ByteLevel.alphabet()
    tokenizer = Tokenizer(models.BPE(vocab={c: i for i, c in enumerate(alphabet)}, merges=[]))
    tokenizer.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    path = tmp_path / "tokenizer.json"
    tokenizer.save(str(path))
    return EmbeddingConfig(
        dense={"provider": "openai", "model": "fixture", "dimension": 2, "api_key": "fixture"},
        chunking={
            "enabled": True,
            "tokenizer_path": str(path),
            "tokenizer_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "child_target_tokens": 256,
            "child_max_tokens": 384,
            "parent_max_tokens": 1024,
            "overlap_tokens": 32,
            "special_token_reserve": 16,
        },
    )


def long_source():
    filler = "".join(f"{i} 检查液压回路并保留全部正文。\n\n" for i in range(90))
    return (
        f"# 维修说明\n\n头部事实：{HEAD_FACT}。\n\n{filler}\n"
        f"中部事实：{MID_FACT}。\n\n{filler}\n尾部事实：{TAIL_FACT}。"
    )


def token_counter(embedding):
    tokenizer = Tokenizer.from_file(embedding.chunking.tokenizer_path)

    def count(text):
        return len(tokenizer.encode(text, add_special_tokens=False).ids)

    return count


# --------------------------------------------------------------------------- #
# (a) one parent, many children, distinct primary keys
# --------------------------------------------------------------------------- #


def test_multiple_children_share_parent_but_get_distinct_record_ids(embedding):
    plan = plan_resource(long_source(), URI, "acct", embedding)
    assert len(plan.chunks) > 3
    groups = children_by_parent(plan.chunks)
    multi = [members for members in groups.values() if len(members) > 1]
    assert multi, "expected at least one parent with several children"

    records = [
        {
            "account_id": "acct",
            "uri": URI,
            "level": 2,
            **{
                "chunk_id": c.chunk_id,
                "parent_chunk_id": c.parent_chunk_id,
                "index_generation": "gen-1",
            },
        }
        for c in plan.chunks
    ]
    ids = [vector_record_id_for_data(record) for record in records]
    assert len(set(ids)) == len(ids), "children of one parent collided on a primary key"

    # The children of the same parent provably differ only by chunk identity.
    for members in multi:
        same_parent = [
            vector_record_id_for_data(
                {
                    "account_id": "acct",
                    "uri": URI,
                    "level": 2,
                    "chunk_id": c.chunk_id,
                    "parent_chunk_id": c.parent_chunk_id,
                    "index_generation": "gen-1",
                }
            )
            for c in members
        ]
        assert len(set(same_parent)) == len(same_parent)
        assert len({c.parent_chunk_id for c in members}) == 1


def test_chunk_record_id_requires_chunk_identity(embedding):
    # A URI-only record would overwrite its siblings; the shared writer refuses it.
    from openviking.storage.vector_ids import chunk_vector_record_id

    with pytest.raises(ValueError, match="chunk identity"):
        chunk_vector_record_id("acct", URI, "")


# --------------------------------------------------------------------------- #
# (b) distant tail fact recallable and not dropped for sharing a URI
# --------------------------------------------------------------------------- #


async def test_head_mid_tail_facts_all_written_with_original_uri(embedding, monkeypatch):
    source = long_source()
    queue = _RecordingQueue()
    base = EmbeddingMsg(
        source,
        {"uri": URI, "account_id": "acct", "level": 2, "context_type": "resource", "is_leaf": True},
    )
    count = await enqueue_text_plan(base, source, embedding, queue)
    assert count == len(queue.messages) > 3
    assert len({m.context_data["chunk_id"] for m in queue.messages}) == count

    config = SimpleNamespace(
        storage=SimpleNamespace(vectordb=SimpleNamespace(name="context")),
        embedding=SimpleNamespace(
            dimension=2,
            get_embedder=lambda: _Embedder(),
            circuit_breaker=SimpleNamespace(
                failure_threshold=5, reset_timeout=60, max_reset_timeout=600
            ),
        ),
    )
    monkeypatch.setattr("openviking_cli.utils.config.get_openviking_config", lambda: config)
    database = _Database()
    handler = TextEmbeddingHandler(database)
    for msg in queue.messages:
        result = await handler.on_dequeue({"data": msg.to_json()})
        assert result.outcome.value == "success", result.error

    texts = {record["content"] for record in database.records.values()}
    for fact in (HEAD_FACT, MID_FACT, TAIL_FACT):
        assert any(fact in text for text in texts), f"lost fact: {fact}"
    # Every stored chunk still cites the ORIGINAL URI, never a derived path.
    assert {record["uri"] for record in database.records.values()} == {URI}
    assert {record["source_uri"] for record in database.records.values()} == {URI}


def test_distant_hits_not_dropped_by_uri_aggregation(embedding):
    from openviking.retrieve.fusion import Candidate, aggregate_by_uri

    head = Candidate(uri=URI, chunk_id="c-head", chunk_text=HEAD_FACT, score=0.9)
    tail = Candidate(uri=URI, chunk_id="c-tail", chunk_text=TAIL_FACT, score=0.4)
    groups = aggregate_by_uri([head, tail])
    assert len(groups) == 1
    group = groups[0]
    assert group["source_uri"] == URI
    # Both hits survive; sharing a URI does not collapse the second one.
    assert {m["chunk_id"] for m in group["matches"]} == {"c-head", "c-tail"}
    assert any(TAIL_FACT in m["text"] for m in group["matches"])
    assert group["score"] == 0.9  # group score is the best member


# --------------------------------------------------------------------------- #
# (c) parent expansion respects a token budget
# --------------------------------------------------------------------------- #


def test_parent_expansion_stays_within_budget_and_keeps_child_hits(embedding):
    count = token_counter(embedding)
    plan = plan_resource(long_source(), URI, "acct", embedding)
    plan.validate_parent_budget(count)

    hits = [
        {
            "uri": URI,
            "chunk_id": c.chunk_id,
            "parent_text": c.parent_text,
            "chunk_text": c.source_text,
        }
        for c in plan.chunks
    ]
    budget = count(hits[0]["parent_text"])
    expanded = expand_parent_budget(hits, budget_tokens=budget, count=count)
    # Budget covers exactly one parent; later parents are dropped but children stay.
    assert len(expanded) == len(hits)
    assert sum(count(h["parent_text"]) for h in expanded) <= budget
    assert expanded[0]["parent_text"] == hits[0]["parent_text"]
    assert all(h["chunk_id"] for h in expanded)
    assert {h["chunk_text"] for h in expanded} == {h["chunk_text"] for h in hits}

    disabled = expand_parent_budget(hits, budget_tokens=0, count=count)
    assert disabled == hits


def test_plan_rejects_parent_over_budget(embedding):
    # Force a plan whose parent context cannot fit a tiny declared budget.
    source = "# 标题\n\n" + ("正文段落。" * 400)
    plan = plan_resource(source, URI, "acct", embedding)
    with pytest.raises(ValueError, match="parent token budget"):
        plan.validate_parent_budget(lambda _text: 10_000)


def test_declared_parent_budget_is_the_configured_parent_max(embedding):
    plan = plan_resource(long_source(), URI, "acct", embedding)
    assert plan.parent_token_budget == embedding.chunking.parent_max_tokens


# --------------------------------------------------------------------------- #
# (d) all chunks point at the ORIGINAL URI
# --------------------------------------------------------------------------- #


def test_every_chunk_cites_original_uri_without_position_burden(embedding):
    source = long_source()
    plan = plan_resource(source, URI, "acct", embedding)
    from openviking.indexing.plan import chunk_fields

    for chunk in plan.chunks:
        fields = chunk_fields(plan, chunk)
        assert fields["source_uri"] == URI
        # The plan never emits a derived/private citation or a position hint.
        assert fields.get("uri", URI) == URI
        assert {"chunk_offset", "span", "page", "bbox", "content_hash"}.isdisjoint(fields)
        assert fields["chunk_text"] == chunk.embedding_text


# --------------------------------------------------------------------------- #
# (e) superseded generation excluded
# --------------------------------------------------------------------------- #


def test_superseded_generation_is_excluded_from_visible_set(tmp_path):
    from openviking.indexing.publication import PublicationStore

    store = PublicationStore(str(tmp_path / "publication.db"))
    old = _publish(store, ["a", "b"])
    newer = _publish(store, ["c", "d"])
    lease, manifest = store.capture("acct")
    store.release(lease)
    assert manifest[URI]["generation"] == newer
    assert old not in repr(store.visibility(manifest))
    assert newer in repr(store.visibility(manifest))
    assert store.state(old)["state"] == "published"  # retired, not deleted


def _publish(store, children):
    generation = store.begin("acct", "resources", URI, "plan", children)
    store.seal(generation)
    for child in children:
        store.acknowledge(generation, child)
    return generation


# --------------------------------------------------------------------------- #
# D2 path: reindex existing Markdown reuses text plan, no parser, no rewrite
# --------------------------------------------------------------------------- #


async def test_reindex_markdown_skips_parser_and_does_not_rewrite_source(
    embedding, monkeypatch
):
    from openviking.server.identity import RequestContext, Role
    from openviking.service import reindex_executor as reindex
    from openviking.utils import embedding_utils as utility
    from openviking_cli.session.user_id import UserIdentifier

    source = long_source()
    queue = _RecordingQueue()
    fs = SimpleNamespace(
        read_file=AsyncMock(return_value=source),
        stat=AsyncMock(return_value={}),
        write_file=AsyncMock(side_effect=AssertionError("reindex must not rewrite the source")),
    )
    manager = SimpleNamespace(EMBEDDING="embedding", get_queue=lambda name: queue)
    cfg = SimpleNamespace(embedding=embedding)
    monkeypatch.setattr(utility, "get_queue_manager", lambda: manager)
    monkeypatch.setattr(utility, "get_viking_fs", lambda: fs)
    monkeypatch.setattr(utility, "get_openviking_config", lambda: cfg)
    monkeypatch.setattr(utility, "_get_existing_created_at", AsyncMock(return_value=None))
    monkeypatch.setattr(reindex, "get_openviking_config", lambda: cfg)
    monkeypatch.setattr(
        reindex,
        "get_service",
        lambda: SimpleNamespace(
            vikingdb_manager=SimpleNamespace(fetch_by_uri=AsyncMock(return_value=None))
        ),
    )

    # Any PDF/Office parse is a failure of the D2 contract.
    parse_calls = []

    class GuardedParser:
        async def parse(self, *args, **kwargs):
            parse_calls.append(args)
            raise AssertionError("reindex of existing Markdown must not call a parser")

        async def submit(self, *args, **kwargs):
            parse_calls.append(args)
            raise AssertionError("reindex of existing Markdown must not call a parser")

    monkeypatch.setattr("openviking.parse.parser_router.ParserRouter.parse", GuardedParser.parse)

    executor = reindex.ReindexExecutor()
    executor._best_file_summary = AsyncMock(return_value="summary")
    executor._effective_file_vectorization_concurrency = lambda: 1
    counters = reindex._ReindexCounters()
    ctx = RequestContext(user=UserIdentifier("acct", "tester"), role=Role.ROOT)
    await executor._reindex_resource_vectors_from_entries(
        root_uri=URI, directories=[], files=[URI], counters=counters, ctx=ctx
    )
    assert counters.failed_records == 0, counters.warnings
    assert parse_calls == []
    assert fs.write_file.await_count == 0
    assert len(queue.messages) > 1
    assert {m.context_data["uri"] for m in queue.messages} == {URI}
    assert {m.context_data["source_uri"] for m in queue.messages} == {URI}
    # The plan is built from the original bytes; no _toc.json re-split is needed.
    read_uris = {call.args[0] for call in fs.read_file.await_args_list}
    assert read_uris == {URI}
    assert not any(uri.endswith("_toc.json") for uri in read_uris)
    assert fs.write_file.await_count == 0


# --------------------------------------------------------------------------- #
# Fixtures shared with test_index_plan-style consumers
# --------------------------------------------------------------------------- #


class _RecordingQueue:
    def __init__(self):
        self.messages = []

    async def enqueue(self, msg):
        self.messages.append(msg)
        return msg.id


class _Embedder:
    def prepare_embedding_input(self, text):
        return text

    async def embed_async(self, text, is_query=False):
        return EmbedResult(dense_vector=[len(text), 1.0])


class _Database:
    is_closing = False
    uses_content_field = True
    has_queue_manager = False

    def __init__(self):
        self.records = {}

    async def upsert(self, data, ctx=None, options=None):
        self.records[data["id"]] = dict(data)
        return data["id"]


def test_build_plan_direct_call_keeps_budget(embedding):
    count = token_counter(embedding)
    plan = build_plan(
        long_source(),
        URI,
        "acct",
        embedding.chunking,
        "space",
        count,
    )
    assert plan.parent_token_budget == embedding.chunking.parent_max_tokens
    assert plan.validate_coverage(long_source()) is None