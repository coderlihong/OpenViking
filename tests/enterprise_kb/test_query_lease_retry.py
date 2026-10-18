# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""A lapsed query view lease must retry the read-only query, not fail it.

Under a bulk reindex the publication write lock is held for long stretches, so
the heartbeat's renewal ``UPDATE`` can wait out its busy timeout and the lease
lapses. Serving the result is correctly refused; the request must survive it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from openviking.indexing import publication


class _Ctx:
    account_id = "acct"


def _install(monkeypatch, failures: int):
    """Make the first ``failures`` renew calls raise QueryLeaseExpired."""
    monkeypatch.setattr(publication, "configured_store", lambda: object())

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(publication.asyncio, "sleep", _no_sleep)

    calls: list[int] = []

    def flaky_renew(self, ttl=None):  # noqa: ANN001, ARG001
        calls.append(1)
        if len(calls) <= failures:
            raise publication.QueryLeaseExpired("Publication query lease expired; retry query")

    monkeypatch.setattr(publication.QueryView, "renew", flaky_renew)
    return calls


def test_query_retries_on_a_fresh_snapshot_after_lease_expiry(monkeypatch):
    calls = _install(monkeypatch, failures=1)
    runs: list[int] = []

    @publication.query_snapshot
    async def query(ctx=None):  # noqa: ANN001, ARG001
        runs.append(1)
        return "ok"

    assert asyncio.run(query(ctx=_Ctx())) == "ok"
    assert len(runs) == 2, "the query must run again on a fresh view"
    assert len(calls) == 2


def test_query_fails_only_after_the_last_attempt(monkeypatch):
    calls = _install(monkeypatch, failures=99)

    @publication.query_snapshot
    async def query(ctx=None):  # noqa: ANN001, ARG001
        return "ok"

    with pytest.raises(publication.QueryLeaseExpired):
        asyncio.run(query(ctx=_Ctx()))
    assert len(calls) == publication._QUERY_SNAPSHOT_ATTEMPTS


def test_query_without_a_registry_is_untouched(monkeypatch):
    monkeypatch.setattr(publication, "configured_store", lambda: None)
    runs: list[int] = []

    @publication.query_snapshot
    async def query(ctx=None):  # noqa: ANN001, ARG001
        runs.append(1)
        return "ok"

    assert asyncio.run(query(ctx=SimpleNamespace(account_id="acct"))) == "ok"
    assert len(runs) == 1
