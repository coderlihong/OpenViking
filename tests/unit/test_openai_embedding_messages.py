"""Exercise actual OpenAI SDK transport for the vLLM messages extension."""

import base64
import json
import struct

import httpx
import openai
import pytest

from openviking.models.embedder.openai_embedders import OpenAIDenseEmbedder
from openviking_cli.utils.config.embedding_config import EmbeddingConfig

PARTS = [{"type": "text", "text": "repair diagram"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}}]


def service(request, seen):
    body = json.loads(request.content)
    seen.append(body)
    assert request.url.path == "/v1/embeddings"
    assert "input" not in body
    vector = [1.0, 2.0]
    if body["encoding_format"] == "base64":
        vector = base64.b64encode(struct.pack("<ff", *vector)).decode()
    return httpx.Response(200, json={"object": "list", "model": "fixture", "data": [{"object": "embedding", "index": 0, "embedding": vector}], "usage": {"prompt_tokens": 1, "total_tokens": 1}})


@pytest.mark.parametrize("encoding", [None, "base64"])
def test_sync_messages_joint_and_text_query(encoding):
    seen = []
    model = OpenAIDenseEmbedder(model_name="fixture", api_base="http://fixture/v1", dimension=2, input_type="multimodal", wire_format="messages", encoding_format=encoding, query_param="task=query", document_param="task=document", config={"max_retries": 0})
    model.client.close()
    model.client = openai.OpenAI(api_key="fixture", base_url="http://fixture/v1", http_client=httpx.Client(transport=httpx.MockTransport(lambda r: service(r, seen))))
    try:
        assert model.embed(PARTS).dense_vector == [1.0, 2.0]
        assert seen[0]["messages"] == [{"role": "user", "content": PARTS}]
        assert seen[0]["task"] == "document"
        assert model.embed("symptom", is_query=True).dense_vector == [1.0, 2.0]
        assert seen[1]["messages"] == [{"role": "user", "content": "symptom"}]
        assert seen[1]["task"] == "query"
    finally:
        model.close()


async def test_async_messages_uses_real_sdk_transport():
    seen = []
    client = openai.AsyncOpenAI(api_key="fixture", base_url="http://fixture/v1", http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: service(r, seen))))
    model = OpenAIDenseEmbedder(model_name="fixture", api_base="http://fixture/v1", dimension=2, input_type="multimodal", wire_format="messages")
    model._get_async_client = lambda: client
    try:
        assert (await model.embed_async(PARTS)).dense_vector == [1.0, 2.0]
        assert seen[0]["messages"][0]["content"] == PARTS
    finally:
        await client.close()
        model.close()


@pytest.mark.parametrize("override", ["messages", "input", "model", "dimensions", "encoding_format"])
def test_messages_rejects_extra_body_input_or_identity_override(override):
    model = OpenAIDenseEmbedder(model_name="fixture", api_base="http://fixture/v1", dimension=2, wire_format="messages", extra_body={override: "override"})
    try:
        with pytest.raises(ValueError, match="override"):
            model._build_kwargs("text")
    finally:
        model.close()


def test_config_factory_preserves_protocol_and_does_not_merge_text_batches():
    config = EmbeddingConfig(dense={"provider": "openai", "model": "fixture", "api_base": "http://fixture/v1", "dimension": 2, "input": "multimodal", "wire_format": "messages"})
    model = config.get_embedder()
    try:
        assert model.wire_format == "messages" and model.supports_multimodal
        with pytest.raises(ValueError, match="batch"):
            model._build_kwargs(["one", "two"])
    finally:
        model.close()
