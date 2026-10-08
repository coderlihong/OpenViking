"""Chapter/image occurrence context and no silent text-only image downgrade."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding

from openviking.indexing.image_context import collect_image_contexts, extract_occurrences
from openviking.models.embedder.base import EmbedResult
from openviking.parse.parsers.media import utils as media
from openviking.storage.queuefs.embedding_msg import EmbeddingMsg
from openviking.utils.embedding_utils import vectorize_file
from openviking.utils.token_estimation import estimate_text_tokens

URI = "viking://resources/manual/chapter.md"
IMAGE = "viking://resources/manual/images/a.png"


def test_ast_uses_real_reference_headings_caption_and_neighbors():
    source = "# 手册\n\n## 液压阀\n\n先检查油路。\n\n![阀芯示意图](images/a.png)\n\n图1：检查阀芯磨损。\n\n```md\n![假引用](images/a.png)\n```\n"
    found = extract_occurrences(source, URI, IMAGE)
    assert len(found) == 1
    assert found[0]["heading_path"] == ["手册", "液压阀"]
    assert found[0]["caption"] == "阀芯示意图"
    assert "检查油路" in found[0]["context"] and "检查阀芯磨损" in found[0]["context"]
    assert found[0]["context_uri"] == URI


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
        IMAGE, "a.png", contexts=contexts, strict=True, ctx=e.ctx
    )
    assert [o["summary"] for o in result["image_occurrences"]] == ["用途A", "用途B"]
    calls = vlm.get_vision_completion_async.await_args_list
    assert "hydraulic valve" in calls[0].kwargs["prompt"]
    assert "electronics" in calls[1].kwargs["prompt"]
    assert "uncertainty" in calls[0].kwargs["prompt"]
    assert all(len(call.kwargs["images"]) == 1 for call in calls)
    vlm.get_vision_completion_async.side_effect = RuntimeError("VLM offline")
    with pytest.raises(RuntimeError, match="offline"):
        await media.generate_image_summary(
            IMAGE, "a.png", contexts=contexts, strict=True, ctx=e.ctx
        )


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
    result = await media.generate_image_summary(IMAGE, "a.png", strict=True, ctx=e.ctx)
    assert result["summary"] == "恢复摘要"
    assert vlm.get_vision_completion_async.await_count == 2

    # Deterministically empty (near-blank image): settle with honest
    # low-information placeholder instead of raising SEMANTICS_REQUIRED forever.
    vlm2 = SimpleNamespace(get_vision_completion_async=AsyncMock(return_value="  "))
    monkeypatch.setattr(media, "get_openviking_config", lambda: SimpleNamespace(vlm=vlm2))
    result2 = await media.generate_image_summary(IMAGE, "a.png", strict=True, ctx=e.ctx)
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
async def test_enterprise_image_producer_rejects_missing_semantics_or_text_only_provider(
    environment, embedding, monkeypatch, failure
):
    e = environment
    model = SimpleNamespace(supports_multimodal=False)
    monkeypatch.setattr(type(embedding), "get_embedder", lambda self: model)
    await e.fs.write(IMAGE, b"image bytes", ctx=e.ctx)
    with pytest.raises(ValueError):
        await vectorize_file(
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
        "Chapter: 手册 > 液压阀\nCaption: 阀芯示意图\n"
        "Neighboring source text:\n先检查油路，再检查阀芯磨损。"
    )
    prompt = manager.render("parsing.image_summary", {"context": context})
    assert "Separate visible facts from contextual interpretation" in prompt
    assert "mark uncertainty" in prompt

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
    await executor._reindex_resource_vectors_from_entries(
        root_uri=IMAGE, directories=[], files=[IMAGE], counters=counters, ctx=e.ctx
    )
    assert counters.failed_records == 0, counters.warnings
    assert counters.rebuilt_records == 1
    message = e.queue.messages[0]
    assert message.context_data["_require_joint_image_embedding"] is True
    assert {part["type"] for part in message.message} == {"text", "image_url"}
    executor._upsert_context.assert_not_awaited()
