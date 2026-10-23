# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""OVPack export/import and backup/restore operations."""

import asyncio
import json
import time
import os
import zipfile
from typing import Any, Optional

from openviking.core.context import ResourceContentType
from openviking.core.namespace import (
    content_owner_context_for_uri,
    context_type_for_uri,
    is_session_uri,
    relative_uri_path,
)
from openviking.resource.watch_storage import is_watch_task_control_uri
from openviking.server.identity import RequestContext
from openviking.storage.index_consistency import check_index_consistency
from openviking.storage.internal_names import CHAPTER_METADATA_FILENAMES
from openviking.storage.ovpack.format import (
    OVPACK_BACKUP_NAME,
    OVPACK_BACKUP_TYPE,
    OVPACK_FILES_DIR,
    OVPACK_INDEX_RECORDS_PATH,
    OVPACK_INTERNAL_DIR,
    OVPACK_MANIFEST_ZIP_LEAF,
    ensure_dir_exists,
    ensure_ovpack_extension,
    get_ovpack_zip_path,
    internal_zip_path,
    join_uri,
    jsonl_bytes,
    leaf_name,
    manifest_content_sha256,
    normalize_on_conflict,
    normalize_vector_mode,
    sha256_hex,
    strip_uri_trailing_slash,
)
from openviking.storage.ovpack.index import build_manifest, read_text_if_exists
from openviking.storage.ovpack.publication_bundle import (
    append_publication_bundle,
    has_publication_bundle,
    import_publication_bundle,
    published_generations,
    ship_vectordb_index,
)
from openviking.storage.ovpack.manifest import (
    manifest_entries_by_path,
    manifest_entry_target_uri,
    read_manifest,
    validate_manifest_root_matches_zip,
)
from openviking.storage.ovpack.policy import (
    PUBLIC_SCOPES,
    backup_scopes_from_manifest,
    is_backup_package,
    is_excluded_rel_path,
    manifest_root_uri,
    resolve_import_root_uri,
    validate_export_source_uri,
    validate_import_scope_compatibility,
    validate_import_target_uri,
    validate_ovpack_user_rel_path,
    validate_public_scope,
)
from openviking.storage.ovpack.validation import (
    base_name_from_entries,
    validate_manifest_content,
    validated_import_members,
)
from openviking.storage.ovpack.vectors import (
    build_dense_snapshot_manifest,
    choose_vector_restore_action,
    ensure_dense_snapshot_supported,
    read_dense_vectors,
    restore_vector_snapshot,
)
from openviking.utils.embedding_utils import (
    get_resource_content_type,
    vectorize_directory_meta,
    vectorize_file,
)
from openviking_cli.exceptions import (
    ConflictError,
    FailedPreconditionError,
    InvalidArgumentError,
    NotFoundError,
)
from openviking_cli.utils.logger import get_logger
from openviking_cli.utils.uri import VikingURI

logger = get_logger(__name__)

OPTIONAL_ABSTRACT_OVERVIEW_FILES = frozenset({".abstract.md", ".overview.md"})


def _index_records_by_level(
    index_records: list[dict[str, Any]], rel_path: str
) -> dict[int, dict[str, Any]]:
    by_level: dict[int, dict[str, Any]] = {}
    for record in index_records:
        if not isinstance(record, dict):
            continue
        if record.get("path") != rel_path:
            continue
        try:
            level = int(record.get("level", 2))
        except (TypeError, ValueError):
            continue
        by_level[level] = record
    return by_level


def _index_scalar_overrides(
    index_records: list[dict[str, Any]], rel_path: str
) -> dict[int, dict[str, Any]]:
    """Per-level scalars for the recompute path.

    One file carries a summary row plus one row per chunk at L2; the summary
    row is what re-vectorization rebuilds, so it wins over a chunk row.
    """
    best: dict[int, tuple[bool, dict[str, Any]]] = {}
    for record in index_records:
        if not isinstance(record, dict) or record.get("path") != rel_path:
            continue
        scalars = record.get("scalars")
        if not isinstance(scalars, dict) or not scalars:
            continue
        try:
            level = int(record.get("level", 2))
        except (TypeError, ValueError):
            continue
        is_chunk = bool(scalars.get("chunk_id"))
        current = best.get(level)
        if current is None or (current[0] and not is_chunk):
            best[level] = (is_chunk, dict(scalars))
    return {level: scalars for level, (_, scalars) in best.items()}


def _image_occurrence_records(
    index_records: list[dict[str, Any]], rel_path: str, root_uri: str
) -> Optional[list[dict[str, Any]]]:
    """Replay the source instance's occurrence rows for one image file.

    An image row is bound to the chapters citing it, so recomputing its
    context on the destination would need the exporting instance's citing
    tree and tokenizer budget, and could disagree with the very rows being
    reproduced. The pack already holds each occurrence exactly as the source
    wrote it — citing chapter, context window and hash, caption — so the
    import replays them in ordinal order, one row per logical occurrence.
    An occurrence whose chapter lives outside the imported root is dropped
    rather than rewritten: its context text embeds the exporting path.
    """
    rows: dict[str, dict[str, Any]] = {}
    for record in index_records:
        if not isinstance(record, dict) or record.get("path") != rel_path:
            continue
        scalars = record.get("scalars")
        if not isinstance(scalars, dict):
            continue
        chunk_id = str(scalars.get("chunk_id") or "")
        if not chunk_id.startswith("image:"):
            continue
        # One logical occurrence can appear once per live generation (the pack
        # emits them oldest first, newest last); restore collapses them onto
        # the canonical record id and keeps the newest. Replaying every row
        # instead would index a twice-written occurrence twice.
        rows[chunk_id] = scalars
    if not rows:
        return None
    ordered = sorted(rows.values(), key=lambda scalars: int(scalars.get("chunk_ordinal") or 0))
    occurrences: list[dict[str, Any]] = []
    for scalars in ordered:
        summary = str(scalars.get("chunk_text") or scalars.get("abstract") or "")
        if not summary.strip():
            continue
        context_uri = str(scalars.get("image_context_uri") or "")
        if context_uri and not (
            context_uri == root_uri or context_uri.startswith(root_uri + "/")
        ):
            logger.warning(f"[ovpack] Dropping out-of-root image context: {context_uri}")
            continue
        occurrences.append(
            {
                "context_uri": context_uri,
                "context_hash": str(scalars.get("image_context_hash") or ""),
                "context": str(scalars.get("parent_text") or ""),
                "ordinal": int(scalars.get("chunk_ordinal") or 0),
                "summary": summary,
            }
        )
    return occurrences or None


async def _root_exists(viking_fs, root_uri: str, ctx: RequestContext) -> bool:
    try:
        await viking_fs.ls(root_uri, ctx=ctx)
        return True
    except NotFoundError:
        return False
    except FileNotFoundError:
        return False


async def _clear_scope_root_children(viking_fs, root_uri: str, ctx: RequestContext) -> None:
    """Empty a scope root (e.g. ``viking://resources``) for a top-level import.

    A tenant-scoped identity cannot delete the namespace root itself
    ("Deleting a namespace root requires root access"), but every child of the
    root is a concrete content path that the same identity may delete. This is
    the overwrite path for imports that target the root URI: remove each child,
    never the root.
    """
    try:
        children = await viking_fs.ls(root_uri, output="original", ctx=ctx)
    except Exception as exc:  # noqa: BLE001 - listing failures surface on write anyway
        logger.warning(f"[ovpack] Cannot list {root_uri} children for overwrite: {exc}")
        return
    child_uris = []
    for child in children or []:
        if isinstance(child, dict) and child.get("uri"):
            child_uris.append(str(child["uri"]))
        elif isinstance(child, str):
            child_uris.append(child)
    for child_uri in child_uris:
        try:
            await viking_fs.rm(child_uri, recursive=True, ctx=ctx)
            logger.info(f"[ovpack] Overwrite cleared existing child {child_uri}")
        except (NotFoundError, FileNotFoundError):
            continue
        except FailedPreconditionError as exc:
            # Generated directory catalogues ({dir}/index.md) refuse deletion and
            # are regenerated from the directory's children; a pack member with
            # the same rel path overwrites their content below, so skipping is safe.
            if "generated directory catalogue" in str(exc):
                logger.info(
                    f"[ovpack] Overwrite kept regenerated catalogue {child_uri} "
                    "(content rewritten by the pack member)"
                )
                continue
            raise ConflictError(
                f"Cannot overwrite existing child at {child_uri}. "
                "Clear it manually or use on_conflict='skip'/'fail'.",
                resource=child_uri,
            ) from exc
        except Exception as exc:  # noqa: BLE001 - keep clearing siblings, fail on write if fatal
            raise ConflictError(
                f"Cannot overwrite existing child at {child_uri}. "
                "Clear it manually or use on_conflict='skip'/'fail'.",
                resource=child_uri,
            ) from exc


async def _ensure_parent_exists(viking_fs, parent: str, ctx: RequestContext) -> None:
    try:
        await viking_fs.stat(parent, ctx=ctx, skip_count=True)
    except Exception:
        await viking_fs.mkdir(parent, ctx=ctx)


async def _remove_existing_root(viking_fs, root_uri: str, ctx: RequestContext) -> None:
    if not hasattr(viking_fs, "rm"):
        logger.warning(f"[ovpack] Cannot remove existing resource without rm(): {root_uri}")
        return
    try:
        await viking_fs.rm(root_uri, recursive=True, ctx=ctx)
    except NotFoundError:
        return
    except FileNotFoundError:
        return


def _exportable_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    exportable: list[dict[str, Any]] = []
    for entry in entries:
        rel_path = str(entry.get("rel_path") or "")
        validate_ovpack_user_rel_path(rel_path, operation="export")
        if not is_excluded_rel_path(rel_path):
            exportable.append(entry)
    return exportable


def _is_optional_abstract_overview(entry: dict[str, Any]) -> bool:
    if entry.get("isDir"):
        return False
    rel_path = str(entry.get("rel_path") or "")
    return leaf_name(rel_path) in OPTIONAL_ABSTRACT_OVERVIEW_FILES


async def _filter_existing_optional_sidecars(
    viking_fs,
    root_uri: str,
    entries: list[dict[str, Any]],
    ctx: RequestContext,
) -> list[dict[str, Any]]:
    # The entries come from a single directory walk of the export root, so an
    # optional sidecar entry is present exactly when the walk saw it. Probing
    # each of the ~400k sidecars with its own ``exists`` call made a full backup
    # crawl for hours; the set lookup is the same information without the IO.
    walked = {str(entry.get("rel_path") or "") for entry in entries}
    filtered: list[dict[str, Any]] = []
    dropped = 0
    for entry in entries:
        if not _is_optional_abstract_overview(entry):
            filtered.append(entry)
            continue
        rel_path = str(entry.get("rel_path") or "")
        if rel_path in walked:
            filtered.append(entry)
        else:
            dropped += 1
    if dropped:
        logger.info(f"[ovpack] skipped {dropped} optional sidecars missing from the walk")
    logger.warning(f"[ovpack] sidecar filter: {len(entries)} entries -> {len(filtered)}")
    return filtered


async def _enqueue_direct_vectorization(
    viking_fs,
    uri: str,
    ctx: RequestContext,
    index_records: Optional[list[dict[str, Any]]] = None,
    manifest_path_root_uri: Optional[str] = None,
    entries: Optional[list[dict[str, Any]]] = None,
) -> None:
    if is_session_uri(uri):
        logger.info(f"[ovpack] Skipped vectorization for session namespace: {uri}")
        return

    index_records = index_records or []
    manifest_path_root_uri = manifest_path_root_uri or uri
    if entries is None:
        entries = await viking_fs.tree(uri, node_limit=None, level_limit=None, ctx=ctx)
    dir_uris = {uri}
    file_entries: list[tuple[str, str, str, str]] = []
    for entry in entries:
        entry_uri = entry.get("uri")
        if not entry_uri:
            continue
        if is_session_uri(entry_uri):
            continue
        rel_path = entry.get("rel_path") or relative_uri_path(uri, entry_uri)
        manifest_rel_path = relative_uri_path(manifest_path_root_uri, entry_uri)
        if entry.get("isDir"):
            dir_uris.add(entry_uri)
            continue
        name = entry.get("name", "") or leaf_name(rel_path)
        # Mirror the write path's skip set (resource_processor._vectorize_resource_files)
        # and the export gate's (index_consistency._file_candidates): dotfiles and
        # chapter metadata sidecars are never vectorized, and neither is a generated
        # navigation catalogue (its entry list would outrank the entries it points at).
        # A migrated tree must agree or it gains rows the source instance never had.
        if name.startswith(".") or name in CHAPTER_METADATA_FILENAMES:
            continue
        from openviking.core.directory_index import is_generated_index_at

        if await is_generated_index_at(viking_fs, entry_uri, ctx=ctx):
            continue
        parent = VikingURI(entry_uri).parent
        if parent:
            file_entries.append((entry_uri, parent.uri, name, manifest_rel_path))

    async def index_dir(dir_uri: str) -> None:
        rel_path = relative_uri_path(manifest_path_root_uri, dir_uri)
        records_by_level = _index_records_by_level(index_records, rel_path)
        scalar_overrides = _index_scalar_overrides(index_records, rel_path)
        abstract = str(records_by_level.get(0, {}).get("text") or "")
        overview = str(records_by_level.get(1, {}).get("text") or "")

        if not abstract:
            abstract = await read_text_if_exists(viking_fs, f"{dir_uri}/.abstract.md", ctx)
        if not overview:
            overview = await read_text_if_exists(viking_fs, f"{dir_uri}/.overview.md", ctx)

        if not abstract and not overview and not scalar_overrides:
            return
        await vectorize_directory_meta(
            dir_uri,
            abstract,
            overview,
            context_type=context_type_for_uri(dir_uri),
            ctx=ctx,
            include_overview=bool(overview),
            scalar_overrides=scalar_overrides,
        )

    chapter_texts: dict[str, str] = {}

    async def chapter_source_meta(chapter_uri: str) -> dict[str, Any]:
        """Provenance block of the citing chapter, read from the imported tree."""
        if not chapter_uri:
            return {}
        text = chapter_texts.get(chapter_uri)
        if text is None:
            from openviking.indexing.content import draft_sources

            try:
                with draft_sources():
                    raw = await viking_fs.read_file(chapter_uri, ctx=ctx)
            except Exception as exc:
                logger.warning(f"[ovpack] Unreadable image source chapter {chapter_uri}: {exc}")
                raw = None
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
            chapter_texts[chapter_uri] = text
        if not text:
            return {}
        from openviking.indexing.record_meta import extract_inline_metadata

        return extract_inline_metadata(text)

    async def index_file(file_uri: str, parent_uri: str, name: str, rel_path: str) -> None:
        overrides = _index_scalar_overrides(index_records, rel_path)
        scalar_override = overrides.get(2) or next(iter(overrides.values()), {})
        summary = str(scalar_override.get("abstract") or "")
        summary_dict: dict[str, Any] = {"name": name, "summary": summary}
        if get_resource_content_type(name) == ResourceContentType.IMAGE:
            occurrences = _image_occurrence_records(
                index_records, rel_path, manifest_path_root_uri
            )
            if occurrences:
                replayed: list[dict[str, Any]] = []
                for occurrence in occurrences:
                    occurrence = dict(occurrence)
                    source_meta = await chapter_source_meta(occurrence["context_uri"])
                    if source_meta:
                        occurrence["source_meta"] = source_meta
                    replayed.append(occurrence)
                summary_dict["image_occurrences"] = replayed
                summary_dict["summary"] = "\n\n".join(
                    occurrence["summary"] for occurrence in replayed
                )
        await vectorize_file(
            file_path=file_uri,
            summary_dict=summary_dict,
            parent_uri=parent_uri,
            context_type=context_type_for_uri(file_uri),
            ctx=ctx,
            scalar_override=scalar_override,
        )

    await asyncio.gather(*(index_dir(dir_uri) for dir_uri in dir_uris))
    await asyncio.gather(
        *(
            index_file(file_uri, parent_uri, file_name, rel_path)
            for file_uri, parent_uri, file_name, rel_path in file_entries
        )
    )


async def _restore_snapshot_with_ledger(
    *,
    viking_fs,
    vector_store,
    file_path: str,
    base_name: str,
    root_uri: str,
    index_records: list[dict[str, Any]],
    dense_vectors: dict[str, list[float]],
    manifest_entries: dict[str, dict[str, Any]],
    entries: list[dict[str, Any]],
    ctx: RequestContext,
) -> None:
    """Install the pack's publication ledger, then replay its dense snapshot.

    The ledger must go first: restored rows keep ``index_generation`` and
    ``index_plan_id`` (the managed-write window needs the destination ledger to
    know that generation, otherwise it fails with "Unknown index generation").
    After the shipped head is installed, rows whose generation the head no
    longer references are superseded — a pack exported from a source that was
    reindexed more than once can carry them — and are skipped rather than
    aborting the migration.
    """
    ledger_present = has_publication_bundle(file_path, base_name)
    if ledger_present:
        await import_publication_bundle(
            viking_fs, file_path, base_name, root_uri, ctx, entries
        )
    allowed = published_generations(ctx.account_id) if ledger_present else None
    restored, skipped = await restore_vector_snapshot(
        vector_store,
        root_uri,
        index_records,
        dense_vectors,
        manifest_entries,
        ctx,
        keep_generation=ledger_present,
        allowed_generations=allowed,
    )
    logger.info(
        "[ovpack] Restored %s vector record(s) for %s%s",
        restored,
        root_uri,
        f" (skipped {skipped} superseded)" if skipped else "",
    )


async def import_ovpack(
    viking_fs,
    file_path: str,
    parent: str,
    ctx: RequestContext,
    on_conflict: Optional[str] = None,
    vector_mode: Optional[str] = None,
    vector_store=None,
) -> str:
    """
    Import .ovpack file to the specified parent path.

    Args:
        viking_fs: VikingFS instance
        file_path: Local .ovpack file path
        parent: Target parent URI (e.g., viking://resources/...)
        on_conflict: One of "fail", "overwrite", or "skip"
        vector_mode: One of "auto", "recompute", or "require"

    Returns:
        Root resource URI after import
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    parent = strip_uri_trailing_slash(parent)
    validate_public_scope(parent, operation="import", allow_root=True)
    conflict_action = normalize_on_conflict(on_conflict)
    vector_action_mode = normalize_vector_mode(vector_mode)
    index_records: list[dict[str, Any]] = []
    dense_vectors: dict[str, list[float]] = {}
    imported_entries: list[dict[str, Any]] = []
    vector_action = "recompute"

    with zipfile.ZipFile(file_path, "r") as zf:
        infolist = zf.infolist()
        if not infolist:
            raise ValueError("Empty ovpack file")

        base_name = base_name_from_entries(infolist)
        manifest = read_manifest(zf, base_name)
        validate_manifest_root_matches_zip(manifest, base_name)
        root_uri = resolve_import_root_uri(parent, base_name, manifest)
        validate_import_scope_compatibility(manifest, root_uri)
        validate_import_target_uri(root_uri)

        members = validated_import_members(infolist, base_name, root_uri)

        index_records = validate_manifest_content(zf, manifest, infolist, base_name)
        dense_vectors = read_dense_vectors(zf, manifest, base_name, index_records)

        existing_roots = [root_uri] if await _root_exists(viking_fs, root_uri, ctx) else []
        if existing_roots:
            if conflict_action == "skip":
                logger.info(f"[ovpack] Skipped existing resource at {root_uri}")
                return root_uri
            if conflict_action == "fail":
                resource = existing_roots[0]
                raise ConflictError(
                    f"Resource already exists at {resource}. "
                    "Use on_conflict='overwrite' to replace it.",
                    resource=resource,
                )

        if not is_session_uri(root_uri):
            vector_action = choose_vector_restore_action(
                manifest,
                index_records,
                dense_vectors,
                vector_store=vector_store,
                vector_mode=vector_action_mode,
            )

        if parent != "viking://":
            await _ensure_parent_exists(viking_fs, parent, ctx)

        for existing_root in existing_roots:
            logger.info(f"[ovpack] Overwriting existing resource at {existing_root}")
            if parent == "viking://" and manifest_root_uri(manifest) == existing_root:
                # The target is a namespace root (e.g. viking://resources): the
                # calling tenant usually cannot delete the root itself, so clear
                # its children instead and merge the pack on top of the root.
                await _clear_scope_root_children(viking_fs, existing_root, ctx)
            else:
                await _remove_existing_root(viking_fs, existing_root, ctx)

        for _, safe_zip_path, kind, rel_path in members:
            if kind in {"manifest", "internal"}:
                continue
            target_file_uri = join_uri(root_uri, rel_path)
            imported_entries.append(
                {
                    "rel_path": rel_path,
                    "uri": target_file_uri,
                    "isDir": kind == "directory",
                }
            )
            if kind == "directory":
                await viking_fs.mkdir(target_file_uri, exist_ok=True, ctx=ctx)
                continue

            data = zf.read(safe_zip_path)
            await viking_fs.write_file_bytes(target_file_uri, data, ctx=ctx)

    logger.info(f"[ovpack] Successfully imported {file_path} to {root_uri}")

    if not is_session_uri(root_uri):
        if vector_action == "restore":
            await _restore_snapshot_with_ledger(
                viking_fs=viking_fs,
                vector_store=vector_store,
                file_path=file_path,
                base_name=base_name,
                root_uri=root_uri,
                index_records=index_records,
                dense_vectors=dense_vectors,
                manifest_entries=manifest_entries_by_path(manifest),
                entries=imported_entries,
                ctx=ctx,
            )
        else:
            await _enqueue_direct_vectorization(
                viking_fs,
                root_uri,
                ctx=ctx,
                index_records=index_records,
            )
            logger.info(f"[ovpack] Enqueued direct vectorization for: {root_uri}")
    else:
        logger.info(f"[ovpack] Skipped vectorization for session namespace: {root_uri}")

    return root_uri


def _physical_scope_root(viking_fs, ctx: RequestContext) -> Optional[str]:
    """Return the on-disk AGFS root for the calling account, when it is local.

    The AGFS virtual path (/local/{account}/...) is materialized under
    ``{agfs.path}/viking/{account}`` for the local backend. A backup already
    runs with account-wide authority, so walking that tree directly is both
    safe and orders of magnitude cheaper than the ACL-filtered, per-node
    ``viking_fs.tree`` traversal (1s vs >20min for a 390k-node tenant).
    """
    account_id = getattr(ctx, "account_id", None)
    if not account_id:
        return None
    bases: list[str] = []
    try:
        from openviking_cli.utils.config import get_openviking_config

        storage = get_openviking_config().storage
        for value in (
            getattr(storage.agfs, "path", None),
            getattr(storage, "workspace", None),
        ):
            if value:
                bases.append(str(value))
    except Exception:  # noqa: BLE001 - still try the working directory
        pass
    cwd = os.getcwd()
    bases.extend([cwd, os.path.join(cwd, "data")])
    seen: set[str] = set()
    for base in bases:
        if base in seen:
            continue
        seen.add(base)
        for layout in ("viking",):
            root = os.path.join(base, layout, str(account_id))
            if os.path.isdir(os.path.join(root, "resources")):
                return root
    return None


def _physical_backup_entries(root: str) -> list[dict[str, Any]]:
    """Build backup entries from the local AGFS tree in one directory walk."""
    entries: list[dict[str, Any]] = []
    for scope in PUBLIC_SCOPES:
        scope_dir = os.path.join(root, *scope.split("/"))
        if not os.path.isdir(scope_dir):
            continue
        entries.append(
            {"rel_path": scope, "uri": f"viking://{scope}", "isDir": True, "size": 0}
        )
        for dirpath, dirnames, filenames in os.walk(scope_dir):
            dirnames.sort()
            filenames.sort()
            rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
            for name in dirnames:
                rel_path = f"{rel_dir}/{name}"
                entries.append(
                    {"rel_path": rel_path, "uri": f"viking://{rel_path}", "isDir": True, "size": 0}
                )
            for name in filenames:
                rel_path = f"{rel_dir}/{name}"
                full = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                entries.append(
                    {"rel_path": rel_path, "uri": f"viking://{rel_path}", "isDir": False, "size": size}
                )
    return entries


async def _backup_entries(viking_fs, ctx: RequestContext) -> list[dict[str, Any]]:
    started = time.monotonic()
    physical_root = _physical_scope_root(viking_fs, ctx)
    logger.warning("[ovpack] physical root probe: %s", physical_root)
    if physical_root is not None:
        entries = _physical_backup_entries(physical_root)
        logger.warning(
            "[ovpack] walked %s directly: %d entries in %.1fs",
            physical_root, len(entries), time.monotonic() - started,
        )
        return entries

    entries: list[dict[str, Any]] = []
    for scope in PUBLIC_SCOPES:
        scope_uri = f"viking://{scope}"
        scope_started = time.monotonic()
        entries.append(
            {
                "rel_path": scope,
                "uri": scope_uri,
                "isDir": True,
                "size": 0,
            }
        )
        try:
            top_entries = await viking_fs.tree(
                scope_uri,
                show_all_hidden=True,
                node_limit=None,
                level_limit=1,
                ctx=ctx,
            )
        except (NotFoundError, FileNotFoundError):
            continue

        # Walk each top-level child concurrently. A single unlimited tree of a
        # 390k-node scope serializes every AGFS readdir behind one await chain
        # (minutes with no output); fanning out the subtrees keeps several
        # readdirs in flight and lets the export report progress per subtree.
        top_children = [
            entry
            for entry in top_entries
            if entry.get("rel_path") and entry.get("isDir")
        ]
        top_files = [
            entry
            for entry in top_entries
            if entry.get("rel_path") and not entry.get("isDir")
        ]
        scope_entries = list(top_files)
        walk_sem = asyncio.Semaphore(8)

        async def _walk_child(child_entry: dict[str, Any]) -> None:
            child_uri = join_uri(scope_uri, child_entry["rel_path"])
            async with walk_sem:
                try:
                    child_entries = await viking_fs.tree(
                        child_uri,
                        show_all_hidden=True,
                        node_limit=None,
                        level_limit=None,
                        ctx=ctx,
                    )
                except (NotFoundError, FileNotFoundError):
                    return
            for entry in child_entries:
                rel = str(entry.get("rel_path") or "")
                if not rel:
                    continue
                merged = dict(entry)
                merged["rel_path"] = f"{child_entry['rel_path']}/{rel}"
                scope_entries.append(merged)

        await asyncio.gather(*[_walk_child(child) for child in top_children])
        logger.warning(
            "[ovpack] walked %s: %d entries (%d subtrees) in %.1fs",
            scope_uri,
            len(scope_entries),
            len(top_children),
            time.monotonic() - scope_started,
        )
        for entry in _exportable_entries(scope_entries):
            rel_path = entry.get("rel_path", "")
            if not rel_path:
                continue
            scoped_entry = dict(entry)
            scoped_entry["rel_path"] = f"{scope}/{rel_path}"
            scoped_entry["uri"] = join_uri(scope_uri, rel_path)
            if is_watch_task_control_uri(scoped_entry["uri"]):
                continue
            entries.append(scoped_entry)
    return entries


# Raster content that zlib cannot shrink: stored uncompressed to save the
# deflate CPU (the pack checksums cover content, not compression method).
_ZIP_STORED_IMAGE_EXTS = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
)
# Text members still shrink ~3x at level 1 with a fraction of the level-6 CPU.
_ZIP_TEXT_COMPRESS_LEVEL = 1
# Files whose bytes are prefetched while earlier members deflate; the reads go
# through the full VikingFS path (ACL + AGFS + content projection) and overlap
# with compression instead of serialising read-then-write per file.
_ZIP_READ_PREFETCH = 16


def _zip_member_info(zip_path: str, *, store: bool) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=zip_path, date_time=(1980, 1, 1, 0, 0, 0))
    info.external_attr = 0o644 << 16
    if store:
        info.compress_type = zipfile.ZIP_STORED
    else:
        info.compress_type = zipfile.ZIP_DEFLATED
    return info


def _zip_write_member(
    zf: zipfile.ZipFile, zip_path: str, data: bytes, *, store: bool
) -> None:
    info = _zip_member_info(zip_path, store=store)
    if store:
        zf.writestr(info, data)
    else:
        zf.writestr(info, data, compresslevel=_ZIP_TEXT_COMPRESS_LEVEL)


async def _write_ovpack_archive(
    viking_fs,
    root_uri: str,
    to: str,
    base_name: str,
    entries: list[dict[str, Any]],
    manifest: dict[str, Any],
    index_records: list[dict[str, Any]],
    dense_values: list[float],
    ctx: RequestContext,
) -> str:
    ensure_dir_exists(to)
    manifest_entries = manifest_entries_by_path(manifest)
    manifest_file_entries = {
        rel_path: entry
        for rel_path, entry in manifest_entries.items()
        if entry.get("kind") == "file"
    }

    with zipfile.ZipFile(to, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        zf.writestr(base_name + "/", "")
        zf.writestr(f"{base_name}/{OVPACK_FILES_DIR}/", "")

        content_entries = [entry for entry in entries if entry["rel_path"] != ""]
        file_positions = [
            position for position, entry in enumerate(content_entries) if not entry.get("isDir")
        ]
        pending_reads: dict[int, "asyncio.Task[bytes]"] = {}
        next_prefetch_position = 0

        def _fill_read_window() -> None:
            nonlocal next_prefetch_position
            while (
                len(pending_reads) < _ZIP_READ_PREFETCH
                and next_prefetch_position < len(file_positions)
            ):
                member_position = file_positions[next_prefetch_position]
                entry = content_entries[member_position]
                full_uri = entry.get("uri") or join_uri(root_uri, entry["rel_path"])
                pending_reads[member_position] = asyncio.ensure_future(
                    viking_fs.read_file_bytes(full_uri, ctx=ctx)
                )
                next_prefetch_position += 1

        for position, entry in enumerate(content_entries):
            rel_path = entry["rel_path"]
            zip_path = get_ovpack_zip_path(base_name, rel_path)

            if entry.get("isDir"):
                zf.writestr(zip_path.rstrip("/") + "/", "")
                continue

            _fill_read_window()
            try:
                data = await pending_reads.pop(position)
            except Exception as exc:
                full_uri = entry.get("uri") or join_uri(root_uri, rel_path)
                logger.warning(f"Failed to export file {full_uri}: {exc}")
                raise

            manifest_entry = manifest_file_entries.get(rel_path)
            if manifest_entry is not None:
                manifest_entry["size"] = len(data)
                manifest_entry["sha256"] = sha256_hex(data)
            _zip_write_member(
                zf,
                zip_path,
                data,
                store=rel_path.lower().endswith(tuple(_ZIP_STORED_IMAGE_EXTS)),
            )

        manifest["content_sha256"] = manifest_content_sha256(manifest_file_entries)
        index_bytes = jsonl_bytes(index_records)
        manifest["index"] = {
            "records": {
                "path": OVPACK_INDEX_RECORDS_PATH,
                "count": len(index_records),
                "sha256": sha256_hex(index_bytes),
            }
        }

        zf.writestr(f"{base_name}/{OVPACK_INTERNAL_DIR}/", "")
        _zip_write_member(
            zf, internal_zip_path(base_name, OVPACK_INDEX_RECORDS_PATH), index_bytes, store=False
        )

        dense_snapshot = build_dense_snapshot_manifest(index_records, dense_values)
        if dense_snapshot is not None:
            dense_bytes, dense_manifest = dense_snapshot
            manifest["index"]["dense"] = dense_manifest
            _zip_write_member(
                zf, internal_zip_path(base_name, dense_manifest["path"]), dense_bytes, store=True
            )

        _zip_write_member(
            zf,
            f"{base_name}/{OVPACK_MANIFEST_ZIP_LEAF}",
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"),
            store=False,
        )
    return to


async def export_ovpack(
    viking_fs,
    uri: str,
    to: str,
    ctx: RequestContext,
    vector_store=None,
    include_vectors: bool = False,
    include_index: bool = False,
) -> str:
    """
    Export the specified context path as a .ovpack file.

    Args:
        viking_fs: VikingFS instance
        uri: Viking URI
        to: Target file path (can be an existing directory or a path ending with .ovpack)
        vector_store: Optional vector store used to export portable index metadata
        include_vectors: Whether to include pure-dense vector snapshots

    Returns:
        Exported file path

    """
    uri = strip_uri_trailing_slash(uri)
    validate_export_source_uri(uri)

    base_name = leaf_name(uri) or "export"

    if os.path.isdir(to):
        to = os.path.join(to, f"{base_name}.ovpack")
    else:
        to = ensure_ovpack_extension(to)

    entries = _exportable_entries(
        await viking_fs.tree(
            uri,
            show_all_hidden=True,
            node_limit=None,
            level_limit=None,
            ctx=ctx,
        )
    )
    entries = await _filter_existing_optional_sidecars(viking_fs, uri, entries, ctx)
    if include_vectors:
        ensure_dense_snapshot_supported(vector_store)
        report = await check_index_consistency(
            viking_fs,
            vector_store,
            uri,
            entries,
            ctx,
        )
        if not report.ok:
            raise InvalidArgumentError(
                "Cannot export incomplete OpenViking vector index snapshot",
                details=report.details(),
            )
    current_generations = published_generations(ctx.account_id) if include_vectors else None
    manifest, index_records, dense_values = await build_manifest(
        viking_fs,
        vector_store,
        uri,
        base_name,
        entries,
        ctx,
        include_vectors=include_vectors,
        current_generations=current_generations,
    )
    await _write_ovpack_archive(
        viking_fs,
        uri,
        to,
        base_name,
        entries,
        manifest,
        index_records,
        dense_values,
        ctx,
    )

    if include_vectors:
        await append_publication_bundle(to, base_name, uri, entries, ctx)
    if include_index:
        # The engine keeps one global index per collection; shipping it with a
        # subtree pack would carry rows the pack does not contain. Only a full
        # backup is self-contained enough to pair with the index.
        logger.info("[ovpack] include_index ignored for subtree exports; use ov backup")

    logger.info(f"[ovpack] Exported {uri} to {to}")
    return to


async def backup_ovpack(
    viking_fs,
    to: str,
    ctx: RequestContext,
    vector_store=None,
    include_vectors: bool = False,
    include_index: bool = False,
) -> str:
    """Export all public OpenViking scopes as a restore-only backup package."""
    logger.warning("[ovpack] backup start: to=%s include_vectors=%s", to, include_vectors)
    base_name = OVPACK_BACKUP_NAME
    if os.path.isdir(to):
        to = os.path.join(to, f"{base_name}.ovpack")
    else:
        to = ensure_ovpack_extension(to)

    entries = await _backup_entries(viking_fs, ctx)
    entries = await _filter_existing_optional_sidecars(viking_fs, "viking://", entries, ctx)
    if include_vectors:
        ensure_dense_snapshot_supported(vector_store)
        report = await check_index_consistency(
            viking_fs,
            vector_store,
            "viking://",
            entries,
            ctx,
        )
        if not report.ok:
            raise InvalidArgumentError(
                "Cannot export incomplete OpenViking vector index snapshot",
                details=report.details(),
            )
    current_generations = published_generations(ctx.account_id) if include_vectors else None
    manifest, index_records, dense_values = await build_manifest(
        viking_fs,
        vector_store,
        "viking://",
        base_name,
        entries,
        ctx,
        package_type=OVPACK_BACKUP_TYPE,
        scopes=list(PUBLIC_SCOPES),
        include_vectors=include_vectors,
        current_generations=current_generations,
    )
    await _write_ovpack_archive(
        viking_fs,
        "viking://",
        to,
        base_name,
        entries,
        manifest,
        index_records,
        dense_values,
        ctx,
    )

    if include_vectors:
        await append_publication_bundle(to, base_name, "viking://", entries, ctx)
    if include_index:
        with zipfile.ZipFile(to, "a", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            ship_vectordb_index(zf, base_name)

    logger.info(f"[ovpack] Backed up OpenViking public scopes to {to}")
    return to


async def restore_ovpack(
    viking_fs,
    file_path: str,
    ctx: RequestContext,
    on_conflict: Optional[str] = None,
    vector_mode: Optional[str] = None,
    vector_store=None,
) -> str:
    """Restore a backup package to its original public scope roots."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    conflict_action = normalize_on_conflict(on_conflict)
    vector_action_mode = normalize_vector_mode(vector_mode)
    root_uri = "viking://"
    index_records: list[dict[str, Any]] = []
    dense_vectors: dict[str, list[float]] = {}
    vector_action = "recompute"
    manifest_entries: dict[str, dict[str, Any]] = {}
    restored_entries: list[dict[str, Any]] = []

    with zipfile.ZipFile(file_path, "r") as zf:
        infolist = zf.infolist()
        if not infolist:
            raise ValueError("Empty ovpack file")

        base_name = base_name_from_entries(infolist)
        manifest = read_manifest(zf, base_name)
        validate_manifest_root_matches_zip(manifest, base_name)
        if not is_backup_package(manifest):
            raise InvalidArgumentError(
                "Only backup ovpack packages can be restored with ov restore or the restore API",
                details={"root": base_name},
            )

        manifest_entries = manifest_entries_by_path(manifest)
        backup_scopes = backup_scopes_from_manifest(manifest, manifest_entries)
        members = validated_import_members(infolist, base_name, root_uri)

        index_records = validate_manifest_content(zf, manifest, infolist, base_name)
        dense_vectors = read_dense_vectors(zf, manifest, base_name, index_records)

        existing_roots = []
        for scope in backup_scopes:
            scope_uri = f"viking://{scope}"
            if await _root_exists(viking_fs, scope_uri, ctx):
                existing_roots.append(scope_uri)
        if existing_roots:
            if conflict_action == "skip":
                logger.info("[ovpack] Skipped backup restore because target scopes exist")
                return root_uri
            if conflict_action == "fail":
                resource = existing_roots[0]
                raise ConflictError(
                    f"Resource already exists at {resource}. "
                    "Use on_conflict='overwrite' to replace it.",
                    resource=resource,
                )

        vector_action = choose_vector_restore_action(
            manifest,
            index_records,
            dense_vectors,
            vector_store=vector_store,
            vector_mode=vector_action_mode,
        )

        content_members = [
            member for member in members if member[2] not in {"manifest", "internal"} and member[3]
        ]
        content_members.sort(key=lambda member: (member[2] != "directory", member[3].count("/")))

        for _, safe_zip_path, kind, rel_path in content_members:
            manifest_entry = manifest_entries[rel_path]
            target_uri = manifest_entry_target_uri(root_uri, rel_path, manifest_entry)
            if target_uri == "viking://user":
                continue

            try:
                target_stat = await viking_fs.stat(target_uri, ctx=ctx, skip_count=True)
            except (NotFoundError, FileNotFoundError):
                target_stat = None

            target_is_dir = bool(target_stat and target_stat.get("isDir"))
            if target_stat is not None and target_is_dir != (kind == "directory"):
                await _remove_existing_root(viking_fs, target_uri, ctx)

            if kind == "directory":
                await viking_fs.mkdir(target_uri, exist_ok=True, ctx=ctx)
            else:
                await viking_fs.write_file_bytes(target_uri, zf.read(safe_zip_path), ctx=ctx)

            restored_entries.append(
                {
                    "rel_path": rel_path,
                    "uri": target_uri,
                    "name": leaf_name(rel_path),
                    "isDir": kind == "directory",
                }
            )

    logger.info(f"[ovpack] Successfully restored backup {file_path}")

    if vector_action == "restore":
        await _restore_snapshot_with_ledger(
            viking_fs=viking_fs,
            vector_store=vector_store,
            file_path=file_path,
            base_name=base_name,
            root_uri=root_uri,
            index_records=index_records,
            dense_vectors=dense_vectors,
            manifest_entries=manifest_entries,
            entries=restored_entries,
            ctx=ctx,
        )
        return root_uri

    vectorization_groups: dict[str, list[dict[str, Any]]] = {}
    for entry in restored_entries:
        target_uri = entry["uri"]
        parts = target_uri.removeprefix("viking://").split("/")
        if parts[0] == "resources":
            group_uri = "viking://resources"
        elif parts[0] == "user" and len(parts) >= 2:
            group_uri = f"viking://user/{parts[1]}"
        else:
            continue
        vectorization_groups.setdefault(group_uri, []).append(entry)

    for group_uri, entries in vectorization_groups.items():
        owner_ctx = content_owner_context_for_uri(group_uri, ctx)
        await _enqueue_direct_vectorization(
            viking_fs,
            group_uri,
            ctx=owner_ctx,
            index_records=index_records,
            manifest_path_root_uri=root_uri,
            entries=entries,
        )
        logger.info(f"[ovpack] Enqueued direct vectorization for: {group_uri}")

    return root_uri
