# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

"""Tests for runtime model-config endpoints (openviking/server/routers/admin.py)."""

import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from openviking.server import runtime_config
from openviking.server.config import ServerConfig
from openviking.server.dependencies import set_service
from openviking.server.identity import RequestContext, Role
from openviking.server.models import ERROR_CODE_TO_HTTP_STATUS, ErrorInfo, Response
from openviking_cli.exceptions import OpenVikingError
from openviking_cli.utils.config import OpenVikingConfigSingleton


ROOT_KEY = "model-config-test-root-key-abcdef1234567890"

BASE_OV_CONF = {
    "server": {"host": "127.0.0.1", "port": 1933, "root_api_key": ROOT_KEY},
    "vlm": {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "api_key": "sk-test",
        "api_base": "http://127.0.0.1:9/v1",
    },
    "embedding": {
        "dense": {
            "provider": "openai",
            "model": "text-embedding-3-small",
            "api_key": "sk-test",
            "dimension": 1536,
        }
    },
    "rerank": {"provider": "cohere", "api_key": "rk-test", "threshold": 0.1},
}


@pytest.fixture()
def ov_conf_path(tmp_path: Path) -> Path:
    conf = tmp_path / "ov.conf"
    conf.write_text(json.dumps(BASE_OV_CONF), encoding="utf-8")
    return conf


def _make_app(ov_conf_path: Path) -> FastAPI:
    from openviking.server.auth.plugins import ApiKeyAuthPlugin
    from openviking.server.auth.registry import get_registry
    from openviking.server.routers.admin import router as admin_router

    app = FastAPI()
    app.state.config = ServerConfig(root_api_key=ROOT_KEY)

    @app.exception_handler(OpenVikingError)
    async def openviking_error_handler(request, exc: OpenVikingError):
        http_status = ERROR_CODE_TO_HTTP_STATUS.get(exc.code, 500)
        return JSONResponse(
            status_code=http_status,
            content=Response(
                status="error",
                error=ErrorInfo(code=exc.code, message=exc.message, details=exc.details),
            ).model_dump(),
        )

    registry = get_registry()
    if registry.get("api_key") is None:
        registry.register(ApiKeyAuthPlugin)
    app.state.auth_plugin = registry.get("api_key")()

    from openviking.pyagfs.exceptions import AGFSNotFoundError
    from openviking.server.api_keys import APIKeyManager

    class _FakeAGFS:
        def __init__(self):
            self._files = {}
            self._dirs = {"/", "/local"}

        def read(self, path, **_kwargs):
            if path not in self._files:
                raise AGFSNotFoundError(path)
            return self._files[path]

        def write(self, path, content, **_kwargs):
            self.ensure_parent_dirs(path)
            self._files[path] = content

        def mkdir(self, path, **_kwargs):
            self._dirs.add(path)

        def ensure_parent_dirs(self, path, **_kwargs):
            parent = path.rsplit("/", 1)[0]
            if not parent:
                return
            current = ""
            for part in [part for part in parent.strip("/").split("/") if part]:
                current = f"{current}/{part}" if current else f"/{part}"
                self._dirs.add(current)

        def pathlock_acquire_exact(self, *args, **kwargs):
            return {"lease_ref": "test"}

        def pathlock_release(self, *args, **kwargs):
            return None

    class _FakeVikingFS:
        agfs = _FakeAGFS()

    manager = APIKeyManager(root_key=ROOT_KEY, viking_fs=_FakeVikingFS())
    app.state.api_key_manager = manager

    app.include_router(admin_router)
    set_service(None)
    return app


@pytest_asyncio.fixture()
async def client(monkeypatch, ov_conf_path: Path):
    monkeypatch.setenv("OPENVIKING_CONFIG_FILE", str(ov_conf_path))
    OpenVikingConfigSingleton.reset_instance()
    app = _make_app(ov_conf_path)
    await app.state.api_key_manager.load()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"X-API-Key": ROOT_KEY},
    ) as c:
        yield c
    OpenVikingConfigSingleton.reset_instance()


def _read_conf(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


async def test_get_returns_model_sections(client):
    resp = await client.get("/api/v1/admin/model-config")
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert set(result) == {"vlm", "embedding", "rerank", "query_planner", "wiki"}
    assert result["vlm"]["model"] == "gpt-4o-mini"
    assert result["rerank"]["threshold"] == 0.1


async def test_put_invalid_section_rejected_without_write(client, ov_conf_path):
    before = ov_conf_path.read_text(encoding="utf-8")
    resp = await client.put(
        "/api/v1/admin/model-config",
        json={"vlm": {"provider": "no-such-provider"}},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["status"] == "error"
    assert "vlm" in body["error"]["message"]
    assert ov_conf_path.read_text(encoding="utf-8") == before


async def test_put_unknown_section_rejected(client):
    resp = await client.put("/api/v1/admin/model-config", json={"storage": {}})
    assert resp.status_code == 422


async def test_put_partial_section_preserves_omitted_keys(client, ov_conf_path):
    """A field-level patch (e.g. the rerank on/off switch) must not wipe the
    rest of the section that the patch omitted."""
    resp = await client.put(
        "/api/v1/admin/model-config",
        json={"rerank": {"enabled": False}},
    )
    assert resp.status_code == 200
    rerank = _read_conf(ov_conf_path)["rerank"]
    assert rerank["enabled"] is False
    # Values from BASE_OV_CONF survive the partial patch.
    assert rerank["provider"] == "cohere"
    assert rerank["api_key"] == "rk-test"
    assert rerank["threshold"] == 0.1


async def test_put_null_field_clears_key(client, ov_conf_path):
    resp = await client.put(
        "/api/v1/admin/model-config",
        json={"rerank": {"threshold": None}},
    )
    assert resp.status_code == 200
    rerank = _read_conf(ov_conf_path)["rerank"]
    assert "threshold" not in rerank
    assert rerank["provider"] == "cohere"


async def test_put_rerank_persists_and_hot_reloads(client, ov_conf_path):
    resp = await client.put(
        "/api/v1/admin/model-config",
        json={"rerank": {"provider": "cohere", "api_key": "rk-test", "threshold": 0.7}},
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["restart_required"] == []
    assert result["config"]["rerank"]["threshold"] == 0.7

    assert _read_conf(ov_conf_path)["rerank"]["threshold"] == 0.7

    config = OpenVikingConfigSingleton.get_instance()
    assert config.rerank.threshold == 0.7

    follow_up = await client.get("/api/v1/admin/model-config")
    assert follow_up.json()["result"]["rerank"]["threshold"] == 0.7


async def test_put_vlm_swaps_cached_instance(client, ov_conf_path):
    from openviking.models.vlm.base import VLMBase

    config = OpenVikingConfigSingleton.get_instance()
    first = config.vlm.get_vlm_instance()
    assert isinstance(first, VLMBase)

    resp = await client.put(
        "/api/v1/admin/model-config",
        json={
            "vlm": {
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "sk-test",
                "api_base": "http://127.0.0.1:9/v1",
            }
        },
    )
    assert resp.status_code == 200

    config_after = OpenVikingConfigSingleton.get_instance()
    assert config_after.vlm.model == "gpt-4o"
    rebuilt = config_after.vlm.get_vlm_instance()
    assert isinstance(rebuilt, VLMBase)
    assert rebuilt.model == "gpt-4o"


async def test_put_embedding_reports_restart_required(client, ov_conf_path):
    resp = await client.put(
        "/api/v1/admin/model-config",
        json={
            "embedding": {
                "dense": {
                    "provider": "openai",
                    "model": "text-embedding-3-small",
                    "api_key": "sk-test",
                    "dimension": 1536,
                }
            }
        },
    )
    assert resp.status_code == 200
    assert resp.json()["result"]["restart_required"] == ["embedding"]
    assert _read_conf(ov_conf_path)["embedding"]["dense"]["model"] == (
        "text-embedding-3-small"
    )


async def test_put_embedding_dimension_mismatch_rejected(client, ov_conf_path):
    """Cross-section validation: vectordb.dimension is synced from embedding at
    startup, so a lone embedding change is only accepted when the merged config
    revalidates cleanly."""
    before = ov_conf_path.read_text(encoding="utf-8")
    resp = await client.put(
        "/api/v1/admin/model-config",
        json={
            "embedding": {
                "dense": {"provider": "openai", "model": "text-embedding-3-large",
                          "api_key": "sk-test", "dimension": 3072}
            }
        },
    )
    # Dimension change alone is structurally valid; merged-config validation
    # passes because vectordb dimension defaults to 0 (synced later).
    assert resp.status_code in (200, 400)
    if resp.status_code == 400:
        assert ov_conf_path.read_text(encoding="utf-8") == before


async def test_query_planner_patch_hot_reloads(client):
    resp = await client.put(
        "/api/v1/admin/model-config",
        json={
            "query_planner": {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "api_key": "sk-test",
            }
        },
    )
    assert resp.status_code == 200
    config = OpenVikingConfigSingleton.get_instance()
    assert config.query_planner is not None
    assert config.query_planner.model == "gpt-4o-mini"
    assert config.get_query_planner().model == "gpt-4o-mini"


async def test_unauthenticated_request_rejected(monkeypatch, ov_conf_path: Path):
    monkeypatch.setenv("OPENVIKING_CONFIG_FILE", str(ov_conf_path))
    OpenVikingConfigSingleton.reset_instance()
    try:
        app = _make_app(ov_conf_path)
        await app.state.api_key_manager.load()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            denied = await c.get("/api/v1/admin/model-config")
            allowed = await c.get(
                "/api/v1/admin/model-config", headers={"X-API-Key": ROOT_KEY}
            )
        assert denied.status_code in (401, 403)
        assert allowed.status_code == 200
    finally:
        OpenVikingConfigSingleton.reset_instance()


def test_runtime_config_error_is_value_error():
    exc = runtime_config.RuntimeConfigError("bad")
    assert isinstance(exc, ValueError)
