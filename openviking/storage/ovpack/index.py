# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""OVPack index record construction helpers."""

from __future__ import annotations

from array import array
from typing import Any

from openviking.core.namespace import context_type_for_uri, is_session_uri, owner_fields_for_uri
from openviking.server.identity import RequestContext
from openviking.storage.expr import Eq, PathScope
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


def assemble_entry_records(
    *,
    records: list[dict[str, Any]],
    texts: dict[int, str],
    is_dir: bool,
    uri: str,
    include_vectors: bool = False,
) -> list[dict[str, Any]]:
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
    return assemble_entry_records(
        records=records,
        texts=texts,
        is_dir=is_dir,
        uri=uri,
        include_vectors=include_vectors,
    )


def normalize_uri_key(uri: str) -> str:
    """Canonical map key for a record URI (trailing slashes are never meaningful)."""
    return uri.rstrip("/") or "/"


def _export_output_fields(include_vectors: bool) -> list[str]:
    """Same field set the per-URI exporter fetches, timestamps included."""
    return [
        *(EXPORT_VECTOR_FIELDS if include_vectors else PORTABLE_VECTOR_SCALAR_FIELDS),
        *EXPORT_TIMESTAMP_FIELDS,
    ]


async def _vector_filter_query(
    vector_store,
    filter_expr: Any,
    ctx: RequestContext,
    output_fields: list[str],
    scan_limit: int,
) -> list[dict[str, Any]] | None:
    """One vector-store filter query, or None when the query itself failed."""
    kwargs = {
        "filter": filter_expr,
        "limit": scan_limit,
        "output_fields": output_fields,
    }
    try:
        return await vector_store.filter(**kwargs, ctx=ctx)
    except TypeError:
        try:
            return await vector_store.filter(**kwargs)
        except Exception as exc:
            logger.warning(f"[ovpack] vector filter query failed: {exc}")
            return None
    except Exception as exc:
        logger.warning(f"[ovpack] vector filter query failed: {exc}")
        return None


async def _query_uri_records(
    vector_store,
    uri: str,
    ctx: RequestContext,
    output_fields: list[str],
    scan_limit: int = OVPACK_VECTOR_SCAN_LIMIT,
) -> list[dict[str, Any]]:
    """Exact per-URI fetch with the exporter's historical failure semantics."""
    if not vector_store or not hasattr(vector_store, "filter"):
        return []
    records = await _vector_filter_query(
        vector_store, Eq("uri", uri), ctx, output_fields, scan_limit
    )
    return records if records is not None else []


async def _query_subtree_records(
    vector_store,
    prefix_uri: str,
    ctx: RequestContext,
    output_fields: list[str],
    scan_limit: int = OVPACK_VECTOR_SCAN_LIMIT,
) -> list[dict[str, Any]] | None:
    """Every record at prefix_uri or below, or None when the query failed."""
    if not vector_store or not hasattr(vector_store, "filter"):
        return []
    return await _vector_filter_query(
        vector_store, PathScope("uri", prefix_uri, depth=-1), ctx, output_fields, scan_limit
    )


def _split_entry_group(
    prefix_uri: str, uris: list[str]
) -> tuple[list[str], dict[str, list[str]]]:
    """Partition entry URIs below prefix_uri by their next path component.

    Returns ``(self_bucket, groups)``: URIs that ARE the prefix (or outside it,
    which should not happen) go to ``self_bucket`` for exact per-URI queries;
    everything else is grouped under its child prefix. Splitting always makes
    progress because each child prefix is at least one component deeper.
    """
    prefix_norm = normalize_uri_key(prefix_uri)
    is_store_root = prefix_norm in ("/", "viking:")
    self_bucket: list[str] = []
    groups: dict[str, list[str]] = {}
    for uri in uris:
        normalized = normalize_uri_key(uri)
        if is_store_root:
            # viking:// has no meaningful path prefix; bucket by top scope.
            parts = normalized.split("/")
            key = "/".join(parts[:3]) if len(parts) >= 3 else normalized
        elif normalized == prefix_norm:
            self_bucket.append(uri)
            continue
        elif not normalized.startswith(prefix_norm + "/"):
            self_bucket.append(uri)
            continue
        else:
            rest = normalized[len(prefix_norm) + 1 :]
            key = f"{prefix_norm}/{rest.split('/', 1)[0]}"
        groups.setdefault(key, []).append(uri)
    return self_bucket, groups


async def walk_uri_record_batches(
    vector_store,
    root_uri: str,
    uris: list[str],
    ctx: RequestContext,
    *,
    output_fields: list[str],
    consume,
    scan_limit: int = OVPACK_VECTOR_SCAN_LIMIT,
    group_scan_limit: int = 25_000,
) -> None:
    """Fetch index records for many URIs with subtree-batched filter queries.

    The per-URI exporter issues one ``Eq("uri", ...)`` query per entry, which
    dominates OVPack export time on large trees (hundreds of thousands of
    round trips). This walker instead issues ``PathScope`` subtree queries and
    groups the rows locally, keeping single-query semantics per subtree:
    a subtree whose result set reaches ``group_scan_limit`` may be truncated
    (and its full-field fetch would also balloon memory on vector exports), so
    it is split by the next path component and retried; a single URI always
    falls back to the exact per-URI query with the full ``scan_limit``.
    ``consume(uris, by_uri)`` is awaited once per resolved batch, with the
    batch's URIs in caller order and ``{normalized_uri: [raw store record]}``
    (URIs with no records map to []).
    """
    if not vector_store or not hasattr(vector_store, "filter"):
        await consume(list(uris), {})
        return
    targets = {normalize_uri_key(u) for u in uris}

    async def walk(prefix: str, subset: list[str]) -> None:
        if not subset:
            return
        if len(subset) == 1:
            uri = subset[0]
            records = await _query_uri_records(vector_store, uri, ctx, output_fields, scan_limit)
            await consume(subset, {normalize_uri_key(uri): records})
            return

        prefix_norm = normalize_uri_key(prefix)
        raw: list[dict[str, Any]] | None = None
        if prefix_norm != "viking:":
            # Cheap probe first: a truncated full-field fetch of a huge subtree
            # is expensive to throw away, while an empty ["uri"] probe is not.
            probe = await _query_subtree_records(
                vector_store, prefix_norm, ctx, ["uri"], group_scan_limit
            )
            if probe is not None and len(probe) < group_scan_limit:
                raw = await _query_subtree_records(
                    vector_store, prefix_norm, ctx, output_fields, group_scan_limit
                )
                if raw is not None and len(raw) < group_scan_limit:
                    by_uri: dict[str, list[dict[str, Any]]] = {}
                    for record in raw:
                        key = normalize_uri_key(str(record.get("uri") or ""))
                        if key in targets:
                            by_uri.setdefault(key, []).append(record)
                    await consume(subset, by_uri)
                    return
        # Truncated, failed, or the store root: partition deeper and retry.
        split_prefix = "viking://" if prefix_norm == "viking:" else prefix
        self_bucket, groups = _split_entry_group(split_prefix, subset)
        for uri in self_bucket:
            records = await _query_uri_records(vector_store, uri, ctx, output_fields, scan_limit)
            await consume([uri], {normalize_uri_key(uri): records})
        for child_prefix, child_uris in groups.items():
            await walk(child_prefix, child_uris)

    await walk(root_uri, list(uris))


def append_index_records(
    index_records: list[dict[str, Any]],
    dense_values: "list[float] | array",
    rel_path: str,
    kind: str,
    records: list[dict[str, Any]],
    user_id: str | None = None,
    entry_seq: int | None = None,
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
        if entry_seq is not None:
            # Batched exports append grouped-by-subtree, then re-sort by this
            # sequence to reproduce the per-URI exporter's record order.
            item["_entry_seq"] = entry_seq
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
) -> tuple[dict[str, Any], list[dict[str, Any]], "list[float] | array"]:
    root_user_id = owner_fields_for_uri(root_uri).get("owner_user_id")
    root_entry: dict[str, Any] = {"path": "", "kind": "directory"}
    if root_user_id:
        root_entry["user_id"] = root_user_id
    manifest_entries: list[dict[str, Any]] = [root_entry]
    index_records: list[dict[str, Any]] = []
    # float32 array instead of a list of Python floats: a full-tenant export
    # with dense vectors carries ~400k x 2048 floats, which is ~3GB packed as
    # an array but ~25GB+ as boxed Python floats (observed OOM).
    dense_values = array("f")

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
            entry_seq=-1,
        )

    # Manifest entries only depend on the tree, so build them in entry order
    # up front; index records below are appended grouped-by-subtree and
    # re-sorted back into entry order at the end.
    entry_plans: list[tuple[int, str, str, bool, str | None]] = []
    for seq, entry in enumerate(entries):
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
        entry_plans.append((seq, rel_path, entry_uri, is_dir, entry_user_id))

    plan_by_uri = {
        normalize_uri_key(plan[2]): plan
        for plan in entry_plans
    }

    async def consume(
        uris: list[str], by_uri: dict[str, list[dict[str, Any]]]
    ) -> None:
        for uri in uris:
            plan = plan_by_uri.get(normalize_uri_key(uri))
            if plan is None:
                continue
            seq, rel_path, plan_uri, is_dir, entry_user_id = plan
            records = by_uri.get(normalize_uri_key(uri), [])
            texts = (
                await directory_vector_texts(viking_fs, plan_uri, ctx) if is_dir else {}
            )
            items = assemble_entry_records(
                records=records,
                texts=texts,
                is_dir=is_dir,
                uri=plan_uri,
                include_vectors=include_vectors,
            )
            append_index_records(
                index_records,
                dense_values,
                rel_path,
                "directory" if is_dir else "file",
                items,
                user_id=entry_user_id,
                entry_seq=seq,
            )
            # Free the fetched dense float lists as soon as they are packed;
            # retaining every raw record's 2048-float vector until the end of
            # the export is what OOMs large tenants.
            for record in records:
                record.pop("vector", None)

    await walk_uri_record_batches(
        vector_store,
        root_uri,
        [plan[2] for plan in entry_plans],
        ctx,
        output_fields=_export_output_fields(include_vectors),
        consume=consume,
    )

    if index_records:
        # Stable sort back into entry order (root first), then re-stamp the
        # record ids so a batched export is byte-identical to the per-URI one.
        index_records.sort(key=lambda item: item.get("_entry_seq", 0))
        for position, item in enumerate(index_records):
            item["record_id"] = f"r{position + 1:06d}"
            item.pop("_entry_seq", None)

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
