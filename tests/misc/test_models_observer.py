# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Focused tests for model status observability."""

from openviking.storage.observers.models_observer import ModelsObserver


class _ConfiguredVLM:
    model = "astron-code-latest"
    provider = "litellm"

    def get_token_usage(self):
        return {"usage_by_model": {}}


class _ConfiguredVLMWithUnavailableUsage(_ConfiguredVLM):
    def get_token_usage(self):
        raise RuntimeError("usage backend unavailable")


def test_configured_vlm_is_visible_before_usage_is_recorded():
    status = ModelsObserver(vlm_instance=_ConfiguredVLM()).get_status_table()

    assert "VLM Models:" in status
    assert "astron-code-latest" in status
    assert "litellm" in status
    assert "configured" in status


def test_configured_vlm_is_visible_when_usage_lookup_fails():
    status = ModelsObserver(vlm_instance=_ConfiguredVLMWithUnavailableUsage()).get_status_table()

    assert "VLM Models:" in status
    assert "astron-code-latest" in status


class _UnreachableRerank:
    """A configured reranker that has never answered (endpoint refused)."""

    model_name = "qwen3-vl-reranker-2b"
    provider = "openai"
    endpoint_url = "http://10.147.254.3:8029/v1/rerank"

    def get_token_usage(self):
        return {"usage_by_model": {}}


def test_configured_rerank_is_visible_before_it_ever_answers():
    """An unreachable reranker must not be invisible.

    Live: the configured endpoint was down for hours and this table omitted the
    whole Rerank section, so every search fell back to RRF-only ranking with no
    operator-visible trace of why.
    """
    status = ModelsObserver(rerank_instance=_UnreachableRerank()).get_status_table()

    assert "Rerank Models:" in status
    assert "qwen3-vl-reranker-2b" in status
    assert "http://10.147.254.3:8029/v1/rerank" in status
    assert "no successful call yet" in status


def test_recorded_rerank_usage_still_wins_over_the_configured_row():
    class _UsedRerank(_UnreachableRerank):
        def get_token_usage(self):
            return {
                "usage_by_model": {
                    "qwen3-vl-reranker-2b": {
                        "usage_by_provider": {
                            "openai": {
                                "call_count": 7,
                                "prompt_tokens": 70,
                                "completion_tokens": 0,
                                "total_tokens": 70,
                                "last_updated": "2026-10-18T00:00:00Z",
                            }
                        }
                    }
                }
            }

    status = ModelsObserver(rerank_instance=_UsedRerank()).get_status_table()

    assert "qwen3-vl-reranker-2b" in status
    assert "no successful call yet" not in status
