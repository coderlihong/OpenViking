# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Deterministic vector record ID generation.

Every vector record in VikingDB has a stable primary key derived from
``account_id``, the resource ``uri``, and the semantic ``level``:

- Level 0 (directory abstract): seed URI is ``{uri}/.abstract.md``
- Level 1 (directory overview): seed URI is ``{uri}/.overview.md``
- Level 2 (file / chunk): seed URI is the ``uri`` itself

The final record ID is ``md5(f"{account_id}:{seed_uri}")``.

This is the single source of truth for computing vector record IDs.
All code paths that need to derive or re-derive a record ID MUST use
``vector_record_id`` / ``seed_uri_for_vector_id`` from this module rather than
re-implementing the hash inline, so that future changes to the ID scheme stay
consistent.
"""

import hashlib
import re
from typing import Any

_VECTOR_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def is_vector_record_id(value: str) -> bool:
    """Return True if ``value`` looks like a 32-char hex MD5 vector record ID."""
    return bool(_VECTOR_ID_RE.fullmatch(value.strip()))


def seed_uri_for_vector_id(uri: str, level: Any) -> str:
    """Return the seed URI used to compute a vector record ID.

    For L0/L1 semantic sidecars the appropriate suffix is appended if not
    already present; for L2 (regular files / chunks) the URI is returned
    unchanged.
    """
    try:
        level_int = int(level)
    except (TypeError, ValueError):
        level_int = 2

    if level_int == 0:
        return uri if uri.endswith("/.abstract.md") else f"{uri}/.abstract.md"
    if level_int == 1:
        return uri if uri.endswith("/.overview.md") else f"{uri}/.overview.md"
    return uri


def vector_record_id(account_id: str, uri: str, level: Any = 2) -> str:
    """Return the deterministic vector DB primary key for (account_id, uri, level).

    This MUST match the ID produced at upsert time so that callers (stat, ovpack,
    migrations, URI remapping) can compute the same key without a round-trip to
    the database.
    """
    seed_uri = seed_uri_for_vector_id(uri, level)
    return hashlib.md5(f"{account_id}:{seed_uri}".encode("utf-8")).hexdigest()


def chunk_vector_record_id(account_id: str, uri: str, chunk_id: str) -> str:
    """Chunk identity includes revision/config; URI remains the readable citation."""
    if not chunk_id:
        raise ValueError("A chunk vector requires a chunk identity")
    return hashlib.md5(f"{account_id}:{uri}:chunk:{chunk_id}".encode()).hexdigest()


def vector_record_id_for_data(data: dict) -> str:
    if data.get("chunk_id"):
        if int(data.get("level", 2)) != 2:
            raise ValueError("Chunk records must be at L2")
        identity = data["chunk_id"]
        if data.get("index_generation"):
            identity += ":generation:" + data["index_generation"]
        return chunk_vector_record_id(data.get("account_id", "default"), data["uri"], identity)
    if data.get("index_generation"):
        seed = seed_uri_for_vector_id(data["uri"], data.get("level", 2))
        return hashlib.md5(
            f"{data.get('account_id', 'default')}:{seed}:generation:{data['index_generation']}".encode()
        ).hexdigest()
    return vector_record_id(data.get("account_id", "default"), data["uri"], data.get("level", 2))


def publication_member_id(data: dict) -> str:
    if data.get("chunk_id"):
        if int(data.get("level", 2)) != 2:
            raise ValueError("Chunk publication member must be L2")
        return data["chunk_id"]
    level = int(data.get("level", 2))
    if level not in (0, 1) or data.get("is_leaf"):
        raise ValueError("Non-chunk publication member must be directory L0/L1")
    return f"semantic:{level}"
