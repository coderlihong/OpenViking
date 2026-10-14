"""Chapter/image occurrence context and no silent text-only image downgrade."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding

from openviking.indexing.image_context import collect_image_contexts, extract_occurrences
from openviking.indexing.record_meta import extract_inline_metadata
from openviking.models.embedder.base import EmbedResult
from openviking.parse.parsers.media import utils as media
from openviking.storage.queuefs.embedding_msg import EmbeddingMsg
from openviking.utils.embedding_utils import vectorize_file
from openviking.utils.token_estimation import estimate_text_tokens

URI = "viking://resources/manual/chapter.md"
IMAGE = "viking://resources/manual/images/a.png"


def test_occurrence_carries_the_chapter_provenance_whether_or_not_the_window_reaches_it():
    """The figure row must stay filterable by the chapter it illustrates.

    A late figure in a long chapter gets a context window that does not contain
    the chapter's ``- 机型:``/``- page:`` block at all, and even for an early
    figure the window opens with a ``Source URI:`` header that the leading-block
    scan cannot get past.  So the provenance is read off the source document here
    and travels with every occurrence.
    """
    head = (
        "# 10.2 进行操作时的注意事项\n\n"
        "- document_id: 797105495448853\n"
        "- chapter_uri: kb://chapters/843907453002281\n"
        "- page: p.638-641\n"
        "- 机型: SY215C9液压挖掘机\n"
        "- 关键词: 拆装, 注意事项, 盲堵\n\n"
    )
    body = "".join(f"第 {i} 条：拆开软管或钢管后，须进行遮盖或装上盲堵以避免污物或灰尘进入。\n\n" for i in range(40))
    figure = '<figure>\n    <img src="images/a.png"/>\n    <figcaption>回转支承螺栓力矩 320 N·m</figcaption>\n</figure>\n'
    found = extract_occurrences(head + body + figure, URI, IMAGE)
    assert found
    meta = found[0]["source_meta"]
    assert meta["page"] == "p.638-641"
    assert meta["machine_model"] == "SY215C9液压挖掘机"
    assert meta["source_document_id"] == "797105495448853"
    assert meta["keywords"] == "拆装, 注意事项, 盲堵"
    # The window alone could not have produced this: the block is far outside it.
    assert "document_id" not in found[0]["context"]
    assert extract_inline_metadata(found[0]["context"]) == {}
    # A chapter with no declared block contributes nothing invented.
    assert extract_occurrences("## 液压阀\n\n![阀](images/a.png)\n", URI, IMAGE)[0]["source_meta"] == {}


def test_ast_uses_real_reference_headings_and_neighbors():
    source = "# 手册\n\n## 液压阀\n\n先检查油路。\n\n![阀芯示意图](images/a.png)\n\n图1：检查阀芯磨损。\n\n```md\n![假引用](images/a.png)\n```\n"
    found = extract_occurrences(source, URI, IMAGE)
    assert len(found) == 1
    assert found[0]["heading_path"] == ["手册", "液压阀"]
    assert "检查油路" in found[0]["context"] and "检查阀芯磨损" in found[0]["context"]
    assert found[0]["context_uri"] == URI


def test_figcaption_is_the_occurrence_description_markdown_alt_is_not():
    # The figcaption is generated offline before upload and is authoritative,
    # so it surfaces as the occurrence's caption. A markdown embed's alt text
    # carries no trusted description and never becomes one.
    source = (
        "# 手册\n\n![markdown 的 alt](images/a.png)\n\n"
        '<figure>\n    <img src="images/a.png"/>\n'
        "    <figcaption>\n    权威图注：液压泵剖面\n    </figcaption>\n</figure>\n"
    )
    found = extract_occurrences(source, URI, IMAGE)
    assert found, "both references must still be discovered"
    captions = [o["caption"] for o in found]
    # Exactly one reference (the figure) carries the trusted caption.
    assert "权威图注：液压泵剖面" in captions
    assert "markdown 的 alt" not in captions
    # The trusted figcaption stays in the neighboring source text as well.
    figure_occurrence = next(o for o in found if o["caption"])
    assert "权威图注" in figure_occurrence["context"]


def test_img_without_figcaption_yields_empty_caption():
    # An <img> with no figcaption (or an empty one) has no trusted description,
    # so the summary path must regenerate through the VLM.
    source = '# 手册\n\n<img src="images/a.png"/>\n'
    found = extract_occurrences(source, URI, IMAGE)
    assert found
    assert found[0]["caption"] == ""


def test_html_reference_repeated_occurrences_and_encoded_relative_paths():
    source = '<img src="images/a.png" alt="液压图">\n\n![图](images/a.png) ![图](images/a.png)\n'
    found = extract_occurrences(source, URI, IMAGE)
    assert len(found) == 3
    assert len({o["context_hash"] for o in found}) == 3
    assert (
        len(extract_occurrences("![图](images/a%20b.png)", URI, IMAGE.replace("a.png", "a b.png")))
        == 1
    )
    assert not extract_occurrences("![外部图](https://example.com/a.png)", URI, IMAGE)


def test_context_budget_centered_on_late_image():
    source = (
        "# 手册\n\n"
        + "前文\n" * 5000
        + "## 末尾章节\n\n![晚图](images/a.png)\n\n晚图用途是校准。\n"
    )
    found = extract_occurrences(source, URI, IMAGE, token_budget=100)
    assert "晚图用途" in found[0]["context"]
    assert estimate_text_tokens(found[0]["context"]) <= 100


async def test_collector_preserves_distinct_source_uri_and_propagates_access_error():
    fs = SimpleNamespace(
        ls=AsyncMock(return_value=[{"name": "a.md"}, {"name": "b.md"}]),
        read_file=AsyncMock(return_value="![图](images/a.png)"),
    )
    found = await collect_image_contexts(fs, IMAGE, "authorized-context")
    assert [o["context_uri"] for o in found] == [
        URI.replace("chapter.md", "a.md"),
        URI.replace("chapter.md", "b.md"),
    ]
    assert all(call.kwargs["ctx"] == "authorized-context" for call in fs.read_file.await_args_list)
    fs.read_file.side_effect = PermissionError("denied")
    with pytest.raises(PermissionError):
        await collect_image_contexts(fs, IMAGE, "authorized-context")


async def test_collector_skips_vanished_source_without_aborting_siblings():
    # A source deleted between listing and reading (or tombstone-refused on a
    # re-created tree) is skipped so the chapter's remaining images still index.
    fs = SimpleNamespace(
        ls=AsyncMock(return_value=[{"name": "a.md"}, {"name": "b.md"}, {"name": "c.md"}]),
        read_file=AsyncMock(return_value="![图](images/a.png)"),
    )
    from openviking_cli.exceptions import NotFoundError

    async def read_by_call(source_uri, ctx=None):
        calls = fs.read_file.await_count
        if calls == 1:
            raise NotFoundError(source_uri, "file")
        return "![图](images/a.png)"

    fs.read_file.side_effect = read_by_call
    found = await collect_image_contexts(fs, IMAGE, None)
    assert [o["context_uri"] for o in found] == [
        URI.replace("chapter.md", "b.md"),
        URI.replace("chapter.md", "c.md"),
    ]


async def test_collector_keeps_more_than_sixteen_references():
    # The old per-image cap of 16 must be gone: 17+ references are all kept.
    fs = SimpleNamespace(
        ls=AsyncMock(return_value=[{"name": "chapter.md"}]),
        read_file=AsyncMock(return_value="![图](images/a.png)\n\n" * 25),
    )
    found = await collect_image_contexts(fs, IMAGE, None)
    assert len(found) == 25
    assert len({o["context_hash"] for o in found}) == 25


async def test_collector_paginates_and_discovers_cross_directory_references():
    # A page boundary at LIST_PAGE_SIZE must not drop entries, and a reference
    # living in a sibling directory must be discovered by recursing.
    from openviking.indexing import image_context as module

    root = "viking://resources/manual"
    sibling = root + "/appendix"
    page = module.LIST_PAGE_SIZE
    listing = {
        root: [{"name": f"fill{index}.md"} for index in range(page)]
        + [{"name": "appendix", "isDir": True, "uri": sibling}, {"name": "images", "isDir": True}],
        sibling: [{"name": "notes.md", "uri": sibling + "/notes.md"}],
    }
    calls = []

    async def fake_ls(directory, node_limit=None, offset=0, ctx=None):
        calls.append((directory, offset))
        return listing.get(directory, [])[offset : offset + (node_limit or len(listing.get(directory, [])))]

    async def fake_read(source_uri, ctx=None):
        if source_uri == sibling + "/notes.md":
            return "![附录图](../images/a.png)"
        return "no image here"

    fs = SimpleNamespace(ls=AsyncMock(side_effect=fake_ls), read_file=AsyncMock(side_effect=fake_read))
    found = await collect_image_contexts(fs, IMAGE, None)
    assert [o["context_uri"] for o in found] == [sibling + "/notes.md"]
    assert (root, 0) in calls and (root, page) in calls  # pagination advanced
    assert (sibling, 0) in calls  # recursion entered the sibling directory


async def test_collector_uses_injected_tokenizer_budget_not_fixed_count():
    # The budget is measured by the supplied counter, not a hardcoded length.
    seen = []

    def counter(text):
        seen.append(text)
        return len(text.split())

    fs = SimpleNamespace(
        ls=AsyncMock(return_value=[{"name": "chapter.md"}]),
        read_file=AsyncMock(return_value="word " * 5000 + "\n\n![图](images/a.png)\n\n用途说明\n"),
    )
    found = await collect_image_contexts(fs, IMAGE, None, token_budget=50, count_tokens=counter)
    assert len(found) == 1
    assert counter(found[0]["context"]) <= 50
    assert "用途说明" in found[0]["context"]  # tail retained
    assert seen  # the injected tokenizer was actually used


async def test_run_cache_reads_and_parses_each_source_once_across_images():
    # Two assets in one chapter must share a single read+parse of the chapter's
    # markdown; without the run cache every image re-reads and re-parses it.
    from openviking.indexing.image_context import image_context_cache

    fs = SimpleNamespace(
        ls=AsyncMock(return_value=[{"name": "chapter.md"}]),
        read_file=AsyncMock(return_value="![图](images/a.png)\n\n![图2](images/b.png)\n"),
    )
    image_a = "viking://resources/manual/images/a.png"
    image_b = "viking://resources/manual/images/b.png"

    with image_context_cache():
        first = await collect_image_contexts(fs, image_a, None)
        second = await collect_image_contexts(fs, image_b, None)

    assert [o["context_uri"] for o in first] == [URI]
    assert [o["context_uri"] for o in second] == [URI]
    assert fs.read_file.await_count == 1  # chapter read once for both images
    assert fs.ls.await_count == 1  # chapter listing reused too


def test_resolve_reference_treats_hash_and_query_as_filename_characters():
    # A Viking path is a filesystem path: '#' and '?' are ordinary filename
    # characters. url-based parsing truncated them, rejecting valid sources and
    # aborting the whole chapter's context collection.
    from openviking.indexing.image_context import resolve_reference

    hashed = "viking://resources/raw/menus/1/9.6/9.6.53_nozzle_#1_drive.md"
    assert resolve_reference(hashed, hashed) == hashed
    percent = "viking://resources/raw/menus/1/9.9/9.9.9_SOC_below_20%.md"
    assert resolve_reference(percent, percent) == percent
    # Relative and absolute local references still resolve; foreign schemes do not.
    assert (
        resolve_reference("viking://resources/a/b/c.md", "images/x.jpg")
        == "viking://resources/a/b/images/x.jpg"
    )
    assert resolve_reference("viking://resources/a/b/c.md", "//evil/x.jpg") is None
    assert resolve_reference("viking://resources/a/b/c.md", "http://evil/x.jpg") is None


def test_vlm_token_counter_is_none_without_configured_tokenizer(monkeypatch):
    from openviking.indexing import image_context as module

    monkeypatch.setattr(
        "openviking_cli.utils.config.get_openviking_config",
        lambda: SimpleNamespace(embedding=SimpleNamespace(chunking=SimpleNamespace(tokenizer_path="", tokenizer_sha256=""))),
    )
    assert module.load_vlm_token_counter() is None


def test_vlm_token_counter_uses_configured_model_tokenizer(tmp_path, monkeypatch):
    import hashlib as _hashlib

    from tokenizers import Tokenizer, models, pre_tokenizers

    from openviking.indexing import image_context as module

    alphabet = pre_tokenizers.ByteLevel.alphabet()
    tokenizer = Tokenizer(models.BPE(vocab={c: i for i, c in enumerate(alphabet)}, merges=[]))
    tokenizer.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    path = tmp_path / "tokenizer.json"
    tokenizer.save(str(path))
    chunking = SimpleNamespace(
        tokenizer_path=str(path), tokenizer_sha256=_hashlib.sha256(path.read_bytes()).hexdigest()
    )
    monkeypatch.setattr(
        "openviking_cli.utils.config.get_openviking_config",
        lambda: SimpleNamespace(vlm=SimpleNamespace(model="vision-test", context_tokenizers=[SimpleNamespace(model="vision-test", path=chunking.tokenizer_path, sha256=chunking.tokenizer_sha256)])),
    )
    counter = module.load_vlm_token_counter()
    assert counter is not None
    assert counter("hello world") > 0


async def test_vlm_receives_each_occurrence_with_image_and_uncertainty_prompt(
    environment, monkeypatch
):
    e = environment
    import io

    from PIL import Image

    raw = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(raw, format="PNG")
    await e.fs.write(IMAGE, raw.getvalue(), ctx=e.ctx)
    from pathlib import Path

    from openviking.prompts.manager import PromptManager

    manager = PromptManager(templates_dir=Path("openviking/prompts/templates"))
    monkeypatch.setattr(media, "render_prompt", manager.render)
    vlm = SimpleNamespace(get_vision_completion_async=AsyncMock(side_effect=["用途A", "用途B"]))
    monkeypatch.setattr(media, "get_viking_fs", lambda: e.fs)
    monkeypatch.setattr(media, "get_openviking_config", lambda: SimpleNamespace(vlm=vlm))
    contexts = [
        {"context": "Chapter A hydraulic valve", "context_uri": URI, "context_hash": "A"},
        {
            "context": "Chapter B electronics",
            "context_uri": URI.replace("chapter", "other"),
            "context_hash": "B",
        },
    ]
    result = await media.generate_image_summary(
        IMAGE, "a.png", contexts=contexts, ctx=e.ctx
    )
    assert [o["summary"] for o in result["image_occurrences"]] == ["用途A", "用途B"]
    calls = vlm.get_vision_completion_async.await_args_list
    assert "hydraulic valve" in calls[0].kwargs["prompt"]
    assert "electronics" in calls[1].kwargs["prompt"]
    assert "uncertainty" in calls[0].kwargs["prompt"]
    assert all(len(call.kwargs["images"]) == 1 for call in calls)
    vlm.get_vision_completion_async.side_effect = RuntimeError("VLM offline")
    result_offline = await media.generate_image_summary(
        IMAGE, "a.png", contexts=contexts, ctx=e.ctx
    )
    # A VLM outage is a failed description, not a crash: the caller records
    # the image as unsupported and the reindex continues.
    assert "generation failed" in result_offline["summary"]


async def test_trusted_caption_short_circuits_vlm_and_missing_caption_regenerates(
    environment, monkeypatch
):
    """A trusted figcaption is the summary; only a caption-less occurrence hits the VLM."""
    e = environment
    import io

    from PIL import Image

    raw = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(raw, format="PNG")
    await e.fs.write(IMAGE, raw.getvalue(), ctx=e.ctx)
    from pathlib import Path

    from openviking.prompts.manager import PromptManager

    manager = PromptManager(templates_dir=Path("openviking/prompts/templates"))
    monkeypatch.setattr(media, "render_prompt", manager.render)
    vlm = SimpleNamespace(get_vision_completion_async=AsyncMock(return_value="VLM B"))
    monkeypatch.setattr(media, "get_viking_fs", lambda: e.fs)
    monkeypatch.setattr(media, "get_openviking_config", lambda: SimpleNamespace(vlm=vlm))

    contexts = [
        {"context": "Chapter A", "context_uri": URI, "context_hash": "A", "caption": "阀芯磨损示意"},
        {"context": "Chapter B", "context_uri": URI + "b", "context_hash": "B", "caption": "  "},
    ]
    result = await media.generate_image_summary(
        IMAGE, "a.png", contexts=contexts, ctx=e.ctx
    )
    records = result["image_occurrences"]
    # The trusted caption is reused verbatim; only the caption-less occurrence
    # is regenerated by the VLM (exactly one call).
    assert [r["summary"] for r in records] == ["阀芯磨损示意", "VLM B"]
    assert vlm.get_vision_completion_async.await_count == 1


async def test_empty_vlm_response_retries_then_settles_low_information_placeholder(
    environment, monkeypatch
):
    e = environment
    import io

    from PIL import Image

    raw = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(raw, format="PNG")
    await e.fs.write(IMAGE, raw.getvalue(), ctx=e.ctx)
    from pathlib import Path

    from openviking.prompts.manager import PromptManager

    manager = PromptManager(templates_dir=Path("openviking/prompts/templates"))
    monkeypatch.setattr(media, "render_prompt", manager.render)
    # First call transiently empty, second call recovers: no placeholder.
    vlm = SimpleNamespace(get_vision_completion_async=AsyncMock(side_effect=["", "恢复摘要"]))
    monkeypatch.setattr(media, "get_viking_fs", lambda: e.fs)
    monkeypatch.setattr(media, "get_openviking_config", lambda: SimpleNamespace(vlm=vlm))
    result = await media.generate_image_summary(IMAGE, "a.png", ctx=e.ctx)
    assert result["summary"] == "恢复摘要"
    assert vlm.get_vision_completion_async.await_count == 2

    # Deterministically empty (near-blank image): settle with honest
    # low-information placeholder instead of raising SEMANTICS_REQUIRED forever.
    vlm2 = SimpleNamespace(get_vision_completion_async=AsyncMock(return_value="  "))
    monkeypatch.setattr(media, "get_openviking_config", lambda: SimpleNamespace(vlm=vlm2))
    result2 = await media.generate_image_summary(IMAGE, "a.png", ctx=e.ctx)
    assert "Low-information image" in result2["summary"]
    assert vlm2.get_vision_completion_async.await_count == 2


class JointEmbedder:
    supports_multimodal = True

    def __init__(self):
        self.inputs = []

    def prepare_embedding_input(self, content):
        return content

    async def embed_async(self, content, is_query=False):
        self.inputs.append(content)
        return EmbedResult(dense_vector=[1.0, 2.0])


async def test_actual_image_producer_and_consumer_send_joint_input(
    environment, embedding, monkeypatch
):
    e = environment
    model = JointEmbedder()
    monkeypatch.setattr(type(embedding), "get_embedder", lambda self: model)
    e.handler._embedder = model
    monkeypatch.setattr("openviking.storage.viking_fs.get_viking_fs", lambda: e.fs)
    await e.fs.write(IMAGE, b"image bytes", ctx=e.ctx)
    assert await vectorize_file(
        IMAGE,
        {"name": "a.png", "summary": "阀芯图用于检查磨损"},
        IMAGE.rsplit("/", 1)[0],
        ctx=e.ctx,
    )
    message = e.queue.messages[0]
    assert message.context_data["_require_joint_image_embedding"]
    result = await e.handler.on_dequeue({"data": message.to_json()})
    assert result.outcome.value == "success", result.error
    assert len(model.inputs) == 1
    assert [p["type"] for p in model.inputs[0]] == ["text", "image_url"]
    assert model.inputs[0][0]["text"] == "阀芯图用于检查磨损"
    assert "_require_joint_image_embedding" not in next(iter(e.handler._vikingdb.records.values()))
    assert message.context_data["_require_joint_image_embedding"]  # Retry envelope keeps guard.


@pytest.mark.parametrize("failure", ["summary", "provider"])
async def test_enterprise_image_producer_skips_undescribed_or_text_only_provider(
    environment, embedding, monkeypatch, failure
):
    """A captionless image (or a text-only embedder) enqueues nothing: skip."""
    e = environment
    model = SimpleNamespace(supports_multimodal=False)
    monkeypatch.setattr(type(embedding), "get_embedder", lambda self: model)
    await e.fs.write(IMAGE, b"image bytes", ctx=e.ctx)
    assert not await vectorize_file(
        IMAGE,
        {"name": "a.png", "summary": "" if failure == "summary" else "valid summary"},
        IMAGE.rsplit("/", 1)[0],
        ctx=e.ctx,
    )
    assert not e.queue.messages


async def test_consumer_rejects_joint_envelope_if_runtime_provider_is_text_only(environment):
    e = environment
    msg = EmbeddingMsg(
        [
            {"type": "text", "text": "summary"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}},
        ],
        {
            "uri": IMAGE,
            "account_id": "acct",
            "level": 2,
            "is_leaf": True,
            "context_type": "resource",
            "_require_joint_image_embedding": True,
        },
    )
    result = await e.handler.on_dequeue({"data": msg.to_json()})
    assert result.outcome.value == "failed"
    assert not e.handler._vikingdb.records


def _vlm_endpoint_reachable(host="10.147.254.3", port=36688):
    import socket

    probe = socket.socket()
    probe.settimeout(2)
    try:
        probe.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


@pytest.mark.integration
async def test_real_vlm_image_summary_follows_template_contract():
    """Real VLM call proves the template contract is actually exercised.

    Quality against a human gold answer is NOT_RUN; this only proves the real
    endpoint returns a nonempty summary produced from the updated template.
    """
    import base64
    import io
    from pathlib import Path

    import httpx
    from PIL import Image

    from openviking.prompts.manager import PromptManager

    if not _vlm_endpoint_reachable():
        pytest.skip("real VLM endpoint is unreachable")

    manager = PromptManager(templates_dir=Path("openviking/prompts/templates"))
    context = (
        "Source URI: viking://resources/manual/chapter.md\n"
        "Chapter: 手册 > 液压阀\n"
        "Neighboring source text:\n先检查油路，再检查阀芯磨损。"
    )
    prompt = manager.render("parsing.image_summary", {"context": context})
    assert "Separate observation from inference" in prompt
    assert "uncertainty" in prompt

    raw = io.BytesIO()
    Image.new("RGB", (64, 64), "steelblue").save(raw, format="PNG")
    data_url = "data:image/png;base64," + base64.b64encode(raw.getvalue()).decode()
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            "http://10.147.254.3:36688/v1/chat/completions",
            json={
                "model": "qwen3.8-27b",
                "temperature": 0.0,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
            },
        )
    if response.status_code != 200:
        pytest.skip(f"real VLM endpoint returned {response.status_code}")
    summary = response.json()["choices"][0]["message"]["content"]
    assert summary.strip()


async def test_uri_image_reindex_uses_same_joint_producer(environment, embedding, monkeypatch):
    from openviking.service import reindex_executor as reindex

    e = environment
    model = JointEmbedder()
    monkeypatch.setattr(type(embedding), "get_embedder", lambda self: model)
    monkeypatch.setattr(reindex, "get_viking_fs", lambda: e.fs)
    monkeypatch.setattr(reindex, "get_openviking_config", lambda: SimpleNamespace(embedding=embedding))
    await e.fs.write(IMAGE, b"original image", ctx=e.ctx)
    executor = reindex.ReindexExecutor()
    executor._effective_file_vectorization_concurrency = lambda: 1
    executor._best_file_summary = AsyncMock(return_value="阀芯检修图")
    executor._chunk_reindex_ingest_options = AsyncMock(return_value=None)
    executor._upsert_context = AsyncMock(side_effect=AssertionError("text-only path forbidden"))
    counters = reindex._ReindexCounters()
    # Reindex never calls the VLM itself: the citation stage generated the
    # caption before this run, and its result rides in as image_summaries.
    details = {
        "name": IMAGE.rsplit("/", 1)[-1],
        "summary": "阀芯检修图",
        "image_occurrences": [{"summary": "阀芯检修图"}],
    }
    await executor._reindex_resource_vectors_from_entries(
        root_uri=IMAGE,
        directories=[],
        files=[IMAGE],
        counters=counters,
        ctx=e.ctx,
        image_summaries={IMAGE: details},
    )
    assert counters.failed_records == 0, counters.warnings
    assert counters.rebuilt_records == 1
    message = e.queue.messages[0]
    assert message.context_data["_require_joint_image_embedding"] is True
    assert {part["type"] for part in message.message} == {"text", "image_url"}
    executor._upsert_context.assert_not_awaited()


TWIN_RECORD = {
    "name": "a.png",
    "summary": "另一分支的图描述",
    "image_occurrences": [
        {
            "summary": "另一分支的图描述",
            "context_status": "twin_reuse",
            "asset_sha256": "b" * 64,
            "semantics_signature": "published-twin",
        }
    ],
}


async def _twin_sandbox(environment, monkeypatch):
    """One published twin asset plus a mock VLM that counts its calls."""
    import io
    from pathlib import Path

    from PIL import Image

    from openviking.prompts.manager import PromptManager

    e = environment
    raw = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(raw, format="PNG")
    await e.fs.write(IMAGE, raw.getvalue(), ctx=e.ctx)
    monkeypatch.setattr(
        media, "render_prompt", PromptManager(templates_dir=Path("openviking/prompts/templates")).render
    )
    vlm = SimpleNamespace(get_vision_completion_async=AsyncMock(return_value="本章上下文下的描述"))
    monkeypatch.setattr(media, "get_viking_fs", lambda: e.fs)
    monkeypatch.setattr(media, "get_openviking_config", lambda: SimpleNamespace(vlm=vlm))
    twin = AsyncMock(return_value=TWIN_RECORD)
    monkeypatch.setattr("openviking.indexing.image_cache.load_twin_image_semantics", twin)
    return e, vlm, twin


async def test_cited_copy_describes_its_own_context_instead_of_a_byte_identical_twin(
    environment, monkeypatch
):
    """Twin reuse serves uncited copies only; a citing copy keeps its own context.

    The twin's description was written from another branch's chapter context, and
    the semantics cache binds every summary to its reference context. Returning it
    for a copy this run's markdown cites also left that markdown's ``<figcaption>``
    empty forever: the reused record carries no ``context_uri``, so caption
    backfill had nothing to target.
    """
    e, vlm, twin = await _twin_sandbox(environment, monkeypatch)
    contexts = [
        {
            "context": "Chapter: 10.2 进行操作时的注意事项\nNeighboring source text:\n装上盲堵",
            "context_uri": URI,
            "context_hash": "A",
            "caption": "",
        }
    ]
    result = await media.generate_image_summary(
        IMAGE, "a.png", contexts=contexts, ctx=e.ctx
    )
    assert twin.await_count == 1
    assert result is not TWIN_RECORD
    assert vlm.get_vision_completion_async.await_count == 1
    records = result["image_occurrences"]
    assert [r["context_uri"] for r in records] == [URI]
    assert records[0]["summary"] == "本章上下文下的描述"

    # The same asset in a directory whose markdown cites nothing has no context
    # of its own, so the published twin still wins and no VLM call is billed.
    vlm.get_vision_completion_async.reset_mock()
    uncited = await media.generate_image_summary(
        IMAGE, "a.png", contexts=[], ctx=e.ctx
    )
    assert uncited is TWIN_RECORD
    assert vlm.get_vision_completion_async.await_count == 0


class _CaptionFS:
    """The VikingFS surface caption backfill needs: read a md, write it back."""

    def __init__(self, files):
        self.files = dict(files)
        self.writes = []

    async def read_file(self, uri, ctx=None):
        return self.files[uri]

    async def write_file(self, uri, content, ctx=None, lease_ref=None):
        self.files[uri] = content
        self.writes.append((uri, lease_ref))


_CAPTION_SUMMARY = {
    "name": "a.png",
    "summary": "本章上下文下的描述",
    "image_occurrences": [
        {"context_uri": URI, "summary": "本章上下文下的描述", "ordinal": 0}
    ],
}
_EMPTY_FIGURE_MARKDOWN = (
    "# 10.2 进行操作时的注意事项\n\n<figure>\n"
    f'    <img src="images/a.png" vikingsrc="{IMAGE}"/>\n'
    "    <figcaption></figcaption>\n</figure>\n"
)


async def test_citation_stage_backfills_the_generated_caption_into_the_corpus(
    environment, monkeypatch
):
    """The citation stage writes its generated description into the corpus.

    Caption generation belongs to the citation-normalization stage, not to
    reindex: the backfill fills the empty ``<figcaption>`` of every figure
    citing the described image, inside the caller's pathlock lease.
    """
    from openviking.indexing import citation_stage

    e = environment
    fs = _CaptionFS({URI: _EMPTY_FIGURE_MARKDOWN})
    monkeypatch.setattr(citation_stage, "get_logger", lambda name: _NullLogger())
    from openviking.parse.figure_caption import backfill_image_captions

    written = await backfill_image_captions(
        fs, IMAGE, _CAPTION_SUMMARY, ctx=e.ctx, lock={"lease_ref": "stage-lease"}
    )
    assert written == [URI]
    assert "本章上下文下的描述" in fs.files[URI]
    # The write stays inside the stage's pathlock lease.
    assert fs.writes == [(URI, {"lease_ref": "stage-lease"})]


class _NullLogger:
    def warning(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass


async def test_reindex_does_not_backfill_captions_but_forwards_semantic_summaries(
    environment, monkeypatch
):
    """Reindex is pure compute over published bytes.

    The vector phase receives whatever image summaries the semantic phase
    produced, but reindex itself never rewrites the markdown it archives —
    caption generation runs in the citation stage before reindex starts.
    """
    from openviking.service import reindex_executor as reindex

    e = environment
    executor = reindex.ReindexExecutor()
    order: list[str] = []

    async def semantic(**kwargs):
        order.append("semantic")
        return {"image_summaries": {IMAGE: _CAPTION_SUMMARY}}

    async def vectors(**kwargs):
        order.append("vectors")
        assert kwargs.get("image_summaries") == {IMAGE: _CAPTION_SUMMARY}

    monkeypatch.setattr(executor, "_run_semantic_processor", semantic)
    monkeypatch.setattr(executor, "_reindex_resource_vectors", vectors)
    monkeypatch.setattr(executor, "_update_stage", AsyncMock())
    run = reindex._ReindexRunContext(
        ctx=e.ctx, counters=reindex._ReindexCounters(), lock={"lease_ref": "l"}
    )
    await executor._reindex_resource(uri=URI, mode="semantic_and_vectors", run=run)
    assert order == ["semantic", "vectors"]
