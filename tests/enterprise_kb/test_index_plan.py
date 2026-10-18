"""Source coverage, bounded parents, unique child writes, and original URI hits."""

import hashlib
from types import SimpleNamespace

import pytest
from tokenizers import Tokenizer, models, pre_tokenizers

from openviking.indexing.enqueue import enqueue_text_plan
from openviking.indexing.plan import plan_resource
from openviking.models.embedder.base import EmbedResult
from openviking.storage.collection_schemas import TextEmbeddingHandler
from openviking.storage.queuefs.embedding_msg import EmbeddingMsg
from openviking.storage.vector_ids import vector_record_id_for_data
from openviking_cli.utils.config.embedding_config import EmbeddingConfig


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
            "child_target_tokens": 128,
            "child_max_tokens": 192,
            "parent_max_tokens": 768,
            "overlap_tokens": 24,
            "special_token_reserve": 16,
        },
    )


SOURCE = (
    "# 维修说明\n\n"
    + "".join(f"{i} 检查液压回路并保留全部正文。\n\n" for i in range(100))
    + "最终答案在末尾。"
)
URI = "viking://resources/raw/维修.md"


def test_lossless_token_bounded_parent_child_and_stable_id(embedding):
    first = plan_resource(SOURCE, URI, "acct", embedding)
    assert len(first.chunks) > 10
    assert "".join(c.source_text for c in first.chunks) == SOURCE
    assert first.chunks[-1].source_text.endswith("最终答案在末尾。")
    assert first == plan_resource(SOURCE, URI, "acct", embedding)
    tokenizer = Tokenizer.from_file(embedding.chunking.tokenizer_path)

    def count(text):
        return len(tokenizer.encode(text, add_special_tokens=False).ids)

    assert all(count(c.embedding_text) + 16 <= 192 for c in first.chunks)
    assert all(count(c.parent_text) <= 768 for c in first.chunks)


def test_figure_block_embedding_text_carries_caption_not_markup(embedding):
    from openviking.parse.image_rewrite import wrap_in_figure

    figure_uri = "viking://resources/raw/images/abc.jpg"
    source = (
        "# 故障案例\n\n"
        + wrap_in_figure(figure_uri, figure_uri, caption="平衡阀故障示意图，展示油缸连接。")
        + "\n\n后续正文段落。\n"
    )
    plan = plan_resource(source, URI, "acct", embedding)
    assert "".join(c.source_text for c in plan.chunks) == source
    embedded = "\n".join(c.embedding_text for c in plan.chunks)
    assert "平衡阀故障示意图" in embedded, embedded
    assert "<img" not in embedded, embedded
    assert "<figure>" not in embedded and "</figure>" not in embedded, embedded
    assert figure_uri in embedded, "caption text must keep the storage pointer"
    assert all("figcap" not in c.embedding_text for c in plan.chunks), embedded


def test_precaption_figure_block_plans_with_empty_render(embedding):
    # A PDF-derived chapter ships figures with an empty <figcaption> and no
    # vikingsrc before caption backfill runs. The plan must keep the span for
    # coverage while embedding no markup; this state used to raise "Figure
    # partition lost source content" and requeue the whole branch forever.
    source = (
        "# 拆卸液压管路\n\n"
        "<figure>\n"
        '    <img src="images/844775934052881.jpg">\n'
        "        <figcaption></figcaption>\n"
        "</figure>\n\n"
        "拆卸前先释放系统压力。\n"
    )
    plan = plan_resource(source, URI, "acct", embedding)
    assert "".join(c.source_text for c in plan.chunks) == source
    embedded = "\n".join(c.embedding_text for c in plan.chunks)
    assert "拆卸前先释放系统压力" in embedded
    assert "<img" not in embedded and "<figure>" not in embedded, embedded
    assert "figcaption" not in embedded, embedded


def test_torn_caption_list_still_fragments_within_child_budget(embedding):
    # A PDF-derived caption can embed a whole VLM description in a bullet list
    # that ends with a stray </figcaption>. The list carries no <img>, so it is
    # not a figure block, but it still must fragment by size: rendering it whole
    # overflowed the child budget and aborted the entire file's plan.
    source = (
        "\n* **detailed_purpose_description**: "
        + "该图展示了电子控制面板的正面布局与接口说明。" * 60
        + "\n\n* **citation_source**: 设备用户手册\n</figcaption>\n"
    )
    plan = plan_resource(source, URI, "acct", embedding)
    plan.validate_coverage(source)
    tokenizer = Tokenizer.from_file(embedding.chunking.tokenizer_path)

    def count(text):
        return len(tokenizer.encode(text, add_special_tokens=False).ids)

    assert all(count(c.embedding_text) + 16 <= 192 for c in plan.chunks)
    embedded = "\n".join(c.embedding_text for c in plan.chunks)
    assert "figcaption" not in embedded, embedded


def test_wrong_tokenizer_sha_fails_before_enqueuing(embedding):
    embedding.chunking.tokenizer_sha256 = "0" * 64
    with pytest.raises(ValueError, match="SHA256"):
        plan_resource(SOURCE, URI, "acct", embedding)


def test_plan_rejection_carries_dedicated_error_type(embedding):
    # The semantic processor settles deterministic plan rejections instead of
    # requeueing them, keyed on this type; a plain ValueError would regress
    # into an infinite retry that also poisons the API circuit breaker.
    from openviking.indexing.plan import PlanRejectionError

    embedding.chunking.tokenizer_sha256 = "0" * 64
    with pytest.raises(PlanRejectionError, match="SHA256"):
        plan_resource(SOURCE, URI, "acct", embedding)


def test_code_table_and_unicode_fragment_coverage(embedding):
    source = (
        "```python\n"
        + ('print("维修")\n' * 80)
        + "```\n\n|列|值|\n|---|---|\n"
        + ("|液压|正常|\n" * 80)
        + "\n末尾"
    )
    plan = plan_resource(source, URI, "acct", embedding)
    plan.validate_coverage(source)
    assert any(c.continuation for c in plan.chunks)
    assert "\ufffd" not in "".join(c.embedding_text for c in plan.chunks)


class Queue:
    def __init__(self):
        self.messages = []

    async def enqueue(self, msg):
        self.messages.append(msg)
        return msg.id


class Embedder:
    def prepare_embedding_input(self, text):
        return text

    async def embed_async(self, text, is_query=False):
        return EmbedResult(dense_vector=[len(text), 1.0])


class Database:
    is_closing = False
    uses_content_field = True
    has_queue_manager = False

    def __init__(self):
        self.records = {}

    async def upsert(self, data, ctx=None, options=None):
        self.records[data["id"]] = dict(data)
        return data["id"]


async def test_shared_plan_queue_consumer_preserves_all_children_and_parent_context(
    embedding, monkeypatch
):
    queue = Queue()
    base = EmbeddingMsg(
        SOURCE,
        {
            "uri": URI,
            "account_id": "acct",
            "level": 2,
            "context_type": "resource",
            "is_leaf": True,
            "abstract": "summary",
        },
    )
    count = await enqueue_text_plan(base, SOURCE, embedding, queue)
    assert count == len(queue.messages) > 1
    config = SimpleNamespace(
        storage=SimpleNamespace(vectordb=SimpleNamespace(name="context")),
        embedding=SimpleNamespace(
            dimension=2,
            get_embedder=lambda: Embedder(),
            circuit_breaker=SimpleNamespace(
                failure_threshold=5, reset_timeout=60, max_reset_timeout=600
            ),
        ),
    )
    monkeypatch.setattr("openviking_cli.utils.config.get_openviking_config", lambda: config)
    database = Database()
    handler = TextEmbeddingHandler(database)
    for msg in queue.messages:
        result = await handler.on_dequeue({"data": msg.to_json()})
        assert result.outcome.value == "success", result.error
    assert len(database.records) == count
    assert {r["uri"] for r in database.records.values()} == {URI}
    assert all(r["content"] == r["chunk_text"] for r in database.records.values())
    assert any("最终答案在末尾" in r["content"] for r in database.records.values())
    assert all(r["parent_text"] for r in database.records.values())
    assert all(r["id"] == vector_record_id_for_data(r) for r in database.records.values())


def test_heading_ancestry_follows_children_without_parsing_fenced_headings(embedding):
    source = (
        "# 手册\n\n## 液压回路\n\n" + ("检查阀芯。\n\n" * 40) + "\n```md\n# 假标题\n```\n\n尾部诊断"
    )
    plan = plan_resource(source, URI, "acct", embedding)
    plan.validate_coverage(source)
    assert all("液压回路" in c.embedding_text for c in plan.chunks[1:])
    assert all("假标题" not in c.embedding_text.split("```")[0] for c in plan.chunks)


async def test_delete_uri_level_removes_more_than_one_page(monkeypatch):
    from openviking.core.context import ContextLevel
    from openviking.service.reindex_executor import ReindexExecutor

    records = {str(n): {"id": str(n)} for n in range(237)}

    class Manager:
        async def get_context_by_uri(self, *, uri, level, limit, ctx):
            assert uri == URI and level == 2
            return list(records.values())[:limit]

        async def delete(self, ids, *, ctx):
            for ident in ids:
                del records[ident]
            return len(ids)

    monkeypatch.setattr(
        "openviking.service.reindex_executor.get_service",
        lambda: SimpleNamespace(vikingdb_manager=Manager()),
    )
    assert (
        await ReindexExecutor().delete_uri_level(uri=URI, level=ContextLevel.DETAIL, ctx=None)
        == 237
    )
    assert not records


async def test_delete_uri_level_rejects_partial_cleanup(monkeypatch):
    from openviking.core.context import ContextLevel
    from openviking.service.reindex_executor import ReindexExecutor

    class Manager:
        async def get_context_by_uri(self, **kwargs):
            return [{"id": "1"}, {"id": "2"}]

        async def delete(self, ids, *, ctx):
            return 1

    monkeypatch.setattr(
        "openviking.service.reindex_executor.get_service",
        lambda: SimpleNamespace(vikingdb_manager=Manager()),
    )
    with pytest.raises(RuntimeError, match="Partial vector deletion"):
        await ReindexExecutor().delete_uri_level(uri=URI, level=ContextLevel.DETAIL, ctx=None)


def test_long_table_repeats_header_and_keeps_complete_rows(embedding):
    from markdown_it import MarkdownIt

    header = "| code | pressure |\n| --- | --- |\n"
    rows = [f"| E{n:03d} | {n} MPa |\n" for n in range(50)]
    source = "\n" + header + "".join(rows)
    plan = plan_resource(source, URI, "acct", embedding)
    plan.validate_coverage(source)
    assert len(plan.chunks) > 5
    parser = MarkdownIt("commonmark").enable("table")
    for chunk in plan.chunks:
        assert header in chunk.embedding_text
        assert any(t.type == "table_open" for t in parser.parse(chunk.embedding_text))
    # Original rows stay whole and occur exactly once in owned source text.
    assert all(sum(row in c.source_text for c in plan.chunks) == 1 for row in rows)
    assert all(c.continuation for c in plan.chunks)


def test_single_oversized_table_row_keeps_header_and_entire_value(embedding):
    header = "| key | value |\n| --- | --- |\n"
    source = header + "| valve | " + "液压维修参数" * 80 + " |\n"
    plan = plan_resource(source, URI, "acct", embedding)
    plan.validate_coverage(source)
    assert len(plan.chunks) > 5
    assert all(header in c.embedding_text for c in plan.chunks)
    assert all(c.continuation for c in plan.chunks)


def test_oversized_table_header_is_visible_failure_before_work(embedding):
    source = "| " + "very_long_header_" * 100 + " |\n| --- |\n| value |\n"
    with pytest.raises(ValueError, match="Table header exceeds"):
        plan_resource(source, URI, "acct", embedding)


async def test_ingest_vectorization_and_uri_reindex_enqueue_same_text_plan(embedding, monkeypatch):
    from unittest.mock import AsyncMock

    from openviking.server.identity import RequestContext, Role
    from openviking.service import reindex_executor as reindex
    from openviking.utils import embedding_utils as utility
    from openviking_cli.session.user_id import UserIdentifier

    queue = Queue()
    fs = SimpleNamespace(read_file=AsyncMock(return_value=SOURCE), stat=AsyncMock(return_value={}))
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
    ctx = RequestContext(user=UserIdentifier("acct", "tester"), role=Role.ROOT)
    assert await utility.vectorize_file(
        URI, {"name": "维修.md", "summary": "summary"}, "viking://resources/raw", ctx=ctx
    )
    first = list(queue.messages)
    queue.messages.clear()
    executor = reindex.ReindexExecutor()
    executor._best_file_summary = AsyncMock(return_value="summary")
    executor._effective_file_vectorization_concurrency = lambda: 1
    counters = reindex._ReindexCounters()
    await executor._reindex_resource_vectors_from_entries(
        root_uri=URI, directories=[], files=[URI], counters=counters, ctx=ctx
    )
    assert counters.failed_records == 0, counters.warnings
    assert counters.rebuilt_records == 1
    assert len(queue.messages) == len(first) > 1
    for original, rebuilt in zip(first, queue.messages, strict=True):
        assert original.message == rebuilt.message
        for key in [
            "uri",
            "chunk_id",
            "parent_chunk_id",
            "index_plan_id",
            "content_revision",
            "account_id",
        ]:
            assert original.context_data[key] == rebuilt.context_data[key]
    assert fs.read_file.await_count == 2


@pytest.mark.parametrize(
    "incoming, expected",
    [
        (None, ["modelid=sy215"]),
        (
            {"search_tags": ["subject=hydraulics"], "search_tag_mode": "append"},
            ["modelid=sy215", "subject=hydraulics"],
        ),
        ({"search_tags": [], "search_tag_mode": "replace"}, []),
    ],
)
async def test_new_revision_chunks_preserve_or_explicitly_replace_source_tags(
    embedding, monkeypatch, incoming, expected
):
    from unittest.mock import AsyncMock

    from openviking.server.identity import RequestContext, Role
    from openviking.service import reindex_executor as reindex
    from openviking.utils.ingest_options import IngestOptions
    from openviking_cli.session.user_id import UserIdentifier

    manager = SimpleNamespace(
        fetch_by_uri=AsyncMock(return_value={"search_tags": ["modelid=sy215"]})
    )
    monkeypatch.setattr(reindex, "get_service", lambda: SimpleNamespace(vikingdb_manager=manager))
    ctx = RequestContext(user=UserIdentifier("acct", "tester"), role=Role.ROOT)
    result = await reindex.ReindexExecutor()._chunk_reindex_ingest_options(
        URI, ctx=ctx, incoming=IngestOptions.from_value(incoming)
    )
    assert result.search_tags == expected
    assert result.search_tag_mode == "replace"
    queue = Queue()
    base = EmbeddingMsg(
        SOURCE, {"uri": URI, "account_id": "acct", "search_tags": result.search_tags}
    )
    await enqueue_text_plan(base, SOURCE, embedding, queue)
    assert len(queue.messages) > 1
    assert all(msg.context_data["search_tags"] == expected for msg in queue.messages)


def test_chunker_version_change_invalidates_collection_signature(embedding, monkeypatch):
    from openviking.storage.collection_schemas import _build_embedding_metadata

    config = SimpleNamespace(embedding=embedding)
    current = _build_embedding_metadata(config)
    monkeypatch.setattr("openviking.indexing.plan.VERSION", "future-chunker")
    future = _build_embedding_metadata(config)
    assert current["model"] == future["model"]
    assert current["model_identity"] != future["model_identity"]


def test_oversized_figure_caption_splits_into_children_instead_of_failing(embedding):
    """A caption that alone exceeds the child budget must not lose the document.

    PDF-derived captions carry a whole VLM description. Before this, one such
    caption raised ``Child token budget exceeded; no truncation allowed`` and the
    file received no vectors at all -- four manuals in the enterprise tree were
    left one file short of full coverage for exactly this reason.
    """
    from openviking.parse.image_rewrite import wrap_in_figure

    figure_uri = "viking://resources/raw/images/oversize.jpg"
    # Byte-level fixture tokenizer: ~3 tokens per CJK character, so this
    # caption (~104 chars) overflows the 192-token child budget while still
    # fitting inside the 768-token parent budget.
    caption = "液压泵总成拆装与扭矩参数说明。" * 8
    source = "# 维修\n\n" + wrap_in_figure(figure_uri, figure_uri, caption=caption) + "\n\n正文。\n"
    plan = plan_resource(source, URI, "acct", embedding)
    assert "".join(c.source_text for c in plan.chunks) == source
    embedded = "\n".join(c.embedding_text for c in plan.chunks)
    assert "液压泵总成拆装与扭矩参数说明" in embedded, embedded[:120]
    assert all(c.token_count + 16 <= 192 for c in plan.chunks)
    assert len([c for c in plan.chunks if "液压泵总成" in c.embedding_text]) >= 2


def test_split_render_verifies_every_fragment_with_a_non_monotone_tokenizer():
    """A line-boundary cut can price a fragment above the budget it came from.

    Byte-level BPE does not guarantee that a shorter prefix costs fewer tokens
    (this fake prices any newline-terminated prefix at 100), and the running
    server hit exactly that: reindexing 11.3.1_车身线束图.md still failed with
    "Child token budget exceeded" after the render had been split.
    """
    from openviking.indexing.plan import _split_render

    def count(text: str) -> int:
        return 100 if text.endswith("\n") else len(text)

    text = "aaaa\n" + "a" * 30
    fragments = _split_render(text, 20, count)
    assert "".join(fragments) == text
    assert all(count(fragment) <= 20 for fragment in fragments)
