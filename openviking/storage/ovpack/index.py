# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""OVPack index record construction helpers."""

from __future__ import annotations

from typing import Any

from openviking.core.namespace import context_type_for_uri, is_session_uri, owner_fields_for_uri
from openviking.server.identity import RequestContext
from openviking.storage.expr import Eq
from openviking.storage.ovpack.format import (
    OVPACK_FORMAT_VERSION,
    OVPACK_KIND,
    join_uri,
)
from openviking_cli.utils.logger import get_logger
from openviking_cli.utils.uri import VikingURI

logger = get_logger(__name__)

PORTABLE_VECTOR_SCALAR_FIELDS = [
    "uri",
    "type",
    "context_type",
    "level",
    "name",
    "description",
    "tags",
    "search_tags",
    "abstract",
    # Chunk identity and parent/child linkage. A file legitimately owns many L2
    # chunk rows; without these fields a restored package keeps the rows but
    # loses which chunk is which and how the hierarchy hangs together.
    "chunk_id",
    "parent_chunk_id",
    "chunk_ordinal",
    "chunk_continuation",
    "chunk_text",
    "parent_text",
    "content_revision",
    "chunker_version",
    "document_id",
    "embedding_space_id",
    "index_plan_id",
    "index_generation",
    # A joint image row cites the markdown it illustrates; the record's own
    # business meta (modality category, chapter/page, machine model, keywords)
    # is what meta queries filter on.
    "image_context_uri",
    "image_context_hash",
    "business_meta_payload",
    "business_meta_tokens",
    "business_meta_fingerprint",
    "business_meta_source_fingerprint",
]
EXPORT_VECTOR_FIELDS = [*PORTABLE_VECTOR_SCALAR_FIELDS, "vector"]

# Recency is fetched for ordering only; the package carries no timestamps. A
# store can still hold several live generations of one record (an interrupted
# publisher leaves them untombstoned), and restore collapses them onto the
# canonical record id -- so the export must emit each logical record oldest
# first, newest last, and the surviving row is then the current generation.
EXPORT_TIMESTAMP_FIELDS = ["created_at", "updated_at"]

# A package must carry every index record under the exported subtree. Offset
# pagination is not a reachable cursor here: the filter path behind it samples
# with a random query vector, so successive pages can repeat or skip rows. One
# query whose limit exceeds any realistic per-URI row count returns the whole
# match set instead (same bound the descendant sweep uses).
OVPACK_VECTOR_SCAN_LIMIT = 100_000


def portable_scalars(record: dict[str, Any]) -> dict[str, Any]:
    return {
        field: record[field]
        for field in PORTABLE_VECTOR_SCALAR_FIELDS
        if field != "uri" and record.get(field) is not None
    }


def record_level(record: dict[str, Any], default: int = 2) -> int:
    try:
        return int(record.get("level", default))
    except (TypeError, ValueError):
        return default


def _record_sort_key(record: dict[str, Any]) -> tuple[int, int, str, str]:
    try:
        ordinal = int(record.get("chunk_ordinal", 0))
    except (TypeError, ValueError):
        ordinal = 0
    return (
        record_level(record),
        ordinal,
        str(record.get("chunk_id") or ""),
        str(record.get("updated_at") or record.get("created_at") or ""),
    )


def coerce_dense_vector(value: Any) -> list[float]:
    if not isinstance(value, list) or not value:
        return []
    dense: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return []
        dense.append(float(item))
    return dense


async def call_vector_filter(
    vector_store,
    uri: str,
    ctx: RequestContext,
    *,
    include_vectors: bool = False,
) -> list[dict[str, Any]]:
    if not vector_store or not hasattr(vector_store, "filter"):
        return []

    kwargs = {
        "filter": Eq("uri", uri),
        "limit": OVPACK_VECTOR_SCAN_LIMIT,
        "output_fields": [
            *(EXPORT_VECTOR_FIELDS if include_vectors else PORTABLE_VECTOR_SCALAR_FIELDS),
            *EXPORT_TIMESTAMP_FIELDS,
        ],
    }
    try:
        return await vector_store.filter(**kwargs, ctx=ctx)
    except TypeError:
        try:
            return await vector_store.filter(**kwargs)
        except Exception as exc:
            logger.warning(f"Failed to export vector scalars for {uri}: {exc}")
    except Exception as exc:
        logger.warning(f"Failed to export vector scalars for {uri}: {exc}")
    return []


async def read_text_if_exists(viking_fs, uri: str, ctx: RequestContext) -> str:
    try:
        if not await viking_fs.exists(uri, ctx=ctx):
            return ""
        content = await viking_fs.read_file(uri, ctx=ctx)
        return content.decode("utf-8") if isinstance(content, bytes) else content
    except Exception:
        return ""


async def directory_vector_texts(viking_fs, uri: str, ctx: RequestContext) -> dict[int, str]:
    abstract = await read_text_if_exists(viking_fs, f"{uri}/.abstract.md", ctx)
    overview = await read_text_if_exists(viking_fs, f"{uri}/.overview.md", ctx)
    return {0: abstract, 1: overview}


async def index_records_for_uri(
    viking_fs,
    vector_store,
    uri: str,
    is_dir: bool,
    ctx: RequestContext,
    *,
    include_vectors: bool = False,
) -> list[dict[str, Any]]:
    records = await call_vector_filter(vector_store, uri, ctx, include_vectors=include_vectors)
    texts = await directory_vector_texts(viking_fs, uri, ctx) if is_dir else {}

    indexed: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    indexed_levels: set[int] = set()
    for source_record in records:
        level = record_level(source_record)
        item = {
            "level": level,
            "scalars": portable_scalars(source_record),
        }
        text = texts.get(level)
        if text:
            item["text"] = text
        if include_vectors:
            dense = coerce_dense_vector(source_record.get("vector"))
            if dense:
                item["_dense_vector"] = dense
        indexed.append((_record_sort_key(source_record), item))
        indexed_levels.add(level)

    if is_dir:
        abstract = texts.get(0, "")
        for level, text in texts.items():
            if text and level not in indexed_levels:
                item = {
                    "level": level,
                    "text": text,
                    "scalars": {
                        "context_type": context_type_for_uri(uri),
                        "level": level,
                        "abstract": abstract,
                    },
                }
                indexed.append((_record_sort_key({"level": level}), item))

    return [item for _, item in sorted(indexed, key=lambda pair: pair[0])]


def append_index_records(
    index_records: list[dict[str, Any]],
    dense_values: list[float],
    rel_path: str,
    kind: str,
    records: list[dict[str, Any]],
    user_id: str | None = None,
) -> None:
    for record in records:
        item = {
            "record_id": f"r{len(index_records) + 1:06d}",
            "path": rel_path,
            "kind": kind,
            "level": int(record.get("level", 2)),
        }
        if user_id:
            item["user_id"] = user_id
        text = record.get("text")
        if isinstance(text, str) and text:
            item["text"] = text
        scalars = record.get("scalars")
        if isinstance(scalars, dict) and scalars:
            item["scalars"] = dict(scalars)

        dense = coerce_dense_vector(record.get("_dense_vector"))
        if dense:
            item["vector"] = {
                "dense": {
                    "offset": len(dense_values),
                    "dimensions": len(dense),
                }
            }
            dense_values.extend(dense)
        index_records.append(item)


async def build_manifest(
    viking_fs,
    vector_store,
    root_uri: str,
    base_name: str,
    entries: list[dict[str, Any]],
    ctx: RequestContext,
    package_type: str | None = None,
    scopes: list[str] | None = None,
    include_vectors: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[float]]:
    root_user_id = owner_fields_for_uri(root_uri).get("owner_user_id")
    root_entry: dict[str, Any] = {"path": "", "kind": "directory"}
    if root_user_id:
        root_entry["user_id"] = root_user_id
    manifest_entries = [root_entry]
    index_records: list[dict[str, Any]] = []
    dense_values: list[float] = []

    if root_uri != "viking://" and not is_session_uri(root_uri):
        root_records = await index_records_for_uri(
            viking_fs,
            vector_store,
            root_uri,
            is_dir=True,
            ctx=ctx,
            include_vectors=include_vectors,
        )
        append_index_records(
            index_records,
            dense_values,
            "",
            "directory",
            root_records,
            user_id=root_user_id,
        )

    for entry in entries:
        rel_path = entry["rel_path"]
        is_dir = bool(entry.get("isDir"))
        entry_uri = join_uri(root_uri, rel_path)
        entry_user_id = owner_fields_for_uri(entry_uri).get("owner_user_id")
        manifest_entry = {
            "path": rel_path,
            "kind": "directory" if is_dir else "file",
            "size": entry.get("size", 0) if not is_dir else 0,
        }
        if entry_user_id:
            manifest_entry["user_id"] = entry_user_id
        manifest_entries.append(manifest_entry)
        if is_session_uri(entry_uri):
            continue
        records = await index_records_for_uri(
            viking_fs,
            vector_store,
            entry_uri,
            is_dir=is_dir,
            ctx=ctx,
            include_vectors=include_vectors,
        )
        append_index_records(
            index_records,
            dense_values,
            rel_path,
            "directory" if is_dir else "file",
            records,
            user_id=entry_user_id,
        )

    root = {
        "name": base_name,
        "uri": root_uri,
        "scope": "root" if root_uri == "viking://" else VikingURI(root_uri).scope,
    }
    manifest: dict[str, Any] = {
        "kind": OVPACK_KIND,
        "format_version": OVPACK_FORMAT_VERSION,
        "root": root,
        "entries": manifest_entries,
    }
    if package_type:
        root["package_type"] = package_type
    if root_user_id:
        root["user_id"] = root_user_id
    if scopes is not None:
        manifest["scopes"] = scopes
    return manifest, index_records, dense_values
