"""Self-contained publication state for ovpack migration.

An ovpack restores the file tree plus the dense snapshot, but a *published*
knowledge base also lives in the publication ledger (``builds``, ``sources``,
``semantic_sources``, ``desired``, ``receipts``, ``publication_lexical``) and in
content-addressed blobs under ``_system/index_blobs``. Without that state a
restored tenant is dense-only: reads raise "no immutable content" or hit a
missing blob, BM25 recall has no postings, and directory sidecars cannot be
projected.

This module writes that state next to the pack (``<base>/_ovpack/publication``)
and replays it on import:

1. ledger rows (idempotent upserts),
2. content blobs rebuilt from the restored files themselves (no need to ship
   megabytes of duplicated bytes),
3. head snapshot merged into the destination's published view,
4. content refs reconciled against the on-disk revision,
5. the lexical FTS mirror marked stale and rebuilt.

The bundle is optional: packs without it restore exactly as before.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import zipfile
from typing import Any, Iterable, Optional

from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)

_PUBLICATION_DIR = "_ovpack/publication"
_META_LEAF = f"{_PUBLICATION_DIR}/meta.json"
_BUILDS_LEAF = f"{_PUBLICATION_DIR}/builds.jsonl"
_SOURCES_LEAF = f"{_PUBLICATION_DIR}/sources.jsonl"
_SEMANTICS_LEAF = f"{_PUBLICATION_DIR}/semantic_sources.jsonl"
_DESIRED_LEAF = f"{_PUBLICATION_DIR}/desired.jsonl"
_LEXICAL_LEAF = f"{_PUBLICATION_DIR}/lexical.jsonl"
_RECEIPTS_LEAF = f"{_PUBLICATION_DIR}/receipts.jsonl"
_SNAPSHOT_LEAF = f"{_PUBLICATION_DIR}/head_snapshot.bin"

_PUBLICATION_FORMAT_VERSION = 1
_IN_QUERY_CHUNK = 900
_BLOB_WORKERS = 8


def _leaf(base_name: str, leaf: str) -> str:
    return f"{base_name}/{leaf}"


def publication_bundle_present(zf: zipfile.ZipFile, base_name: str) -> bool:
    return _leaf(base_name, _META_LEAF) in zf.namelist()


def _jsonl_bytes(rows: Iterable[Iterable[Any]]) -> bytes:
    return "".join(
        json.dumps(list(row), ensure_ascii=False, separators=(",", ":")) + "\n"
        for row in rows
    ).encode("utf-8")


def _store():
    from openviking.indexing.publication import configured_store

    return configured_store()


def _chunks(items: list[Any], size: int = _IN_QUERY_CHUNK):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _map_uri(uri: str, source_root: str, target_root: str) -> Optional[str]:
    """Map a source-side URI onto the import target (export→import relocates)."""
    if not isinstance(uri, str) or not uri.startswith("viking://"):
        return None
    if not source_root or not target_root or source_root == target_root:
        return uri
    source = source_root.rstrip("/")
    target = target_root.rstrip("/")
    if uri == source:
        return target
    prefix = source + "/"
    if uri.startswith(prefix):
        return target + "/" + uri[len(prefix) :]
    return None


async def export_publication_bundle(
    zf: zipfile.ZipFile,
    base_name: str,
    root_uri: str,
    uris: list[str],
    ctx: Any,
) -> Optional[dict[str, Any]]:
    """Write the publication state of ``uris`` into the pack. Returns meta."""
    store = _store()
    if store is None:
        return None
    account = ctx.account_id
    wanted = sorted({uri for uri in uris if isinstance(uri, str) and uri.startswith("viking://")})
    if not wanted:
        return None
    try:
        with store.db(readonly=True) as db:
            generations: list[str] = []
            builds: list[tuple] = []
            for chunk in _chunks(wanted):
                marks = ",".join("?" * len(chunk))
                for row in db.execute(
                    "SELECT generation, account, namespace, uri, plan, expected, state, created, error "
                    f"FROM builds WHERE account=? AND uri IN ({marks})",
                    (account, *chunk),
                ):
                    builds.append(tuple(row))
                    generations.append(row["generation"])
            generation_set = sorted(set(generations))
            if not generation_set:
                return None
            sources: list[tuple] = []
            semantics: list[tuple] = []
            receipts: list[tuple] = []
            for chunk in _chunks(generation_set):
                marks = ",".join("?" * len(chunk))
                sources.extend(
                    tuple(row)
                    for row in db.execute(
                        f"SELECT generation, reference FROM sources WHERE generation IN ({marks})",
                        chunk,
                    )
                )
                semantics.extend(
                    tuple(row)
                    for row in db.execute(
                        "SELECT generation, reference FROM semantic_sources "
                        f"WHERE generation IN ({marks})",
                        chunk,
                    )
                )
                receipts.extend(
                    tuple(row)
                    for row in db.execute(
                        f"SELECT generation, child FROM receipts WHERE generation IN ({marks})",
                        chunk,
                    )
                )
            desired: list[tuple] = []
            for chunk in _chunks(wanted):
                marks = ",".join("?" * len(chunk))
                desired.extend(
                    tuple(row)
                    for row in db.execute(
                        "SELECT account, namespace, uri, generation FROM desired "
                        f"WHERE account=? AND uri IN ({marks})",
                        (account, *chunk),
                    )
                )
            lexical: list[tuple] = []
            namespace_by_uri = {row[3]: row[2] for row in builds}
            for chunk in _chunks(wanted):
                marks = ",".join("?" * len(chunk))
                lexical.extend(
                    tuple(row)
                    for row in db.execute(
                        "SELECT generation, child, uri, tokens, tokenizer, payload "
                        f"FROM publication_lexical WHERE uri IN ({marks})",
                        chunk,
                    )
                )
            head_items: dict[str, Any] = {}
            for row in db.execute(
                "SELECT namespace FROM heads WHERE account=?", (account,)
            ):
                namespace = row["namespace"]
                row = db.execute(
                    "SELECT s.manifest FROM heads h JOIN snapshots s ON s.id=h.snapshot "
                    "WHERE h.account=? AND h.namespace=?",
                    (account, namespace),
                ).fetchone()
                manifest = _decode_manifest(row[0] if row is not None else None)
                for uri, value in (manifest or {}).items():
                    if uri in wanted:
                        head_items[uri] = value
        tokenizer = ""
        if lexical:
            tokenizer = str(lexical[0][4] or "")
        meta = {
            "format": _PUBLICATION_FORMAT_VERSION,
            "account": account,
            "root_uri": root_uri,
            "source_host": os.uname().nodename if hasattr(os, "uname") else "",
            "tokenizer": tokenizer,
            "counts": {
                "builds": len(builds),
                "sources": len(sources),
                "semantic_sources": len(semantics),
                "desired": len(desired),
                "receipts": len(receipts),
                "lexical": len(lexical),
                "head_snapshot": len(head_items),
            },
            "namespaces": sorted({v for v in namespace_by_uri.values() if v}),
        }
        if not builds:
            return None
        zf.writestr(_leaf(base_name, _META_LEAF), json.dumps(meta, ensure_ascii=False).encode("utf-8"))
        zf.writestr(_leaf(base_name, _BUILDS_LEAF), _jsonl_bytes(builds))
        zf.writestr(_leaf(base_name, _SOURCES_LEAF), _jsonl_bytes(sources))
        zf.writestr(_leaf(base_name, _SEMANTICS_LEAF), _jsonl_bytes(semantics))
        zf.writestr(_leaf(base_name, _DESIRED_LEAF), _jsonl_bytes(desired))
        zf.writestr(_leaf(base_name, _RECEIPTS_LEAF), _jsonl_bytes(receipts))
        zf.writestr(_leaf(base_name, _LEXICAL_LEAF), _jsonl_bytes(lexical))
        if head_items:
            snapshot_blob = _encode_manifest(head_items)
            zf.writestr(_leaf(base_name, _SNAPSHOT_LEAF), snapshot_blob)
        meta["written"] = True
        logger.info(
            "[ovpack] Exported publication bundle: %s",
            {k: v for k, v in meta["counts"].items()},
        )
        return meta
    except Exception as exc:  # noqa: BLE001 - the bundle must never break a plain export
        logger.warning(f"[ovpack] Skipping publication bundle: {exc}")
        return None


# ── codec helpers (mirror openviking.indexing.manifest_codec) ──


def _encode_manifest(manifest: dict) -> bytes:
    return gzip.compress(
        json.dumps(manifest, separators=(",", ":")).encode("utf-8"), 1
    )


def _decode_manifest(raw) -> Optional[dict]:
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        data = bytes(raw)
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        return json.loads(data.decode("utf-8"))
    return json.loads(str(raw))


async def import_publication_bundle(
    viking_fs,
    file_path: str,
    base_name: str,
    root_uri: str,
    ctx: Any,
    entries: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Replay the bundle after the file tree and dense snapshot are restored."""
    with zipfile.ZipFile(file_path) as probe:
        if not publication_bundle_present(probe, base_name):
            return None
    with zipfile.ZipFile(file_path) as zf:
        return await _import_publication_bundle_zip(
            viking_fs, zf, base_name, root_uri, ctx, entries
        )


async def _import_publication_bundle_zip(
    viking_fs,
    zf: zipfile.ZipFile,
    base_name: str,
    root_uri: str,
    ctx: Any,
    entries: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    store = _store()
    if store is None:
        logger.warning("[ovpack] publication bundle present but no publication store configured")
        return None
    account = ctx.account_id
    meta = json.loads(zf.read(_leaf(base_name, _META_LEAF)).decode("utf-8"))
    result: dict[str, Any] = {"meta": meta, "ledger_rows": 0, "blobs": 0, "refs_fixed": 0}
    source_root = str(meta.get("root_uri") or "viking://")
    target_root = root_uri

    def _remap(uri: Any) -> Optional[str]:
        return _map_uri(uri, source_root, target_root)

    def _remap_manifest(items: dict) -> dict:
        out = {}
        for uri, value in items.items():
            mapped = _remap(uri)
            if mapped:
                out[mapped] = value
        return out

    # 1) ledger rows
    def _rows(leaf: str) -> list[tuple]:
        raw = zf.read(_leaf(base_name, leaf))
        return [tuple(json.loads(line)) for line in raw.decode("utf-8").splitlines() if line]

    # exported builds rows already carry: generation, account, namespace, uri,
    # plan, expected, state, created, error
    builds = []
    for row in _rows(_BUILDS_LEAF):
        mapped = _remap(row[3])
        if mapped:
            builds.append((row[0], account, row[2], mapped, row[4], row[5], row[6], row[7], row[8]))
    sources = _rows(_SOURCES_LEAF)
    semantics = _rows(_SEMANTICS_LEAF)
    # exported desired rows: account, namespace, uri, generation
    desired = []
    for row in _rows(_DESIRED_LEAF):
        mapped = _remap(row[2])
        if mapped:
            desired.append((account, row[1], mapped, row[3]))
    receipts = _rows(_RECEIPTS_LEAF)
    lexical = []
    for row in _rows(_LEXICAL_LEAF):
        mapped = _remap(row[2])
        if mapped:
            lexical.append((row[0], row[1], mapped, row[3], row[4], row[5]))

    with store.db() as db:
        for row in builds:
            db.execute(
                "INSERT OR REPLACE INTO builds VALUES (?,?,?,?,?,?,?,?,?)", row
            )
        for row in sources:
            db.execute("INSERT OR REPLACE INTO sources VALUES (?,?)", row)
        for row in semantics:
            db.execute("INSERT OR REPLACE INTO semantic_sources VALUES (?,?)", row)
        for row in desired:
            db.execute("INSERT OR REPLACE INTO desired VALUES (?,?,?,?)", row)
        for row in receipts:
            db.execute("INSERT OR IGNORE INTO receipts VALUES (?,?)", row)
        for row in lexical:
            db.execute(
                "INSERT OR IGNORE INTO publication_lexical VALUES (?,?,?,?,?,?)", row
            )
        result["ledger_rows"] = len(builds) + len(sources) + len(semantics) + len(lexical)

    # 2) content blobs rebuilt from the restored files (AGFS raw read: the
    #    publication view is stale at this point by design).
    result["blobs"], result["refs_fixed"] = await _rebuild_blobs_and_refs(
        viking_fs, store, account, root_uri, entries, ctx
    )

    # 3) merge the shipped head snapshot into the destination's published view
    merged = _install_head_snapshot(store, account, zf, base_name, source_root, target_root)

    # 4) mark the lexical mirror stale and rebuild it so BM25 covers the corpus
    try:
        with store.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO publication_lexical_index VALUES (?,?,?,?)",
                (account, -1, meta.get("tokenizer") or "zh-lexical-v2", 0.0),
            )
        rebuilt = await asyncio.to_thread(store.rebuild_lexical_index, [account])
        result["lexical"] = rebuilt
    except Exception as exc:  # noqa: BLE001 - retrieval still works on dense
        logger.warning(f"[ovpack] lexical mirror rebuild failed: {exc}")
        result["lexical"] = f"failed: {exc}"

    result["head_uris"] = merged
    logger.info(f"[ovpack] Imported publication bundle: {result}")
    return result


async def _rebuild_blobs_and_refs(
    viking_fs,
    store,
    account: str,
    root_uri: str,
    entries: list[dict[str, Any]],
    ctx: Any,
) -> tuple[int, int]:
    from openviking.indexing.content import blob_path
    from openviking.storage.ovpack.format import join_uri

    blob_dir = blob_path(viking_fs, ctx, "0" * 64).rsplit("/", 1)[0]
    await viking_fs._async_agfs.ensure_parent_dirs(blob_dir + "/.keep")

    file_entries = [
        entry for entry in entries if not entry.get("isDir") and entry.get("rel_path")
    ]
    semaphore = asyncio.Semaphore(_BLOB_WORKERS)
    counter = {"written": 0, "present": 0}

    async def _one(entry: dict[str, Any]) -> None:
        async with semaphore:
            uri = entry.get("uri") or join_uri(root_uri, entry["rel_path"])
            path = viking_fs._uri_to_path(uri, ctx=ctx)
            try:
                data = await viking_fs._async_agfs.read(path)
            except Exception:  # noqa: BLE001 - optional sidecars may be missing
                return
            digest = hashlib.sha256(data).hexdigest()
            target = blob_path(viking_fs, ctx, digest)
            try:
                await viking_fs._async_agfs.stat(target)
                counter["present"] += 1
                return
            except Exception:  # noqa: BLE001 - absent blob
                pass
            await viking_fs._async_agfs.write(target, data)
            counter["written"] += 1

    await asyncio.gather(*[_one(entry) for entry in file_entries])

    # reconcile manifest content refs with the on-disk revision
    fixed = 0
    try:
        with store.db() as db:
            for namespace in ("resources", "user"):
                row = db.execute(
                    "SELECT s.manifest FROM heads h JOIN snapshots s ON s.id=h.snapshot "
                    "WHERE h.account=? AND h.namespace=?",
                    (account, namespace),
                ).fetchone()
                if row is None:
                    continue
                manifest = _decode_manifest(row[0]) or {}
                changed = False
                for uri, item in manifest.items():
                    if not isinstance(item, dict) or not isinstance(item.get("content"), dict):
                        continue
                    path = viking_fs._uri_to_path(uri, ctx=ctx)
                    try:
                        data = await viking_fs._async_agfs.read(path)
                    except Exception:  # noqa: BLE001
                        continue
                    digest = hashlib.sha256(data).hexdigest()
                    if item["content"].get("sha256") != digest:
                        item["content"]["sha256"] = digest
                        item["content"]["size"] = len(data)
                        if "text_sha256" in item["content"]:
                            item["content"]["text_sha256"] = digest
                        db.execute(
                            "UPDATE sources SET reference=? WHERE generation=?",
                            (json.dumps(item["content"]), item.get("generation")),
                        )
                        changed = True
                        fixed += 1
                if changed:
                    _replace_head(db, account, namespace, manifest)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ovpack] content ref reconciliation failed: {exc}")
    return counter["written"], fixed


def _replace_head(db, account: str, namespace: str, manifest: dict) -> None:
    import time
    import uuid

    snapshot_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO snapshots VALUES (?,?,?,?,?)",
        (snapshot_id, account, namespace, _encode_manifest(manifest), time.time()),
    )
    db.execute(
        "INSERT OR REPLACE INTO heads VALUES (?,?,?)", (account, namespace, snapshot_id)
    )
    db.execute("DELETE FROM snapshots WHERE account=? AND namespace=? AND id!=?",
               (account, namespace, snapshot_id))


def _install_head_snapshot(
    store,
    account: str,
    zf: zipfile.ZipFile,
    base_name: str,
    source_root: str = "viking://",
    target_root: str = "viking://",
) -> int:
    leaf = _leaf(base_name, _SNAPSHOT_LEAF)
    if leaf not in zf.namelist():
        return 0
    raw_shipped = _decode_manifest(zf.read(leaf)) or {}
    shipped: dict[str, Any] = {}
    for uri, value in raw_shipped.items():
        mapped = _map_uri(uri, source_root, target_root)
        if mapped:
            shipped[mapped] = value
    if not shipped:
        return 0
    by_namespace: dict[str, dict] = {}
    for uri, value in shipped.items():
        namespace = "user" if uri.startswith("viking://user") else "resources"
        by_namespace.setdefault(namespace, {})[uri] = value
    installed = 0
    with store.db() as db:
        for namespace, items in by_namespace.items():
            row = db.execute(
                "SELECT s.manifest FROM heads h JOIN snapshots s ON s.id=h.snapshot "
                "WHERE h.account=? AND h.namespace=?",
                (account, namespace),
            ).fetchone()
            merged = dict(_decode_manifest(row[0]) or {}) if row else {}
            merged.update(items)
            _replace_head(db, account, namespace, merged)
            installed += len(items)
    return installed


async def append_publication_bundle(
    to: str,
    base_name: str,
    root_uri: str,
    entries: list[dict[str, Any]],
    ctx: Any,
) -> Optional[dict[str, Any]]:
    """Append the publication bundle to an already-written pack (zip append)."""
    from openviking.storage.ovpack.format import join_uri

    uris = []
    for entry in entries:
        uri = entry.get("uri")
        if not uri:
            rel_path = entry.get("rel_path")
            if not rel_path:
                uri = root_uri
            else:
                uri = join_uri(root_uri, rel_path)
        uris.append(uri)
    if root_uri not in uris:
        uris.append(root_uri)
    try:
        with zipfile.ZipFile(to, "a", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            return await export_publication_bundle(zf, base_name, root_uri, uris, ctx)
    except Exception as exc:  # noqa: BLE001 - never break a plain export
        logger.warning(f"[ovpack] publication bundle append failed: {exc}")
        return None


# ── vector index snapshot (offline migration) ──

_INDEX_LEAF = f"{_PUBLICATION_DIR}/vectordb_index.json"


def _vectordb_index_dir() -> Optional[str]:
    """Resolve the live vector-index directory of the local engine."""
    try:
        from openviking_cli.utils.config import get_openviking_config

        config = get_openviking_config()
        root = getattr(config.storage.vectordb, "path", None) or getattr(
            config.storage, "workspace", None
        )
    except Exception:  # noqa: BLE001 - index shipping is optional
        return None
    if not root:
        return None
    candidate = os.path.join(str(root), "vectordb", "context", "index", "default")
    return candidate if os.path.isdir(candidate) else None


def ship_vectordb_index(zf: zipfile.ZipFile, base_name: str) -> Optional[dict[str, Any]]:
    """Pack the engine's persisted vector index into the pack.

    The local engine keeps the searchable vector index in ``index/default``
    (``index_meta.json`` + ``versions/*``). Restore writes records through the
    payload layer only, so a target instance rebuilt from a pack would answer
    scans while its search index lacks the restored rows until a full index
    rebuild. Shipping the source index file makes the pack self-contained for
    an offline target that rebuilds the same payload (same records, same
    deterministic ids, therefore the same index labels).
    """
    index_dir = _vectordb_index_dir()
    if index_dir is None:
        return None
    files = []
    total = 0
    for dirpath, _dirnames, filenames in os.walk(index_dir):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, index_dir).replace(os.sep, "/")
            size = os.path.getsize(full)
            files.append((full, rel, size))
            total += size
    if not files:
        return None
    manifest = {
        "files": [{"path": rel, "size": size} for _full, rel, size in files],
        "total_bytes": total,
    }
    zf.writestr(_leaf(base_name, _INDEX_LEAF), json.dumps(manifest).encode("utf-8"))
    for full, rel, _size in files:
        with open(full, "rb") as handle:
            zf.writestr(_leaf(base_name, f"{_PUBLICATION_DIR}/vectordb_index/{rel}"), handle.read())
    logger.info(
        "[ovpack] Shipped vector index: %d files, %.2f GB",
        len(files), total / (1024**3),
    )
    return manifest


def restore_vectordb_index(file_path: str, base_name: str) -> Optional[dict[str, Any]]:
    """Install a shipped vector index into the local engine directory.

    Intended for a freshly restored / offline target: call it before starting
    the server (or restart it afterwards) so the engine loads the index. The
    existing index is moved aside, never deleted.
    """
    index_dir = _vectordb_index_dir()
    if index_dir is None:
        return None
    with zipfile.ZipFile(file_path) as zf:
        manifest_leaf = _leaf(base_name, _INDEX_LEAF)
        if manifest_leaf not in zf.namelist():
            return None
        manifest = json.loads(zf.read(manifest_leaf).decode("utf-8"))
        backup_dir = index_dir + f".bak-{int(__import__('time').time())}"
        if os.path.isdir(index_dir):
            os.replace(index_dir, backup_dir)
        os.makedirs(index_dir, exist_ok=True)
        for item in manifest.get("files", []):
            rel = item["path"]
            leaf = _leaf(base_name, f"{_PUBLICATION_DIR}/vectordb_index/{rel}")
            target = os.path.join(index_dir, *rel.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as handle:
                handle.write(zf.read(leaf))
    logger.info(
        "[ovpack] Installed vector index (%s files); previous index at %s",
        len(manifest.get("files", [])), backup_dir,
    )
    return {"installed": True, "backup": backup_dir, "files": len(manifest.get("files", []))}
