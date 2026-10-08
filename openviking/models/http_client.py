# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""HTTP client construction shared by model backends."""

from __future__ import annotations

import ipaddress
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

_LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain"}


def _endpoint_is_local(api_base: str) -> bool:
    """Return True when ``api_base`` names a loopback/private/link-local host."""
    host = (urlparse(api_base or "").hostname or "").strip().strip("[]")
    if not host:
        return False
    normalized = host.rstrip(".").lower()
    if normalized in _LOCAL_HOSTNAMES or normalized.endswith(".localhost"):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        # A DNS name is treated as remote so an ambient proxy is still honored.
        return False


def build_http_client(
    api_base: str,
    *,
    timeout: Optional[float] = None,
    limits: Optional[httpx.Limits] = None,
    is_async: bool = False,
) -> Any:
    """Build an httpx client for a model endpoint.

    httpx honors ambient proxy environment variables by default. On a
    self-hosted deployment the model endpoint is typically a loopback/private
    address that an ambient system/VPN proxy cannot reach, so a large request
    body deadlocks in the proxy's socket buffer until the request times out.
    Local endpoints therefore connect directly; public endpoints still honor
    the ambient proxy.
    """
    import openai

    factory = openai.DefaultAsyncHttpxClient if is_async else openai.DefaultHttpxClient
    kwargs: dict[str, Any] = {"trust_env": not _endpoint_is_local(api_base)}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if limits is not None:
        kwargs["limits"] = limits
    return factory(**kwargs)