# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Regression tests for stable pagination of the vector scroll API.

Without an explicit order the adapter answers a filter-only query with a
random-vector sample, so consecutive offset pages overlap and skip rows
(measured on a real store: 1055 rows page-walked into 1001 distinct ids). ACL
sweeps and backups page through ``scroll`` as well, so they lost records the
same way.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openviking.storage.viking_vector_index_backend import _SingleAccountBackend


def _backend(rows: list[dict], calls: list[dict]) -> _SingleAccountBackend:
    backend = _SingleAccountBackend.__new__(_SingleAccountBackend)
    backend._bound_account_id = "acct"

    async def _call(name: str, **kwargs):
        assert name == "query"
        calls.append(dict(kwargs))
        offset, limit = kwargs["offset"], kwargs["limit"]
        return rows[offset : offset + limit]

    backend._async_adapter = SimpleNamespace(call=_call)
    return backend


@pytest.mark.asyncio
async def test_scroll_walks_a_stable_order_without_overlap_or_loss():
    rows = [
        {"id": f"id-{index:03d}", "uri": f"viking://resources/doc-{index:03d}.md"}
        for index in range(120)
    ]
    calls: list[dict] = []
    backend = _backend(rows, calls)

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        records, cursor = await backend.scroll(limit=50, cursor=cursor)
        seen.extend(record["id"] for record in records)
        pages += 1
        if cursor is None:
            break

    assert pages == 3
    assert seen == [row["id"] for row in rows]
    assert len(seen) == len(set(seen)) == len(rows)


@pytest.mark.asyncio
async def test_scroll_asks_the_adapter_for_scalar_order():
    calls: list[dict] = []
    backend = _backend([{"id": "id-0"}], calls)

    await backend.scroll(limit=10)

    # The regression is the missing ``order_by``: a filter-only query samples
    # with a random vector instead of walking a stable sequence.
    assert calls[0]["order_by"] == "updated_at"
    assert calls[0]["offset"] == 0


@pytest.mark.asyncio
async def test_scroll_cursor_advances_by_rows_returned():
    calls: list[dict] = []
    rows = [
        {"id": f"id-{index:02d}", "uri": f"viking://resources/doc-{index:02d}.md"}
        for index in range(7)
    ]
    backend = _backend(rows, calls)

    records, cursor = await backend.scroll(limit=5)
    assert len(records) == 5
    assert cursor == "5"

    records, cursor = await backend.scroll(limit=5, cursor=cursor)
    assert [record["id"] for record in records] == ["id-05", "id-06"]
    assert cursor is None
    assert [call["offset"] for call in calls] == [0, 5]
