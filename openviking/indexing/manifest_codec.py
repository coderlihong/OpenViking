# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Storage codec for publication snapshot manifests.

A head manifest carries one reference record per published URI and grows to tens
of megabytes (measured 36MB / 68,882 URIs on the e2etest corpus). Every publish
rewrites that blob, so its size is paid twice: once in WAL/database growth, and
once in how long the publication write lock is held. The JSON is highly
repetitive, so level-1 gzip cuts the stored bytes ~3.4x for roughly 130ms of CPU
per publish.

Manifests written before this codec are plain UTF-8 JSON and remain readable.
"""

from __future__ import annotations

import gzip
import json

MANIFEST_GZIP_MAGIC = b"\x1f\x8b"
# A delta snapshot stores only the entries that changed against its parent:
#   b"\x1f\x8bD" + gzip(json({"parent": <snapshot id>, "set": {...}, "drop": [...]}))
# The "D" marker distinguishes it from a full manifest, whose gzip stream begins
# with the gzip header directly. Every existing row is a full manifest and stays
# readable, so no migration is needed.
MANIFEST_DELTA_MAGIC = b"\x1f\x8bD"


def encode_manifest(manifest: dict) -> bytes:
    """Serialize a snapshot manifest for storage (gzip, level 1)."""
    return gzip.compress(json.dumps(manifest, separators=(",", ":")).encode("utf-8"), 1)


def encode_manifest_delta(parent, set_entries: dict, drop_uris=()) -> bytes:
    """Serialize a manifest delta against *parent*.

    A publish changes a handful of URIs but the full manifest is tens of
    megabytes, and re-serialising it measured 184ms of JSON plus 398ms of gzip
    on the e2etest corpus (188k entries, 28.7MB) -- 586ms of the single writer's
    time per publish, for a change set that is usually a few entries. A delta
    keeps that cost proportional to what actually changed.
    """
    payload = {"parent": parent, "set": set_entries, "drop": list(drop_uris)}
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return MANIFEST_DELTA_MAGIC[:2] + b"D" + gzip.compress(body, 1)


def decode_manifest(raw) -> dict:
    """Decode a snapshot manifest written by any version of this codec.

    Full manifests are handled here. A delta needs its parent chain, so it can
    only be resolved by :func:`decode_manifest_with_parent`, which the store
    calls with a resolver; calling this on a delta raises rather than silently
    returning the delta envelope as if it were a manifest.
    """
    if isinstance(raw, memoryview):
        raw = bytes(raw)
    if isinstance(raw, (bytes, bytearray)):
        data = bytes(raw)
        if data[:3] == MANIFEST_DELTA_MAGIC:
            raise ValueError("Delta manifest requires a parent resolver")
        if data[:2] == MANIFEST_GZIP_MAGIC:
            data = gzip.decompress(data)
        return json.loads(data.decode("utf-8"))
    return json.loads(raw)


def is_delta_manifest(raw) -> bool:
    """True when *raw* is a delta rather than a complete manifest."""
    if isinstance(raw, memoryview):
        raw = bytes(raw)
    return isinstance(raw, (bytes, bytearray)) and bytes(raw[:3]) == MANIFEST_DELTA_MAGIC


def decode_manifest_delta(raw) -> dict:
    """Decode a delta envelope: ``{"parent", "set", "drop"}``."""
    if isinstance(raw, memoryview):
        raw = bytes(raw)
    data = bytes(raw)
    if data[:3] != MANIFEST_DELTA_MAGIC:
        raise ValueError("Not a delta manifest")
    return json.loads(gzip.decompress(data[3:]).decode("utf-8"))
