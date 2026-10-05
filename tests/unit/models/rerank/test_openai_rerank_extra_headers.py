# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Tests for OpenAIRerankClient extra_headers support."""

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


def test_openai_rerank_client_init_with_extra_headers():
    """Test that OpenAIRerankClient accepts and stores extra_headers."""
    client = OpenAIRerankClient(
        api_key="test-key",
        api_base="https://api.example.com/v1",
        model_name="gpt-4",
        extra_headers={"x-gw-apikey": "Bearer real-key"},
    )

    assert client.extra_headers == {"x-gw-apikey": "Bearer real-key"}


def test_openai_rerank_client_init_without_extra_headers():
    """Test that OpenAIRerankClient defaults to empty dict when extra_headers is None."""
    client = OpenAIRerankClient(
        api_key="test-key",
        api_base="https://api.example.com/v1",
        model_name="gpt-4",
        extra_headers=None,
    )

    assert client.extra_headers == {}


def test_openai_rerank_from_config_with_extra_headers():
    """Test that from_config correctly extracts extra_headers from RerankConfig."""
    config = RerankConfig(
        model="gpt-4",
        api_key="test-key",
        api_base="https://api.example.com/v1",
        extra_headers={"x-custom": "value"},
    )

    client = OpenAIRerankClient.from_config(config)

    assert client.extra_headers == {"x-custom": "value"}


def test_openai_rerank_from_config_without_extra_headers():
    """Test that from_config handles None extra_headers correctly."""
    config = RerankConfig(model="gpt-4", api_key="test-key", api_base="https://api.example.com/v1")

    client = OpenAIRerankClient.from_config(config)

    assert client.extra_headers == {}


def test_rerank_batch_includes_extra_headers():
    """Test that rerank_batch passes extra_headers as SDK default_headers."""
    client = OpenAIRerankClient(
        api_key="test-key",
        api_base="https://api.example.com/v1",
        model_name="gpt-4",
        extra_headers={"x-gw-apikey": "Bearer real-key", "X-Custom-Header": "custom-value"},
    )

    capture: dict = {}
    with _mock_sdk(
        {"results": [{"index": 0, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.8}]},
        capture=capture,
    ):
        client.rerank_batch(query="test query", documents=["doc1", "doc2"])

    # The SDK receives extra_headers verbatim as default_headers; it merges the
    # Authorization header itself from api_key.
    assert capture["default_headers"] == {
        "x-gw-apikey": "Bearer real-key",
        "X-Custom-Header": "custom-value",
    }
    assert capture["api_key"] == "test-key"


def test_rerank_batch_without_extra_headers():
    """Test that no default_headers kwarg is sent when extra_headers is empty."""
    client = OpenAIRerankClient(
        api_key="test-key", api_base="https://api.example.com/v1", model_name="gpt-4"
    )

    capture: dict = {}
    with _mock_sdk({"results": [{"index": 0, "relevance_score": 0.9}]}, capture=capture):
        client.rerank_batch(query="test query", documents=["doc1"])

    assert "default_headers" not in capture
    assert capture["api_key"] == "test-key"


def test_extra_headers_can_override_defaults():
    """Test that extra_headers can carry a custom Content-Type through the SDK."""
    client = OpenAIRerankClient(
        api_key="test-key",
        api_base="https://api.example.com/v1",
        model_name="gpt-4",
        extra_headers={"Content-Type": "application/json; charset=utf-8"},
    )

    capture: dict = {}
    with _mock_sdk({"results": []}, capture=capture):
        client.rerank_batch(query="test", documents=["doc"])

    assert capture["default_headers"]["Content-Type"] == "application/json; charset=utf-8"
