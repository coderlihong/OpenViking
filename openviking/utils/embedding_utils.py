# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
Embedding utilities for OpenViking.

Common logic for creating Context objects and enqueuing them to EmbeddingQueue.
"""

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from charset_normalizer import from_bytes

from openviking.core.context import Context, ContextLevel, ResourceContentType, Vectorize
from openviking.core.namespace import (
    context_type_for_uri,
    is_session_uri,
    owner_space_for_uri,
)
from openviking.indexing.batch import text_publication_batch
from openviking.indexing.content import draft_source_job
from openviking.parse.parsers.media.utils import (
    MPEG_TS_PROBE_BYTES,
    is_mpeg_ts,
)
from openviking.parse.parsers.upload_utils import is_text_file
from openviking.server.identity import RequestContext
from openviking.service.task_work_index import TaskWorkRejected
from openviking.storage.abstract_overview import body_for_preview, embedding_text_for_body
from openviking.storage.acl import CreatorAclGrant
from openviking.storage.queuefs import get_queue_manager
from openviking.storage.queuefs.embedding_msg_converter import EmbeddingMsgConverter
from openviking.storage.viking_fs import LS_ALL_NODES, get_viking_fs
from openviking.telemetry.request_wait_tracker import get_request_wait_tracker
from openviking.utils.embedding_input import truncate_embedding_input
from openviking.utils.image_search import image_bytes_to_model_data_uri
from openviking.utils.ingest_options import IngestOptions
from openviking.utils.time_utils import parse_iso_datetime
from openviking_cli.utils import VikingURI, get_logger
from openviking_cli.utils.config import get_openviking_config
from openviking_cli.utils.config.embedding_config import (
    SUMMARY_TEXT_SOURCES,
    TEXT_SOURCE_SUMMARY_ONLY,
)

logger = get_logger(__name__)

# The `abstract` scalar is persisted as a vector-store bytes_row string field,
# which is length-prefixed with a uint16 (STRING_MAX_UINT16_LENGTH = 65535). An
# oversized abstract raises "string field 'abstract' exceeds 65535 bytes" and
# fails embedding enqueue, so the resource is silently never vectorized (and thus
# not retrievable). Cap it with headroom, mirroring
# memory_updater._truncate_memory_abstract introduced for the memory path (#2774).
_ABSTRACT_MAX_BYTES = 50_000


def _truncate_abstract_bytes(abstract: str) -> str:
    """Cap an abstract scalar below the vector-store bytes_row byte limit."""
    encoded = (abstract or "").encode("utf-8")
    if len(encoded) <= _ABSTRACT_MAX_BYTES:
        return abstract or ""
    return encoded[:_ABSTRACT_MAX_BYTES].decode("utf-8", errors="ignore")


_PORTABLE_SCALAR_FIELDS = frozenset(
    {
        "type",
        "level",
        "name",
        "description",
        "tags",
        "abstract",
    }
)


def _apply_scalar_overrides(embedding_msg, overrides: Optional[Dict[str, Any]]) -> None:
    if not embedding_msg or not overrides:
        return
    for field in _PORTABLE_SCALAR_FIELDS:
        value = overrides.get(field)
        if value is not None:
            embedding_msg.context_data[field] = value


def _apply_ingest_options(
    embedding_msg,
    ingest_options: IngestOptions | None,
) -> None:
    ingest_options = IngestOptions.from_value(ingest_options)
    if not embedding_msg or ingest_options.search_tags is None:
        return
    embedding_msg.context_data["search_tags"] = list(ingest_options.search_tags or [])
    embedding_msg.context_data["_upsert_options"] = {
        "search_tag_mode": ingest_options.search_tag_mode
    }


async def _enqueue_embedding_message(
    embedding_queue,
    embedding_msg,
    *,
    failure_message: str,
) -> bool:
    """Persist one embedding message and settle request tracking on enqueue failure."""
    wait_tracker = get_request_wait_tracker()
    wait_tracker.register_embedding_root(embedding_msg.telemetry_id, embedding_msg.id)

    try:
        enqueue_id = await embedding_queue.enqueue(embedding_msg)
    except BaseException as exc:
        wait_tracker.mark_embedding_failed(
            embedding_msg.telemetry_id,
            embedding_msg.id,
            f"{failure_message}: {exc}",
        )
        raise

    if not enqueue_id:
        wait_tracker.mark_embedding_failed(
            embedding_msg.telemetry_id,
            embedding_msg.id,
            failure_message,
        )
        return False
    return True


def _coerce_datetime(value: object) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return parse_iso_datetime(value)
        except Exception:
            return None
    return None


async def _get_existing_created_at(
    uri: str,
    ctx: Optional[RequestContext],
) -> Optional[datetime]:
    if ctx is None:
        return None
    try:
        from openviking.server.dependencies import get_service

        service = get_service()
        if not service or not service.vikingdb_manager:
            return None
        record = await service.vikingdb_manager.fetch_by_uri(uri, ctx=ctx)
        if not record:
            return None
        return _coerce_datetime(record.get("created_at"))
    except Exception:
        return None


async def _resolve_context_timestamps(
    uri: str,
    ctx: Optional[RequestContext],
    *,
    preserve_existing_created_at: bool = False,
) -> tuple[datetime, datetime]:
    updated_at = datetime.now(timezone.utc)
    try:
        stat_result = await get_viking_fs().stat(uri, ctx=ctx, skip_count=True)
        stat_mod_time = _coerce_datetime((stat_result or {}).get("modTime"))
        if stat_mod_time is not None:
            updated_at = stat_mod_time
    except Exception:
        pass

    created_at = updated_at
    if preserve_existing_created_at:
        existing_created_at = await _get_existing_created_at(uri, ctx)
        if existing_created_at is not None:
            created_at = existing_created_at

    return created_at, updated_at


def get_resource_content_type(file_name: str) -> Optional[ResourceContentType]:
    """Determine resource content type based on file extension.

    Returns None if the file type is not recognized.
    """
    file_name = file_name.lower()

    text_extensions = {
        ".txt",
        ".md",
        ".csv",
        ".json",
        ".jsonl",
        ".xml",
        ".svg",
        ".py",
        ".js",
        ".ts",
        ".java",
        ".cpp",
        ".c",
        ".cu",
        ".cuh",
        ".h",
        ".go",
        ".rs",
        ".lua",
        ".rb",
        ".php",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".sql",
        ".kt",
        ".swift",
        ".scala",
        ".r",
        ".m",
        ".pl",
        ".toml",
        ".yaml",
        ".yml",
        ".ini",
        ".cfg",
        ".conf",
        ".tsx",
        ".jsx",
        ".cs",
        ".env",
        ".properties",
        ".rst",
        ".tf",
        ".proto",
        ".gradle",
        ".cc",
        ".cxx",
        ".hpp",
        ".hh",
        ".dart",
        ".vue",
        ".groovy",
        ".ps1",
        ".ex",
        ".exs",
        ".erl",
        ".jl",
        ".mm",
    }
    image_extensions = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
    video_extensions = {".mp4", ".avi", ".mov", ".wmv", ".flv", ".mkv", ".webm"}
    audio_extensions = {".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a", ".opus", ".ac3"}

    if any(file_name.endswith(ext) for ext in text_extensions):
        return ResourceContentType.TEXT
    elif any(file_name.endswith(ext) for ext in image_extensions):
        return ResourceContentType.IMAGE
    elif any(file_name.endswith(ext) for ext in video_extensions):
        return ResourceContentType.VIDEO
    elif any(file_name.endswith(ext) for ext in audio_extensions):
        return ResourceContentType.AUDIO

    return None


async def _build_image_data_uri(
    file_path: str,
    file_name: str,
    viking_fs,
    ctx: Optional[RequestContext],
    raw: Optional[bytes] = None,
) -> Optional[str]:
    """Read an image file and encode it as a base64 ``data:`` URI.

    Oversized images are downsampled only for the embedding request. The
    original resource bytes in VikingFS are left unchanged.
    Returns None if the image cannot be read.
    """
    try:
        content = raw if raw is not None else await viking_fs.read_file_bytes(file_path, ctx=ctx)
        image_config = getattr(get_openviking_config(), "image", None)
        return image_bytes_to_model_data_uri(content, file_name, config=image_config)
    except Exception as e:
        logger.warning(f"Failed to read image for multimodal vectorization {file_path}: {e}")
        return None


async def _resolve_resource_content_type(
    file_path: str,
    file_name: str,
    viking_fs: Any,
    ctx: Optional[RequestContext],
) -> Optional[ResourceContentType]:
    content_type = get_resource_content_type(file_name)
    if Path(file_name).suffix.lower() != ".ts":
        return content_type
    try:
        prefix = await viking_fs.read(
            file_path,
            offset=0,
            size=MPEG_TS_PROBE_BYTES,
            ctx=ctx,
        )
    except Exception:
        return content_type
    if is_mpeg_ts(prefix):
        return ResourceContentType.VIDEO
    return content_type


def _coerce_text_file_content(raw: Any) -> str:
    """Coerce known text-file content returned by VikingFS into str."""
    if isinstance(raw, bytes):
        return _decode_text_bytes(raw)
    return raw or ""


def _looks_like_binary_bytes(raw: bytes) -> bool:
    """Conservative binary check for unknown file bytes."""
    if not raw:
        return False
    if b"\x00" in raw[:4096]:
        return True

    allowed_controls = {9, 10, 12, 13}
    sample = raw[:4096]
    control_count = sum(byte < 32 and byte not in allowed_controls for byte in sample)
    return control_count / len(sample) > 0.3


def _decode_text_bytes(raw: bytes) -> str:
    """Decode file bytes for BM25 content.

    Prefer UTF-8. If UTF-8 fails, reject binary-looking bytes, then try charset
    sniffing. Return an empty string when no text encoding can be recognized.
    """
    if not raw:
        return ""

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass

    if _looks_like_binary_bytes(raw):
        return ""

    best = from_bytes(raw).best()
    if best is None:
        return ""

    return str(best)


async def vectorize_directory_meta(
    uri: str,
    abstract: str,
    overview: str,
    context_type: str = "resource",
    ctx: Optional[RequestContext] = None,
    include_overview: bool = True,
    scalar_overrides: Optional[Dict[int, Dict[str, Any]]] = None,
    ingest_options: IngestOptions | None = None,
    creator_acl_grant: CreatorAclGrant | None = None,
    include_abstract: bool = True,
) -> None:
    """
    Vectorize directory metadata (.abstract.md and .overview.md).

    Creates Context objects for abstract and overview and enqueues them.
    """
    abstract = body_for_preview(abstract)
    overview = body_for_preview(overview)
    if not ctx:
        logger.warning("No context provided for vectorization")
        return
    from openviking.indexing.publication import configured_store

    managed = context_type == "resource" and configured_store() is not None
    embedding = get_openviking_config().embedding if managed else None
    # Partial semantic writes must retain the other existing level. A complete
    # directory generation replaces all of its levels in the active manifest.
    if managed and (not include_abstract or not include_overview):
        from openviking.indexing.content import draft_sources

        fs = get_viking_fs()
        with draft_sources():
            if not include_abstract and await fs.exists(f"{uri}/.abstract.md", ctx=ctx):
                abstract = body_for_preview(await fs.read_file(f"{uri}/.abstract.md", ctx=ctx))
                include_abstract = True
            if not include_overview and await fs.exists(f"{uri}/.overview.md", ctx=ctx):
                overview = body_for_preview(await fs.read_file(f"{uri}/.overview.md", ctx=ctx))
                include_overview = True
    semantic_refs = None
    if managed and embedding.chunking.publish_content:
        from openviking.indexing.content import archive_source

        semantic_refs = {}
        fs = get_viking_fs()
        for level, body, include, suffix in (
            ("0", abstract, include_abstract, ".abstract.md"),
            ("1", overview, include_overview, ".overview.md"),
        ):
            if include:
                reference, raw = await archive_source(fs, f"{uri}/{suffix}", ctx)
                if body_for_preview(fs._decode_bytes(raw)) != body:
                    raise RuntimeError("Semantic source body does not match vector plan")
                semantic_refs[level] = reference
    queue_manager = get_queue_manager()
    queue = queue_manager.get_queue(queue_manager.EMBEDDING)
    created_at, updated_at = await _resolve_context_timestamps(uri, ctx)
    messages = []
    for level, body, include in (
        (ContextLevel.ABSTRACT, abstract, include_abstract),
        (ContextLevel.OVERVIEW, overview, include_overview),
    ):
        if not include:
            continue
        context = Context(
            uri=uri,
            parent_uri=VikingURI(uri).parent.uri,
            is_leaf=False,
            abstract=_truncate_abstract_bytes(body),
            context_type=context_type,
            level=level,
            created_at=created_at,
            updated_at=updated_at,
            user=ctx.user,
            account_id=ctx.account_id,
            owner_space=owner_space_for_uri(uri),
        )
        context.set_vectorize(Vectorize(text=embedding_text_for_body(level, uri, body)))
        message = EmbeddingMsgConverter.from_context(context, creator_acl_grant)
        _apply_scalar_overrides(message, (scalar_overrides or {}).get(int(level.value)))
        _apply_ingest_options(message, ingest_options)
        if message:
            messages.append(message)
    if managed and messages:
        from openviking.indexing.semantic import enqueue_semantic_plan
        from openviking.indexing.metadata_integration import load_business_meta

        metadata_input = await load_business_meta(get_viking_fs(), uri, ctx, is_directory=True)
        for message in messages:
            message.context_data["_business_meta_input"] = metadata_input

        await enqueue_semantic_plan(messages, embedding, queue, semantic_refs=semantic_refs)
        return
    first_error = None
    for message in messages:
        try:
            await _enqueue_embedding_message(
                queue, message, failure_message=f"Failed to enqueue directory vector for {uri}"
            )
        except TaskWorkRejected:
            logger.debug("Skipped directory vectorization for cancelling task: %s", uri)
            return
        except Exception as exc:
            first_error = first_error or exc
    if first_error is not None:
        raise first_error


async def vectorize_file(
    file_path: str,
    summary_dict: Dict[str, Any],
    parent_uri: str,
    context_type: str = "resource",
    ctx: Optional[RequestContext] = None,
    use_summary: bool = False,
    preserve_existing_created_at: bool = False,
    scalar_override: Optional[Dict[str, Any]] = None,
    ingest_options: IngestOptions | None = None,
    creator_acl_grant: CreatorAclGrant | None = None,
) -> bool:
    """
    Vectorize a single file.

    Creates Context object for the file and enqueues it.
    The effective vectorization strategy is resolved once from either the explicit
    `use_summary` flag (code path override) or the embedding config.
    Returns whether an embedding message was enqueued.
    """
    try:
        if not ctx:
            logger.warning("No context provided for vectorization")
            return False

        queue_manager = get_queue_manager()
        embedding_queue = queue_manager.get_queue(queue_manager.EMBEDDING)
        viking_fs = get_viking_fs()

        file_name = summary_dict.get("name") or os.path.basename(file_path)
        summary = summary_dict.get("summary", "")
        # Cap below the bytes_row 65535-byte abstract-scalar limit (#2774 parity).
        summary = _truncate_abstract_bytes(summary)

        created_at, updated_at = await _resolve_context_timestamps(
            file_path,
            ctx,
            preserve_existing_created_at=preserve_existing_created_at,
        )
        context = Context(
            uri=file_path,
            parent_uri=parent_uri,
            is_leaf=True,
            abstract=summary,
            context_type=context_type,
            created_at=created_at,
            updated_at=updated_at,
            user=ctx.user,
            account_id=ctx.account_id,
            owner_space=owner_space_for_uri(file_path),
        )

        content_type = await _resolve_resource_content_type(file_path, file_name, viking_fs, ctx)
        embedding_cfg = get_openviking_config().embedding
        configured_text_source = embedding_cfg.text_source
        effective_text_source = TEXT_SOURCE_SUMMARY_ONLY if use_summary else configured_text_source
        embed_summary = bool(summary and effective_text_source in SUMMARY_TEXT_SOURCES)

        chunking = getattr(embedding_cfg, "chunking", None)
        if (
            chunking
            and chunking.enabled
            and context_type == "resource"
            and (
                content_type == ResourceContentType.TEXT
                or (content_type is None and is_text_file(file_name))
            )
        ):
            from openviking.indexing.enqueue import enqueue_text_plan

            source_snapshot = None
            if chunking.publish_content:
                from openviking.indexing.content import archive_source

                source_snapshot, raw = await archive_source(viking_fs, file_path, ctx)
                content = _coerce_text_file_content(raw)
                import hashlib

                source_snapshot["text_sha256"] = hashlib.sha256(content.encode()).hexdigest()
            else:
                from openviking.indexing.content import draft_sources

                with draft_sources():
                    content = _coerce_text_file_content(
                        await viking_fs.read_file(file_path, ctx=ctx)
                    )
            context.set_vectorize(Vectorize(text=content or " "))
            base = EmbeddingMsgConverter.from_context(context, creator_acl_grant)
            if source_snapshot:
                base.context_data["_source_snapshot"] = source_snapshot
            if chunking.publish_content:
                from openviking.indexing.metadata_integration import load_business_meta

                base.context_data["_business_meta_input"] = await load_business_meta(viking_fs, file_path, ctx)
            _apply_scalar_overrides(base, scalar_override)
            _apply_ingest_options(base, ingest_options)
            count = await enqueue_text_plan(base, content, embedding_cfg, embedding_queue)
            return count > 0

        image_snapshot = None
        if content_type in (ResourceContentType.AUDIO, ResourceContentType.VIDEO):
            effective_text = summary or file_name
            context.abstract = effective_text
            context.set_vectorize(Vectorize(text=effective_text))
        elif content_type is None:
            if summary:
                logger.warning(
                    f"Unsupported file type for {file_path}, falling back to summary for vectorization"
                )
                context.set_vectorize(Vectorize(text=summary))
            elif is_text_file(file_name):
                content = _coerce_text_file_content(await viking_fs.read_file(file_path, ctx=ctx))
                embedding_text = truncate_embedding_input(
                    content,
                    embedding_cfg.max_input_tokens,
                )
                del content
                context.set_vectorize(Vectorize(text=embedding_text))
            else:
                logger.warning(
                    f"Unsupported file type for {file_path} and no summary available, skipping vectorization"
                )
                return False
        elif content_type == ResourceContentType.TEXT:
            if embed_summary:
                context.set_vectorize(Vectorize(text=summary))
            else:
                try:
                    content = _coerce_text_file_content(
                        await viking_fs.read_file(file_path, ctx=ctx)
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to read file content for {file_path}, falling back to summary: {e}"
                    )
                    if summary:
                        context.set_vectorize(Vectorize(text=summary))
                    else:
                        logger.warning(
                            f"No summary available for {file_path}, skipping vectorization"
                        )
                        return False
                else:
                    embedding_text = truncate_embedding_input(
                        content,
                        embedding_cfg.max_input_tokens,
                    )
                    del content
                    context.set_vectorize(Vectorize(text=embedding_text))
        elif content_type == ResourceContentType.IMAGE:
            strict_image = bool(chunking and chunking.enabled and context_type == "resource")
            if strict_image:
                if not chunking.require_joint_image_embedding:
                    raise ValueError(
                        "Degraded image indexing is not implemented; joint embedding is required"
                    )
                if not summary.strip():
                    raise ValueError(
                        "SEMANTICS_REQUIRED: image indexing requires a current image summary"
                    )
                if not getattr(embedding_cfg.get_embedder(), "supports_multimodal", False):
                    raise ValueError(
                        "Configured embedding model does not support joint image and text input"
                    )
            # Multimodal embedders consume both parts; text-only embedders fall back to summary.
            image_raw = None
            if strict_image and chunking.publish_content:
                from openviking.indexing.content import archive_source

                image_snapshot, image_raw = await archive_source(viking_fs, file_path, ctx)
            image_uri = await _build_image_data_uri(
                file_path, file_name, viking_fs, ctx, raw=image_raw
            )
            if image_uri:
                context.set_vectorize(Vectorize(text=summary, images=[image_uri]))
            elif strict_image:
                raise ValueError("Image bytes are required for joint embedding")
            elif summary:
                context.set_vectorize(Vectorize(text=summary))
            else:
                logger.debug(
                    f"Skipping image {file_path} (image unreadable and no summary available)"
                )
                return False
        elif summary:
            # For non-text files, use summary
            context.set_vectorize(Vectorize(text=summary))
        else:
            logger.debug(f"Skipping file {file_path} (no text content or summary)")
            return False

        embedding_msg = EmbeddingMsgConverter.from_context(context, creator_acl_grant)
        if not embedding_msg:
            return False

        if (
            content_type == ResourceContentType.IMAGE
            and chunking
            and chunking.enabled
            and context_type == "resource"
        ):
            embedding_msg.context_data["_require_joint_image_embedding"] = True
        _apply_scalar_overrides(embedding_msg, scalar_override)
        _apply_ingest_options(embedding_msg, ingest_options)
        if embedding_msg.context_data.get("_require_joint_image_embedding"):
            from openviking.indexing.image import enqueue_image_plan
            from openviking.indexing.metadata_integration import load_business_meta

            embedding_msg.context_data["_business_meta_input"] = await load_business_meta(viking_fs, file_path, ctx)

            return bool(
                await enqueue_image_plan(
                    embedding_msg, embedding_cfg, embedding_queue, source_ref=image_snapshot,
                    occurrences=summary_dict.get("image_occurrences"), fs=viking_fs, ctx=ctx
                )
            )
        enqueued = await _enqueue_embedding_message(
            embedding_queue,
            embedding_msg,
            failure_message=f"Failed to enqueue file vector for {file_path}",
        )
        if not enqueued:
            return False
        logger.debug(f"Enqueued file for vectorization: {file_path}")

    except TaskWorkRejected:
        logger.debug("Skipped file vectorization for cancelling task: %s", file_path)
        return False
    except Exception as e:
        logger.error(f"Failed to vectorize file {file_path}: {e}", exc_info=True)
        raise
    return True


@draft_source_job
@text_publication_batch
async def index_resource(
    uri: str,
    ctx: RequestContext,
    ingest_options: IngestOptions | None = None,
) -> None:
    """
    Build vector index for a resource directory.

    1. Reads .abstract.md and .overview.md and vectorizes them.
    2. Scans files in the directory and vectorizes them.

    The context_type is derived from the URI so that memory directories
    (``/memories/``) are indexed as ``"memory"`` rather than the default
    ``"resource"``.
    """
    if is_session_uri(uri):
        logger.info("Skipping indexing for session namespace: %s", uri)
        return

    viking_fs = get_viking_fs()
    context_type = context_type_for_uri(uri)

    # 1. Index Directory Metadata
    abstract_uri = f"{uri}/.abstract.md"
    overview_uri = f"{uri}/.overview.md"

    abstract = ""
    overview = ""

    if await viking_fs.exists(abstract_uri, ctx=ctx):
        content = await viking_fs.read_file(abstract_uri, ctx=ctx)
        abstract = content.decode("utf-8") if isinstance(content, bytes) else content

    if await viking_fs.exists(overview_uri, ctx=ctx):
        content = await viking_fs.read_file(overview_uri, ctx=ctx)
        overview = content.decode("utf-8") if isinstance(content, bytes) else content

    if abstract or overview:
        await vectorize_directory_meta(
            uri,
            abstract,
            overview,
            context_type=context_type,
            ctx=ctx,
            ingest_options=ingest_options,
        )

    # 2. Index Files
    try:
        files = await viking_fs.ls(uri, node_limit=LS_ALL_NODES, ctx=ctx)
        for file_info in files:
            file_name = file_info["name"]

            # Skip hidden files (like .abstract.md)
            if file_name.startswith("."):
                continue

            if file_info.get("type") == "directory" or file_info.get("isDir"):
                # TODO: Recursive indexing? For now, skip subdirectories to match previous behavior
                continue

            file_uri = file_info.get("uri") or f"{uri}/{file_name}"

            # For direct indexing, we might not have summaries.
            # We pass empty summary_dict, vectorize_file will try to read content for text files.
            await vectorize_file(
                file_path=file_uri,
                summary_dict={"name": file_name},
                parent_uri=uri,
                context_type=context_type,
                ctx=ctx,
                ingest_options=ingest_options,
            )

    except Exception as e:
        logger.error(f"Failed to scan directory {uri} for indexing: {e}")
        raise
