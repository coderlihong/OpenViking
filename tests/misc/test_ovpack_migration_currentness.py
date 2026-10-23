# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Regression tests for the ovpack migration reuse path.

Covers the three issues found while migrating a published tenant with
``vector_mode=require``:

* the destination ledger has to be installed before the dense snapshot is
  replayed (restored rows keep ``index_generation`` and the managed-write
  window rejects unknown generations),
* content blob rebuilds race on the same blob while several workers run and
  must tolerate lock conflicts instead of aborting the bundle,
* a pack exported after repeated reindexes can carry superseded generations;
  export must drop them and restore must skip them if an older pack still has
  them.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from openviking.server.identity import RequestContext, Role
from openviking.storage.ovpack import operations
from openviking.storage.ovpack import publication_bundle
from openviking.storage.ovpack.index import current_generation_records
from openviking.storage.ovpack.publication_bundle import (
    _rebuild_blobs_and_refs,
    published_generations,
)
from openviking.storage.ovpack.vectors import restore_vector_snapshot
from openviking_cli.session.user_id import UserIdentifier


def _ctx() -> RequestContext:
    return RequestContext(user=UserIdentifier("acct", "alice"), role=Role.ADMIN)


class _FakeVectorStore:
    def __init__(self) -> None:
        self.upserts: list[dict] = []

    async def upsert(self, payload, ctx=None):
        self.upserts.append(payload)
        return payload.get("id")


def _record(record_id: str, generation: str, text: str = "text") -> dict:
    return {
        "record_id": record_id,
        "kind": "file",
        "level": "2",
        "path": "a.md",
        "scalars": {"index_generation": generation, "uri": "viking://resources/x/a.md"},
        "text": text,
    }


# ── export filtering ─────────────────────────────────────────────────


def test_current_generation_records_drops_superseded_rows():
    records = [
        _record("r1", "g-old", "old"),
        _record("r2", "g-new", "new"),
        _record("r3", "g-old", "old-2"),
    ]

    kept = current_generation_records(records, "g-new")

    assert [row["record_id"] for row in kept] == ["r2"]


def test_current_generation_records_keeps_legacy_rows_without_head():
    records = [_record("r1", "g-old")]

    assert current_generation_records(records, None) == records


def test_current_generation_records_keeps_rows_without_generation():
    record = {"record_id": "r1", "path": "a.md", "scalars": {"uri": "viking://resources/x/a.md"}}

    assert current_generation_records([record], "g-new") == [record]


# ── restore filtering ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restore_skips_superseded_generations():
    store = _FakeVectorStore()
    records = [_record("r1", "g-old"), _record("r2", "g-new")]
    manifest_entries = {"a.md": {"path": "a.md", "kind": "file"}}

    restored, skipped = await restore_vector_snapshot(
        store,
        "viking://resources/x",
        records,
        {"r1": [0.1, 0.2], "r2": [0.3, 0.4]},
        manifest_entries,
        _ctx(),
        keep_generation=True,
        allowed_generations={"viking://resources/x/a.md": "g-new"},
    )

    assert (restored, skipped) == (1, 1)
    assert len(store.upserts) == 1


@pytest.mark.asyncio
async def test_restore_without_ledger_replays_every_row():
    store = _FakeVectorStore()
    records = [_record("r1", "g-old"), _record("r2", "g-new")]

    restored, skipped = await restore_vector_snapshot(
        store,
        "viking://resources/x",
        records,
        {"r1": [0.1, 0.2], "r2": [0.3, 0.4]},
        {"a.md": {"path": "a.md", "kind": "file"}},
        _ctx(),
        keep_generation=False,
        allowed_generations=None,
    )

    assert (restored, skipped) == (2, 0)
    assert len(store.upserts) == 2


# ── ledger first ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ledger_is_installed_before_dense_snapshot(monkeypatch):
    calls: list[str] = []
    seen_allowed: dict = {}

    async def _fake_bundle(*args, **kwargs):
        calls.append("ledger")

    async def _fake_restore(*args, **kwargs):
        calls.append("vectors")
        seen_allowed.update(kwargs.get("allowed_generations") or {})
        return (3, 0)

    monkeypatch.setattr(operations, "import_publication_bundle", _fake_bundle)
    monkeypatch.setattr(operations, "restore_vector_snapshot", _fake_restore)
    monkeypatch.setattr(operations, "has_publication_bundle", lambda *a, **k: True)
    monkeypatch.setattr(
        operations,
        "published_generations",
        lambda account: {"viking://resources/x/a.md": "g-new"},
    )

    await operations._restore_snapshot_with_ledger(
        viking_fs=object(),
        vector_store=object(),
        file_path="pack.ovpack",
        base_name="base",
        root_uri="viking://resources/x",
        index_records=[],
        dense_vectors={},
        manifest_entries={},
        entries=[],
        ctx=SimpleNamespace(account_id="acct"),
    )

    assert calls == ["ledger", "vectors"]
    assert seen_allowed == {"viking://resources/x/a.md": "g-new"}


@pytest.mark.asyncio
async def test_ledger_less_pack_does_not_pass_generations(monkeypatch):
    seen: dict = {}

    async def _fake_restore(*args, **kwargs):
        seen.update(kwargs)
        return (0, 0)

    monkeypatch.setattr(operations, "restore_vector_snapshot", _fake_restore)
    monkeypatch.setattr(operations, "has_publication_bundle", lambda *a, **k: False)
    monkeypatch.setattr(
        operations,
        "published_generations",
        lambda account: pytest.fail("must not query generations for a legacy pack"),
    )

    await operations._restore_snapshot_with_ledger(
        viking_fs=object(),
        vector_store=object(),
        file_path="pack.ovpack",
        base_name="base",
        root_uri="viking://resources/x",
        index_records=[],
        dense_vectors={},
        manifest_entries={},
        entries=[],
        ctx=SimpleNamespace(account_id="acct"),
    )

    assert seen["keep_generation"] is False
    assert seen["allowed_generations"] is None


# ── blob rebuild lock races ──────────────────────────────────────────


class _RacyAgfs:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.reads: list[str] = []

    async def ensure_parent_dirs(self, path: str) -> None:
        return None

    async def read(self, path: str) -> bytes:
        self.reads.append(path)
        return b"payload"

    async def stat(self, path: str) -> None:
        raise FileNotFoundError(path)

    async def write(self, path: str, data: bytes) -> None:
        if not self.writes:
            self.writes.append("<attempt-1>")
            raise RuntimeError("internal error: lock error: lock acquire timed out after 0ms")
        self.writes.append(path)


class _FakeDB:
    def execute(self, *args, **kwargs):
        return self

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeStore:
    def db(self, *args, **kwargs):
        return _FakeDB()


class _FakeVikingFS:
    def __init__(self) -> None:
        self._async_agfs = _RacyAgfs()

    def _uri_to_path(self, uri: str, ctx=None) -> str:
        return "/local/acct/" + uri.removeprefix("viking://")


@pytest.mark.asyncio
async def test_blob_rebuild_retries_lock_conflicts():
    fs = _FakeVikingFS()

    written, fixed = await _rebuild_blobs_and_refs(
        fs,
        _FakeStore(),
        "acct",
        "viking://resources/x",
        [{"rel_path": "a.md", "uri": "viking://resources/x/a.md", "isDir": False}],
        _ctx(),
    )

    assert written == 1
    assert fixed == 0
    assert fs._async_agfs.writes and fs._async_agfs.writes[-1].endswith(".bin")


# ── ledger head lookup ───────────────────────────────────────────────


def test_published_generations_reads_head_manifests(monkeypatch):
    manifest = {
        "viking://resources/x": {"generation": "g-root"},
        "viking://resources/x/a.md": {"generation": "g-a"},
        "viking://resources/x/b.md": {"plan": "no-generation"},
    }

    class _RowDB:
        def execute(self, *args, **kwargs):
            return self

        def fetchall(self):
            return [(json.dumps(manifest).encode("utf-8"),)]

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    class _Store:
        def db(self, *args, **kwargs):
            return _RowDB()

    monkeypatch.setattr(publication_bundle, "_store", lambda: _Store())

    assert published_generations("acct") == {
        "viking://resources/x": "g-root",
        "viking://resources/x/a.md": "g-a",
    }
