# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""OVPack dense vector snapshot helpers."""

from __future__ import annotations

import struct
import zipfile
from typing import Any, Iterator

from openviking.core.namespace import (
    content_owner_context_for_uri,
    context_type_for_uri,
    is_session_uri,
    owner_fields_for_uri,
)
from openviking.server.identity import RequestContext
from openviking.storage.ovpack.format import (
    OVPACK_DENSE_PATH,
    dense_values_bytes,
    internal_zip_path,
    sha256_hex,
)
from openviking.storage.ovpack.manifest import manifest_dense_info, manifest_entry_target_uri
from openviking.storage.vector_ids import vector_record_id, vector_record_id_for_data
from openviking.storage.ovpack.validation import dense_record_count, record_dense_ref
from openviking.utils.time_utils import get_current_timestamp
from openviking_cli.exceptions import InvalidArgumentError
from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)


def _index_meta_is_hybrid(index_meta: Any) -> bool:
    if not isinstance(index_meta, dict):
        return False
    vector_index = index_meta.get("VectorIndex")
    if not isinstance(vector_index, dict):
        return False
    index_type = str(vector_index.get("IndexType") or "").lower()
    return "hybrid" in index_type


def _vector_store_index_meta_is_hybrid(vector_store: Any) -> bool:
    candidates = [vector_store, getattr(vector_store, "_manager", None)]
    for candidate in candidates:
        if candidate is None:
            continue
        index_name = getattr(candidate, "_index_name", None)
        try:
            backend = candidate._get_default_backend()
            index_name = index_name or getattr(backend, "_index_name", None)
            collection = backend._get_collection()
            if index_name and hasattr(collection, "get_index_meta_data"):
                if _index_meta_is_hybrid(collection.get_index_meta_data(index_name)):
                    return True
        except Exception:
            continue
    return False


def dense_snapshot_unsupported_reason(vector_store=None) -> str | None:
    """Return why dense vector snapshots are unsupported in the current runtime."""
    if _vector_store_index_meta_is_hybrid(vector_store):
        return "current vector index type is hybrid"

    return None


def ensure_dense_snapshot_supported(vector_store=None) -> None:
    reason = dense_snapshot_unsupported_reason(vector_store)
    if reason:
        raise InvalidArgumentError(
            "ovpack vector snapshots only support pure dense vector indexes",
            details={"reason": reason},
        )


def embedding_snapshot_metadata(dimensions: int | None) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    try:
        from openviking_cli.utils.config import get_openviking_config

        embedding_cfg = get_openviking_config().embedding
        model_cfg = embedding_cfg.hybrid or embedding_cfg.dense
        if model_cfg:
            metadata = {
                "provider": model_cfg.provider,
                "model": model_cfg.model,
                "input": model_cfg.input,
                "query_param": model_cfg.query_param,
                "document_param": model_cfg.document_param,
            }
            if dimensions is None:
                dimensions = model_cfg.get_effective_dimension()
    except Exception:
        pass

    if dimensions is not None:
        metadata["dimensions"] = dimensions
    return {key: value for key, value in metadata.items() if value is not None}


def build_dense_snapshot_manifest(
    index_records: list[dict[str, Any]],
    dense_values: list[float],
) -> tuple[bytes, dict[str, Any]] | None:
    if not dense_values:
        return None

    dense_bytes = dense_values_bytes(dense_values)
    dense_dimensions_set: set[int] = set()
    dense_count = 0
    for record in index_records:
        dense = record_dense_ref(record)
        if dense is None:
            continue
        dense_dimensions_set.add(dense["dimensions"])
        dense_count += 1
    if len(dense_dimensions_set) != 1:
        raise InvalidArgumentError(
            "Cannot export ovpack vectors with mixed dimensions",
            details={"dimensions": sorted(dense_dimensions_set)},
        )
    dense_dimensions = next(iter(dense_dimensions_set))
    return dense_bytes, {
        "path": OVPACK_DENSE_PATH,
        "count": dense_count,
        "dtype": "float32",
        "byte_order": "little",
        "dimensions": dense_dimensions,
        "sha256": sha256_hex(dense_bytes),
        "embedding": embedding_snapshot_metadata(dense_dimensions),
    }


def read_dense_vectors(
    zf: zipfile.ZipFile,
    manifest: dict[str, Any],
    base_name: str,
    index_records: list[dict[str, Any]],
) -> dict[str, list[float]]:
    dense_info = manifest_dense_info(manifest)
    if dense_info is None:
        return {}
    data = zf.read(internal_zip_path(base_name, OVPACK_DENSE_PATH))
    vectors: dict[str, list[float]] = {}
    for record in index_records:
        record_id = record.get("record_id")
        dense = record_dense_ref(record)
        if not isinstance(record_id, str) or dense is None:
            continue
        offset = dense["offset"] * 4
        dimensions = dense["dimensions"]
        values = struct.unpack_from(f"<{dimensions}f", data, offset)
        vectors[record_id] = [float(value) for value in values]
    return vectors


def current_embedding_metadata() -> dict[str, Any]:
    try:
        from openviking_cli.utils.config import get_openviking_config

        embedding_cfg = get_openviking_config().embedding
        model_cfg = embedding_cfg.hybrid or embedding_cfg.dense
        if not model_cfg:
            return {}
        return {
            "provider": model_cfg.provider,
            "model": model_cfg.model,
            "input": model_cfg.input,
            "query_param": model_cfg.query_param,
            "document_param": model_cfg.document_param,
            "dimensions": model_cfg.get_effective_dimension(),
        }
    except Exception:
        return {}


def _embedding_snapshot_compatible(manifest: dict[str, Any]) -> tuple[bool, str]:
    dense_info = manifest_dense_info(manifest)
    if dense_info is None:
        return False, "missing dense vector snapshot"

    package_embedding = dense_info.get("embedding")
    if not isinstance(package_embedding, dict):
        return False, "missing embedding metadata"

    current_embedding = current_embedding_metadata()
    if not current_embedding:
        return False, "current embedding metadata is unavailable"

    fields = ("provider", "model", "input", "query_param", "document_param", "dimensions")
    for field in fields:
        package_value = package_embedding.get(field)
        current_value = current_embedding.get(field)
        if package_value != current_value:
            return False, f"embedding {field} mismatch"
    return True, ""


def choose_vector_restore_action(
    manifest: dict[str, Any],
    index_records: list[dict[str, Any]],
    dense_vectors: dict[str, list[float]],
    *,
    vector_store,
    vector_mode: str,
) -> str:
    if vector_mode == "recompute":
        return "recompute"

    unsupported_reason = dense_snapshot_unsupported_reason(vector_store)
    if unsupported_reason:
        if vector_mode == "require":
            raise InvalidArgumentError(
                "ovpack dense vector snapshot cannot be restored into a sparse or hybrid index",
                details={"reason": unsupported_reason},
            )
        logger.info(
            "[ovpack] Recomputing vectors because dense snapshot restore is unsupported: "
            f"{unsupported_reason}"
        )
        return "recompute"

    dense_count = dense_record_count(index_records)
    if dense_count == 0 or not dense_vectors:
        if vector_mode == "require":
            raise InvalidArgumentError(
                "ovpack package does not contain a dense vector snapshot",
                details={"vector_mode": vector_mode},
            )
        return "recompute"

    if dense_count != len(dense_vectors):
        if vector_mode == "require":
            raise InvalidArgumentError("ovpack dense vector snapshot is incomplete")
        return "recompute"

    if not vector_store or not hasattr(vector_store, "upsert"):
        if vector_mode == "require":
            raise InvalidArgumentError("Vector restore requires a writable vector store")
        return "recompute"

    compatible, reason = _embedding_snapshot_compatible(manifest)
    if not compatible:
        if vector_mode == "require":
            raise InvalidArgumentError(
                "ovpack dense vector snapshot is incompatible with current embedding config",
                details={"reason": reason},
            )
        logger.info(f"[ovpack] Recomputing vectors because snapshot is incompatible: {reason}")
        return "recompute"

    return "restore"


def _vector_record_id(payload: dict[str, Any], ctx: RequestContext) -> str:
    """Rebuild the primary key the write path would have produced.

    Chunked files hold many L2 rows under one URI, so the level-derived key
    would let them overwrite each other; a row carrying ``chunk_id`` keys off
    its chunk identity instead, exactly as ``vector_record_id_for_data`` does
    on the write path. Call this on the sanitized restore payload so a
    generation dropped by the restore is dropped from the key too.
    """
    if payload.get("chunk_id"):
        return vector_record_id_for_data(payload)
    return vector_record_id(
        ctx.account_id, str(payload["uri"]), int(payload.get("level", 2))
    )


async def _upsert_vector_snapshot_record(
    vector_store,
    target_uri: str,
    record: dict[str, Any],
    dense_vector: list[float],
    ctx: RequestContext,
) -> None:
    payload = _build_vector_snapshot_payload(record, dense_vector, target_uri, ctx)
    try:
        await vector_store.upsert(payload, ctx=ctx)
    except TypeError:
        await vector_store.upsert(payload)


def _build_vector_snapshot_payload(
    record: dict[str, Any],
    dense_vector: list[float],
    target_uri: str,
    ctx: RequestContext,
) -> dict[str, Any]:
    level = int(record.get("level", 2))
    scalars = dict(record.get("scalars") or {})
    # Keep ``index_generation``: the pack ships the source head snapshot (see
    # publication_bundle._install_head_snapshot), so the destination manifest
    # carries the same generations. Dropping it makes every restored row fail
    # the post-search visibility recheck (a row whose generation is empty is
    # only visible when the manifest does not know its URI), which silently
    # hides the whole restored subtree from retrieval.

    owner_fields = owner_fields_for_uri(target_uri)
    timestamp = get_current_timestamp()
    payload = {
        **scalars,
        "uri": target_uri,
        "context_type": context_type_for_uri(target_uri),
        "level": level,
        "created_at": timestamp,
        "updated_at": timestamp,
        "active_count": 0,
        "account_id": ctx.account_id,
        "owner_user_id": owner_fields.get("owner_user_id"),
        "vector": dense_vector,
    }
    if not payload.get("abstract"):
        payload["abstract"] = str(record.get("text") or "")
    if level <= 1:
        # Directory summary rows keep their vector (the semantic essence) but
        # must stay well under the embedded engine's per-row byte budget; an
        # oversized row is rejected by the index while the payload layer still
        # answers scans, which looks like an unsearchable level.
        summary = str(payload.get("abstract") or "")
        if len(summary) > 1200:
            payload["abstract"] = summary[:1200]
        payload.pop("business_meta_payload", None)
        payload.pop("business_meta_tokens", None)
    chunk_text = payload.get("chunk_text")
    if isinstance(chunk_text, str) and chunk_text:
        payload["content"] = chunk_text
    elif "content" not in payload and level >= 2:
        # Directory summaries already carry their text in ``abstract``; copying
        # it into ``content`` doubles the row and pushes the serialized record
        # at (or past) the embedded engine's per-row byte limit, which rejects
        # the write while the payload layer still answers scans.
        payload["content"] = str(record.get("text") or "")
    payload["id"] = _vector_record_id(payload, ctx)
    return payload


# Records per bulk upsert. The engine's upsert_many lands a whole batch in one
# adapter call (one index write window) instead of one round trip per record,
# which is what kept snapshot restores of large tenants at ~100 records/s.
_VECTOR_SNAPSHOT_BATCH = 500


async def restore_vector_snapshot(
    vector_store,
    root_uri: str,
    index_records: list[dict[str, Any]],
    dense_vectors: dict[str, list[float]],
    manifest_entries: dict[str, dict[str, Any]],
    ctx: RequestContext,
) -> None:
    def _iter_targeted_payloads() -> "Iterator[tuple[str, dict[str, Any]]]":
        for record in index_records:
            record_id = record.get("record_id")
            if not isinstance(record_id, str) or record_id not in dense_vectors:
                continue
            rel_path = record.get("path")
            if not isinstance(rel_path, str):
                continue
            manifest_entry = manifest_entries.get(rel_path)
            if manifest_entry is None:
                continue
            target_uri = manifest_entry_target_uri(root_uri, rel_path, manifest_entry)
            if target_uri == "viking://user" or is_session_uri(target_uri):
                continue
            owner_ctx = content_owner_context_for_uri(target_uri, ctx)
            payload = _build_vector_snapshot_payload(
                record, dense_vectors[record_id], target_uri, owner_ctx
            )
            yield payload["id"], payload

    payloads = list(_iter_targeted_payloads())
    bulk = hasattr(vector_store, "upsert_many")
    if bulk and hasattr(vector_store, "begin_bulk_ingest"):
        try:
            await vector_store.begin_bulk_ingest()
        except Exception as exc:  # noqa: BLE001 - rebuild deferral is best-effort
            logger.warning(f"[ovpack] begin_bulk_ingest unavailable: {exc}")

    try:
        if not bulk:
            # Sequential replay: every generation is written in pack order and
            # the last write wins, exactly like the pre-batching restore.
            for _, payload in payloads:
                try:
                    result_id = await vector_store.upsert(payload, ctx=ctx)
                except TypeError:
                    result_id = await vector_store.upsert(payload)
                if not result_id:
                    logger.warning(
                        "[ovpack] snapshot upsert rejected: level=%s uri=%s",
                        payload.get("level"), payload.get("uri"),
                    )
            return

        for start in range(0, len(payloads), _VECTOR_SNAPSHOT_BATCH):
            chunk = payloads[start : start + _VECTOR_SNAPSHOT_BATCH]
            # A pack may carry several generations of one logical record
            # (oldest first, newest last); sequential upserts end on the
            # newest, so keep the last payload per id within the batch.
            deduped: dict[str, dict[str, Any]] = {}
            for payload_id, payload in chunk:
                deduped[payload_id] = payload
            batch = list(deduped.values())
            try:
                try:
                    written = await vector_store.upsert_many(batch, ctx=ctx)
                except TypeError as exc:
                    if "ctx" not in str(exc) and "unexpected keyword" not in str(exc):
                        raise
                    written = await vector_store.upsert_many(batch)
                if isinstance(written, list) and len(written) != len(batch):
                    logger.warning(
                        "[ovpack] bulk snapshot wrote %s/%s records (levels: %s)",
                        len(written), len(batch),
                        sorted({p.get("level") for p in batch}),
                    )
            except (TypeError, ValueError) as exc:
                # No bulk path (or an unexpected intra-batch conflict): keep the
                # snapshot correct via the sequential replay of this chunk.
                logger.warning(f"[ovpack] bulk snapshot upsert unavailable ({exc}); falling back")
                for _, payload in chunk:
                    try:
                        await vector_store.upsert(payload, ctx=ctx)
                    except TypeError:
                        await vector_store.upsert(payload)
    finally:
        if bulk and hasattr(vector_store, "end_bulk_ingest"):
            try:
                await vector_store.end_bulk_ingest()
            except Exception as exc:  # noqa: BLE001 - best-effort
                logger.warning(f"[ovpack] end_bulk_ingest failed: {exc}")
