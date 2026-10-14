# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Index-expectation tests: the checker must mirror what the pipeline indexes.

The export gate and ``/system/consistency`` both consume
``build_index_expectations``; if it expects rows the pipeline deliberately never
builds, every cartalogue-bearing branch reports missing records and cannot be
exported with vectors.
"""

from __future__ import annotations

from typing import Any

import pytest

from openviking.server.identity import RequestContext, Role
from openviking.storage.expr import Eq
from openviking.storage.index_consistency import (
    build_index_expectations,
    check_index_consistency,
)
from openviking_cli.session.user_id import UserIdentifier

ROOT = "viking://resources/branch"

GENERATED_INDEX = (
    "---\n"
    "kind: directory_index\n"
    "directory: viking://resources/branch/chapter/\n"
    "entry_count: 1\n"
    "---\n\n"
    "# 目录索引\n\n- [正文](viking://resources/branch/chapter/chapter.md)\n"
)
LEGACY_INDEX = "# 第十章 液压系统\n\n这是历史语料，正文写在 index.md 里。\n"


class FakeVikingFS:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    async def exists(self, uri: str, ctx: Any = None) -> bool:
        return uri in self.files

    async def read_file(self, uri: str, ctx: Any = None) -> bytes:
        return self.files[uri].encode("utf-8")


class FakeVectorStore:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def filter(self, filter: Any, limit: int = 10, output_fields=None, ctx=None):
        uri = filter.value if isinstance(filter, Eq) else None
        return [row for row in self.rows if row.get("uri") == uri][:limit]


def _ctx() -> RequestContext:
    return RequestContext(user=UserIdentifier.the_default_user(), role=Role.ROOT)


def _entries() -> list[dict[str, Any]]:
    def entry(rel_path: str, **extra: Any) -> dict[str, Any]:
        name = rel_path.rstrip("/").split("/")[-1]
        return {
            "uri": f"{ROOT}/{rel_path.rstrip('/')}",
            "rel_path": rel_path,
            "name": name,
            "isDir": False,
            **extra,
        }

    return [
        {
            "uri": f"{ROOT}/chapter",
            "rel_path": "chapter",
            "name": "chapter",
            "isDir": True,
        },
        entry("chapter/index.md"),  # generated catalogue: never indexed
        entry("chapter/chapter.md"),  # chapter body: indexed L2
        entry("legacy/index.md"),  # authored prose under the reserved name: indexed
        entry("toc.md"),
        entry("_toc.json"),
        entry("_manifest.json"),
        entry("figure.png"),
    ]


def _files() -> dict[str, str]:
    return {
        f"{ROOT}/.abstract.md": "分支摘要",
        f"{ROOT}/chapter/.abstract.md": "章节摘要",
        f"{ROOT}/chapter/index.md": GENERATED_INDEX,
        f"{ROOT}/chapter/chapter.md": "# 正文\n\n内容",
        f"{ROOT}/legacy/index.md": LEGACY_INDEX,
        f"{ROOT}/toc.md": "# 文档目录\n",
        f"{ROOT}/_toc.json": "{}",
        f"{ROOT}/_manifest.json": "{}",
    }


def _rows_for_expected() -> list[dict[str, Any]]:
    return [
        {"uri": ROOT, "level": 0},
        {"uri": f"{ROOT}/chapter", "level": 0},
        {"uri": f"{ROOT}/chapter/chapter.md", "level": 2},
        {"uri": f"{ROOT}/legacy/index.md", "level": 2},
    ]


@pytest.mark.asyncio
async def test_expectations_skip_generated_catalogues_and_chapter_metadata() -> None:
    expectations = await build_index_expectations(
        FakeVikingFS(_files()), ROOT, _entries(), _ctx()
    )
    keys = sorted(item.key for item in expectations)
    assert keys == [
        ".#level=0",
        "chapter#level=0",
        "chapter/chapter.md#level=2",
        "legacy/index.md#level=2",
    ]


@pytest.mark.asyncio
async def test_generated_catalogue_absence_is_not_a_missing_record() -> None:
    report = await check_index_consistency(
        FakeVikingFS(_files()), FakeVectorStore(_rows_for_expected()), ROOT, _entries(), _ctx()
    )
    assert report.ok, [item.key for item in report.missing_records]


@pytest.mark.asyncio
async def test_legacy_authored_index_md_absence_is_a_missing_record() -> None:
    rows = [row for row in _rows_for_expected() if not row["uri"].endswith("legacy/index.md")]
    report = await check_index_consistency(
        FakeVikingFS(_files()), FakeVectorStore(rows), ROOT, _entries(), _ctx()
    )
    assert not report.ok
    assert [item.key for item in report.missing_records] == ["legacy/index.md#level=2"]


@pytest.mark.asyncio
async def test_real_chapter_body_absence_is_still_a_missing_record() -> None:
    rows = [row for row in _rows_for_expected() if not row["uri"].endswith("chapter/chapter.md")]
    report = await check_index_consistency(
        FakeVikingFS(_files()), FakeVectorStore(rows), ROOT, _entries(), _ctx()
    )
    assert not report.ok
    assert [item.key for item in report.missing_records] == ["chapter/chapter.md#level=2"]
