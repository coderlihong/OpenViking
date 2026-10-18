# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Query-path lease bookkeeping must never block the event loop for minutes.

The request path renews its publication view lease on the event loop. When a
bulk reindex keeps the single writer lock busy, an unbounded ``BEGIN IMMEDIATE``
there queued for 204-244s and froze the whole server (watchdog stacks:
``db -> renew -> QueryView.read -> visible_filter -> vector search``). The lease
is kept alive by the off-loop heartbeat, so the request path renews lazily and
waits only briefly; a failed pin is retryable instead of a freeze.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from openviking.indexing import publication
from openviking.indexing.publication import PublicationStore, QueryView


class _FakeRegistry:
    path = "/tmp/fake-publication.db"

    def __init__(self, *, capture_error=None, renew_error=None):
        self.capture_error = capture_error
        self.renew_error = renew_error
        self.captures: list[int | None] = []
        self.renews: list[int | None] = []

    def capture(self, account, ttl=120, busy_timeout_ms=None):  # noqa: ANN001, ARG002
        self.captures.append(busy_timeout_ms)
        if self.capture_error is not None:
            raise self.capture_error
        return "lease-1", {"viking://resources/a.md": {"generation": "g1"}}

    def renew(self, lease, ttl=120, busy_timeout_ms=None):  # noqa: ANN001, ARG002
        self.renews.append(busy_timeout_ms)
        if self.renew_error is not None:
            raise self.renew_error


@pytest.fixture(autouse=True)
def _no_heartbeat_thread(monkeypatch):
    monkeypatch.setattr(QueryView, "_start_heartbeat", lambda self: None)


def test_fresh_lease_is_not_renewed_from_the_request_path():
    registry = _FakeRegistry()
    view = QueryView(registry, "acct")

    lease, manifest = view.read("acct", registry)

    assert lease == "lease-1"
    assert "viking://resources/a.md" in manifest
    assert registry.captures == [publication.QUERY_RENEW_BUSY_TIMEOUT_MS]
    assert registry.renews == [], "a fresh lease must not demand the writer lock"


def test_expiring_lease_renews_with_a_bounded_wait():
    registry = _FakeRegistry()
    view = QueryView(registry, "acct")
    view.read("acct", registry)
    view._renewed_at = time.monotonic() - publication.QUERY_LEASE_TTL_SECONDS / 3 - 1

    view.read("acct", registry)

    assert registry.renews == [publication.QUERY_RENEW_BUSY_TIMEOUT_MS]


def test_busy_store_defers_the_renewal_instead_of_blocking():
    registry = _FakeRegistry(renew_error=sqlite3.OperationalError("database is locked"))
    view = QueryView(registry, "acct")
    view.read("acct", registry)
    view._renewed_at = time.monotonic() - publication.QUERY_LEASE_TTL_SECONDS / 3 - 1

    lease, manifest = view.read("acct", registry)

    assert lease == "lease-1"
    assert "viking://resources/a.md" in manifest, "the pinned snapshot is still served"


def test_busy_store_while_pinning_is_a_retryable_lease_error():
    registry = _FakeRegistry(capture_error=sqlite3.OperationalError("database is locked"))
    view = QueryView(registry, "acct")

    with pytest.raises(publication.QueryLeaseExpired):
        view.read("acct", registry)


def test_bounded_busy_timeout_is_set_and_restored(tmp_path):
    store = PublicationStore(str(tmp_path / "db"))
    with store.db(busy_timeout_ms=250) as db:
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 250
    with store.db() as db:
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 120000
