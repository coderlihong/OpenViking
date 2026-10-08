# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Model HTTP clients must not route local endpoints through an ambient proxy."""

from __future__ import annotations

import httpx
import openai
import pytest

from openviking.models.http_client import _endpoint_is_local, build_http_client


@pytest.mark.parametrize(
    "api_base",
    [
        "http://127.0.0.1:36688/v1",
        "http://localhost:8020/v1",
        "http://10.147.254.3:36688/v1",
        "http://192.168.1.20:8029/v1",
        "http://[::1]:8080/v1",
    ],
)
def test_local_endpoints_detected(api_base):
    assert _endpoint_is_local(api_base) is True


@pytest.mark.parametrize(
    "api_base",
    [
        "https://api.openai.com/v1",
        "https://ark.cn-beijing.volces.com/api/v3",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ],
)
def test_public_endpoints_not_local(api_base):
    assert _endpoint_is_local(api_base) is False


def test_public_endpoint_honors_ambient_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    client = build_http_client("https://api.openai.com/v1", is_async=True)
    assert client.trust_env is True


def test_local_endpoint_ignores_ambient_proxy(monkeypatch):
    # A proxy that cannot reach the LAN endpoint deadlocks large image bodies
    # in its socket buffer until the request times out, stalling the producer.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    client = build_http_client("http://10.147.254.3:36688/v1", timeout=600.0, is_async=True)
    assert client.trust_env is False
    assert client.timeout == httpx.Timeout(600.0)


def test_build_http_client_preserves_limits():
    limits = httpx.Limits(max_connections=4, max_keepalive_connections=4)
    client = build_http_client("http://10.147.254.3:8020/v1", limits=limits)
    pool = client._transport._pool
    assert pool._max_connections == 4


def test_vlm_async_client_has_no_proxy_for_local_endpoint():
    from openviking.models.vlm.backends.openai_vlm import _build_openai_client_kwargs

    kwargs = _build_openai_client_kwargs(
        "openai", "sk-x", "http://10.147.254.3:36688/v1", None, None, 600.0, is_async=True
    )
    client = openai.AsyncOpenAI(**kwargs)
    assert client._client.trust_env is False
    assert client._client.timeout == httpx.Timeout(600.0)