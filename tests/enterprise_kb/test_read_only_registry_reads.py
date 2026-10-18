"""Pure registry reads must not queue behind the single publication writer.

``PublicationStore`` runs every write in a ``BEGIN IMMEDIATE`` transaction, so
one writer at a time.  Readers that take the same writer transaction (the
default of ``db()``) therefore serialize behind bulk publications: in the live
run that surfaced as multi-second ``lock wait`` stalls inside
``read_file_bytes`` (``is_tombstoned`` is called on every source read, from the
event loop) and as whole reindex batches waiting seconds for a read-only check.

These tests pin the contract: read-only entry points open a *readonly*
transaction, so WAL readers never block writers and never enqueue behind them.
"""
from __future__ import annotations

from contextlib import contextmanager

from openviking.indexing.publication import PublicationStore


def _record_readonly(store, monkeypatch, call):
    """Run ``call`` with ``store.db`` spied on; return (readonly flags, result)."""
    seen: list[bool] = []
    real_db = store.db

    @contextmanager
    def spy(readonly: bool = False):
        seen.append(readonly)
        with real_db(readonly=readonly) as db:
            yield db

    monkeypatch.setattr(store, "db", spy)
    return seen, call()


def test_is_tombstoned_does_not_take_the_writer_lock(tmp_path, monkeypatch):
    store = PublicationStore(str(tmp_path / "db"))
    seen, result = _record_readonly(
        store, monkeypatch, lambda: store.is_tombstoned("acct", "viking://resources/a.md")
    )
    assert result is False
    assert seen == [True], "is_tombstoned must read without the writer transaction"


def test_is_tombstoned_sees_an_ancestor_tombstone(tmp_path, monkeypatch):
    store = PublicationStore(str(tmp_path / "db"))
    store.tombstone("acct", "viking://resources/raw/menus/1")
    assert store.is_tombstoned("acct", "viking://resources/raw/menus/1/2.md") is True
    assert store.is_tombstoned("acct", "viking://resources/raw/menus/other.md") is False


def test_tombstones_and_sources_read_without_the_writer_lock(tmp_path, monkeypatch):
    store = PublicationStore(str(tmp_path / "db"))
    seen, (rows, published) = _record_readonly(
        store,
        monkeypatch,
        lambda: (store.tombstones("acct"), store.sources("acct")),
    )
    assert rows == [] and published == []
    assert seen == [True, True]
