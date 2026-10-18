"""Snapshot manifests are gzip-compressed on write and must stay readable.

The head manifest is rewritten on every publish, so its stored size drives both
WAL/database growth and how long the publication write lock is held. These tests
pin the codec contract: new writes are compressed, old plain-text rows still
decode, and a manifest survives a round trip through the SQLite column it is
kept in.
"""
from __future__ import annotations

import json
import sqlite3

from openviking.indexing.manifest_codec import (
    MANIFEST_GZIP_MAGIC,
    decode_manifest,
    encode_manifest,
)


def _manifest(entries: int = 3) -> dict:
    return {
        f"viking://resources/raw/menus/805/{i}/x.md": {
            "generation": f"g{i}",
            "plan": "p" * 32,
            "content": {"sha256": "0" * 64},
            "semantic_contents": {"0": {"sha256": "1" * 64}},
        }
        for i in range(entries)
    }


def test_round_trip_preserves_every_reference():
    manifest = _manifest()
    blob = encode_manifest(manifest)
    assert isinstance(blob, bytes)
    assert blob[:2] == MANIFEST_GZIP_MAGIC
    assert decode_manifest(blob) == manifest


def test_legacy_plain_text_manifest_still_decodes():
    manifest = _manifest()
    assert decode_manifest(json.dumps(manifest)) == manifest


def test_legacy_utf8_bytes_manifest_still_decodes():
    manifest = _manifest()
    assert decode_manifest(json.dumps(manifest).encode("utf-8")) == manifest


def test_compressed_blob_is_much_smaller_than_the_json():
    manifest = _manifest(2000)
    raw = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    assert len(encode_manifest(manifest)) < len(raw) / 2


def test_manifest_survives_the_sqlite_column(tmp_path):
    """The column is declared TEXT; SQLite stores the BLOB unchanged."""
    manifest = _manifest()
    db = sqlite3.connect(tmp_path / "snapshots.db")
    try:
        db.execute("CREATE TABLE snapshots(id TEXT PRIMARY KEY, manifest TEXT NOT NULL)")
        db.execute("INSERT INTO snapshots VALUES (?,?)", ("s1", encode_manifest(manifest)))
        db.commit()
        raw = db.execute("SELECT manifest FROM snapshots WHERE id=?", ("s1",)).fetchone()[0]
    finally:
        db.close()
    assert isinstance(raw, bytes)
    assert decode_manifest(raw) == manifest
