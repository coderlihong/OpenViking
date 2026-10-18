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


def encode_manifest(manifest: dict) -> bytes:
    """Serialize a snapshot manifest for storage (gzip, level 1)."""
    return gzip.compress(json.dumps(manifest, separators=(",", ":")).encode("utf-8"), 1)


def decode_manifest(raw) -> dict:
    """Decode a snapshot manifest written by any version of this codec."""
    if isinstance(raw, memoryview):
        raw = bytes(raw)
    if isinstance(raw, (bytes, bytearray)):
        data = bytes(raw)
        if data[:2] == MANIFEST_GZIP_MAGIC:
            data = gzip.decompress(data)
        return json.loads(data.decode("utf-8"))
    return json.loads(raw)
