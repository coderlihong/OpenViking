# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

"""Tests for the decoupled retrieval endpoints: index (catalogue) + vector."""

import httpx
import pytest

from openviking.server.identity import RequestContext, Role
from openviking_cli.retrieve import ContextType, FindResult, MatchedContext
from openviking_cli.session.user_id import UserIdentifier

ROOT = "viking://resources/cat"

INDEX_BODY = """---
kind: directory_index
directory: viking://resources/cat/
entry_count: 3
---

# 目录索引 · cat

本文件由 OpenViking 自动生成，是本目录的导航清单。

## 子目录

- [5_Standard_Values](viking://resources/cat/5_Standard_Values) — 标准值：液压泵参数表

## 文件

- [pump.md](viking://resources/cat/pump.md) — Pump pressure standard values 液压泵
- [valve.md](viking://resources/cat/valve.md) — 阀芯卡滞故障排查
"""

SUB_INDEX_BODY = """---
kind: directory_index
directory: viking://resources/cat/5_Standard_Values/
entry_count: 1
---

## 文件

- [5.1_Hydraulic_Pump.md](viking://resources/cat/5_Standard_Values/5.1_Hydraulic_Pump.md) — 液压泵标准值 Pump
"""


def _ctx() -> RequestContext:
    return RequestContext(user=UserIdentifier.the_default_user(), role=Role.ROOT)


async def _write_catalogue(service, *, root: str = ROOT) -> None:
    ctx = _ctx()
    await service.viking_fs.mkdir(root, exist_ok=True, ctx=ctx)
    await service.viking_fs.mkdir(f"{root}/5_Standard_Values", exist_ok=True, ctx=ctx)
    await service.viking_fs.write(f"{root}/index.md", INDEX_BODY, ctx=ctx)
    await service.viking_fs.write(f"{root}/5_Standard_Values/index.md", SUB_INDEX_BODY, ctx=ctx)
    await service.viking_fs.write(f"{root}/pump.md", "# pump\n", ctx=ctx)
    await service.viking_fs.write(f"{root}/valve.md", "# valve\n", ctx=ctx)
    await service.viking_fs.write(
        f"{root}/5_Standard_Values/5.1_Hydraulic_Pump.md", "# pump values\n", ctx=ctx
    )


async def test_index_search_ranks_catalogue_entries(client: httpx.AsyncClient, service):
    await _write_catalogue(service)

    resp = await client.post(
        "/api/v1/index/search",
        json={
            "uri": ROOT,
            "query": "Pump|液压泵",
            "include_content": True,
        },
    )

    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["count"] == 3
    assert result["index_files_scanned"] == 2
    names = [m["name"] for m in result["matches"]]
    assert names == ["5.1_Hydraulic_Pump.md", "pump.md", "5_Standard_Values"]
    scores = [m["score"] for m in result["matches"]]
    assert scores == sorted(scores, reverse=True)
    kinds = {m["name"]: m["kind"] for m in result["matches"]}
    assert kinds == {
        "5.1_Hydraulic_Pump.md": "file",
        "pump.md": "file",
        "5_Standard_Values": "directory",
    }
    for match in result["matches"]:
        assert match["content"].startswith("- [")
        assert match["index_uri"].endswith("/index.md")
        assert match["line"] > 0
    sub = result["matches"][0]
    assert sub["index_uri"] == f"{ROOT}/5_Standard_Values/index.md"
    assert sub["description"] == "液压泵标准值 Pump"


async def test_index_search_content_omitted_by_default(client: httpx.AsyncClient, service):
    await _write_catalogue(service)

    resp = await client.post("/api/v1/index/search", json={"uri": ROOT, "query": "Pump"})

    assert resp.status_code == 200
    for match in resp.json()["result"]["matches"]:
        assert "content" not in match


async def test_index_search_level_limit_zero_scans_only_root(client: httpx.AsyncClient, service):
    await _write_catalogue(service)

    resp = await client.post(
        "/api/v1/index/search",
        json={"uri": ROOT, "query": "Pump|液压泵", "level_limit": 0},
    )

    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["index_files_scanned"] == 1
    assert [m["uri"] for m in result["matches"]] == [
        f"{ROOT}/pump.md",
        f"{ROOT}/5_Standard_Values",
    ]


async def test_index_search_skips_legacy_body_named_index(client: httpx.AsyncClient, service):
    ctx = _ctx()
    legacy = "viking://resources/legacy"
    await service.viking_fs.mkdir(legacy, exist_ok=True, ctx=ctx)
    await service.viking_fs.write(
        f"{legacy}/index.md",
        "# 章节正文\n\n- [假条目](viking://resources/legacy/x.md) — Pump 关键词\n",
        ctx=ctx,
    )

    resp = await client.post("/api/v1/index/search", json={"uri": legacy, "query": "Pump"})

    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["matches"] == []
    assert result["count"] == 0
    assert result["index_files_scanned"] == 0


async def test_index_search_rejects_empty_query(client: httpx.AsyncClient, service):
    resp = await client.post("/api/v1/index/search", json={"uri": ROOT, "query": "|"})

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENT"


async def test_index_search_missing_uri_is_404(client: httpx.AsyncClient):
    resp = await client.post(
        "/api/v1/index/search",
        json={"uri": "viking://resources/does-not-exist", "query": "Pump"},
    )

    assert resp.status_code == 404


async def test_index_search_file_uri_is_400(client: httpx.AsyncClient, service):
    await _write_catalogue(service)

    resp = await client.post("/api/v1/index/search", json={"uri": f"{ROOT}/pump.md", "query": "Pump"})

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENT"


async def test_index_search_rejects_unknown_params(client: httpx.AsyncClient, service):
    await _write_catalogue(service)

    resp = await client.post(
        "/api/v1/index/search",
        json={"uri": ROOT, "query": "Pump", "engine": "auto"},
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENT"


async def test_vector_search_mirrors_find_without_context_params(
    client: httpx.AsyncClient, service, monkeypatch
):
    captured = {}

    async def fake_find(**kwargs):
        captured.update(kwargs)
        return FindResult(
            memories=[],
            resources=[
                MatchedContext(
                    uri="viking://resources/visible.md",
                    context_type=ContextType.RESOURCE,
                    score=0.9,
                )
            ],
            skills=[],
        )

    monkeypatch.setattr(service.search, "find", fake_find)

    resp = await client.post(
        "/api/v1/vector/search",
        json={"query": "visible", "limit": 3, "score_threshold": 0.5},
    )

    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["resources"][0]["uri"] == "viking://resources/visible.md"
    assert captured["limit"] == 3
    assert captured["score_threshold"] == 0.5
    assert captured["target_uri"] == ""
    assert "session_id" not in captured
    assert "mode" not in captured


@pytest.mark.parametrize("extra", [{"session_id": "s1"}, {"mode": "context"}, {"max_tokens": 1024}])
async def test_vector_search_rejects_coupled_params(client: httpx.AsyncClient, extra):
    resp = await client.post("/api/v1/vector/search", json={"query": "x", **extra})

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENT"


async def test_vector_search_applies_score_floor_to_final_scores(
    client: httpx.AsyncClient, service, monkeypatch
):
    async def fake_find(**kwargs):
        del kwargs
        return FindResult(
            memories=[],
            resources=[
                MatchedContext(
                    uri="viking://resources/high.md",
                    context_type=ContextType.RESOURCE,
                    score=0.9,
                ),
                MatchedContext(
                    uri="viking://resources/low.md",
                    context_type=ContextType.RESOURCE,
                    score=0.3,
                ),
            ],
            skills=[],
        )

    monkeypatch.setattr(service.search, "find", fake_find)

    resp = await client.post(
        "/api/v1/vector/search", json={"query": "x", "score_threshold": 0.5}
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert [r["uri"] for r in result["resources"]] == ["viking://resources/high.md"]
    assert result["total"] == 1

    resp = await client.post("/api/v1/vector/search", json={"query": "x"})
    assert resp.status_code == 200
    assert resp.json()["result"]["total"] == 2


async def test_vector_search_inline_visible_content_when_requested(
    client: httpx.AsyncClient, service, monkeypatch
):
    async def fake_read_visible(uri, *, ctx):
        assert ctx is not None
        return f"visible content for {uri}"

    async def fake_find(**kwargs):
        del kwargs
        return FindResult(
            memories=[],
            resources=[
                MatchedContext(
                    uri="viking://resources/visible.md",
                    context_type=ContextType.RESOURCE,
                )
            ],
            skills=[],
        )

    monkeypatch.setattr(service.fs, "read_visible", fake_read_visible)
    monkeypatch.setattr(service.search, "find", fake_find)

    resp = await client.post(
        "/api/v1/vector/search",
        json={"query": "visible", "read_content": True},
    )

    assert resp.status_code == 200
    assert resp.json()["result"]["resources"][0]["content"] == (
        "visible content for viking://resources/visible.md"
    )


def test_mcp_index_and_vector_tools_registered():
    from openviking.server.mcp_endpoint import mcp

    tools = {tool.name for tool in mcp._tool_manager.list_tools()}
    assert {"index_search", "vector_search"} <= tools
