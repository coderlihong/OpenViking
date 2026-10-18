"""Snapshot manifests are append-only, so their generation sets are memoized.

``generation_write_begin`` used to decode an ~11MB gzip manifest *per managed
write* (i.e. once per embedded item) while holding the single writer lock.  In
the live run that produced multi-second lock waits for every other writer and
strangled the vector phase.  The manifest of a snapshot id never changes, so the
derived ``{uri: generation}`` index is cached; these tests pin the memoization
and the fail-closed behaviour it must keep.
"""
from __future__ import annotations

import pytest

from openviking.indexing import registry_gc
from openviking.indexing.manifest_codec import encode_manifest
from openviking_cli.exceptions import FailedPreconditionError


class _Cursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _SnapshotDB:
    """Minimal stand-in for the snapshot lookup used by the helper."""

    def __init__(self, manifests: dict):
        self.manifests = manifests
        self.queries: list[str] = []

    def execute(self, sql, params):
        self.queries.append(params[0])
        raw = self.manifests.get(params[0])
        return _Cursor(None if raw is None else (raw,))


@pytest.fixture(autouse=True)
def _clean_cache():
    registry_gc._SNAPSHOT_MANIFEST_CACHE.clear()
    registry_gc._SNAPSHOT_GENERATIONS_CACHE.clear()
    yield
    registry_gc._SNAPSHOT_MANIFEST_CACHE.clear()
    registry_gc._SNAPSHOT_GENERATIONS_CACHE.clear()


def _counting_decode(monkeypatch):
    calls: list[object] = []
    real = registry_gc.decode_manifest

    def counted(raw):
        calls.append(raw)
        return real(raw)

    monkeypatch.setattr(registry_gc, "decode_manifest", counted)
    return calls


def test_snapshot_index_is_decoded_once_then_reused(monkeypatch):
    calls = _counting_decode(monkeypatch)
    raw = encode_manifest({"viking://a.md": {"generation": "g1"}, "viking://b.md": {"generation": "g2"}})
    db = _SnapshotDB({"snap1": raw})

    first = registry_gc._snapshot_generation_index(db, "snap1")
    second = registry_gc._snapshot_generation_index(db, "snap1")

    assert first == {"viking://a.md": "g1", "viking://b.md": "g2"}
    assert second == first
    assert len(calls) == 1, "a snapshot manifest was decoded more than once"
    # The second hit must not even re-read the row.
    assert db.queries == ["snap1"]


def test_snapshot_index_ignores_entries_without_a_generation(monkeypatch):
    _counting_decode(monkeypatch)
    raw = encode_manifest(
        {
            "viking://a.md": {"generation": "g1"},
            "viking://plain.md": "not-a-dict",
            "viking://blank.md": {"plan": "p"},
        }
    )
    db = _SnapshotDB({"snap1": raw})
    assert registry_gc._snapshot_generation_index(db, "snap1") == {"viking://a.md": "g1"}


def test_missing_snapshot_still_fails_closed(monkeypatch):
    _counting_decode(monkeypatch)
    db = _SnapshotDB({})
    with pytest.raises(FailedPreconditionError):
        registry_gc._snapshot_generation_index(db, "gone")


def test_cache_is_bounded(monkeypatch):
    _counting_decode(monkeypatch)
    db = _SnapshotDB({f"snap{i}": encode_manifest({f"viking://{i}.md": {"generation": f"g{i}"}}) for i in range(6)})
    for i in range(6):
        assert registry_gc._snapshot_generation_index(db, f"snap{i}") == {f"viking://{i}.md": f"g{i}"}
    assert len(registry_gc._SNAPSHOT_MANIFEST_CACHE) <= registry_gc._SNAPSHOT_MANIFEST_CACHE_MAX
    assert len(registry_gc._SNAPSHOT_GENERATIONS_CACHE) <= registry_gc._SNAPSHOT_MANIFEST_CACHE_MAX


def test_manifest_is_decoded_once_across_index_and_manifest_readers(monkeypatch):
    """Both the {uri: generation} index and the raw manifest share one decode."""
    calls = _counting_decode(monkeypatch)
    raw = encode_manifest({"viking://a.md": {"generation": "g1", "content": {"x": 1}}})
    db = _SnapshotDB({"snap1": raw})

    index = registry_gc._snapshot_generation_index(db, "snap1")
    manifest = registry_gc._snapshot_manifest(db, "snap1")

    assert index == {"viking://a.md": "g1"}
    assert manifest == {"viking://a.md": {"generation": "g1", "content": {"x": 1}}}
    assert len(calls) == 1, "the snapshot manifest was decoded twice"


def test_publisher_seeding_removes_the_decode_for_readers(monkeypatch):
    """The manifest a publish just stored is handed to the caches.

    Each publish mints a new snapshot id, so without seeding every reader of the
    new head re-gunzipped a ~13MB blob (measured ~154ms of GIL-bound CPU per
    file during reindex).  A reader after seeding must not touch the database at
    all.
    """
    calls = _counting_decode(monkeypatch)
    manifest = {"viking://a.md": {"generation": "g1"}, "viking://skip.md": "plain"}
    registry_gc.record_snapshot_manifest("fresh", manifest)

    class _ExplodingDB:
        def execute(self, *args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("seeded snapshots must not be read back")

    assert registry_gc._snapshot_generation_index(_ExplodingDB(), "fresh") == {"viking://a.md": "g1"}
    assert registry_gc._snapshot_manifest(_ExplodingDB(), "fresh") is manifest
    assert calls == [], "the publisher's own manifest was decoded again"


def test_under_tombstone_matches_the_ancestor_scan():
    denied = {"viking://resources/dead", "viking://resources/old/"}
    assert registry_gc._under_tombstone("viking://resources/dead", denied)
    assert registry_gc._under_tombstone("viking://resources/dead/a/b.md", denied)
    assert registry_gc._under_tombstone("viking://resources/old/x.md", denied)
    assert not registry_gc._under_tombstone("viking://resources/alive/x.md", denied)
    # A sibling directory that merely shares a string prefix is not covered.
    assert not registry_gc._under_tombstone("viking://resources/deadline/x.md", denied)
