# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Tests for OpenAIRerankClient configurable HTTP timeout support."""

from unittest.mock import MagicMock, patch

from openviking.models.rerank.openai_rerank import OpenAIRerankClient
from openviking_cli.utils.config.rerank_config import RerankConfig


def _mock_sdk(json_payload, capture=None):
    """Patch openai.OpenAI; capture the constructor kwargs when given a dict."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = json_payload

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    mock_client.post.return_value = mock_response

    def _factory(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        return mock_client

    return patch(
        "openviking.models.rerank.openai_rerank.openai.OpenAI", side_effect=_factory
    )


def test_openai_rerank_client_default_timeout():
    """Client defaults to a 30s timeout when none is provided."""
    client = OpenAIRerankClient(
        api_key="test-key",
        api_base="https://api.example.com/v1",
        model_name="qwen3-rerank",
    )

    assert client.timeout == 30.0


def test_openai_rerank_client_custom_timeout():
    """Client stores an explicitly provided timeout."""
    client = OpenAIRerankClient(
        api_key="test-key",
        api_base="https://api.example.com/v1",
        model_name="qwen3-rerank",
        timeout=120.0,
    )

    assert client.timeout == 120.0


def test_rerank_config_default_timeout():
    """RerankConfig defaults timeout to 30s for backwards compatibility."""
    config = RerankConfig(
        model="qwen3-rerank",
        api_key="test-key",
        api_base="https://api.example.com/v1",
    )

    assert config.timeout == 30.0


def test_openai_rerank_from_config_with_custom_timeout():
    """from_config threads a custom timeout through to the client."""
    config = RerankConfig(
        model="qwen3-rerank",
        api_key="test-key",
        api_base="https://api.example.com/v1",
        timeout=120.0,
    )

    client = OpenAIRerankClient.from_config(config)

    assert client.timeout == 120.0


def test_openai_rerank_from_config_default_timeout():
    """from_config preserves the 30s default when timeout is unset."""
    config = RerankConfig(
        model="qwen3-rerank",
        api_key="test-key",
        api_base="https://api.example.com/v1",
    )

    client = OpenAIRerankClient.from_config(config)

    assert client.timeout == 30.0


def test_rerank_batch_uses_configured_timeout():
    """rerank_batch passes the configured timeout to the OpenAI SDK client."""
    client = OpenAIRerankClient(
        api_key="test-key",
        api_base="https://api.example.com/v1",
        model_name="qwen3-rerank",
        timeout=120.0,
    )

    capture: dict = {}
    with _mock_sdk(
        {"results": [{"index": 0, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.8}]},
        capture=capture,
    ):
        client.rerank_batch(query="test query", documents=["doc1", "doc2"])

    assert capture["timeout"] == 120.0


def test_rerank_batch_uses_default_timeout():
    """rerank_batch falls back to the 30s default when no timeout is configured."""
    client = OpenAIRerankClient(
        api_key="test-key", api_base="https://api.example.com/v1", model_name="qwen3-rerank"
    )

    capture: dict = {}
    with _mock_sdk({"results": [{"index": 0, "relevance_score": 0.9}]}, capture=capture):
        client.rerank_batch(query="test query", documents=["doc1"])

    assert capture["timeout"] == 30.0
