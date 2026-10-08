"""Sync/async SDK wire parity: both faces must emit identical method/path/payload.

The sync client is a thin run_async delegation over the async client; these
tests pin the contract at the transport seam so a future divergence (renamed
field, dropped option, changed endpoint) fails here instead of on a user's
server. parse_backend is the parsing-router option this repo added.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openviking_sdk import AddResourceOptions, AsyncHTTPClient, SearchOptions, SyncHTTPClient


def _capture_calls(client):
    calls = []

    async def recording_post(url, **kwargs):
        calls.append((url, kwargs))
        return object()

    client._http = SimpleNamespace(post=recording_post)
    client._handle_response_data = lambda _response: {"result": {}}
    return calls


@pytest.mark.asyncio
async def test_sync_and_async_add_resource_emit_identical_wire_requests():
    add_options: AddResourceOptions = {
        "parse_backend": "teleocr",
        "model_mode": "layout",
        "section_target_tokens": 4096,
        "reason": "parity-check",
    }
    async_client = AsyncHTTPClient(url="http://localhost:1933")
    async_calls = _capture_calls(async_client)
    await async_client.add_resource(
        "https://example.com/manual.pdf",
        to="viking://resources/raw/manual",
        options=add_options,
    )

    sync_client = SyncHTTPClient(url="http://localhost:1933")
    sync_calls = _capture_calls(sync_client._async_client)
    sync_client.add_resource(
        "https://example.com/manual.pdf",
        to="viking://resources/raw/manual",
        options=add_options,
    )

    assert async_calls == sync_calls
    method_path, kwargs = async_calls[0]
    assert method_path == "/api/v1/resources"
    assert kwargs["json"]["path"] == "https://example.com/manual.pdf"
    assert kwargs["json"]["to"] == "viking://resources/raw/manual"
    # Parsing-router options survive payload compaction on the shared seam.
    assert kwargs["json"]["parse_backend"] == "teleocr"
    assert kwargs["json"]["model_mode"] == "layout"
    assert kwargs["json"]["section_target_tokens"] == 4096


@pytest.mark.asyncio
async def test_sync_and_async_search_emit_identical_wire_requests():
    search_options: SearchOptions = {
        "node_limit": 8,
        "score_threshold": 0.2,
    }
    async_client = AsyncHTTPClient(url="http://localhost:1933")
    async_calls = _capture_calls(async_client)
    await async_client.search(
        "履带张紧油缸漏油",
        target_uri="viking://resources/raw/manual",
        limit=5,
        options=search_options,
    )

    sync_client = SyncHTTPClient(url="http://localhost:1933")
    sync_calls = _capture_calls(sync_client._async_client)
    sync_client.search(
        "履带张紧油缸漏油",
        target_uri="viking://resources/raw/manual",
        limit=5,
        options=search_options,
    )

    assert async_calls == sync_calls
    method_path, kwargs = async_calls[0]
    assert method_path == "/api/v1/search/search"
    assert kwargs["json"]["query"] == "履带张紧油缸漏油"
    assert kwargs["json"]["target_uri"] == "viking://resources/raw/manual"
    assert kwargs["json"]["limit"] == 5
    assert kwargs["json"]["node_limit"] == 8


def test_sync_delegates_share_async_signature_for_router_surface():
    import inspect

    for name in ("add_resource", "search"):
        sync_sig = inspect.signature(getattr(SyncHTTPClient, name))
        async_sig = inspect.signature(getattr(AsyncHTTPClient, name))
        sync_params = [p for p in sync_sig.parameters if p != "self"]
        async_params = [p for p in async_sig.parameters if p != "self"]
        assert sync_params == async_params, f"{name} signature diverged"
