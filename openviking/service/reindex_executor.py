# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Admin reindex executor."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Optional

from openviking.core.context import (
    Context,
    ContextLevel,
    ContextType,
    ResourceContentType,
    Vectorize,
)
from openviking.core.directory_index import DIRECTORY_INDEX_FILENAME
from openviking.core.namespace import (
    classify_uri,
    content_owner_context_for_uri,
    context_type_for_uri,
    is_hidden_by_actor_peer_view,
    is_session_uri,
    may_include_hidden_actor_peers,
    owner_fields_for_uri,
    owner_space_for_uri,
    resolve_uri,
    uri_parts,
)
from openviking.indexing.batch import text_publication_batch
from openviking.indexing.content import draft_source_job
from openviking.indexing.publication import configured_store
from openviking.server.dependencies import get_service
from openviking.server.identity import RequestContext, Role
from openviking.service.task_tracker import get_task_tracker
from openviking.service.task_work_index import bind_task_context, get_task_context
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.storage.abstract_overview import body_for_preview, embedding_text_for_body
from openviking.storage.expr import And, Eq, Or, PathScope
from openviking.storage.internal_names import CHAPTER_METADATA_FILENAMES
from openviking.storage.queuefs.embedding_msg_converter import EmbeddingMsgConverter
from openviking.storage.queuefs.process_result import ProcessOutcome
from openviking.storage.queuefs.semantic_msg import SemanticMsg
from openviking.storage.queuefs.semantic_processor import SemanticProcessor
from openviking.storage.vector_ids import chunk_vector_record_id
from openviking.storage.viking_fs import get_viking_fs
from openviking.telemetry import get_current_telemetry
from openviking.telemetry.request_wait_tracker import get_request_wait_tracker
from openviking.utils.embedding_input import truncate_embedding_input
from openviking.utils.embedding_utils import (
    _apply_ingest_options,
    _truncate_abstract_bytes,
    get_resource_content_type,
)
from openviking.utils.ingest_options import IngestOptions
from openviking.utils.skill_processor import SkillProcessor
from openviking.utils.tags import merge_search_tags
from openviking_cli.exceptions import (
    InvalidArgumentError,
    NotFoundError,
    OpenVikingError,
    PermissionDeniedError,
)
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils import VikingURI, get_logger
from openviking_cli.utils.config import get_openviking_config
from openviking_cli.utils.config.concurrency_config import (
    effective_file_vectorization_concurrency,
    effective_subtree_shard_concurrency,
)
from openviking_cli.utils.config.embedding_config import SUMMARY_TEXT_SOURCES

logger = get_logger(__name__)

REINDEX_TASK_TYPE = "admin_reindex"
PRUNE_ORPHAN_CANDIDATE_LIMIT = 100000
PRUNE_OUTPUT_FIELDS = ["id", "uri", "level", "context_type", "account_id", "owner_user_id"]

# The two enterprise build modes share one IndexPlan with D1 ingestion. They are
# the only modes that may take a dry_run preview (``prune_orphans`` keeps its own
# legacy preview). ``vectors_only`` never rewrites L0/L1; ``semantic_and_vectors``
# regenerates image summaries and directory L0/L1 before the vector phase.
ENTERPRISE_REINDEX_MODES = frozenset({"vectors_only", "semantic_and_vectors"})
DRY_RUN_MODES = ENTERPRISE_REINDEX_MODES | {"prune_orphans"}

# A placeholder sidecar delete can lose against a still-open write fence; retry
# briefly after the stage that owns the fence has finished.
_PLACEHOLDER_SWEEP_ATTEMPTS = 3


# Trailing markers VikingFS appends when a directory has no generated .abstract.md/.overview.md
# (see openviking/storage/viking_fs.py). The rendered value is a placeholder, not semantic
# content, and must never be embedded as an ABSTRACT (L0) / OVERVIEW (L1) vector (issue #2434).
_ABSTRACT_NOT_READY_SUFFIX = "[Directory abstract is not ready]"
_OVERVIEW_NOT_READY_SUFFIX = "[Directory overview is not ready]"


def _is_not_ready_sentinel(text: str, suffix: str) -> bool:
    """Return True if *text* is a VikingFS not-ready directory placeholder.

    VikingFS renders these as a single ``# <uri>`` header followed only by the not-ready marker.
    Match that exact shape (a ``#`` header with no substantive body before the trailing marker)
    so the check is uri-agnostic yet never drops real directory content that merely ends with,
    or mentions, the user-facing marker phrase.
    """
    if not text:
        return False
    head = text.rstrip()
    if not head.endswith(suffix):
        return False
    head = head[: -len(suffix)].strip()
    return head.startswith("#") and "\n" not in head


_reindex_executor: "ReindexExecutor | None" = None


def get_reindex_executor() -> "ReindexExecutor":
    global _reindex_executor
    if _reindex_executor is None:
        _reindex_executor = ReindexExecutor()
    return _reindex_executor


@dataclass
class _ReindexCounters:
    scanned_records: int = 0
    rebuilt_records: int = 0
    # Records whose published generation already held the exact same content, so
    # the vector stage reused it instead of paying for another embedding call.
    reused_records: int = 0
    deleted_records: int = 0
    would_delete_records: int = 0
    unsupported_records: int = 0
    failed_records: int = 0
    warnings: list[str] = field(default_factory=list)

    def merge_from(self, other: "_ReindexCounters") -> None:
        self.scanned_records += other.scanned_records
        self.rebuilt_records += other.rebuilt_records
        self.reused_records += other.reused_records
        self.deleted_records += other.deleted_records
        self.would_delete_records += other.would_delete_records
        self.unsupported_records += other.unsupported_records
        self.failed_records += other.failed_records
        self.warnings.extend(other.warnings)


@dataclass(frozen=True)
class _EffectiveModeConfig:
    """Resolved behavior of the two enterprise build modes.

    The mode is the only switch that decides whether directory L0/L1 and image
    summaries are regenerated. Neither mode may silently degrade a joint image
    record to a text-only summary vector.
    """

    mode: str
    writes_vectors: bool
    writes_semantics: bool
    write_directory_levels: bool
    regenerate_image_summaries: bool
    # Both enterprise modes require a current valid image summary for a joint
    # image record; an image without one is skipped, never degraded to text.
    requires_image_semantics: bool = True


@dataclass
class _ReindexPlanPreview:
    """Zero-write preview produced by a dry_run enterprise reindex."""

    uri: str
    mode: str
    recursive: bool
    planned_chunks: int = 0
    planned_semantic_levels: int = 0
    ancestor_directories: list[str] = field(default_factory=list)
    unsupported_records: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "mode": self.mode,
            "recursive": self.recursive,
            "planned_chunks": self.planned_chunks,
            "planned_semantic_levels": self.planned_semantic_levels,
            "ancestor_directories": list(self.ancestor_directories),
            "unsupported_records": self.unsupported_records,
            "warnings": list(self.warnings),
        }


@dataclass
class _ReindexRunContext:
    ctx: RequestContext
    counters: _ReindexCounters
    lock: dict | None = None
    ingest_options: IngestOptions | None = None
    # Ancestor directories this run will refresh bottom-up. Resolved before the
    # lease is acquired so the lease can cover the files written up there.
    refreshed_ancestors: list[str] = field(default_factory=list)


@dataclass
class _PruneSourceRead:
    exists: bool
    text: str = ""
    error: Exception | None = None


class ReindexExecutor:
    """Non-destructive reindex orchestration for admin maintenance flows."""

    SUPPORTED_MODES_BY_TYPE = {
        "global_namespace": {"vectors_only", "semantic_and_vectors", "prune_orphans"},
        "user_namespace": {"vectors_only", "semantic_and_vectors", "prune_orphans"},
        "skill_namespace": {"vectors_only", "semantic_and_vectors", "prune_orphans"},
        "resource": {"vectors_only", "semantic_and_vectors", "prune_orphans"},
        "skill": {"vectors_only", "semantic_and_vectors", "prune_orphans"},
        "memory": {"vectors_only", "semantic_and_vectors", "prune_orphans"},
    }

    @staticmethod
    def _effective_file_vectorization_concurrency() -> int:
        """File concurrency for this reindex; ceilings come from global config."""
        return effective_file_vectorization_concurrency()

    @staticmethod
    def _effective_subtree_shard_concurrency() -> int:
        """Child-subtree parallelism; 0 keeps the serial walk."""
        return effective_subtree_shard_concurrency()

    async def _run_ordered_counter_batches(
        self,
        items: list[str],
        *,
        concurrency: int,
        processor: Any,
        counters: _ReindexCounters,
    ) -> None:
        for start in range(0, len(items), concurrency):
            batch = items[start : start + concurrency]
            for item_counters in await asyncio.gather(*(processor(item) for item in batch)):
                counters.merge_from(item_counters)

    @staticmethod
    def _content_owner_ctx(uri: str, ctx: RequestContext) -> RequestContext:
        """Return the content-owner context for user-scoped reindex writes.

        Reindex authorization and task ownership use the actor ctx, but semantic
        and vector records should retain ownership from the target URI.
        """
        return content_owner_context_for_uri(uri, ctx)

    @staticmethod
    def effective_mode_config(mode: str) -> _EffectiveModeConfig:
        """Resolve the effective behavior of an enterprise build mode.

        Both modes reuse the shared IndexPlan and rebuild the same vector
        records. Only ``semantic_and_vectors`` regenerates directory L0/L1 and
        image summaries. Neither mode may degrade a joint image record to a
        text-only summary vector, so both require a current valid image summary.
        """
        if mode == "vectors_only":
            return _EffectiveModeConfig(
                mode=mode,
                writes_vectors=True,
                writes_semantics=False,
                write_directory_levels=False,
                regenerate_image_summaries=False,
            )
        if mode == "semantic_and_vectors":
            return _EffectiveModeConfig(
                mode=mode,
                writes_vectors=True,
                writes_semantics=True,
                write_directory_levels=True,
                regenerate_image_summaries=True,
            )
        raise InvalidArgumentError(f"Mode {mode} has no enterprise effective configuration")

    @staticmethod
    def _authorize_reindex_target(uri: str, ctx: RequestContext) -> None:
        """Extend reindex authorization to authorized business writers.

        The historical rule only let a ``USER`` reindex their own private user
        namespace. An authorized enterprise writer/admin may reindex a resource
        domain it can already write, without disguising itself as ROOT. A
        ``USER`` still may not reach another tenant's private space, hidden
        actor-peer views, or another account's root.
        """
        role = getattr(ctx.role, "value", ctx.role)
        if role == Role.ROOT:
            return
        try:
            target = resolve_uri(uri)
        except Exception as exc:  # NamespaceShapeError / ValueError
            raise InvalidArgumentError(f"Unsupported reindex URI: {uri}") from exc

        if is_hidden_by_actor_peer_view(uri, ctx) or may_include_hidden_actor_peers(uri, ctx):
            raise PermissionDeniedError(
                "Reindex cannot target a hidden actor peer view.",
                resource=uri,
            )

        if target.scope == "user":
            if role == Role.USER and (
                not target.owner_user_id or target.owner_user_id != ctx.user.user_id
            ):
                raise PermissionDeniedError(
                    "USER can only reindex their own user namespace.",
                    resource=uri,
                )
            return

        # Enterprise resource domains (and other shared scopes) are writable by
        # an authorized writer/admin. A plain USER is limited to the private
        # user namespace above and cannot rebuild a shared knowledge domain.
        if role == Role.USER:
            raise PermissionDeniedError(
                "USER can only reindex their own user namespace.",
                resource=uri,
            )
        if role in {Role.ADMIN, "admin"}:
            return
        if role == Role.ROOT:
            return
        raise PermissionDeniedError(
            "Reindex requires an authorized writer for the target domain.",
            resource=uri,
        )

    async def execute(
        self,
        *,
        uri: str,
        mode: str,
        wait: bool,
        dry_run: bool = False,
        recursive: bool = True,
        tags: list[str] | None = None,
        tag_mode: str = "replace",
        index_profile: str | None = None,
        ctx: RequestContext,
    ) -> dict[str, Any]:
        object_type = self._infer_target_type(uri)
        self._validate_mode(object_type, mode)
        if dry_run and mode not in DRY_RUN_MODES:
            raise InvalidArgumentError(
                "dry_run is only supported for prune_orphans and enterprise "
                "vectors_only/semantic_and_vectors reindex modes."
            )
        self._authorize_reindex_target(uri, ctx)
        ingest_options = self._resolve_ingest_options(
            mode=mode,
            tags=tags,
            tag_mode=tag_mode,
        )

        tracker = get_task_tracker()
        if wait:
            if await tracker.has_running(
                REINDEX_TASK_TYPE,
                uri,
                account_id=ctx.account_id,
                user_id=ctx.user.user_id,
            ):
                raise OpenVikingError(
                    f"URI {uri} already has a reindex in progress",
                    code="CONFLICT",
                    details={"uri": uri},
                )
            return await self._run(
                uri=uri,
                object_type=object_type,
                mode=mode,
                dry_run=dry_run,
                recursive=recursive,
                ingest_options=ingest_options,
                index_profile=index_profile,
                ctx=ctx,
            )

        task = await tracker.create_if_no_running(
            REINDEX_TASK_TYPE,
            uri,
            account_id=ctx.account_id,
            user_id=ctx.user.user_id,
        )
        if task is None:
            raise OpenVikingError(
                f"URI {uri} already has a reindex in progress",
                code="CONFLICT",
                details={"uri": uri},
            )

        asyncio.create_task(
            self._run_tracked(
                task.task_id,
                uri=uri,
                object_type=object_type,
                mode=mode,
                dry_run=dry_run,
                recursive=recursive,
                ingest_options=ingest_options,
                index_profile=index_profile,
                ctx=ctx,
            )
        )
        return {
            "task_id": task.task_id,
            "status": "accepted",
            "uri": uri,
            "object_type": object_type,
            "mode": mode,
        }

    @staticmethod
    def _resolve_ingest_options(
        *,
        mode: str,
        tags: list[str] | None,
        tag_mode: str,
    ) -> IngestOptions | None:
        if mode == "prune_orphans" or tags is None:
            return None
        if tag_mode not in {"replace", "append"}:
            raise InvalidArgumentError(f"unsupported tag mode: {tag_mode}")
        return IngestOptions.from_search_tags(tags, mode=tag_mode)

    @staticmethod
    def _with_ingest_options(
        kwargs: dict[str, Any],
        ingest_options: IngestOptions | None,
    ) -> dict[str, Any]:
        if ingest_options is not None:
            kwargs["ingest_options"] = ingest_options
        return kwargs

    def _infer_target_type(self, uri: str) -> str:
        if not uri.startswith("viking://"):
            raise OpenVikingError(
                f"Unsupported reindex URI: {uri}",
                code="UNSUPPORTED_URI",
                details={"uri": uri},
            )
        classification = classify_uri(uri)
        parts = classification.parts
        if not parts:
            return "global_namespace"
        if is_session_uri(uri):
            raise OpenVikingError(
                f"Unsupported reindex URI: {uri}",
                code="UNSUPPORTED_URI",
                details={"uri": uri},
            )
        if parts == ("user",):
            return "user_namespace"
        if classification.is_user_namespace_root:
            return "user_namespace"
        if parts[0] == "agent":
            if len(parts) >= 2 and parts[1] in {"skills", "endpoints", "tools", "payments"}:
                if classification.is_skill_namespace:
                    return "skill_namespace"
                if classification.is_skill_root:
                    return "skill"
                if classification.is_skill:
                    raise OpenVikingError(
                        f"Unsupported reindex URI: {uri}",
                        code="UNSUPPORTED_URI",
                        details={"uri": uri},
                    )
                return "resource"
            raise OpenVikingError(
                "viking://agent/{agent_id}/... is no longer supported; "
                "use viking://agent/skills/... or viking://user/... instead.",
                code="UNSUPPORTED_URI",
                details={"uri": uri},
            )
        if classification.is_memory:
            return "memory"
        if classification.is_skill_namespace:
            return "skill_namespace"
        if classification.is_skill_root:
            return "skill"
        if classification.is_skill:
            raise OpenVikingError(
                f"Unsupported reindex URI: {uri}",
                code="UNSUPPORTED_URI",
                details={"uri": uri},
            )
        if parts[0] in {"resources", "user"}:
            return "resource"
        raise OpenVikingError(
            f"Unsupported reindex URI: {uri}",
            code="UNSUPPORTED_URI",
            details={"uri": uri},
        )

    async def _tree_all(
        self,
        viking_fs: Any,
        uri: str,
        *,
        show_all_hidden: bool,
        ctx: RequestContext,
    ) -> list[dict[str, Any]]:
        return await viking_fs.tree(
            uri,
            output="original",
            show_all_hidden=show_all_hidden,
            node_limit=None,
            level_limit=None,
            ctx=ctx,
        )

    async def _refresh_namespace_resource_semantics(
        self,
        *,
        target_root: str,
        directories: list[str],
        files: list[str],
        run: _ReindexRunContext,
    ) -> tuple[list[str], list[str], dict]:
        counters = run.counters
        ctx = run.ctx
        image_summaries: dict = {}
        prefix = self._child_prefix(target_root)
        semantic_roots = sorted(
            {
                directory_uri
                for directory_uri in directories
                if directory_uri.startswith(prefix)
                and "/" not in directory_uri[len(prefix) :]
                and directory_uri[len(prefix) :]
            }
        )
        filtered_directories = [
            directory_uri
            for directory_uri in directories
            if any(
                directory_uri == root or directory_uri.startswith(root + "/")
                for root in semantic_roots
            )
        ]
        filtered_files = [
            file_uri
            for file_uri in files
            if any(file_uri.startswith(root + "/") for root in semantic_roots)
        ]
        filtered_file_set = set(filtered_files)
        for file_uri in files:
            if file_uri in filtered_file_set:
                continue
            counters.unsupported_records += 1
            counters.warnings.append(
                f"Skipped {file_uri}: namespace semantic_and_vectors only refreshes resource directories"
            )
        for semantic_root in semantic_roots:
            semantic_value = await self._run_semantic_processor(
                uri=semantic_root,
                context_type="resource",
                ctx=ctx,
                lock=run.lock,
            )
            if isinstance(semantic_value, dict) and semantic_value.get("image_summaries"):
                image_summaries.update(semantic_value["image_summaries"])
        return filtered_directories, filtered_files, image_summaries

    @staticmethod
    def _child_prefix(root: str) -> str:
        if root.rstrip("/") == "viking:":
            return "viking://"
        return root.rstrip("/") + "/"

    async def _update_stage(self, stage: str) -> None:
        """Best-effort task stage update for observer visibility.

        The reindex body runs under ``bind_task_context``, so the task id is
        recoverable here; outside that context (dry_run, wait=True path) there
        is nothing to update.
        """
        task_ctx = get_task_context()
        if task_ctx is None:
            return
        try:
            await get_task_tracker().update_stage(
                task_ctx.task_id,
                stage,
                account_id=task_ctx.account_id,
                user_id=task_ctx.user_id,
            )
        except Exception as exc:
            get_logger().warning("Failed to update reindex task stage to %s: %s", stage, exc)

    @staticmethod
    def _apply_embedding_wait_status(
        counters: _ReindexCounters,
        queue_status: dict[str, Any],
    ) -> None:
        embedding_status = queue_status.get("Embedding") or {}
        error_count = int(embedding_status.get("error_count", 0) or 0)
        if error_count <= 0:
            return
        counters.failed_records += error_count
        counters.rebuilt_records = max(0, counters.rebuilt_records - error_count)
        for error in embedding_status.get("errors", []) or []:
            message = error.get("message") if isinstance(error, dict) else str(error)
            if message:
                counters.warnings.append(f"Embedding queue failed during reindex: {message}")

    def _is_resource_entry_for_namespace(self, uri: str, target_root: str) -> bool:
        if not uri.startswith(self._child_prefix(target_root)):
            return False
        classification = classify_uri(uri)
        if classification.is_memory or classification.is_skill:
            return False
        return True

    def _is_global_resource_entry(self, uri: str) -> bool:
        return uri == "viking://resources" or uri.startswith("viking://resources/")

    async def _reindex_skill_namespace(
        self,
        *,
        uri: str,
        mode: str,
        run: _ReindexRunContext,
    ) -> None:
        counters = run.counters
        ctx = run.ctx
        viking_fs = get_viking_fs()
        try:
            entries = await self._tree_all(viking_fs, uri, show_all_hidden=True, ctx=ctx)
        except Exception as exc:
            raise NotFoundError(uri, "resource") from exc

        skill_roots = []
        for entry in entries:
            entry_uri = entry.get("uri")
            if entry_uri and entry.get("isDir") and classify_uri(entry_uri).is_skill_root:
                skill_roots.append(entry_uri)

        for skill_root in sorted(set(skill_roots)):
            await self._reindex_skill(
                uri=skill_root,
                mode=mode,
                run=run,
            )

        if not skill_roots:
            counters.unsupported_records += 1
            counters.warnings.append(f"No skill roots found under {uri}")

    def _validate_mode(self, object_type: str, mode: str) -> None:
        supported_modes = self.SUPPORTED_MODES_BY_TYPE[object_type]
        if mode not in supported_modes:
            raise OpenVikingError(
                f"Mode {mode} is not supported for {object_type}",
                code="UNSUPPORTED_MODE",
                details={
                    "mode": mode,
                    "object_type": object_type,
                    "supported_modes": sorted(supported_modes),
                },
            )

    async def _run(
        self,
        *,
        uri: str,
        object_type: str,
        mode: str,
        dry_run: bool = False,
        recursive: bool = True,
        ingest_options: IngestOptions | None = None,
        index_profile: str | None = None,
        ctx: RequestContext,
    ) -> dict[str, Any]:
        service = get_service()
        if service.viking_fs is None or service.vikingdb_manager is None:
            raise RuntimeError("OpenVikingService not initialized")
        # An enterprise dry_run returns the plan and estimated counts with ZERO
        # writes. It never touches the embedding queue, so a missing queue must
        # not fail the preview.
        enterprise_dry_run = dry_run and mode in ENTERPRISE_REINDEX_MODES
        if (
            not dry_run
            and mode != "prune_orphans"
            and not service.vikingdb_manager.has_queue_manager
        ):
            raise OpenVikingError(
                "Reindex requires embedding queue",
                code="FAILED_PRECONDITION",
                details={"uri": uri},
            )

        if enterprise_dry_run:
            return await self._preview_enterprise_reindex(
                uri=uri,
                object_type=object_type,
                mode=mode,
                recursive=recursive,
                ctx=ctx,
            )

        path = service.viking_fs._uri_to_path(uri, ctx=ctx)
        started_at = time.perf_counter()
        counters = _ReindexCounters()
        telemetry_id = get_current_telemetry().telemetry_id
        wait_tracker = get_request_wait_tracker()
        if telemetry_id:
            wait_tracker.register_request(telemetry_id)

        acquire_lock = service.viking_fs._async_agfs.pathlock_acquire_tree
        if mode != "prune_orphans" or await service.viking_fs.exists(uri, ctx=ctx):
            stat = await service.viking_fs.stat(uri, ctx=ctx, skip_count=True)
            if not stat.get("isDir", stat.get("is_dir")):
                acquire_lock = service.viking_fs._async_agfs.pathlock_acquire_exact
        # A finished leaf bubbles L0/L1 (and the navigation index) up into its
        # ancestor directories. Those writes live outside the target's lock
        # scope, so the ancestor pass takes its own short-lived lease (see
        # _refresh_ancestor_semantics); the list is resolved here so the pass and
        # the lease it takes cannot disagree about which directories those are.
        ancestors = await self._refreshable_ancestors(uri, ctx=ctx)
        lease = await acquire_lock(path)
        try:
            borrowed = await service.viking_fs._async_agfs.pathlock_as_borrowed(lease)
            run = _ReindexRunContext(
                ctx=ctx,
                counters=counters,
                lock=borrowed,
                ingest_options=ingest_options,
                refreshed_ancestors=ancestors,
            )
            await self._update_stage(f"{mode}:{object_type}")
            if mode == "prune_orphans":
                await self._prune_orphan_vectors(
                    uri=uri,
                    object_type=object_type,
                    dry_run=dry_run,
                    counters=counters,
                    ctx=ctx,
                )
            elif object_type == "global_namespace":
                await self._reindex_global_namespace(
                    uri=uri,
                    mode=mode,
                    run=run,
                )
            elif object_type == "user_namespace":
                await self._reindex_user_namespace(
                    uri=uri,
                    mode=mode,
                    run=run,
                )
            elif object_type == "skill_namespace":
                await self._reindex_skill_namespace(
                    uri=uri,
                    mode=mode,
                    run=run,
                )
            elif object_type == "resource":
                resource_kwargs = {"uri": uri, "mode": mode, "run": run}
                if not recursive:
                    resource_kwargs["recursive"] = False
                await self._reindex_resource(**resource_kwargs)
            elif object_type == "skill":
                skill_kwargs = {"uri": uri, "mode": mode, "run": run}
                if not recursive:
                    skill_kwargs["recursive"] = False
                await self._reindex_skill(**skill_kwargs)
            elif object_type == "memory":
                memory_kwargs = {"uri": uri, "mode": mode, "run": run}
                if not recursive:
                    memory_kwargs["recursive"] = False
                await self._reindex_memory(**memory_kwargs)
            else:
                raise OpenVikingError(
                    f"Unsupported reindex type: {object_type}",
                    code="UNSUPPORTED_URI",
                    details={"uri": uri},
                )

            if telemetry_id and mode != "prune_orphans":
                await wait_tracker.wait_for_request(telemetry_id)
                self._apply_embedding_wait_status(
                    counters,
                    wait_tracker.build_queue_status(telemetry_id),
                )
        finally:
            await service.viking_fs._async_agfs.pathlock_release(lease)
            if telemetry_id:
                wait_tracker.cleanup(telemetry_id)

        result = {
            "status": "completed",
            "uri": uri,
            "object_type": object_type,
            "mode": mode,
            "scanned_records": counters.scanned_records,
            "rebuilt_records": counters.rebuilt_records,
            "reused_records": counters.reused_records,
            "deleted_records": counters.deleted_records,
            "would_delete_records": counters.would_delete_records,
            "unsupported_records": counters.unsupported_records,
            "failed_records": counters.failed_records,
            "duration_ms": int((time.perf_counter() - started_at) * 1000),
            "warnings": counters.warnings,
        }
        if index_profile is not None:
            result["index_profile"] = index_profile
        return result

    async def _preview_enterprise_reindex(
        self,
        *,
        uri: str,
        object_type: str,
        mode: str,
        recursive: bool,
        ctx: RequestContext,
    ) -> dict[str, Any]:
        """Compute an enterprise reindex preview with ZERO writes.

        Reuses the shared IndexPlan for every Markdown/Text file so the preview
        counts match the real build, without enqueuing any embedding message,
        rewriting the original source, calling a parser, or re-splitting
        chapters from ``_toc.json``.
        """
        viking_fs = get_viking_fs()
        config = self.effective_mode_config(mode)
        preview = _ReindexPlanPreview(uri=uri, mode=mode, recursive=recursive)
        try:
            stat = await viking_fs.stat(uri, ctx=ctx, skip_count=True)
            is_dir = bool(stat.get("isDir", stat.get("is_dir")))
        except Exception as exc:
            raise NotFoundError(uri, "resource") from exc

        files: list[str] = []
        directories: list[str] = []
        if is_dir:
            directories.append(uri)
            if recursive:
                try:
                    entries = await self._tree_all(
                        viking_fs, uri, show_all_hidden=True, ctx=ctx
                    )
                except Exception as exc:
                    raise NotFoundError(uri, "resource") from exc
                for entry in entries:
                    entry_uri = entry.get("uri")
                    if not entry_uri:
                        continue
                    if entry.get("isDir"):
                        directories.append(entry_uri)
                    elif not self._is_hidden_meta_file(entry_uri):
                        files.append(entry_uri)
        elif not self._is_hidden_meta_file(uri):
            files.append(uri)

        files = await self._drop_navigation_indexes(files, ctx=ctx)

        if config.write_directory_levels:
            preview.planned_semantic_levels = 2 * len(
                [d for d in dict.fromkeys(directories) if d != "viking://"]
            )

        for file_uri in dict.fromkeys(files):
            try:
                content = await viking_fs.read_file(file_uri, ctx=ctx)
            except Exception as exc:
                preview.unsupported_records += 1
                preview.warnings.append(f"Preview could not read {file_uri}: {exc}")
                continue
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            content_type = get_resource_content_type(file_uri.rsplit("/", 1)[-1])
            if content_type == ResourceContentType.IMAGE:
                # A captionless image is skipped by the real build, not planned.
                preview.unsupported_records += 1
                continue
            preview.planned_chunks += self._preview_chunk_count(file_uri, content)

        result = {
            "status": "preview",
            "dry_run": True,
            "uri": uri,
            "object_type": object_type,
            "mode": mode,
            "effective_config": {
                "mode": config.mode,
                "writes_vectors": config.writes_vectors,
                "writes_semantics": config.writes_semantics,
                "write_directory_levels": config.write_directory_levels,
                "regenerate_image_summaries": config.regenerate_image_summaries,
            },
            "duration_ms": 0,
        }
        result.update(preview.to_dict())
        return result

    def _preview_chunk_count(self, file_uri: str, content: str) -> int:
        """Plan the chunk count without enqueuing or persisting anything."""
        from openviking.indexing.plan import plan_resource

        embedding = get_openviking_config().embedding
        chunking = getattr(embedding, "chunking", None)
        if not (chunking and chunking.enabled and content):
            return 1
        plan = plan_resource(content, file_uri, "preview", embedding)
        return len(plan.chunks)

    async def _run_tracked(
        self,
        task_id: str,
        *,
        uri: str,
        object_type: str,
        mode: str,
        dry_run: bool = False,
        recursive: bool = True,
        ingest_options: IngestOptions | None = None,
        index_profile: str | None = None,
        ctx: RequestContext,
    ) -> None:
        tracker = get_task_tracker()
        tracker.register_running_task(task_id)
        try:
            await tracker.start(task_id, account_id=ctx.account_id, user_id=ctx.user.user_id)
            with bind_task_context(task_id, ctx.account_id, ctx.user.user_id):
                result = await self._run(
                    uri=uri,
                    object_type=object_type,
                    mode=mode,
                    dry_run=dry_run,
                    recursive=recursive,
                    ingest_options=ingest_options,
                    index_profile=index_profile,
                    ctx=ctx,
                )
            await tracker.complete(
                task_id,
                result,
                account_id=ctx.account_id,
                user_id=ctx.user.user_id,
            )
        except asyncio.CancelledError:
            # TaskWorkIndex finalizes after this active task and its queue work settle.
            return
        except Exception as exc:
            await tracker.fail(
                task_id,
                str(exc),
                account_id=ctx.account_id,
                user_id=ctx.user.user_id,
            )
        finally:
            await tracker.unregister_running_task(task_id)

    async def _prune_orphan_vectors(
        self,
        *,
        uri: str,
        object_type: str,
        dry_run: bool,
        counters: _ReindexCounters,
        ctx: RequestContext,
    ) -> None:
        service = get_service()
        vikingdb = service.vikingdb_manager
        delete_groups: dict[tuple[str, str], tuple[RequestContext, list[str]]] = {}

        for context_type in self._prune_context_types(uri=uri, object_type=object_type):
            offset = 0
            while True:
                filter_kwargs: dict[str, Any] = {}
                if offset:
                    filter_kwargs["offset"] = offset
                records = await vikingdb.filter(
                    filter=And(
                        [
                            Eq("account_id", ctx.account_id),
                            Eq("context_type", context_type),
                            Or(
                                [
                                    PathScope("uri", uri, depth=0),
                                    PathScope("uri", uri, depth=-1),
                                ]
                            ),
                        ]
                    ),
                    limit=PRUNE_ORPHAN_CANDIDATE_LIMIT,
                    output_fields=PRUNE_OUTPUT_FIELDS,
                    ctx=ctx,
                    **filter_kwargs,
                )
                if not records:
                    break

                for record in records:
                    counters.scanned_records += 1
                    if not self._is_supported_prune_record(record, counters):
                        continue
                    if not await self._is_orphan_vector_record(record, counters=counters, ctx=ctx):
                        continue

                    if dry_run:
                        counters.would_delete_records += 1
                        continue

                    delete_ctx = self._delete_ctx_for_prune_record(record, ctx)
                    key = (delete_ctx.account_id, delete_ctx.user.user_id)
                    if key not in delete_groups:
                        delete_groups[key] = (delete_ctx, [])
                    delete_groups[key][1].append(str(record["id"]))

                if len(records) < PRUNE_ORPHAN_CANDIDATE_LIMIT:
                    break
                offset += len(records)

        if dry_run:
            return

        for delete_ctx, ids in delete_groups.values():
            try:
                deleted = await vikingdb.delete(ids, ctx=delete_ctx)
                deleted_count = int(deleted if deleted is not None else len(ids))
                counters.deleted_records += deleted_count
                if deleted_count < len(ids):
                    failed_count = len(ids) - max(deleted_count, 0)
                    counters.failed_records += failed_count
                    counters.warnings.append(
                        f"Only deleted {deleted_count} of {len(ids)} orphan vectors for owner "
                        f"{delete_ctx.user.user_id}"
                    )
            except Exception as exc:
                counters.failed_records += len(ids)
                counters.warnings.append(
                    f"Failed to delete {len(ids)} orphan vectors for owner "
                    f"{delete_ctx.user.user_id}: {exc}"
                )

    def _prune_context_types(self, *, uri: str, object_type: str) -> list[str]:
        if object_type == "resource":
            return [ContextType.RESOURCE.value]
        if object_type == "memory":
            return [ContextType.MEMORY.value]
        if object_type in {"skill", "skill_namespace"}:
            return [ContextType.SKILL.value]
        if object_type in {"global_namespace", "user_namespace"}:
            return [
                ContextType.RESOURCE.value,
                ContextType.MEMORY.value,
                ContextType.SKILL.value,
            ]
        return [str(context_type_for_uri(uri))]

    def _is_supported_prune_record(
        self,
        record: dict[str, Any],
        counters: _ReindexCounters,
    ) -> bool:
        record_id = record.get("id")
        uri = record.get("uri")
        context_type = str(record.get("context_type") or "")
        level = record.get("level")
        if not record_id or not uri:
            counters.unsupported_records += 1
            counters.warnings.append(f"Skipping unknown prune record without id/uri: {record!r}")
            return False
        if context_type not in {
            ContextType.RESOURCE.value,
            ContextType.MEMORY.value,
            ContextType.SKILL.value,
        }:
            counters.unsupported_records += 1
            counters.warnings.append(f"Skipping unknown context_type for prune record {uri}")
            return False
        try:
            normalized_level = int(level)
        except (TypeError, ValueError):
            counters.unsupported_records += 1
            counters.warnings.append(f"Skipping unknown level for prune record {uri}: {level}")
            return False
        if normalized_level not in {
            int(ContextLevel.ABSTRACT),
            int(ContextLevel.OVERVIEW),
            int(ContextLevel.DETAIL),
        }:
            counters.unsupported_records += 1
            counters.warnings.append(f"Skipping unknown level for prune record {uri}: {level}")
            return False
        record["_prune_level"] = normalized_level
        return True

    async def _is_orphan_vector_record(
        self,
        record: dict[str, Any],
        *,
        counters: _ReindexCounters,
        ctx: RequestContext,
    ) -> bool:
        uri = str(record["uri"])
        level = int(record.get("_prune_level", record["level"]))
        context_type = str(record["context_type"])
        owner_ctx = self._delete_ctx_for_prune_record(record, ctx)

        if level == int(ContextLevel.ABSTRACT):
            abstract = await self._read_prune_source(
                f"{uri}/.abstract.md",
                ctx=owner_ctx,
            )
            if abstract.error:
                self._record_prune_source_error(
                    counters=counters,
                    uri=uri,
                    source_uri=f"{uri}/.abstract.md",
                    error=abstract.error,
                )
                return False
            return (
                not abstract.exists
                or not abstract.text
                or _is_not_ready_sentinel(abstract.text, _ABSTRACT_NOT_READY_SUFFIX)
            )

        if level == int(ContextLevel.OVERVIEW):
            overview = await self._read_prune_source(
                f"{uri}/.overview.md",
                ctx=owner_ctx,
            )
            if overview.error:
                self._record_prune_source_error(
                    counters=counters,
                    uri=uri,
                    source_uri=f"{uri}/.overview.md",
                    error=overview.error,
                )
                return False
            if (
                overview.exists
                and overview.text
                and not _is_not_ready_sentinel(overview.text, _OVERVIEW_NOT_READY_SUFFIX)
            ):
                return False
            if context_type in {ContextType.RESOURCE.value, ContextType.SKILL.value}:
                abstract = await self._read_prune_source(
                    f"{uri}/.abstract.md",
                    ctx=owner_ctx,
                )
                if abstract.error:
                    self._record_prune_source_error(
                        counters=counters,
                        uri=uri,
                        source_uri=f"{uri}/.abstract.md",
                        error=abstract.error,
                    )
                    return False
                return (
                    not abstract.exists
                    or not abstract.text
                    or _is_not_ready_sentinel(abstract.text, _ABSTRACT_NOT_READY_SUFFIX)
                )
            return True

        if self._is_hidden_meta_file(uri):
            return False
        exists = await self._prune_source_exists(uri, ctx=owner_ctx)
        if exists.error:
            self._record_prune_source_error(
                counters=counters,
                uri=uri,
                source_uri=uri,
                error=exists.error,
            )
            return False
        # A real filename can have the same spelling as a virtual chunk URI.
        if exists.exists:
            return False

        if "#" in uri:
            # Generated chunks append a zero-padded index to the full base URI.
            chunk_match = re.fullmatch(r"(.+)#chunk_[0-9]{4,}", uri)
            if context_type == ContextType.MEMORY.value and chunk_match:
                base_uri = chunk_match.group(1)
                base = await self._read_prune_source(base_uri, ctx=owner_ctx)
                if base.error:
                    self._record_prune_source_error(
                        counters=counters,
                        uri=uri,
                        source_uri=base_uri,
                        error=base.error,
                    )
                    return False
                if not base.exists:
                    return True
                expected = {
                    chunk_uri for chunk_uri, _chunk in self._chunk_memory_body(base_uri, base.text)
                }
                return uri not in expected
            return False

        return True

    async def _read_prune_source(self, uri: str, *, ctx: RequestContext) -> _PruneSourceRead:
        viking_fs = get_viking_fs()
        try:
            exists = await viking_fs.exists(uri, ctx=ctx)
        except Exception as exc:
            return _PruneSourceRead(exists=False, error=exc)
        if not exists:
            return _PruneSourceRead(exists=False)
        try:
            content = await viking_fs.read_file(uri, ctx=ctx)
        except Exception as exc:
            return _PruneSourceRead(exists=True, error=exc)
        if isinstance(content, bytes):
            text = content.decode("utf-8", errors="replace")
        else:
            text = str(content or "")
        return _PruneSourceRead(exists=True, text=text)

    async def _prune_source_exists(self, uri: str, *, ctx: RequestContext) -> _PruneSourceRead:
        try:
            exists = await get_viking_fs().exists(uri, ctx=ctx)
        except Exception as exc:
            return _PruneSourceRead(exists=False, error=exc)
        return _PruneSourceRead(exists=exists)

    def _record_prune_source_error(
        self,
        *,
        counters: _ReindexCounters | None,
        uri: str,
        source_uri: str,
        error: Exception,
    ) -> None:
        if counters is None:
            return
        counters.failed_records += 1
        counters.warnings.append(f"Skipped prune for {uri}: failed to read {source_uri}: {error}")

    def _delete_ctx_for_prune_record(
        self,
        record: dict[str, Any],
        ctx: RequestContext,
    ) -> RequestContext:
        uri = str(record.get("uri") or "")
        owner = record.get("owner_user_id")
        if not owner:
            owner = owner_fields_for_uri(uri).get("owner_user_id")
        if not owner or owner == ctx.user.user_id:
            return ctx
        return RequestContext(
            user=UserIdentifier(ctx.account_id, str(owner)),
            role=ctx.role,
            actor_peer_id=ctx.actor_peer_id,
            from_oauth=ctx.from_oauth,
        )

    async def _reindex_resource_sharded(
        self,
        *,
        uri: str,
        mode: str,
        run: _ReindexRunContext,
    ) -> bool:
        """Process one recursive directory as independent per-child units.

        A serial recursive walk rebuilds the entire subtree's L0/L1 first and
        only then starts the vector phase, and the semantic phase is the only
        consumer of the remote VLM: for the whole vector phase (the long one)
        the VLM therefore sits idle, so a client that reindexes one tree at a
        time leaves the model unused most of the wall clock.

        Sharding keeps the same per-unit order (semantic -> vectors over the
        child's own subtree, directory L0/L1 before that directory's vector
        record) but lets the semantic phase of one child overlap the vector
        phase of another. Nothing about what is published changes; only the
        order in which independent subtrees are visited.

        Returns True when this call handled the subtree, False to fall back to
        the serial walk (leaf, fewer than two children, sharding disabled, or
        the subtree could not be listed).
        """
        concurrency = self._effective_subtree_shard_concurrency()
        if concurrency < 2:
            return False
        ctx = run.ctx
        try:
            viking_fs = get_viking_fs()
            stat = await viking_fs.stat(uri, ctx=ctx, skip_count=True)
            is_dir = stat.get("isDir", stat.get("is_dir")) if isinstance(stat, dict) else False
            if not is_dir:
                return False
            entries = await self._tree_all(viking_fs, uri, show_all_hidden=True, ctx=ctx)
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.warning(
                "Sharded reindex could not list %s (%r); falling back to the serial walk",
                uri,
                exc,
            )
            return False

        root = uri.rstrip("/")
        directories: list[str] = []
        files: list[str] = []
        for entry in entries:
            child_uri = entry.get("uri")
            if not child_uri:
                continue
            child = child_uri.rstrip("/")
            if child == root or child.rsplit("/", 1)[0] != root:
                continue
            if entry.get("isDir"):
                directories.append(child)
            elif not self._is_hidden_meta_file(child):
                files.append(child)
        try:
            # Generated navigation indexes are never indexable content: the
            # vector phase drops them through the same helper. As a shard unit
            # one of them never completes its semantic generation (observed
            # live: nine recursions sat on ``<manual>/index.md`` for hours and
            # then failed the whole directory refresh).
            files = await self._drop_navigation_indexes(files, ctx=ctx)
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.warning(
                "Sharded reindex could not classify navigation indexes under %s (%r); "
                "falling back to the serial walk",
                uri,
                exc,
            )
            return False
        children: list[tuple[str, bool]] = [(directory, True) for directory in directories]
        children.extend((file_uri, False) for file_uri in files)
        if len(children) < 2:
            return False

        logger.info(
            "Sharded reindex for %s: %d child unit(s) at concurrency %d",
            uri,
            len(children),
            concurrency,
        )
        await self._update_stage(f"semantic+vectors:{uri}")
        semaphore = asyncio.Semaphore(concurrency)
        child_runs = [
            (
                child_uri,
                child_is_dir,
                replace(run, counters=_ReindexCounters()),
            )
            for child_uri, child_is_dir in children
        ]

        async def process_child(
            child_uri: str,
            child_is_dir: bool,
            child_run: _ReindexRunContext,
        ) -> None:
            async with semaphore:
                await self._reindex_resource(
                    uri=child_uri,
                    mode=mode,
                    run=child_run,
                    recursive=child_is_dir,
                    shard=False,
                    propagate_to_parent=False,
                )

        results = await asyncio.gather(
            *(
                asyncio.create_task(process_child(child_uri, child_is_dir, child_run))
                for child_uri, child_is_dir, child_run in child_runs
            ),
            return_exceptions=True,
        )
        failure: BaseException | None = None
        for (_child_uri, _child_is_dir, child_run), result in zip(child_runs, results):
            run.counters.merge_from(child_run.counters)
            if isinstance(result, BaseException) and failure is None:
                failure = result

        # This directory's own L0/L1 is rebuilt last, from the sidecars the child
        # units just wrote, so the directory has exactly one writer instead of one
        # per child.
        try:
            await self._run_semantic_processor(
                uri=uri,
                context_type="resource",
                ctx=ctx,
                lock=run.lock,
                recursive=False,
                propagate_to_parent=False,
            )
            await self._update_stage(f"vectors:{uri}")
            await self._reindex_resource_vectors(
                **self._with_ingest_options(
                    {
                        "uri": uri,
                        "counters": run.counters,
                        "ctx": ctx,
                        "recursive": False,
                    },
                    run.ingest_options,
                )
            )
        except Exception as exc:
            if failure is None:
                failure = exc
            else:
                logger.warning(
                    "Sharded reindex for %s: directory-level refresh failed: %r",
                    uri,
                    exc,
                )
        if failure is not None:
            raise failure
        return True

    async def _reindex_resource(
        self,
        *,
        uri: str,
        mode: str,
        run: _ReindexRunContext,
        recursive: bool = True,
        shard: bool = True,
        propagate_to_parent: bool = True,
    ) -> None:
        counters = run.counters
        ctx = run.ctx
        config = self.effective_mode_config(mode)
        vector_kwargs = {"uri": uri, "counters": counters, "ctx": ctx}
        if not recursive:
            vector_kwargs["recursive"] = False
        if config.write_directory_levels and recursive and shard:
            if await self._reindex_resource_sharded(uri=uri, mode=mode, run=run):
                return
        if config.write_directory_levels:
            # semantic_and_vectors regenerates directory L0/L1 before the shared
            # vector phase; vectors_only keeps the published L0/L1 untouched.
            # Figure citations (vikingsrc) and captions are NOT rebuilt here:
            # they belong to the citation-normalization stage, which must run
            # before reindex so the archived markdown and its captions agree.
            await self._update_stage(f"semantic:{uri}")
            semantic_kwargs = {
                "uri": uri,
                "context_type": "resource",
                "ctx": ctx,
                "lock": run.lock,
            }
            if not recursive:
                semantic_kwargs["recursive"] = False
            if not propagate_to_parent:
                semantic_kwargs["propagate_to_parent"] = False
            semantic_value = await self._run_semantic_processor(**semantic_kwargs)
            if isinstance(semantic_value, dict) and semantic_value.get("image_summaries"):
                vector_kwargs["image_summaries"] = semantic_value["image_summaries"]
            if not recursive:
                # A non-recursive rebuild does not let the semantic processor
                # walk upward, so refresh the affected ancestors explicitly to
                # keep their L0/L1 consistent with the changed leaf.
                await self._refresh_ancestor_semantics(uri=uri, run=run)
        await self._update_stage(f"vectors:{uri}")
        await self._reindex_resource_vectors(
            **self._with_ingest_options(
                vector_kwargs,
                run.ingest_options,
            )
        )
        # Placeholder L0/L1 left by the pre-fix overview path can only be deleted
        # once this reindex stopped writing under the directory: the vector stage
        # holds a write fence on every image directory it publishes, so the walk's
        # own attempt (and any attempt made during the semantic stage) is refused
        # with "Resource is being processed".
        await self._sweep_stale_placeholder_sidecars(uri, ctx=ctx)

    async def _reindex_skill(
        self,
        *,
        uri: str,
        mode: str,
        run: _ReindexRunContext,
        recursive: bool = True,
    ) -> None:
        counters = run.counters
        ctx = run.ctx
        if mode == "semantic_and_vectors":
            await self._regenerate_skill_semantics(uri=uri, ctx=ctx)
            vector_kwargs = {"uri": uri, "counters": counters, "ctx": ctx}
            if not recursive:
                vector_kwargs["recursive"] = False
            await self._reindex_skill_vectors(
                **self._with_ingest_options(vector_kwargs, run.ingest_options)
            )
            return
        await self._reindex_skill_vectors(
            **self._with_ingest_options(
                {"uri": uri, "counters": counters, "ctx": ctx},
                run.ingest_options,
            )
        )

    async def _reindex_memory(
        self,
        *,
        uri: str,
        mode: str,
        run: _ReindexRunContext,
        recursive: bool = True,
    ) -> None:
        counters = run.counters
        ctx = run.ctx
        if mode == "semantic_and_vectors":
            stat = await get_viking_fs().stat(uri, ctx=ctx, skip_count=True)
            if stat.get("isDir", stat.get("is_dir")):
                semantic_kwargs = {
                    "uri": uri,
                    "context_type": "memory",
                    "ctx": ctx,
                    "lock": run.lock,
                }
                if not recursive:
                    semantic_kwargs["recursive"] = False
                await self._run_semantic_processor(**semantic_kwargs)
            vector_kwargs = {"uri": uri, "counters": counters, "ctx": ctx}
            if not recursive:
                vector_kwargs["recursive"] = False
            await self._reindex_memory_vectors(
                **self._with_ingest_options(
                    vector_kwargs,
                    run.ingest_options,
                )
            )
            return
        await self._reindex_memory_vectors(
            **self._with_ingest_options(
                {"uri": uri, "counters": counters, "ctx": ctx},
                run.ingest_options,
            )
        )

    async def _run_semantic_processor(
        self,
        *,
        uri: str,
        context_type: str,
        ctx: RequestContext,
        lock: dict | None = None,
        recursive: bool = True,
        propagate_to_parent: bool | None = None,
    ) -> dict | None:
        processor = SemanticProcessor(
            max_concurrent_llm=get_openviking_config().vlm.max_concurrent,
        )
        owner_ctx = self._content_owner_ctx(uri, ctx)
        msg = SemanticMsg(
            uri=uri,
            context_type=context_type,
            recursive=recursive,
            account_id=owner_ctx.account_id,
            user_id=owner_ctx.user.user_id,
            peer_id=owner_ctx.user.user_id,
            role=str(ctx.role),
            skip_vectorization=True,
            generation_trigger="reindex",
            allow_vlm=False,
            use_hierarchical_aggregation=context_type == "memory",
            propagate_to_parent=(
                recursive if propagate_to_parent is None else propagate_to_parent
            ),
        )
        # Every image node resolves its citing chapters by walking the tree;
        # memoize that per run so a tree of images costs one walk rather than a
        # walk per image (the vector path does the same in
        # _reindex_resource_vectors).
        from openviking.indexing.image_context import image_context_cache

        with image_context_cache():
            result = await processor.on_dequeue({"data": msg.to_json()}, lock=lock)
        if result.outcome is not ProcessOutcome.SUCCESS:
            # Spec gate (TC-13.x): a requeued message has no synchronous
            # consumer inside the reindex run — continuing would rebuild
            # vectors over unregenerated L0/L1.
            raise OpenVikingError(
                result.error or f"Semantic generation did not complete: {result.outcome.value}",
                code="PROCESSING_ERROR",
                details={"uri": uri},
            )
        return result.value

    async def _sweep_stale_placeholder_sidecars(self, uri: str, *, ctx: RequestContext) -> int:
        """Delete placeholder L0/L1 sidecars the semantic walk could not remove.

        The walk attempts that delete while it still holds the directory lock, so
        the attempt loses its lock race and the file survives. Once the walk is
        over nothing holds those paths, and the marker is unambiguous: only a
        sidecar whose visible body is the placeholder is removed.
        """

        from openviking.storage.abstract_overview import body_for_preview
        from openviking.storage.queuefs.semantic_dag import _PLACEHOLDER_OVERVIEW_MARKER

        stats = SemanticProcessor.consume_dag_stats(uri=uri)
        dirs = list(getattr(stats, "placeholder_sidecar_dirs", []) or [])
        if not dirs:
            if stats is None:
                # Another consumer took the stats for this URI, so the walk's
                # placeholder candidates are gone. Say so instead of returning
                # as if the sweep had found nothing to do.
                logger.warning(
                    "Placeholder sweep skipped for %s: the semantic walk stats for "
                    "this URI were already consumed",
                    uri,
                )
            return 0
        viking_fs = get_viking_fs()
        removed = 0
        for dir_uri in dict.fromkeys(dirs):
            for name in (".overview.md", ".abstract.md"):
                sidecar_uri = f"{dir_uri.rstrip('/')}/{name}"
                try:
                    body = await viking_fs.read_file(sidecar_uri, ctx=ctx)
                except Exception:
                    continue
                if not isinstance(body, (str, bytes)):
                    continue
                if _PLACEHOLDER_OVERVIEW_MARKER not in body_for_preview(body):
                    continue
                last_error: BaseException | None = None
                for attempt in range(_PLACEHOLDER_SWEEP_ATTEMPTS):
                    try:
                        await viking_fs.rm(sidecar_uri, ctx=ctx)
                        removed += 1
                        last_error = None
                        break
                    except Exception as exc:
                        # A still-open write fence ("Resource is being processed")
                        # clears once the stage that owns it finishes.
                        last_error = exc
                        await asyncio.sleep(1.0 * (attempt + 1))
                if last_error is not None:
                    logger.warning(
                        "Could not remove the stale placeholder sidecar %s: %s",
                        sidecar_uri,
                        last_error,
                    )
        if removed:
            logger.warning(
                "Removed %d stale placeholder sidecar(s) under %s after the semantic walk",
                removed,
                uri,
            )
        return removed

    @staticmethod
    def _ancestor_directories(uri: str, *, namespace_root: str | None = None) -> list[str]:
        """Ancestor directories of *uri* that stay inside the same domain.

        A leaf change propagates upward to the directories whose L0/L1 depend on
        it, but never across the domain boundary (``viking://resources`` for a
        resource, the user root for a private namespace). ``viking://`` and the
        bare scope containers are never rebuilt as an ancestor.
        """
        parts = uri_parts(uri)
        if not parts:
            return []
        if namespace_root is not None:
            root_parts = uri_parts(namespace_root)
            if not root_parts or parts[:len(root_parts)] != root_parts:
                raise InvalidArgumentError("Ancestor namespace root must contain the target URI")
            # The registered knowledge root has its own L0/L1. Refresh it, but
            # never its parent, whose children may belong to another domain.
            return ["viking://" + "/".join(parts[:depth])
                    for depth in range(len(parts) - 1, len(root_parts) - 1, -1)]
        # Determine the domain boundary depth: resources/user/<uid>/... domains
        # stop at their registered root, not at the account root.
        boundary: int
        if parts[0] == "resources":
            boundary = 1
        elif parts[0] == "user" and len(parts) >= 2:
            boundary = 2
        elif parts[0] == "agent" and len(parts) >= 2:
            boundary = 2
        else:
            boundary = 1
        ancestors: list[str] = []
        # Strictly above the domain root: the domain container itself is not a
        # knowledge document and must never be rebuilt as an ancestor.
        for depth in range(len(parts) - 1, boundary, -1):
            ancestors.append("viking://" + "/".join(parts[:depth]))
        return ancestors

    # Files a bottom-up ancestor refresh rewrites inside each ancestor directory.
    # ``index.md`` is the generated navigation catalogue, the other two are L0/L1.
    _ANCESTOR_REFRESH_FILENAMES = (DIRECTORY_INDEX_FILENAME, ".abstract.md", ".overview.md")
    # Bounded retries for the ancestor lease. Concurrent reindexes share these
    # files, so a busy ancestor delays the refresh into per-write locks instead
    # of failing the reindex that owns it.
    _ANCESTOR_LEASE_ATTEMPTS = 3

    async def _refreshable_ancestors(self, uri: str, *, ctx: RequestContext) -> list[str]:
        """Ancestor directories a reindex of *uri* refreshes bottom-up."""
        registry = configured_store()
        namespace_root = None
        if registry is not None:
            namespaces = await asyncio.to_thread(registry.namespace_list, ctx.account_id)
            roots = [row["root_uri"] for row in namespaces
                     if uri == row["root_uri"] or uri.startswith(row["root_uri"] + "/")]
            if len(roots) > 1:
                raise InvalidArgumentError("Target belongs to overlapping registered knowledge roots")
            namespace_root = roots[0] if roots else None
        return self._ancestor_directories(uri, namespace_root=namespace_root)

    async def _acquire_ancestor_refresh_lease(
        self,
        *,
        ancestors: list[str],
        ctx: RequestContext,
    ) -> dict | None:
        """Lease covering exactly the files the ancestor refresh rewrites.

        The run's own lease is a tree lock on the target and cannot cover an
        ancestor write: the native coverage rule rejects that with "does not
        cover the requested operation", and because ``source_write`` had already
        opened a fence the rejection became a permanent ``uncertain`` fence that
        blocked the URI's generation with WRITE_RECOVERY_REQUIRED.

        Taking these exact locks for the whole run would serialize every
        concurrent reindex on the shared ancestors (``raw/index.md`` and
        friends), so the refresh takes them for its own short duration, retrying
        a bounded number of times. ``None`` means "no aggregated lease": the
        refresh then runs with per-write locks, which is always safe -- what is
        never safe is borrowing a lease that covers a different subtree.
        """
        viking_fs = get_viking_fs()
        agfs = getattr(viking_fs, "_async_agfs", None)
        acquire = getattr(agfs, "pathlock_acquire_exact_batch", None)
        if not ancestors or acquire is None:
            return None
        exact_paths = [
            viking_fs._uri_to_path(f"{ancestor.rstrip('/')}/{name}", ctx=ctx)
            for ancestor in ancestors
            for name in self._ANCESTOR_REFRESH_FILENAMES
        ]
        exact_paths = [p for p in dict.fromkeys(exact_paths) if p]
        if not exact_paths:
            return None
        last_error: BaseException | None = None
        for attempt in range(self._ANCESTOR_LEASE_ATTEMPTS):
            try:
                return await acquire(exact_paths)
            except Exception as exc:  # noqa: BLE001 - retried, then per-write locks
                last_error = exc
                message = str(exc).lower()
                if "lock" not in message and "busy" not in message:
                    break
                await asyncio.sleep(0.5 * (attempt + 1))
        logger.warning(
            "Ancestor refresh for %s will take per-write locks: could not lease %d "
            "ancestor file(s) after %d attempt(s): %s",
            ancestors[0],
            len(exact_paths),
            self._ANCESTOR_LEASE_ATTEMPTS,
            last_error,
        )
        return None

    async def _image_summary_for_reindex(
        self, file_uri: str, *, ctx: RequestContext
    ) -> tuple[dict | None, str]:
        """Best-effort image summary for the vector stage.

        Order: the published occurrence cache first (it already encodes the
        model/prompt signature and the citing-context fingerprint), then a
        rebuild from the chapters that cite the image -- which is exactly the
        state a reindex exists to repair.  Returns ``(summary, reason)``; the
        caller reports a ``None`` summary together with ``reason``.
        """
        cache_error = ""
        try:
            from openviking.indexing.image_cache import load_image_semantics

            cached = await load_image_semantics(get_viking_fs(), file_uri, ctx)
            if cached and str(cached.get("summary") or "").strip():
                return cached, ""
            cache_error = "published image semantics carry no caption"
        except Exception as exc:  # noqa: BLE001 - any failure means "rebuild it"
            cache_error = (
                f"published image semantics unavailable ({type(exc).__name__}: {exc})"
            )
        try:
            from openviking.indexing.image_context import collect_image_contexts
            from openviking.parse.parsers.media.utils import generate_image_summary

            contexts = await collect_image_contexts(get_viking_fs(), file_uri, ctx)
            rebuilt = await generate_image_summary(
                file_uri,
                file_uri.rsplit("/", 1)[-1],
                None,
                ctx=ctx,
                contexts=contexts,
                allow_vlm=False,
            )
            if rebuilt and str(rebuilt.get("summary") or "").strip():
                logger.info(
                    "Rebuilt image summary from its citing chapters: %s (%s)",
                    file_uri,
                    cache_error or "published cache missing",
                )
                return rebuilt, ""
            return None, f"{cache_error}; its citing chapters carry no figcaption"
        except Exception as exc:  # noqa: BLE001
            return None, f"{cache_error}; rebuild failed ({type(exc).__name__}: {exc})"

    async def _refresh_ancestor_semantics(
        self,
        *,
        uri: str,
        run: _ReindexRunContext,
    ) -> None:
        """Bottom-up L0/L1 refresh of a target's ancestor directories.

        A leaf change must be reflected in the L0/L1 of the directories that
        depend on it (TC-9.4 / TC-11.6). Ancestors are refreshed deepest-first
        and never beyond the target's domain root.

        The refresh runs under its own exact lease on the files it rewrites; the
        target's tree lease does not cover an ancestor path, and reusing it made
        every ancestor write fail with "does not cover the requested operation"
        inside ``source_write`` -- which converted a coverage bug into permanent
        ``uncertain`` write fences. A busy ancestor skips the refresh (it is an
        affordance) instead of failing the run that owns it.
        """
        ctx = run.ctx
        ancestors = run.refreshed_ancestors or await self._refreshable_ancestors(uri, ctx=ctx)
        if not ancestors:
            return
        lease = await self._acquire_ancestor_refresh_lease(ancestors=ancestors, ctx=ctx)
        borrowed: dict | None = None
        if lease is not None:
            as_borrowed = getattr(
                getattr(get_viking_fs(), "_async_agfs", None), "pathlock_as_borrowed", None
            )
            if as_borrowed is not None:
                borrowed = await as_borrowed(lease)
            else:
                borrowed = lease
        try:
            for ancestor in ancestors:
                try:
                    stat = await get_viking_fs().stat(ancestor, ctx=ctx, skip_count=True)
                except Exception:
                    continue
                if not stat.get("isDir", stat.get("is_dir")):
                    continue
                await self._run_semantic_processor(
                    uri=ancestor,
                    context_type="resource",
                    ctx=ctx,
                    lock=borrowed,
                    recursive=False,
                    propagate_to_parent=False,
                )
        finally:
            if lease is not None:
                release = getattr(
                    getattr(get_viking_fs(), "_async_agfs", None), "pathlock_release", None
                )
                if release is not None:
                    try:
                        await release(lease)
                    except Exception as exc:  # noqa: BLE001 - lease TTL expires anyway
                        logger.warning("Could not release the ancestor refresh lease: %s", exc)

    async def _reindex_resource_vectors(
        self,
        *,
        uri: str,
        counters: _ReindexCounters,
        ctx: RequestContext,
        recursive: bool = True,
        ingest_options: IngestOptions | None = None,
        image_summaries: dict | None = None,
    ) -> None:
        viking_fs = get_viking_fs()
        try:
            if not await viking_fs.exists(uri, ctx=ctx):
                raise NotFoundError(uri, "resource")
            stat = await viking_fs.stat(uri, ctx=ctx, skip_count=True)
            is_dir = stat.get("isDir", stat.get("is_dir")) if isinstance(stat, dict) else False
            if is_dir and recursive:
                entries = await self._tree_all(viking_fs, uri, show_all_hidden=True, ctx=ctx)
            else:
                entries = []
        except Exception as exc:
            raise NotFoundError(uri, "resource") from exc

        if is_dir:
            directories = [uri]
            files: list[str] = []
            for entry in entries:
                entry_uri = entry.get("uri")
                if not entry_uri:
                    continue
                if entry.get("isDir"):
                    directories.append(entry_uri)
                elif not self._is_hidden_meta_file(entry_uri):
                    files.append(entry_uri)
        else:
            directories = []
            files = [] if self._is_hidden_meta_file(uri) else [uri]

        files = await self._drop_navigation_indexes(files, ctx=ctx)

        await self._reindex_resource_vectors_from_entries(
            **self._with_ingest_options(
                {
                    "root_uri": uri,
                    "directories": directories,
                    "files": files,
                    "counters": counters,
                    "ctx": ctx,
                    **({"image_summaries": image_summaries} if image_summaries else {}),
                },
                ingest_options,
            )
        )

    async def _published_content_unchanged(
        self, file_uri: str, *, ctx: RequestContext
    ) -> bool:
        """True when the live generation already holds this exact content.

        Re-embedding identical chunks burns the scarcest resource in the system
        (the remote embedding endpoint) and writes a second generation whose
        records the reclaimer then has to delete: live, that is how 957,757 of
        1,303,573 vectors (73.5% of a 78GB store) became orphaned copies.

        The published ``content_revision`` is the sha256 of the source text, so an
        unchanged file needs no new generation -- but only after its published
        records are verified to still exist, because a missing record must be
        rebuilt rather than assumed present.
        """
        store = configured_store()
        if store is None:
            return False
        try:
            published = store.published_chunks(ctx.account_id, file_uri)
        except Exception:  # noqa: BLE001 - never let the fast path break a run
            return False
        if not published:
            return False
        generation, chunks = published
        try:
            raw = await get_viking_fs().read_file(file_uri, ctx=ctx)
        except Exception:  # noqa: BLE001
            return False
        content = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        revision = hashlib.sha256((content or "").encode()).hexdigest()
        if any(chunk_revision != revision for _chunk_id, chunk_revision in chunks):
            return False
        service = get_service()
        backend = getattr(service, "vikingdb_manager", None)
        if backend is None:
            return False
        record_ids = [
            chunk_vector_record_id(
                ctx.account_id, file_uri, f"{chunk_id}:generation:{generation}"
            )
            for chunk_id, _revision in chunks
        ]
        try:
            found = await backend.get(record_ids, ctx=ctx)
        except Exception:  # noqa: BLE001
            return False
        return len(found) == len(record_ids)

    @draft_source_job
    @text_publication_batch
    async def _reindex_resource_vectors_from_entries(
        self,
        *,
        root_uri: str,
        directories: Iterable[str],
        files: Iterable[str],
        counters: _ReindexCounters,
        ctx: RequestContext,
        ingest_options: IngestOptions | None = None,
        image_summaries: dict | None = None,
    ) -> None:
        deduped_directories = []
        seen_directories = set()
        for directory_uri in directories:
            if directory_uri and directory_uri not in seen_directories:
                deduped_directories.append(directory_uri)
                seen_directories.add(directory_uri)

        deduped_files = []
        seen_files = set()
        for file_uri in files:
            if file_uri and file_uri not in seen_files:
                deduped_files.append(file_uri)
                seen_files.add(file_uri)

        for directory_uri in deduped_directories:
            if directory_uri == "viking://":
                continue
            counters.scanned_records += 1
            abstract = await self._read_directory_abstract(directory_uri, ctx=ctx)
            overview = await self._read_directory_overview(directory_uri, ctx=ctx)
            from openviking.indexing.publication import configured_store

            if configured_store() is not None and context_type_for_uri(directory_uri) == "resource":
                # Managed store archives the on-disk sidecars verbatim, so a
                # missing .overview.md must not be fabricated from the L0
                # body; include flags must reflect real sidecar existence.
                if not abstract and not overview:
                    counters.unsupported_records += 1
                    counters.warnings.append(f"No semantic source found for {directory_uri}")
                    continue
                from openviking.utils.embedding_utils import vectorize_directory_meta

                try:
                    await vectorize_directory_meta(
                        directory_uri,
                        abstract,
                        overview,
                        ctx=self._content_owner_ctx(directory_uri, ctx),
                        include_abstract=bool(abstract),
                        include_overview=bool(overview),
                        ingest_options=ingest_options,
                    )
                    counters.rebuilt_records += int(bool(abstract)) + int(bool(overview))
                except Exception as exc:
                    counters.failed_records += 1
                    counters.warnings.append(
                        f"Failed directory generation for {directory_uri}: {exc}"
                    )
                continue
            if not overview:
                overview = abstract
            if not abstract and not overview:
                counters.unsupported_records += 1
                counters.warnings.append(f"No semantic source found for {directory_uri}")
                continue
            if abstract:
                try:
                    await self._upsert_context(
                        uri=directory_uri,
                        parent_uri=VikingURI(directory_uri).parent.uri,
                        abstract=abstract,
                        vector_text=embedding_text_for_body(
                            ContextLevel.ABSTRACT, directory_uri, abstract
                        ),
                        is_leaf=False,
                        context_type=context_type_for_uri(directory_uri),
                        level=ContextLevel.ABSTRACT,
                        ctx=ctx,
                        ingest_options=ingest_options,
                    )
                    counters.rebuilt_records += 1
                except Exception as exc:
                    counters.failed_records += 1
                    counters.warnings.append(f"Failed to reindex {directory_uri} L0 vector: {exc}")
            if overview:
                try:
                    await self._upsert_context(
                        uri=directory_uri,
                        parent_uri=VikingURI(directory_uri).parent.uri,
                        # L1 abstract scalar carries the overview for Rerank.
                        abstract=_truncate_abstract_bytes(overview),
                        vector_text=embedding_text_for_body(
                            ContextLevel.OVERVIEW, directory_uri, overview
                        ),
                        is_leaf=False,
                        context_type=context_type_for_uri(directory_uri),
                        level=ContextLevel.OVERVIEW,
                        ctx=ctx,
                        ingest_options=ingest_options,
                    )
                    counters.rebuilt_records += 1
                except Exception as exc:
                    counters.failed_records += 1
                    counters.warnings.append(f"Failed to reindex {directory_uri} L1 vector: {exc}")

        async def process_file(file_uri: str) -> _ReindexCounters:
            file_counters = _ReindexCounters(scanned_records=1)
            parent_uri = VikingURI(file_uri).parent.uri
            summary = await self._best_file_summary(file_uri, ctx=ctx)
            embedding = get_openviking_config().embedding
            chunking = getattr(embedding, "chunking", None)
            if (
                chunking
                and chunking.enabled
                and get_resource_content_type(file_uri.rsplit("/", 1)[-1])
                in (ResourceContentType.TEXT, ResourceContentType.IMAGE)
            ):
                from openviking.utils.embedding_utils import vectorize_file

                is_image = (
                    get_resource_content_type(file_uri.rsplit("/", 1)[-1])
                    == ResourceContentType.IMAGE
                )

                def _static_image_summary() -> dict:
                    return {"name": file_uri.rsplit("/", 1)[-1], "summary": summary}

                summary_dict: dict | None = (image_summaries or {}).get(file_uri)
                if is_image and summary_dict is None:
                    # Captions are generated by the citation-normalization stage
                    # BEFORE reindex and a published occurrence cache may be
                    # reused; when that cache is missing or stale the summary is
                    # rebuilt from the chapters that cite the image. Skipping is
                    # only for an image no citing chapter describes, and it is
                    # reported instead of being swallowed.
                    summary_dict, reason = await self._image_summary_for_reindex(
                        file_uri, ctx=ctx
                    )
                    if summary_dict is None:
                        file_counters.unsupported_records += 1
                        message = f"Skipped undescribed image {file_uri}: {reason}"
                        logger.warning(message)
                        file_counters.warnings.append(message)
                        return file_counters
                if summary_dict is None and not is_image:
                    summary_dict = _static_image_summary()
                if summary_dict is None:
                    # No fresh semantic result and no published occurrence cache.
                    file_counters.unsupported_records += 1
                    file_counters.warnings.append(
                        f"Skipped undescribed image {file_uri}: no figcaption content"
                    )
                    return file_counters
                if not is_image and await self._published_content_unchanged(
                    file_uri, ctx=ctx
                ):
                    # The live generation already holds this exact content and its
                    # records are verified present: another embedding call would
                    # only produce a duplicate generation for the reclaimer to
                    # delete.
                    file_counters.reused_records += 1
                    return file_counters
                try:
                    chunk_options = await self._chunk_reindex_ingest_options(
                        file_uri, ctx=ctx, incoming=ingest_options
                    )
                    enqueued = await vectorize_file(
                        file_uri,
                        summary_dict,
                        parent_uri,
                        ctx=self._content_owner_ctx(file_uri, ctx),
                        preserve_existing_created_at=True,
                        ingest_options=chunk_options,
                    )
                    if enqueued:
                        file_counters.rebuilt_records += (
                            1  # existing counter counts sources, not children
                        )
                    else:
                        # ``vectorize_file`` declines without raising (empty
                        # summary, unsupported media, gate refusal). A dropped
                        # document must never look like a clean run.
                        file_counters.unsupported_records += 1
                        message = (
                            f"Skipped {file_uri}: the embedding pipeline declined the "
                            "record (no vector was enqueued)"
                        )
                        logger.warning(message)
                        file_counters.warnings.append(message)
                except Exception as exc:
                    file_counters.failed_records += 1
                    file_counters.warnings.append(f"Failed chunk reindex for {file_uri}: {exc}")
                return file_counters
            vector_text = await self._best_resource_file_vector_text(file_uri, summary, ctx=ctx)
            if not vector_text:
                file_counters.unsupported_records += 1
                file_counters.warnings.append(f"No vector source found for {file_uri}")
                return file_counters
            abstract = self._prefer_non_empty(summary, vector_text)
            try:
                await self._upsert_context(
                    uri=file_uri,
                    parent_uri=parent_uri,
                    abstract=abstract,
                    vector_text=vector_text,
                    is_leaf=True,
                    context_type=context_type_for_uri(file_uri),
                    level=ContextLevel.DETAIL,
                    ctx=ctx,
                    ingest_options=ingest_options,
                )
                file_counters.rebuilt_records += 1
            except Exception as exc:
                file_counters.failed_records += 1
                file_counters.warnings.append(f"Failed to reindex {file_uri} vector: {exc}")
            return file_counters

        concurrency = self._effective_file_vectorization_concurrency()
        if deduped_files:
            logger.info(
                "Reindex resource file vectorization: root=%s files=%d concurrency=%d",
                root_uri,
                len(deduped_files),
                concurrency,
            )
        # Chapter markdown is target-independent, so one tree-wide memo turns
        # the per-image chapter read+parse into a single pass per source. The
        # twin index is likewise captured once and shared by every uncited copy.
        from openviking.indexing.image_context import image_context_cache
        from openviking.indexing.image_cache import image_twin_cache

        with image_context_cache(), image_twin_cache():
            await self._run_ordered_counter_batches(
                deduped_files,
                concurrency=concurrency,
                processor=process_file,
                counters=counters,
            )

    async def _chunk_reindex_ingest_options(
        self, uri: str, *, ctx: RequestContext, incoming: IngestOptions | None
    ) -> IngestOptions:
        """Explicitly carry source tags to every new revision's chunk record.

        A new chunk ID has no prior row for the writer's append/preserve rules.
        Source metadata must eventually come from a document manifest; until
        that registry exists, use the existing authorized source vector row.
        """
        options = IngestOptions.from_value(incoming)
        if options.search_tags is not None and options.search_tag_mode == "replace":
            return options
        service = get_service()
        if service.vikingdb_manager is None:
            raise RuntimeError("Chunk reindex cannot preserve source tags without vector storage")
        existing = await service.vikingdb_manager.fetch_by_uri(
            uri, ctx=self._content_owner_ctx(uri, ctx)
        )
        tags = list((existing or {}).get("search_tags") or [])
        if options.search_tags is not None:
            tags = merge_search_tags(tags, options.search_tags)
        return IngestOptions.from_search_tags(tags, mode="replace")

    @draft_source_job
    async def reindex_directory_marker(
        self, *, dir_uri: str, level: ContextLevel, ctx: RequestContext
    ) -> None:
        """Recompute ONLY this directory's L0 (ABSTRACT) or L1 (OVERVIEW) vector.

        Non-recursive: does not touch descendants. Used by git restore when a
        directory's ``.abstract.md`` / ``.overview.md`` marker changed. When the
        on-disk semantic source is empty, the corresponding vector is deleted
        instead of upserted.
        """
        if level not in (ContextLevel.ABSTRACT, ContextLevel.OVERVIEW):
            raise ValueError(f"reindex_directory_marker only supports L0/L1, got {level!r}")
        if dir_uri == "viking://":
            return

        viking_fs = get_viking_fs()
        marker_name = ".abstract.md" if level == ContextLevel.ABSTRACT else ".overview.md"
        lock_path = viking_fs._uri_to_path(f"{dir_uri}/{marker_name}", ctx=ctx)
        lease = await viking_fs._async_agfs.pathlock_acquire_exact(lock_path)
        try:
            from openviking.indexing.publication import configured_store

            if configured_store() is not None and context_type_for_uri(dir_uri) == "resource":
                from openviking.utils.embedding_utils import vectorize_directory_meta

                abstract = await self._read_directory_abstract(dir_uri, ctx=ctx)
                overview = await self._read_directory_overview(dir_uri, ctx=ctx)
                if not abstract and not overview:
                    raise OpenVikingError(
                        "Empty semantic generation requires explicit clear lifecycle",
                        code="FAILED_PRECONDITION",
                    )
                await vectorize_directory_meta(
                    dir_uri,
                    abstract,
                    overview,
                    ctx=self._content_owner_ctx(dir_uri, ctx),
                    include_abstract=bool(abstract),
                    include_overview=bool(overview),
                )
                return
            abstract = await self._read_directory_abstract(dir_uri, ctx=ctx)
            if level == ContextLevel.ABSTRACT:
                vector_text = abstract
            else:
                overview = await self._read_directory_overview(dir_uri, ctx=ctx)
                vector_text = overview or abstract

            if not vector_text:
                await self.delete_uri_level(uri=dir_uri, level=level, ctx=ctx)
                return

            # L1 abstract scalar carries the overview for Rerank.
            if level == ContextLevel.OVERVIEW:
                abstract = _truncate_abstract_bytes(vector_text)

            await self._upsert_context(
                uri=dir_uri,
                parent_uri=VikingURI(dir_uri).parent.uri,
                abstract=abstract,
                vector_text=vector_text,
                is_leaf=False,
                context_type=context_type_for_uri(dir_uri),
                level=level,
                ctx=ctx,
            )
        finally:
            await viking_fs._async_agfs.pathlock_release(lease)

    async def delete_uri_level(self, *, uri: str, level: ContextLevel, ctx: RequestContext) -> int:
        """Delete ONLY the vector record at ``(uri, level)``. Returns count.

        Used by git restore for both directory markers (dir + L0/L1) and
        deleted source files (file + DETAIL).
        """
        service = get_service()
        assert service.vikingdb_manager is not None
        total = 0
        previous_ids = set()
        while True:
            records = await service.vikingdb_manager.get_context_by_uri(
                uri=uri, level=int(level), limit=100, ctx=ctx
            )
            ids = [str(rec["id"]) for rec in records if rec.get("id")]
            if not ids:
                return total
            if set(ids) & previous_ids:
                raise RuntimeError("Vector deletion did not remove all URI/level records")
            deleted = await service.vikingdb_manager.delete(ids, ctx=ctx)
            if int(deleted or 0) != len(ids):
                raise RuntimeError("Partial vector deletion; URI cleanup is incomplete")
            total += int(deleted)
            previous_ids.update(ids)

    async def _reindex_user_namespace(
        self,
        *,
        uri: str,
        mode: str,
        run: _ReindexRunContext,
    ) -> None:
        counters = run.counters
        ctx = run.ctx
        normalized_uri = uri.rstrip("/")
        target_root = normalized_uri if normalized_uri else uri
        viking_fs = get_viking_fs()
        try:
            entries = await self._tree_all(viking_fs, target_root, show_all_hidden=True, ctx=ctx)
        except Exception as exc:
            raise NotFoundError(uri, "resource") from exc

        if target_root == "viking://user":
            user_roots = [
                entry.get("uri")
                for entry in entries
                if entry.get("isDir") and classify_uri(entry.get("uri", "")).is_user_namespace_root
            ]
            if user_roots:
                for user_root in sorted(set(user_roots)):
                    await self._reindex_user_namespace(
                        uri=user_root,
                        mode=mode,
                        run=run,
                    )
                return

        memory_roots: list[str] = []
        skill_roots: list[str] = []
        resource_directories: list[str] = []
        resource_files: list[str] = []

        for entry in entries:
            entry_uri = entry.get("uri")
            if not entry_uri:
                continue
            if is_session_uri(entry_uri):
                continue
            classification = classify_uri(entry_uri)
            if classification.is_memory:
                if entry.get("isDir") and classification.is_memory_root:
                    memory_roots.append(entry_uri)
                continue
            if classification.is_skill:
                if entry.get("isDir") and classification.is_skill_root:
                    skill_roots.append(entry_uri)
                continue
            if not self._is_resource_entry_for_namespace(entry_uri, target_root):
                continue
            if entry.get("isDir"):
                resource_directories.append(entry_uri)
            elif not self._is_hidden_meta_file(entry_uri):
                resource_files.append(entry_uri)

        resource_files = await self._drop_navigation_indexes(resource_files, ctx=ctx)

        for memory_root in sorted(set(memory_roots)):
            memory_mode = (
                "semantic_and_vectors" if mode == "semantic_and_vectors" else "vectors_only"
            )
            await self._reindex_memory(
                uri=memory_root,
                mode=memory_mode,
                run=run,
            )

        for skill_root in sorted(set(skill_roots)):
            skill_mode = (
                "semantic_and_vectors" if mode == "semantic_and_vectors" else "vectors_only"
            )
            await self._reindex_skill(
                uri=skill_root,
                mode=skill_mode,
                run=run,
            )

        image_summaries: dict = {}
        if mode == "semantic_and_vectors":
            (
                resource_directories,
                resource_files,
                image_summaries,
            ) = await self._refresh_namespace_resource_semantics(
                target_root=target_root,
                directories=resource_directories,
                files=resource_files,
                run=run,
            )

        await self._reindex_resource_vectors_from_entries(
            **self._with_ingest_options(
                {
                    "root_uri": target_root,
                    "directories": resource_directories,
                    "files": resource_files,
                    "counters": counters,
                    "ctx": ctx,
                    "image_summaries": image_summaries,
                },
                run.ingest_options,
            )
        )

    async def _reindex_global_namespace(
        self,
        *,
        uri: str,
        mode: str,
        run: _ReindexRunContext,
    ) -> None:
        counters = run.counters
        ctx = run.ctx
        target_root = "viking://"
        viking_fs = get_viking_fs()
        try:
            entries = await self._tree_all(viking_fs, target_root, show_all_hidden=True, ctx=ctx)
        except Exception as exc:
            raise NotFoundError(uri, "resource") from exc

        user_roots: list[str] = []
        resource_directories: list[str] = []
        resource_files: list[str] = []

        for entry in entries:
            entry_uri = entry.get("uri")
            if not entry_uri:
                continue
            if entry_uri == "viking://user":
                continue
            if entry_uri.startswith("viking://user/"):
                remainder = entry_uri[len("viking://user/") :]
                if entry.get("isDir") and remainder and "/" not in remainder:
                    user_roots.append(entry_uri)
                continue
            if is_session_uri(entry_uri):
                continue
            if not self._is_global_resource_entry(entry_uri):
                continue
            if entry.get("isDir"):
                resource_directories.append(entry_uri)
            elif not self._is_hidden_meta_file(entry_uri):
                resource_files.append(entry_uri)

        resource_files = await self._drop_navigation_indexes(resource_files, ctx=ctx)

        for user_root in sorted(set(user_roots)):
            await self._reindex_user_namespace(
                uri=user_root,
                mode=mode,
                run=run,
            )

        image_summaries: dict = {}
        if mode == "semantic_and_vectors":
            (
                resource_directories,
                resource_files,
                image_summaries,
            ) = await self._refresh_namespace_resource_semantics(
                target_root=target_root,
                directories=resource_directories,
                files=resource_files,
                run=run,
            )

        await self._reindex_resource_vectors_from_entries(
            **self._with_ingest_options(
                {
                    "root_uri": target_root,
                    "directories": resource_directories,
                    "files": resource_files,
                    "counters": counters,
                    "ctx": ctx,
                    "image_summaries": image_summaries,
                },
                run.ingest_options,
            )
        )

    async def _reindex_skill_vectors(
        self,
        *,
        uri: str,
        counters: _ReindexCounters,
        ctx: RequestContext,
        recursive: bool = True,
        ingest_options: IngestOptions | None = None,
    ) -> None:
        viking_fs = get_viking_fs()
        counters.scanned_records += 1

        abstract = await self._read_directory_abstract(uri, ctx=ctx)
        overview = await self._read_directory_overview(uri, ctx=ctx)
        if not abstract:
            record = await self._fetch_existing_record(
                uri=uri,
                level=0,
                ctx=self._content_owner_ctx(uri, ctx),
            )
            abstract = self._record_abstract(record)
        if not overview:
            record = await self._fetch_existing_record(
                uri=uri,
                level=1,
                ctx=self._content_owner_ctx(uri, ctx),
            )
            overview = self._record_abstract(record) or abstract

        if not abstract and not overview:
            counters.unsupported_records += 1
            counters.warnings.append(f"No semantic source found for {uri}")
            return

        parent_uri = VikingURI(uri).parent.uri
        if abstract:
            try:
                await self._upsert_context(
                    uri=uri,
                    parent_uri=parent_uri,
                    abstract=abstract,
                    vector_text=embedding_text_for_body(ContextLevel.ABSTRACT, uri, abstract),
                    is_leaf=False,
                    context_type=ContextType.SKILL.value,
                    level=ContextLevel.ABSTRACT,
                    meta=await self._skill_meta(uri=uri, abstract=abstract, ctx=ctx),
                    ctx=ctx,
                    ingest_options=ingest_options,
                )
                counters.rebuilt_records += 1
            except Exception as exc:
                counters.failed_records += 1
                counters.warnings.append(f"Failed to reindex {uri} L0 vector: {exc}")
        if overview:
            try:
                await self._upsert_context(
                    uri=uri,
                    parent_uri=parent_uri,
                    # L1 abstract scalar carries the overview for Rerank.
                    abstract=_truncate_abstract_bytes(overview),
                    vector_text=embedding_text_for_body(ContextLevel.OVERVIEW, uri, overview),
                    is_leaf=False,
                    context_type=ContextType.SKILL.value,
                    level=ContextLevel.OVERVIEW,
                    meta=await self._skill_meta(uri=uri, abstract=abstract, ctx=ctx),
                    ctx=ctx,
                    ingest_options=ingest_options,
                )
                counters.rebuilt_records += 1
            except Exception as exc:
                counters.failed_records += 1
                counters.warnings.append(f"Failed to reindex {uri} L1 vector: {exc}")

        skill_file_uri = f"{uri}/SKILL.md"
        if recursive and await viking_fs.exists(skill_file_uri, ctx=ctx):
            counters.scanned_records += 1
            skill_content = await self._safe_read_text(skill_file_uri, ctx=ctx)
            if skill_content:
                detail_abstract = self._prefer_non_empty(abstract, skill_content)
                try:
                    await self._upsert_context(
                        uri=skill_file_uri,
                        parent_uri=uri,
                        abstract=detail_abstract,
                        vector_text=skill_content,
                        is_leaf=True,
                        context_type=ContextType.SKILL.value,
                        level=ContextLevel.DETAIL,
                        ctx=ctx,
                        ingest_options=ingest_options,
                    )
                    counters.rebuilt_records += 1
                except Exception as exc:
                    counters.failed_records += 1
                    counters.warnings.append(f"Failed to reindex {skill_file_uri} vector: {exc}")

    async def _reindex_memory_vectors(
        self,
        *,
        uri: str,
        counters: _ReindexCounters,
        ctx: RequestContext,
        recursive: bool = True,
        ingest_options: IngestOptions | None = None,
    ) -> None:
        viking_fs = get_viking_fs()
        if await viking_fs.exists(uri, ctx=ctx):
            stat = await viking_fs.stat(uri, ctx=ctx, skip_count=True)
            if stat.get("isDir", stat.get("is_dir")):
                entries = (
                    await self._tree_all(viking_fs, uri, show_all_hidden=False, ctx=ctx)
                    if recursive
                    else []
                )
                directory_uris = {uri}
                for entry in entries:
                    entry_uri = entry.get("uri")
                    if entry_uri and entry.get("isDir"):
                        directory_uris.add(entry_uri)
                await self._reindex_memory_directory_chain(
                    **self._with_ingest_options(
                        {
                            "directory_uris": sorted(directory_uris),
                            "counters": counters,
                            "ctx": ctx,
                        },
                        ingest_options,
                    )
                )
                file_uris = [entry["uri"] for entry in entries if not entry.get("isDir")]
            else:
                file_uris = [uri]
        else:
            raise NotFoundError(uri, "memory")

        async def process_file(file_uri: str) -> _ReindexCounters:
            file_counters = _ReindexCounters(scanned_records=1)
            body_source = await self._read_memory_body(file_uri, ctx=ctx)
            if body_source.error:
                file_counters.failed_records += 1
                file_counters.warnings.append(
                    f"Skipped {file_uri}: failed to read memory body: {body_source.error}"
                )
                return file_counters
            body = body_source.text if body_source.exists else ""
            memory_content = MemoryFileUtils.read(body).content if body else ""
            existing = await self._fetch_existing_record(
                uri=file_uri,
                level=2,
                ctx=self._content_owner_ctx(file_uri, ctx),
            )
            abstract = self._best_non_empty(
                self._record_abstract(existing),
                await self._best_file_summary(file_uri, ctx=ctx),
            )
            if not body and existing is None:
                file_counters.unsupported_records += 1
                file_counters.warnings.append(f"No memory source found for {file_uri}")
                return file_counters

            parent_uri = VikingURI(file_uri.split("#", 1)[0]).parent.uri
            if body:
                detail_abstract = self._prefer_non_empty(abstract, memory_content, body)
                try:
                    await self._upsert_context(
                        uri=file_uri,
                        parent_uri=parent_uri,
                        abstract=detail_abstract,
                        vector_text=body,
                        is_leaf=True,
                        context_type=ContextType.MEMORY.value,
                        level=ContextLevel.DETAIL,
                        ctx=ctx,
                        ingest_options=ingest_options,
                    )
                    file_counters.rebuilt_records += 1
                except Exception as exc:
                    file_counters.failed_records += 1
                    file_counters.warnings.append(f"Failed to reindex {file_uri} vector: {exc}")
                return file_counters

            try:
                await self._upsert_context(
                    uri=file_uri,
                    parent_uri=parent_uri,
                    abstract=abstract,
                    vector_text=abstract,
                    is_leaf=True,
                    context_type=ContextType.MEMORY.value,
                    level=ContextLevel.DETAIL,
                    ctx=ctx,
                    ingest_options=ingest_options,
                )
                file_counters.rebuilt_records += 1
                file_counters.warnings.append(
                    f"Reindexed {file_uri} from abstract fallback because original memory body is unavailable"
                )
            except Exception as exc:
                file_counters.failed_records += 1
                file_counters.warnings.append(f"Failed to reindex {file_uri} vector: {exc}")
            return file_counters

        concurrency = self._effective_file_vectorization_concurrency()
        if file_uris:
            logger.info(
                "Reindex memory file vectorization: root=%s files=%d concurrency=%d",
                uri,
                len(file_uris),
                concurrency,
            )
        await self._run_ordered_counter_batches(
            file_uris,
            concurrency=concurrency,
            processor=process_file,
            counters=counters,
        )

    async def _reindex_memory_directory_chain(
        self,
        *,
        directory_uris: Iterable[str],
        counters: _ReindexCounters,
        ctx: RequestContext,
        ingest_options: IngestOptions | None = None,
    ) -> None:
        for directory_uri in directory_uris:
            counters.scanned_records += 1
            abstract = await self._read_directory_abstract(directory_uri, ctx=ctx)
            overview = await self._read_directory_overview(directory_uri, ctx=ctx)
            if not abstract and not overview:
                continue

            parent_uri = VikingURI(directory_uri).parent.uri
            if abstract:
                try:
                    await self._upsert_context(
                        uri=directory_uri,
                        parent_uri=parent_uri,
                        abstract=abstract,
                        vector_text=embedding_text_for_body(
                            ContextLevel.ABSTRACT, directory_uri, abstract
                        ),
                        is_leaf=False,
                        context_type=ContextType.MEMORY.value,
                        level=ContextLevel.ABSTRACT,
                        ctx=ctx,
                        ingest_options=ingest_options,
                    )
                    counters.rebuilt_records += 1
                except Exception as exc:
                    counters.failed_records += 1
                    counters.warnings.append(f"Failed to reindex {directory_uri} L0 vector: {exc}")
            if overview:
                try:
                    await self._upsert_context(
                        uri=directory_uri,
                        parent_uri=parent_uri,
                        # L1 abstract scalar carries the overview for Rerank.
                        abstract=_truncate_abstract_bytes(overview),
                        vector_text=embedding_text_for_body(
                            ContextLevel.OVERVIEW, directory_uri, overview
                        ),
                        is_leaf=False,
                        context_type=ContextType.MEMORY.value,
                        level=ContextLevel.OVERVIEW,
                        ctx=ctx,
                        ingest_options=ingest_options,
                    )
                    counters.rebuilt_records += 1
                except Exception as exc:
                    counters.failed_records += 1
                    counters.warnings.append(f"Failed to reindex {directory_uri} L1 vector: {exc}")

    async def _regenerate_skill_semantics(self, *, uri: str, ctx: RequestContext) -> None:
        service = get_service()
        if service.viking_fs is None or service.vikingdb_manager is None:
            raise RuntimeError("OpenVikingService not initialized")

        viking_fs = service.viking_fs
        skill_file_uri = f"{uri}/SKILL.md"
        skill_content = await self._safe_read_text(skill_file_uri, ctx=ctx)
        if not skill_content:
            raise OpenVikingError(
                f"SKILL.md not found for {uri}",
                code="NOT_FOUND",
                details={"uri": uri},
            )

        skill_dict, _, _, _ = SkillProcessor(service.vikingdb_manager)._parse_skill(
            skill_content,
            allow_local_path_resolution=False,
        )
        overview = await SkillProcessor(service.vikingdb_manager)._generate_overview(
            skill_dict,
            get_openviking_config(),
        )
        await viking_fs.write_context(
            uri=uri,
            content=skill_content,
            abstract=skill_dict.get("description", ""),
            overview=overview,
            content_filename="SKILL.md",
            is_leaf=False,
            ctx=ctx,
        )

    async def _read_directory_abstract(self, uri: str, *, ctx: RequestContext) -> str:
        try:
            value = await get_viking_fs().abstract(uri, ctx=ctx)
        except Exception:
            return ""
        return "" if _is_not_ready_sentinel(value, _ABSTRACT_NOT_READY_SUFFIX) else value

    async def _read_directory_overview(self, uri: str, *, ctx: RequestContext) -> str:
        try:
            value = await get_viking_fs().overview(uri, ctx=ctx)
        except Exception:
            return ""
        return "" if _is_not_ready_sentinel(value, _OVERVIEW_NOT_READY_SUFFIX) else value

    async def _best_file_summary(self, uri: str, *, ctx: RequestContext) -> str:
        parent_uri = VikingURI(uri).parent.uri
        file_name = uri.rsplit("/", 1)[-1]
        overviews = await self._safe_read_text(f"{parent_uri}/.overview.md", ctx=ctx)
        if overviews:
            parsed = self._parse_overview_md(body_for_preview(overviews))
            if file_name in parsed:
                return parsed[file_name]
        existing = await self._fetch_existing_record(
            uri=uri,
            level=2,
            ctx=self._content_owner_ctx(uri, ctx),
        )
        return self._record_abstract(existing)

    async def _best_resource_file_vector_text(
        self,
        uri: str,
        summary: str,
        ctx: RequestContext,
    ) -> str:
        existing = await self._fetch_existing_record(
            uri=uri,
            level=2,
            ctx=self._content_owner_ctx(uri, ctx),
        )
        fallback = self._record_abstract(existing)
        content_type = get_resource_content_type(uri.rsplit("/", 1)[-1])

        if content_type == ResourceContentType.TEXT:
            embedding_config = get_openviking_config().embedding
            text_source = embedding_config.text_source
            if text_source in SUMMARY_TEXT_SOURCES and summary:
                return summary
            content = await self._safe_read_text(uri, ctx=ctx)
            if content:
                return truncate_embedding_input(content, embedding_config.max_input_tokens)
            return summary or fallback

        if summary:
            return summary
        return fallback

    async def _upsert_context(
        self,
        *,
        uri: str,
        parent_uri: str,
        abstract: str,
        vector_text: str,
        is_leaf: bool,
        context_type: str,
        level: ContextLevel,
        ctx: RequestContext,
        meta: Optional[dict[str, Any]] = None,
        ingest_options: IngestOptions | None = None,
    ) -> None:
        service = get_service()
        assert service.vikingdb_manager is not None
        merged_meta = dict(meta or {})
        owner_ctx = self._content_owner_ctx(uri, ctx)

        context = Context(
            uri=uri,
            parent_uri=parent_uri,
            is_leaf=is_leaf,
            abstract=abstract or "",
            context_type=context_type,
            level=level,
            user=owner_ctx.user,
            account_id=owner_ctx.account_id,
            owner_space=owner_space_for_uri(uri),
            meta=merged_meta,
        )
        context.set_vectorize(Vectorize(text=vector_text))
        msg = EmbeddingMsgConverter.from_context(context)
        _apply_ingest_options(msg, ingest_options)
        if msg is None:
            raise OpenVikingError(
                f"No vector text generated for {uri}",
                code="FAILED_PRECONDITION",
                details={"uri": uri},
            )
        wait_tracker = get_request_wait_tracker()
        wait_tracker.register_embedding_root(msg.telemetry_id, msg.id)
        enqueued = await service.vikingdb_manager.enqueue_embedding_msg(msg)
        if not enqueued:
            wait_tracker.mark_embedding_failed(
                msg.telemetry_id,
                msg.id,
                f"Failed to enqueue reindex vector for {uri}",
            )
            raise OpenVikingError(
                f"Failed to enqueue reindex vector for {uri}",
                code="PROCESSING_ERROR",
                details={"uri": uri, "level": int(level)},
            )

    async def _fetch_existing_record(
        self,
        *,
        uri: str,
        level: int,
        ctx: RequestContext,
    ) -> Optional[dict[str, Any]]:
        service = get_service()
        assert service.vikingdb_manager is not None
        records = await service.vikingdb_manager.get_context_by_uri(
            uri=uri,
            level=level,
            limit=1,
            ctx=ctx,
        )
        return records[0] if records else None

    async def _skill_meta(
        self,
        *,
        uri: str,
        abstract: str,
        ctx: RequestContext,
    ) -> dict[str, Any]:
        name = uri.rstrip("/").split("/")[-1]
        return {"name": name, "description": abstract}

    def _record_abstract(self, record: Optional[dict[str, Any]]) -> str:
        if not record:
            return ""
        return str(record.get("abstract") or "")

    def _is_hidden_meta_file(self, uri: str) -> bool:
        return (
            uri.rsplit("/", 1)[-1] in CHAPTER_METADATA_FILENAMES
            or uri.endswith("/.abstract.md")
            or uri.endswith("/.overview.md")
        )

    async def _is_navigation_index(self, uri: str, *, ctx: RequestContext) -> bool:
        """Return whether ``uri`` is a generated per-directory navigation index.

        ``index.md`` is reserved for that catalogue, but corpora published before
        the feature use it for a branch chapter's own prose.  The frontmatter
        marker decides, so legacy chapter bodies keep their vectors.
        """

        from openviking.core.directory_index import is_generated_index_at

        return await is_generated_index_at(get_viking_fs(), uri, ctx=ctx)

    async def _drop_navigation_indexes(
        self,
        uris: list[str],
        *,
        ctx: RequestContext,
    ) -> list[str]:
        return [uri for uri in uris if not await self._is_navigation_index(uri, ctx=ctx)]

    async def _safe_read_text(self, uri: str, *, ctx: RequestContext) -> str:
        viking_fs = get_viking_fs()
        try:
            if not await viking_fs.exists(uri, ctx=ctx):
                return ""
            content = await viking_fs.read_file(uri, ctx=ctx)
            if isinstance(content, bytes):
                return content.decode("utf-8", errors="replace")
            return str(content or "")
        except Exception:
            return ""

    async def _read_memory_body(self, uri: str, *, ctx: RequestContext) -> _PruneSourceRead:
        return await self._read_prune_source(uri, ctx=ctx)

    def _chunk_memory_body(self, uri: str, body: str) -> Iterable[tuple[str, str]]:
        semantic = get_openviking_config().semantic
        chunk_chars = semantic.memory_chunk_chars
        overlap = semantic.memory_chunk_overlap
        if len(body) <= chunk_chars:
            return []

        chunks: list[str] = []
        start = 0
        while start < len(body):
            previous_start = start
            end = start + chunk_chars
            if end < len(body):
                boundary = body.rfind("\n\n", start, end)
                if boundary > start + chunk_chars // 2:
                    end = boundary + 2
            chunks.append(body[start:end].strip())
            start = end - overlap
            if start <= previous_start:
                start = previous_start + 1
            if start >= len(body):
                break

        return [(f"{uri}#chunk_{idx:04d}", chunk) for idx, chunk in enumerate(chunks) if chunk]

    def _best_non_empty(self, *values: str) -> str:
        for value in values:
            if value:
                return value
        return ""

    def _prefer_non_empty(self, *values: str) -> str:
        for value in values:
            if value:
                return value
        return ""

    @staticmethod
    def _parse_overview_md(content: str) -> dict[str, str]:
        parsed: dict[str, str] = {}
        current_name: Optional[str] = None
        current_lines: list[str] = []
        for line in (content or "").splitlines():
            if line.startswith("## "):
                if current_name is not None:
                    parsed[current_name] = "\n".join(current_lines).strip()
                current_name = line[3:].strip()
                current_lines = []
                continue
            if current_name is not None:
                current_lines.append(line)
        if current_name is not None:
            parsed[current_name] = "\n".join(current_lines).strip()
        return parsed
