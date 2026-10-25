# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Semantic DAG executor with event-driven lazy dispatch."""

import asyncio
import re
import threading
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List, Optional, Set
from weakref import WeakKeyDictionary

from openviking.indexing.batch import bind_text_batch, producer_batch_context
from openviking.indexing.content import draft_sources
from openviking.parse.parsers.media import get_media_type
from openviking.server.identity import RequestContext
from openviking.service.task_work_index import bind_task_context, get_task_context
from openviking.storage.abstract_overview import (
    AbstractOverviewFormatError,
    AbstractOverviewWriteResult,
    body_for_preview,
    deterministic_sample,
    freshness_metadata,
    read_abstract_overview_pending_snapshot,
    write_abstract_overview,
)
from openviking.storage.acl import CreatorAclGrant
from openviking.storage.errors import LockAcquisitionError
from openviking.core.directory_index import (
    DIRECTORY_INDEX_FILENAME,
    DirectoryIndexEntry,
    is_directory_index_uri,
    is_generated_directory_index,
    one_line,
    navigation_description,
    read_head as _read_head_for_index,
    write_directory_index,
)
from openviking.storage.internal_names import CHAPTER_METADATA_FILENAMES
from openviking.storage.viking_fs import LS_ALL_NODES, get_viking_fs
from openviking.telemetry import bind_telemetry, get_current_telemetry
from openviking.utils.ingest_options import IngestOptions
from openviking_cli.exceptions import NotFoundError
from openviking_cli.utils import VikingURI
from openviking_cli.utils.config import get_openviking_config
from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)

# Marker written by the pre-fix overview path when the model returned nothing.
# Sidecars that contain it carry no content, so they must be replaced (when the
# model answers) or removed - never kept as if they were real L0/L1 text.
_PLACEHOLDER_OVERVIEW_MARKER = "[Directory overview is not generated]"

# ``remove_files`` takes its path lock without waiting, and a sidecar delete
# regularly lands while a sibling node still holds the directory lock; retry a
# few times so a lost race does not leave the placeholder behind for good.
_STALE_SIDECAR_REMOVE_ATTEMPTS = 4


def placeholder_sidecar_names(
    existing_overview: Optional[str], existing_abstract: Optional[str]
) -> list[str]:
    """Return the sidecar file names whose body is only the placeholder marker.

    ``semantic_dag`` skips directories whose overview generation returned no
    content, but a directory that already carries a placeholder sidecar from the
    pre-fix path still has files "on disk" - they must be removed, not kept.
    """

    return [
        name
        for name, body in (
            (".overview.md", existing_overview),
            (".abstract.md", existing_abstract),
        )
        if body is not None and _PLACEHOLDER_OVERVIEW_MARKER in body
    ]


def strip_placeholder_sidecars(
    overview: Optional[str], abstract: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """Treat placeholder sidecars as missing so the directory is refreshed.

    The incremental path skips a directory whose ``.overview.md`` and
    ``.abstract.md`` both exist, so a placeholder written by the pre-fix path
    would never be revisited - and never replaced.  Reporting it as absent
    forces the directory through generation again, where a real answer
    overwrites it and a failed answer removes it.
    """

    stale = set(placeholder_sidecar_names(overview, abstract))
    return (
        None if ".overview.md" in stale else overview,
        None if ".abstract.md" in stale else abstract,
    )


# Session-internal files that should never be summarized by the semantic pipeline.
# These are canonical archives (e.g. session transcripts) whose content provides
# no additional retrieval value and would only waste tokens and add latency.
_SKIP_FILENAMES = frozenset({"messages.jsonl", *CHAPTER_METADATA_FILENAMES})


@dataclass
class DirNode:
    """Directory node state for DAG execution."""

    uri: str
    children_dirs: List[str]
    file_paths: List[str]
    file_index: Dict[str, int]
    child_index: Dict[str, int]
    file_summaries: List[Optional[Dict[str, str]]]
    children_abstracts: List[Optional[Dict[str, str]]]
    pending: int
    pending_snapshot: int = 0
    sampled_children_dirs: Optional[Set[str]] = None
    sampled_file_paths: Optional[Set[str]] = None
    total_entries: Optional[int] = None
    missing_summary_entries: Optional[int] = None
    transfer_inputs_ready: bool = True
    dispatched: bool = False
    overview_scheduled: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class DagStats:
    total_nodes: int = 0
    pending_nodes: int = 0
    in_progress_nodes: int = 0
    done_nodes: int = 0
    lock_skipped_nodes: int = 0
    overview_skipped_nodes: int = 0
    # Directories whose stale placeholder sidecars could not be deleted while the
    # walk held their lock; the caller removes them once the walk is over.
    placeholder_sidecar_dirs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DagWork:
    """A scheduled unit of DAG work."""

    kind: str
    dir_uri: str
    parent_uri: Optional[str] = None
    file_path: Optional[str] = None


@dataclass(frozen=True)
class ScheduledDagWork:
    executor: "SemanticDagExecutor"
    work: DagWork


class SemanticNodeScheduler:
    """Shared node executor for semantic DAG work in one event loop."""

    _idle_timeout = 0.05

    def __init__(self, max_workers: int):
        self._max_workers = max(1, max_workers)
        self._queue: asyncio.Queue[ScheduledDagWork] = asyncio.Queue()
        self._workers: Set[asyncio.Task] = set()

    def configure(self, max_workers: int) -> None:
        self._max_workers = max(1, max_workers)
        self._ensure_workers()

    def submit(self, executor: "SemanticDagExecutor", work: DagWork) -> None:
        self._queue.put_nowait(ScheduledDagWork(executor=executor, work=work))
        self._ensure_workers()

    def _ensure_workers(self) -> None:
        self._workers = {task for task in self._workers if not task.done()}
        if not self._queue.qsize():
            return
        # Size the pool to the work that is waiting *plus* what is already in
        # flight. Sizing it to the raw queue depth let the pool collapse every
        # time an await drained the queue, which serialised the DAG (measured
        # ~0.6 concurrent LLM calls against a configured width of 8).
        target = min(self._max_workers, len(self._workers) + self._queue.qsize())
        while len(self._workers) < target:
            task = asyncio.create_task(self._worker())
            task.add_done_callback(self._workers.discard)
            self._workers.add(task)

    async def _worker(self) -> None:
        while True:
            try:
                item = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=self._idle_timeout,
                )
            except asyncio.TimeoutError:
                if self._queue.empty():
                    return
                continue

            item.executor._start_scheduled_work()
            try:
                if not item.executor.closed:
                    await item.executor._run_work(item.work)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                item.executor.fail(exc)
            finally:
                item.executor._finish_scheduled_work()
                self._queue.task_done()


_node_schedulers: "WeakKeyDictionary[asyncio.AbstractEventLoop, SemanticNodeScheduler]" = (
    WeakKeyDictionary()
)
# One LLM semaphore per event loop, shared by every executor on that loop. It
# used to be created per executor, i.e. per directory message, so the effective
# limit was messages x max_concurrent_llm instead of max_concurrent_llm.
_llm_semaphores: "WeakKeyDictionary[asyncio.AbstractEventLoop, Dict[int, asyncio.Semaphore]]" = (
    WeakKeyDictionary()
)


def get_llm_semaphore(max_concurrent: int) -> asyncio.Semaphore:
    """The shared LLM semaphore for this loop, bounded by ``max_concurrent``."""
    loop = asyncio.get_running_loop()
    limit = max(1, int(max_concurrent))
    by_limit = _llm_semaphores.setdefault(loop, {})
    semaphore = by_limit.get(limit)
    if semaphore is None:
        semaphore = asyncio.Semaphore(limit)
        by_limit[limit] = semaphore
    return semaphore


async def schedule_parent_aggregation(parent_uri, *, changed_files=None, account_id=None, user_id=None, context_type="resource"):
    """Queue one parent aggregation for a directory whose L2 chunks just landed.

    Called from the embedding batch commit, i.e. *after* the chunk vectors and
    their receipts are committed, so the directory holds its final content. The
    caller deduplicates by parent directory (one call per batch per directory) and
    the semantic queue coalesces again by key.

    Identity defaults to the default account/user because the vector phase has no
    request context of its own; a caller that has one should pass it.
    """
    if not parent_uri or not isinstance(parent_uri, str):
        return False
    parent_uri = parent_uri.rstrip("/")
    if not parent_uri or parent_uri in {"viking:", "viking://", "viking://user", "viking://agent"}:
        return False
    from openviking.storage.queuefs import get_queue_manager
    from openviking.storage.queuefs.semantic_msg import SemanticMsg, build_semantic_coalesce_key

    account_id = account_id or "default"
    user_id = user_id or "default"
    queue_manager = get_queue_manager()
    semantic_queue = queue_manager.get_queue(queue_manager.SEMANTIC, allow_create=True)
    msg = SemanticMsg(
        uri=parent_uri,
        # target_uri must equal uri for the incremental path to engage
        # (`_is_direct_incremental_update` checks it); without it the DAG takes
        # the non-incremental route and reports no abstract body change, so the
        # refresh never propagates to the parent.
        target_uri=parent_uri,
        context_type=context_type,
        recursive=False,
        account_id=account_id,
        user_id=user_id,
        role="user",
        skip_vectorization=False,
        # The DAG needs the changed files to summarise; without them the
        # directory's own abstract does not change and the refresh stops
        # propagating upward.
        changes={"modified": list(changed_files or ())},
        generation_trigger="l2_complete",
        aggregate_directory=True,
        coalesce_key=build_semantic_coalesce_key(
            context_type=context_type,
            uri=parent_uri,
            account_id=account_id,
            user_id=user_id,
        ),
    )
    await semantic_queue.enqueue(msg)
    return True


def get_semantic_node_scheduler(max_workers: int) -> SemanticNodeScheduler:
    loop = asyncio.get_running_loop()
    scheduler = _node_schedulers.get(loop)
    if scheduler is None:
        scheduler = SemanticNodeScheduler(max_workers=max_workers)
        _node_schedulers[loop] = scheduler
    else:
        scheduler.configure(max_workers)
    return scheduler


class SemanticDagExecutor:
    """Execute semantic generation with DAG-style, event-driven lazy dispatch."""

    _active_lock: ClassVar[threading.Lock] = threading.Lock()
    _active_executors: ClassVar[Set["SemanticDagExecutor"]] = set()

    def __init__(
        self,
        processor: "SemanticProcessor",
        context_type: str,
        max_concurrent_llm: int,
        ctx: RequestContext,
        incremental_update: bool = False,
        target_uri: Optional[str] = None,
        target_preexisting: Optional[bool] = None,
        recursive: bool = True,
        lock: Optional[Dict[str, Any]] = None,
        is_code_repo: bool = False,
        changes: Optional[Dict[str, List[str]]] = None,
        skip_vectorization: bool = False,
        ingest_options: IngestOptions | None = None,
        coalesce_key: str = "",
        coalesce_version: int = 0,
        source: Optional[Dict[str, str]] = None,
        generation_trigger: str = "semantic_refresh",
        aggregate_directory: bool = True,
        copy_source_uri: str = "",
        allow_vlm: bool = True,
    ):
        self._processor = processor
        self._context_type = context_type
        self._ctx = ctx
        self._incremental_update = incremental_update
        self._target_uri = target_uri
        self._target_preexisting = target_preexisting
        self._recursive = recursive
        self._lock = lock
        self._is_code_repo = is_code_repo
        self._changes = changes or {}
        self._skip_vectorization = skip_vectorization
        self._ingest_options = IngestOptions.from_value(ingest_options)
        self._coalesce_key = coalesce_key
        self._coalesce_version = coalesce_version
        self._source = dict(source) if source else None
        self._generation_trigger = generation_trigger
        self._aggregate_directory = aggregate_directory
        self._copy_source_uri = copy_source_uri
        self._allow_vlm = bool(allow_vlm)
        self._publication_batch = producer_batch_context()
        self._task_context = get_task_context()
        self._telemetry = get_current_telemetry()
        self._stale = False
        self._changed_paths = {
            path for key in ("added", "modified", "deleted") for path in self._changes.get(key, [])
        }
        self._added_paths = {path.rstrip("/") for path in self._changes.get("added", [])}
        self._node_concurrency = max(1, max_concurrent_llm)
        self._llm_sem = get_llm_semaphore(max_concurrent_llm)
        self._viking_fs = get_viking_fs()
        self._nodes: Dict[str, DirNode] = {}
        self.image_summaries: Dict[str, dict] = {}
        # Files whose own vectorization is finished or deliberately skipped. A
        # caption written into one of them after that point has to re-enqueue it:
        # the archived revision and the chunk vectors were built without the
        # caption, and readers/lexical search serve the archived revision. A file
        # still pending here will simply read the captioned bytes in its own pass.
        self._file_vector_settled: set[str] = set()
        # Markdown rewrote by this run's caption backfill, so N figures in one
        # chapter cost one re-vectorization instead of N.
        self._recaptioned_markdown: set[str] = set()
        # Images whose joint row was enqueued against the markdown *before* this run
        # wrote their caption, so each owes exactly one rebuild.
        self._recaptioned_images: set[str] = set()
        self._parent: Dict[str, Optional[str]] = {}
        self._root_uri: Optional[str] = None
        self._root_done: Optional[asyncio.Event] = None
        self._scheduler: Optional[SemanticNodeScheduler] = None
        self._active_scheduled_work = 0
        self._active_work_idle = asyncio.Event()
        self._active_work_idle.set()
        self._closed = False
        self._failure: Optional[Exception] = None
        self._stats = DagStats()
        self._file_change_status: Dict[str, bool] = {}
        self._dir_change_status: Dict[str, bool] = {}
        self._overview_cache: Dict[str, Dict[str, str]] = {}
        self._overview_cache_lock = asyncio.Lock()
        self._root_write_result = AbstractOverviewWriteResult(wrote=False)

    def _creator_acl_grant(self, uri: str) -> CreatorAclGrant | None:
        normalized = uri.rstrip("/")
        if self._generation_trigger == "resource_ingest" and self._target_preexisting is False:
            root = self._root_uri.rstrip("/")
            if normalized == root:
                return CreatorAclGrant.DIRECT
            if normalized.startswith(f"{root}/"):
                return CreatorAclGrant.INHERITED
        return CreatorAclGrant.DIRECT if normalized in self._added_paths else None

    async def run(self, root_uri: str) -> None:
        """Run DAG execution starting from root_uri."""
        self._root_uri = root_uri
        self._root_done = asyncio.Event()
        self._scheduler = get_semantic_node_scheduler(self._node_concurrency)

        try:
            self._register_active()
            self._schedule_dir(root_uri, parent_uri=None)
            await self._root_done.wait()
            if self._failure:
                raise self._failure
        except BaseException:
            self._closed = True
            await self._active_work_idle.wait()
            raise
        finally:
            self._closed = True
            await self._active_work_idle.wait()
            self._unregister_active()

    def _schedule_work(self, work: DagWork) -> None:
        if self._closed:
            return
        if self._scheduler is None:
            self._scheduler = get_semantic_node_scheduler(self._node_concurrency)
        self._scheduler.submit(self, work)

    def _start_scheduled_work(self) -> None:
        self._active_scheduled_work += 1
        self._active_work_idle.clear()

    def _finish_scheduled_work(self) -> None:
        self._active_scheduled_work = max(0, self._active_scheduled_work - 1)
        if self._active_scheduled_work == 0:
            self._active_work_idle.set()

    def _schedule_dir(self, dir_uri: str, parent_uri: Optional[str]) -> None:
        if self._closed:
            return
        self._stats.total_nodes += 1
        self._stats.pending_nodes += 1
        self._schedule_work(DagWork(kind="dir", dir_uri=dir_uri, parent_uri=parent_uri))

    def _schedule_file(self, parent_uri: str, file_path: str) -> None:
        if self._closed:
            return
        self._stats.total_nodes += 1
        self._stats.pending_nodes += 1
        self._schedule_work(DagWork(kind="file", dir_uri=parent_uri, file_path=file_path))

    def _mark_node_started(self) -> None:
        self._stats.pending_nodes = max(0, self._stats.pending_nodes - 1)
        self._stats.in_progress_nodes += 1

    def _mark_node_waiting(self) -> None:
        self._stats.in_progress_nodes = max(0, self._stats.in_progress_nodes - 1)
        self._stats.pending_nodes += 1

    def _mark_node_done(self) -> None:
        self._stats.done_nodes += 1
        self._stats.in_progress_nodes = max(0, self._stats.in_progress_nodes - 1)

    def _release_dir_node(self, dir_uri: str) -> None:
        self._nodes.pop(dir_uri, None)
        self._parent.pop(dir_uri, None)

    def _is_media_directory(self, dir_uri: str) -> bool:
        """True when every file under this child uri (within the node) is media.

        Only files the current node knows about are considered; a mixed
        directory never takes the listing description.
        """

        from openviking.parse.parsers.media.utils import get_media_type

        prefix = dir_uri.rstrip("/") + "/"
        files = [p for p in (self._node_files or []) if p.startswith(prefix)]
        if not files:
            return False
        return all(get_media_type(p.rsplit("/", 1)[-1], None) == "image" for p in files)

    @property
    def closed(self) -> bool:
        return self._closed

    def fail(self, exc: Exception) -> None:
        if self._failure is None:
            self._failure = exc
        self._closed = True
        if self._root_done:
            self._root_done.set()

    def _register_active(self) -> None:
        with self._active_lock:
            self._active_executors.add(self)

    def _unregister_active(self) -> None:
        with self._active_lock:
            self._active_executors.discard(self)

    @classmethod
    def get_active_stats(cls) -> DagStats:
        stats = DagStats()
        with cls._active_lock:
            executors = list(cls._active_executors)
        for executor in executors:
            current = executor.get_stats()
            stats.total_nodes += current.total_nodes
            stats.pending_nodes += current.pending_nodes
            stats.in_progress_nodes += current.in_progress_nodes
            stats.done_nodes += current.done_nodes
            stats.lock_skipped_nodes += current.lock_skipped_nodes
            stats.overview_skipped_nodes += current.overview_skipped_nodes
            stats.placeholder_sidecar_dirs.extend(current.placeholder_sidecar_dirs)
        return stats

    async def _run_work(self, work: DagWork) -> None:
        task_context = (
            bind_task_context(
                self._task_context.task_id,
                self._task_context.account_id,
                self._task_context.user_id,
            )
            if self._task_context is not None
            else nullcontext()
        )
        with (
            bind_telemetry(self._telemetry),
            task_context,
            bind_text_batch(self._publication_batch),
            draft_sources(),
        ):
            await self._run_work_bound(work)

    async def _run_work_bound(self, work: DagWork) -> None:
        self._mark_node_started()

        if work.kind == "dir":
            terminal = False
            try:
                terminal = await self._dispatch_dir(work.dir_uri, work.parent_uri)
            finally:
                if terminal:
                    self._mark_node_done()
                else:
                    self._mark_node_waiting()
            return

        if work.kind == "file":
            if work.file_path is None:
                self._mark_node_done()
                return
            await self._file_summary_task(work.dir_uri, work.file_path)
            return

        if work.kind == "overview":
            await self._overview_task(work.dir_uri)
            return

        self._mark_node_done()
        logger.warning("Unknown semantic DAG work kind: %s", work.kind)

    #: PathLock conflicts on a directory dispatch are transient (another writer
    #: holds the sidecar lease for a moment), so ride them out instead of
    #: dropping the whole subtree's semantic work on the first attempt.
    _DISPATCH_LOCK_ATTEMPTS: ClassVar[int] = 3
    _DISPATCH_LOCK_BACKOFF_SECS: ClassVar[float] = 1.0

    async def _dispatch_dir(self, dir_uri: str, parent_uri: Optional[str]) -> bool:
        """Dispatch a directory, retrying transient path-lock conflicts.

        The legacy behaviour on a permanently blocked directory is preserved:
        the child is reported done so the parent aggregation can finish, but the
        skip is logged at ERROR level and counted in :attr:`DagStats`.
        """
        backoff = self._DISPATCH_LOCK_BACKOFF_SECS
        last_error: Optional[LockAcquisitionError] = None
        for attempt in range(1, self._DISPATCH_LOCK_ATTEMPTS + 1):
            try:
                return await self._dispatch_dir_once(dir_uri, parent_uri)
            except LockAcquisitionError as error:
                last_error = error
                if attempt >= self._DISPATCH_LOCK_ATTEMPTS:
                    break
                logger.warning(
                    "Semantic dispatch for %s is lock-blocked (attempt %d/%d): %s; retrying in %.1fs",
                    dir_uri,
                    attempt,
                    self._DISPATCH_LOCK_ATTEMPTS,
                    error,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff *= 2

        self._stats.lock_skipped_nodes += 1
        logger.error(
            "Giving up on semantic dispatch for %s after %d lock attempts: %s; "
            "the subtree is skipped and its content will be missing until re-indexed",
            dir_uri,
            self._DISPATCH_LOCK_ATTEMPTS,
            last_error,
            exc_info=True,
        )
        if parent_uri:
            await self._on_child_done(parent_uri, dir_uri, "")
        elif self._root_done:
            self._root_done.set()
        return True

    async def _dispatch_dir_once(self, dir_uri: str, parent_uri: Optional[str]) -> bool:
        """Lazy-dispatch tasks for a directory when it is triggered."""
        if dir_uri in self._nodes:
            return True

        self._parent[dir_uri] = parent_uri

        try:
            children_dirs, file_paths = await self._list_dir(dir_uri, "_dispatch_dir")
            if self._generation_trigger == "content_copy":
                node = await self._prepare_transfer_node(dir_uri, children_dirs, file_paths)
                self._nodes[dir_uri] = node
                self._schedule_overview(dir_uri)
                return False

            sample_limit = getattr(
                get_openviking_config().semantic,
                "overview_sample_limit",
                32,
            )
            direct_entries = sorted(
                [("directory", uri) for uri in children_dirs]
                + [("file", uri) for uri in file_paths],
                key=lambda item: item[1].rsplit("/", 1)[-1],
            )
            sampled_entries = deterministic_sample(direct_entries, sample_limit)
            sampled_children_dirs = {uri for kind, uri in sampled_entries if kind == "directory"}
            sampled_file_paths = {uri for kind, uri in sampled_entries if kind == "file"}
            pending_snapshot = 0
            if self._aggregate_directory:
                try:
                    pending_snapshot = await read_abstract_overview_pending_snapshot(
                        viking_fs=self._viking_fs,
                        dir_uri=dir_uri,
                        ctx=self._ctx,
                        lock=self._lock,
                    )
                except LockAcquisitionError:
                    if self._generation_trigger != "content_write":
                        raise
                    # Content writes run one directory; retain their file work.
                    self._aggregate_directory = False
                    logger.info("Skipping busy parent semantic refresh: %s", dir_uri)
            file_index = {path: idx for idx, path in enumerate(file_paths)}
            child_index = {path: idx for idx, path in enumerate(children_dirs)}
            # Recursive/initial work still maintains every file. Incremental
            # parent aggregation prepares only sampled inputs plus files that
            # changed and therefore need their own vector maintenance.
            required_file_paths = (
                set(file_paths)
                if self._recursive
                else (
                    set(file_paths) & self._changed_paths
                    if not self._aggregate_directory
                    else (
                        sampled_file_paths | (set(file_paths) & self._changed_paths)
                        if self._incremental_update
                        else sampled_file_paths
                    )
                )
            )
            if self._recursive:
                pending = len(children_dirs) + len(required_file_paths)
            else:
                pending = len(required_file_paths)

            node = DirNode(
                uri=dir_uri,
                children_dirs=children_dirs,
                file_paths=file_paths,
                file_index=file_index,
                child_index=child_index,
                file_summaries=[None] * len(file_paths),
                children_abstracts=[None] * len(children_dirs),
                pending=pending,
                pending_snapshot=pending_snapshot,
                sampled_children_dirs=sampled_children_dirs,
                sampled_file_paths=sampled_file_paths,
                dispatched=True,
            )
            self._nodes[dir_uri] = node

            if pending == 0:
                self._schedule_overview(dir_uri)
                return False

            for file_path in file_paths:
                if file_path not in required_file_paths:
                    continue
                self._schedule_file(dir_uri, file_path)

            if children_dirs:
                if self._recursive:
                    for child_uri in children_dirs:
                        self._schedule_dir(child_uri, dir_uri)
            return False
        except Exception as e:
            if isinstance(e, LockAcquisitionError):
                # Transient: let _dispatch_dir retry it instead of swallowing it.
                raise
            logger.error(f"Failed to dispatch directory {dir_uri}: {e}", exc_info=True)
            if self._generation_trigger == "content_copy":
                raise
            if parent_uri:
                await self._on_child_done(parent_uri, dir_uri, "")
            elif self._root_done:
                self._root_done.set()
            return True

    @staticmethod
    def _transfer_summary_ready(value: str) -> bool:
        summary = value.strip()
        return bool(summary) and not summary.endswith(
            ("[Directory abstract is not ready]", "[Directory overview is not ready]")
        )

    async def _load_transfer_candidates(
        self,
        candidates: List[tuple[str, str]],
    ) -> Dict[tuple[str, str], Dict[str, str]]:
        file_paths = [uri for kind, uri in candidates if kind == "file"]
        file_summaries = await self._processor._load_transfer_file_summaries(
            file_paths, ctx=self._ctx
        )
        loaded: Dict[tuple[str, str], Dict[str, str]] = {}
        for file_path in file_paths:
            summary = str(file_summaries.get(file_path) or "").strip()
            if self._transfer_summary_ready(summary):
                loaded[("file", file_path)] = {
                    "name": file_path.rsplit("/", 1)[-1],
                    "summary": summary,
                }

        child_uris = [uri for kind, uri in candidates if kind == "directory"]
        if child_uris:
            results = await asyncio.gather(
                *[self._viking_fs.abstract(uri, ctx=self._ctx) for uri in child_uris],
                return_exceptions=True,
            )
            for child_uri, result in zip(child_uris, results, strict=True):
                if isinstance(result, BaseException):
                    continue
                abstract = str(result or "").strip()
                if self._transfer_summary_ready(abstract):
                    loaded[("directory", child_uri)] = {
                        "name": child_uri.rsplit("/", 1)[-1],
                        "abstract": abstract,
                    }
        return loaded

    async def _prepare_transfer_node(
        self,
        dir_uri: str,
        children_dirs: List[str],
        file_paths: List[str],
    ) -> DirNode:
        """Sample the target directory first, then read only existing summaries."""
        candidates = sorted(
            [("file", uri) for uri in file_paths] + [("directory", uri) for uri in children_dirs],
            key=lambda item: (item[1].rsplit("/", 1)[-1], item[0], item[1]),
        )
        sample_limit = getattr(get_openviking_config().semantic, "overview_sample_limit", 32)
        primary = deterministic_sample(candidates, sample_limit)
        loaded = await self._load_transfer_candidates(primary)
        selected = [candidate for candidate in primary if candidate in loaded]

        inspected = set(primary)
        selected.sort(key=lambda item: (item[1].rsplit("/", 1)[-1], item[0], item[1]))
        selected_files = [uri for kind, uri in selected if kind == "file"]
        selected_dirs = [uri for kind, uri in selected if kind == "directory"]
        missing_count = (
            sum(1 for candidate in inspected if candidate not in loaded)
            if len(inspected) == len(candidates)
            else None
        )
        pending_snapshot = await read_abstract_overview_pending_snapshot(
            viking_fs=self._viking_fs,
            dir_uri=dir_uri,
            ctx=self._ctx,
            lock=self._lock,
        )
        return DirNode(
            uri=dir_uri,
            children_dirs=selected_dirs,
            file_paths=selected_files,
            file_index={uri: idx for idx, uri in enumerate(selected_files)},
            child_index={uri: idx for idx, uri in enumerate(selected_dirs)},
            file_summaries=[loaded[("file", uri)] for uri in selected_files],
            children_abstracts=[loaded[("directory", uri)] for uri in selected_dirs],
            pending=0,
            pending_snapshot=pending_snapshot,
            total_entries=len(candidates),
            missing_summary_entries=missing_count,
            transfer_inputs_ready=not candidates or bool(selected),
            dispatched=True,
        )

    async def _list_dir(self, uri: str, from_hint: str) -> tuple[list[str], list[str]]:
        """List directory entries and return (child_dirs, file_paths)."""
        try:
            entries = await self._viking_fs.ls(uri, node_limit=LS_ALL_NODES, ctx=self._ctx)
        except Exception as e:
            logger.warning(
                f"[SemanticDagExecutor] Failed to list directory {uri}: {e} from {from_hint}"
            )
            raise

        children_dirs: List[str] = []
        file_paths: List[str] = []

        for entry in entries:
            name = entry.get("name", "")
            if not name or name.startswith(".") or name in [".", ".."] or name in _SKIP_FILENAMES:
                continue

            item_uri = VikingURI(uri).join(name).uri
            if name == DIRECTORY_INDEX_FILENAME and await self._is_generated_navigation_index(
                item_uri
            ):
                # A catalogue describes knowledge; it is not knowledge.
                continue
            if entry.get("isDir", False):
                children_dirs.append(item_uri)
            else:
                file_paths.append(item_uri)

        return sorted(children_dirs), sorted(file_paths)

    def _get_target_file_path(self, current_uri: str) -> Optional[str]:
        if not self._incremental_update or not self._target_uri or not self._root_uri:
            logger.warning(
                "Invalid target_uri or root_uri for incremental update: "
                f"target_uri={self._target_uri}, root_uri={self._root_uri}"
            )
            return None
        if self._target_uri != self._root_uri:
            logger.warning(
                "Incremental semantic update expects target_uri == root_uri: "
                f"target_uri={self._target_uri}, root_uri={self._root_uri}"
            )
            return None
        return current_uri

    def _is_direct_incremental_update(self) -> bool:
        return (
            self._incremental_update
            and bool(self._changed_paths)
            and self._target_uri == self._root_uri
        )

    def _path_has_direct_change(self, uri: str) -> bool:
        if uri in self._changed_paths:
            return True
        prefix = uri.rstrip("/") + "/"
        return any(path.startswith(prefix) for path in self._changed_paths)

    def _ingest_options_for_file(self, file_path: str) -> IngestOptions:
        if self._generation_trigger == "content_write" and file_path not in self._changed_paths:
            return IngestOptions()
        return self._ingest_options

    def _ingest_options_for_directory(self) -> IngestOptions:
        if self._generation_trigger == "content_write":
            return IngestOptions()
        return self._ingest_options

    async def _check_file_content_changed(self, file_path: str) -> bool:
        if self._is_direct_incremental_update():
            return file_path in self._changed_paths
        target_path = self._get_target_file_path(file_path)
        if not target_path:
            return True
        try:
            current_stat = await self._viking_fs.stat(file_path, ctx=self._ctx, skip_count=True)
            target_stat = await self._viking_fs.stat(target_path, ctx=self._ctx, skip_count=True)
            current_size = current_stat.get("size") if isinstance(current_stat, dict) else None
            target_size = target_stat.get("size") if isinstance(target_stat, dict) else None
            if current_size is not None and target_size is not None and current_size != target_size:
                return True
            current_content = await self._viking_fs.read_file(file_path, ctx=self._ctx)
            target_content = await self._viking_fs.read_file(target_path, ctx=self._ctx)
            return current_content != target_content
        except Exception:
            return True

    async def _read_existing_summary(self, file_path: str) -> Optional[Dict[str, str]]:
        """Read existing summary from parent directory's .overview.md.

        Args:
            file_path: Current file path

        Returns:
            Summary dict with 'name' and 'summary' keys, or None if not found
        """
        target_path = self._get_target_file_path(file_path)
        if not target_path:
            return None

        try:
            parent_uri = "/".join(target_path.rsplit("/", 1)[:-1])
            if not parent_uri:
                return None

            if parent_uri not in self._overview_cache:
                try:
                    from openviking.metrics.datasources.cache import CacheEventDataSource

                    CacheEventDataSource.record_miss("L1")
                except Exception:
                    pass
                async with self._overview_cache_lock:
                    if parent_uri not in self._overview_cache:
                        overview_path = f"{parent_uri}/.overview.md"
                        overview_content = await self._viking_fs.read_file(
                            overview_path, ctx=self._ctx
                        )
                        if overview_content:
                            self._overview_cache[parent_uri] = self._processor._parse_overview_md(
                                body_for_preview(overview_content)
                            )
                        else:
                            self._overview_cache[parent_uri] = {}
            else:
                try:
                    from openviking.metrics.datasources.cache import CacheEventDataSource

                    CacheEventDataSource.record_hit("L1")
                except Exception:
                    pass

            existing_summaries = self._overview_cache.get(parent_uri, {})
            file_name = file_path.split("/")[-1]

            if file_name in existing_summaries:
                return {"name": file_name, "summary": existing_summaries[file_name]}

        except Exception as e:
            logger.debug(f"Failed to read existing summary from overview.md for {file_path}: {e}")

        return None

    async def _check_dir_children_changed(
        self, dir_uri: str, current_files: List[str], current_dirs: List[str]
    ) -> bool:
        if self._is_direct_incremental_update():
            if self._path_has_direct_change(dir_uri):
                return True
            for current_file in current_files:
                if self._file_change_status.get(current_file, True):
                    return True
            for current_dir in current_dirs:
                if self._dir_change_status.get(current_dir, True):
                    return True
            return False

        target_path = self._get_target_file_path(dir_uri)
        if not target_path:
            return True
        try:
            target_dirs, target_files = await self._list_dir(
                target_path, "_check_dir_children_changed"
            )
            current_file_names = {f.split("/")[-1] for f in current_files}
            target_file_names = {f.split("/")[-1] for f in target_files}
            if current_file_names != target_file_names:
                return True
            current_dir_names = {d.split("/")[-1] for d in current_dirs}
            target_dir_names = {d.split("/")[-1] for d in target_dirs}
            if current_dir_names != target_dir_names:
                return True
            for current_file in current_files:
                if self._file_change_status.get(current_file, True):
                    return True
            for current_dir in current_dirs:
                if self._dir_change_status.get(current_dir, True):
                    return True
            return False
        except Exception:
            return True

    def _sidecar_dir_path(self, dir_uri: str) -> str:
        """Directory that holds this directory's L0/L1 sidecars.

        ``_get_target_file_path`` only resolves for an incremental update whose
        target is the root; a full reindex therefore used to read no sidecars at
        all. That made a placeholder left by the pre-fix overview path invisible
        (and unremovable), and it also disabled the keep-the-old-sidecar policy
        on every full run. Falling back to the directory URI itself is what the
        write path does too.
        """

        return self._get_target_file_path(dir_uri) or dir_uri

    async def _remove_stale_sidecar(self, path: str, dir_uri: str) -> bool:
        """Delete a placeholder sidecar, retrying while the path is locked."""

        delay = 0.25
        last_error: Optional[BaseException] = None
        for _ in range(_STALE_SIDECAR_REMOVE_ATTEMPTS):
            try:
                await self._viking_fs.remove_files(path, ctx=self._ctx)
                return True
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(delay)
                delay = min(delay * 2, 2.0)
        logger.warning(
            f"[SemanticDag] {dir_uri}: could not remove the stale placeholder "
            f"sidecar {path.rsplit('/', 1)[-1]} after "
            f"{_STALE_SIDECAR_REMOVE_ATTEMPTS} attempt(s): {last_error}"
        )
        return False

    async def _read_existing_overview_abstract(
        self, dir_uri: str
    ) -> tuple[Optional[str], Optional[str]]:
        sidecar_dir = self._sidecar_dir_path(dir_uri)
        overview: Optional[str] = None
        abstract: Optional[str] = None
        try:
            overview = body_for_preview(
                await self._viking_fs.read_file(f"{sidecar_dir}/.overview.md", ctx=self._ctx)
            )
        except Exception:
            overview = None
        try:
            abstract = body_for_preview(
                await self._viking_fs.read_file(f"{sidecar_dir}/.abstract.md", ctx=self._ctx)
            )
        except Exception:
            abstract = None
        return overview, abstract

    async def _file_summary_task(self, parent_uri: str, file_path: str) -> None:
        """Generate file summary and notify parent completion."""

        file_name = file_path.split("/")[-1]
        need_vectorize = True
        try:
            summary_dict = None
            chunking = getattr(getattr(get_openviking_config(), "embedding", None), "chunking", None)
            contextual_image = bool(
                self._context_type == "resource" and chunking and chunking.enabled
                and get_media_type(file_name, None) == "image"
            )
            if self._incremental_update and not contextual_image:
                content_changed = await self._check_file_content_changed(file_path)
                self._file_change_status[file_path] = content_changed
                node = self._nodes.get(parent_uri)
                regenerate_sampled_summary = bool(
                    self._aggregate_directory
                    and node is not None
                    and node.pending_snapshot > 0
                    and node.sampled_file_paths is not None
                    and file_path in node.sampled_file_paths
                )

                if not content_changed and not regenerate_sampled_summary:
                    summary_dict = await self._read_existing_summary(file_path)
                    if summary_dict is not None:
                        need_vectorize = False
                    else:
                        self._file_change_status[file_path] = True
                elif not content_changed:
                    # Pending freshness only records a count, not the changed
                    # child URIs. Once that debt triggers aggregation, rebuild
                    # every sampled input instead of trusting the old L1 body.
                    # Deferred messages already maintained file vectors, so
                    # this forced summary rebuild does not imply vector work.
                    need_vectorize = False
            else:
                self._file_change_status[file_path] = True
            if summary_dict is None:
                summary_dict = await self._processor._generate_single_file_summary(
                    file_path, llm_sem=self._llm_sem, ctx=self._ctx, allow_vlm=self._allow_vlm
                )
        except NotFoundError:
            # Source vanished between attempts (e.g. replaced tree). Retry can
            # never succeed and re-enqueue loops poison the API circuit
            # breaker; record an empty summary so sibling work and the parent
            # aggregation still finish.
            logger.warning("Skipping vanished semantic source: %s", file_path)
            summary_dict = {"name": file_name, "summary": ""}
        except Exception as e:
            chunking = getattr(
                getattr(get_openviking_config(), "embedding", None), "chunking", None
            )
            contextual_image = bool(
                self._context_type == "resource"
                and chunking
                and chunking.enabled
                and get_media_type(file_name, None) == "image"
            )
            if contextual_image:
                # One VLM outage must not discard sibling summaries: leave this
                # image out of image_summaries; the embedding joint-input gate
                # and reindex regeneration own the failure from here.
                logger.warning(
                    "Deferred image summary after generation failure: %s: %s",
                    file_path,
                    e,
                )
            else:
                logger.warning(f"Failed to generate summary for {file_path}: {e}")
            summary_dict = {"name": file_name, "summary": ""}
        finally:
            self._stats.done_nodes += 1
            self._stats.in_progress_nodes = max(0, self._stats.in_progress_nodes - 1)

        if self._closed:
            return
        if summary_dict.get("image_occurrences"):
            self.image_summaries[file_path] = summary_dict
        if need_vectorize and not self._skip_vectorization:
            use_summary = self._is_code_repo and bool(summary_dict.get("summary"))
            # A caption written into this file's citing markdown is only a
            # problem for files that have already read their bytes: mark the
            # boundary here, at the moment the content is taken in, so a
            # concurrent image task knows this file owes a fresh revision.
            # `_on_file_done` marks every other outcome as well (reused
            # unchanged, failed forward).
            self._file_vector_settled.add(file_path)
            try:
                await self._processor._vectorize_single_file(
                    parent_uri=parent_uri,
                    context_type=self._context_type,
                    file_path=file_path,
                    summary_dict=summary_dict,
                    ctx=self._ctx,
                    use_summary=use_summary,
                    ingest_options=self._ingest_options_for_file(file_path),
                    creator_acl_grant=self._creator_acl_grant(file_path),
                )
                # Caption backfill runs only after joint-index enqueue: writing
                # the citing markdown between context capture and enqueue
                # bumps the draft epoch and trips the "Image context changed"
                # publication gate into a re-enqueue loop.
                if summary_dict.get("image_occurrences"):
                    await self._backfill_figure_captions(file_path, summary_dict)
            except NotFoundError:
                logger.warning("Skipping vectorization for vanished source: %s", file_path)
            except Exception as e:
                logger.error(
                    "Failed to schedule vectorization for %s: %s",
                    file_path,
                    e,
                    exc_info=True,
                )
                raise
        await self._on_file_done(
            parent_uri,
            file_path,
            {
                "name": str(summary_dict.get("name") or file_name),
                "summary": str(summary_dict.get("summary") or ""),
            },
        )

    async def _backfill_figure_captions(self, file_path: str, summary_dict: dict) -> None:
        """Write the generated image caption into citing markdown figure blocks."""
        from openviking.parse.figure_caption import backfill_image_captions

        try:
            updated = await backfill_image_captions(
                self._viking_fs,
                file_path,
                summary_dict,
                ctx=self._ctx,
                lock=self._lock,
            )
        except Exception as e:
            logger.warning(f"[SemanticDag] Figure caption backfill failed for {file_path}: {e}")
            return
        if updated:
            logger.info(
                f"[SemanticDag] Backfilled figure captions for {file_path} into "
                f"{len(updated)} file(s)"
            )
        for md_uri in updated:
            await self._revectorize_recaptioned_markdown(md_uri)
        if updated:
            await self._revectorize_recaptioned_image(file_path, summary_dict)

    async def _revectorize_recaptioned_image(self, image_uri: str, summary_dict: dict) -> None:
        """Rebuild an image's joint row after this run captioned it in its chapter.

        The joint row was enqueued before the caption was written into the citing
        markdown, and it declares that markdown as its context source: the write
        bumps the markdown's draft epoch, so the publication gate condemns the row
        that is already in flight ("Image context changed during index build").
        Nothing else re-enqueues it, so the figure's VL embedding stayed invisible
        to retrieval for the whole branch while the ingest still reported success.
        """
        if image_uri in self._recaptioned_images:
            return
        if image_uri not in self._file_vector_settled:
            return
        self._recaptioned_images.add(image_uri)
        try:
            await self._processor._vectorize_single_file(
                parent_uri=image_uri.rsplit("/", 1)[0],
                context_type=self._context_type,
                file_path=image_uri,
                # Re-collected against the captioned chapter, never the summary
                # this run started with: those occurrences freeze the context
                # hash and draft epoch read *before* the caption write, so the
                # rebuild would trip the same gate it exists to clear
                # ("Image context changed before index enqueue").
                summary_dict=await self._recaptioned_image_summary(image_uri, summary_dict),
                ctx=self._ctx,
                preserve_existing_created_at=True,
                ingest_options=self._ingest_options_for_file(image_uri),
                creator_acl_grant=self._creator_acl_grant(image_uri),
            )
        except Exception as e:
            # Same contract as the markdown rebuild: a caption is derived
            # convenience and never fails the ingestion DAG.
            logger.warning(
                f"[SemanticDag] Joint rebuild after caption backfill failed "
                f"for {image_uri}: {e}"
            )

    async def _recaptioned_image_summary(
        self, image_uri: str, summary_dict: dict
    ) -> dict:
        """Re-read this image's occurrences against the chapter as it now stands.

        Only the reference context is collected again; every description comes
        from ``summary_dict``. The caption the pipeline just wrote is now authored
        markdown, so re-running the summary step would describe the same picture
        from its own caption -- a second VLM round trip buying nothing.

        Falls back to the original dict whenever the references no longer line up
        one for one: a partial re-description would pair a chapter's text with
        another chapter's picture, and the publication gate is the right owner of
        that case.
        """
        records = summary_dict.get("image_occurrences") or []
        if not records:
            return summary_dict
        from openviking.indexing.image_context import collect_image_contexts

        try:
            current = await collect_image_contexts(self._viking_fs, image_uri, self._ctx)
        except Exception as e:
            logger.warning(
                f"[SemanticDag] Image context refresh failed for {image_uri}: {e}"
            )
            return summary_dict
        if len(current) != len(records):
            return summary_dict
        by_reference = {(r.get("context_uri"), r.get("ordinal")): r for r in records}
        refreshed = []
        for occurrence in current:
            previous = by_reference.get((occurrence.get("context_uri"), occurrence.get("ordinal")))
            description = str((previous or {}).get("summary") or "").strip()
            if not description:
                return summary_dict
            refreshed.append(
                {
                    **occurrence,
                    "summary": description,
                    "asset_sha256": (previous or {}).get("asset_sha256"),
                    "semantics_signature": (previous or {}).get("semantics_signature"),
                }
            )
        return {
            **summary_dict,
            "image_occurrences": refreshed,
            "summary": "\n\n".join(r["summary"] for r in refreshed),
        }

    async def _revectorize_recaptioned_markdown(self, md_uri: str) -> None:
        """Rebuild a chapter's vectors after this run wrote its figure caption.

        The caption the pipeline generated was not in the uploaded markdown, so
        the chapter's archived source revision, its parent-child chunk vectors and
        its lexical row were all produced without it. ``content/read`` and BM25
        serve that archived revision, so the description would stay invisible to
        an agent until some later rebuild happened to re-chunk the file. A
        markdown still pending in this run needs nothing: its own pass reads the
        captioned bytes.
        """
        if md_uri in self._recaptioned_markdown:
            return
        if md_uri not in self._file_vector_settled:
            return
        self._recaptioned_markdown.add(md_uri)
        try:
            await self._processor._vectorize_single_file(
                parent_uri=md_uri.rsplit("/", 1)[0],
                context_type=self._context_type,
                file_path=md_uri,
                summary_dict={"name": md_uri.rsplit("/", 1)[-1], "summary": ""},
                ctx=self._ctx,
                preserve_existing_created_at=True,
                ingest_options=self._ingest_options_for_file(md_uri),
                creator_acl_grant=self._creator_acl_grant(md_uri),
            )
        except Exception as e:
            # A caption is derived convenience: never fail the ingestion DAG
            # over its re-publication. The text is on disk and a reindex owns
            # the repair.
            logger.warning(
                f"[SemanticDag] Vector rebuild after caption backfill failed for {md_uri}: {e}"
            )

    async def _on_file_done(
        self, parent_uri: str, file_path: str, summary_dict: Dict[str, str]
    ) -> None:
        # Reached whether the file was vectorized, reused unchanged, or failed
        # forward: this run will not touch its vectors again on its own.
        self._file_vector_settled.add(file_path)
        node = self._nodes.get(parent_uri)
        if not node:
            return

        async with node.lock:
            idx = node.file_index.get(file_path)
            if idx is not None and (
                node.sampled_file_paths is None or file_path in node.sampled_file_paths
            ):
                node.file_summaries[idx] = summary_dict
            node.pending -= 1
            if node.pending == 0 and not node.overview_scheduled:
                self._schedule_overview(parent_uri)

    async def _on_child_done(self, parent_uri: str, child_uri: str, abstract: str) -> None:
        node = self._nodes.get(parent_uri)
        if not node:
            return

        child_name = child_uri.split("/")[-1]
        async with node.lock:
            idx = node.child_index.get(child_uri)
            if idx is not None and (
                node.sampled_children_dirs is None or child_uri in node.sampled_children_dirs
            ):
                node.children_abstracts[idx] = {"name": child_name, "abstract": abstract}
            node.pending -= 1
            if node.pending == 0 and not node.overview_scheduled:
                self._schedule_overview(parent_uri)

    def _schedule_overview(self, dir_uri: str) -> None:
        node = self._nodes.get(dir_uri)
        if not node or node.overview_scheduled:
            return
        node.overview_scheduled = True
        self._schedule_work(DagWork(kind="overview", dir_uri=dir_uri))

    def _finalize_file_summaries(self, node: DirNode) -> List[Dict[str, str]]:
        summaries: List[Dict[str, str]] = []
        for idx, file_path in enumerate(node.file_paths):
            if node.sampled_file_paths is not None and file_path not in node.sampled_file_paths:
                continue
            item = node.file_summaries[idx]
            if item is None:
                summaries.append({"name": file_path.split("/")[-1], "summary": ""})
            else:
                summaries.append(item)
        return summaries

    def _select_direct_media_overview(
        self,
        node: DirNode,
        file_summaries: List[Dict[str, str]],
    ) -> Optional[str]:
        if len(node.file_paths) != 1 or node.children_dirs or len(file_summaries) != 1:
            return None

        file_path = node.file_paths[0]
        filename = file_path.rsplit("/", 1)[-1]
        if get_media_type(file_path, None) not in {"audio", "video"}:
            return None

        summary = str(file_summaries[0].get("summary") or "").strip()
        if not summary.startswith("# "):
            return None

        lines = summary.splitlines()
        brief_start = next((idx for idx in range(1, len(lines)) if lines[idx].strip()), None)
        if brief_start is None or lines[brief_start].lstrip().startswith("#"):
            return None

        brief_end = brief_start
        while brief_end < len(lines) and lines[brief_end].strip():
            if lines[brief_end].lstrip().startswith("#"):
                return None
            brief_end += 1

        filename_heading = re.compile(rf"^###\s+{re.escape(filename)}\s*$", re.MULTILINE)
        if not any(filename_heading.fullmatch(line) for line in lines[brief_end:]):
            return None
        return summary

    @property
    def stale(self) -> bool:
        return self._stale

    async def _finalize_children_abstracts(self, node: DirNode) -> List[Dict[str, str]]:
        results: List[Dict[str, str]] = []
        for idx, child_uri in enumerate(node.children_dirs):
            if (
                node.sampled_children_dirs is not None
                and child_uri not in node.sampled_children_dirs
            ):
                continue
            item = node.children_abstracts[idx]
            if item is None:
                try:
                    abstract = await self._viking_fs.abstract(child_uri, ctx=self._ctx)
                except Exception:
                    abstract = ""
                results.append({"name": child_uri.split("/")[-1], "abstract": abstract})
            else:
                results.append(item)
        return results

    def _is_stale(self) -> bool:
        from openviking.storage.queuefs.semantic_queue import is_semantic_coalesce_stale

        return is_semantic_coalesce_stale(self._coalesce_key, self._coalesce_version)

    async def _is_generated_navigation_index(self, uri: str) -> bool:
        """Decide by content marker, so a legacy chapter body keeps its index."""

        if not is_directory_index_uri(uri):
            return False
        cache = getattr(self, "_navigation_index_cache", None)
        if cache is None:
            cache = {}
            self._navigation_index_cache = cache
        if uri in cache:
            return cache[uri]
        try:
            verdict = is_generated_directory_index(
                await _read_head_for_index(self._viking_fs, uri, ctx=self._ctx)
            )
        except Exception:
            verdict = False
        cache[uri] = verdict
        return verdict

    async def _chapter_manifest(self) -> Dict[str, Dict[str, Any]]:
        """Map a resource root's `_manifest.json` paths to title/page records."""

        cached = getattr(self, "_chapter_manifest_cache", None)
        if cached is not None:
            return cached
        records: Dict[str, Dict[str, Any]] = {}
        try:
            raw = await _read_head_for_index(
                self._viking_fs, f"{self._root_uri}/_manifest.json", ctx=self._ctx
            )
            if raw:
                import json

                manifest = json.loads(raw)
                for record in manifest.get("chapters") or []:
                    path = str(record.get("path") or "").strip("/")
                    if path:
                        records.setdefault(path.lower(), record)
        except Exception:
            records = {}
        self._chapter_manifest_cache = records
        return records

    def _manifest_hint(self, relative_path: str) -> str:
        """Chapter title and page range for one entry, when the manifest knows."""

        manifest = getattr(self, "_chapter_manifest_cache", None) or {}
        record = manifest.get(str(relative_path).strip("/").lower())
        if not record:
            return ""
        parts: List[str] = []
        title = str(record.get("title") or "").strip()
        if title and title.strip("/") != str(relative_path).strip("/"):
            parts.append(title)
        page = record.get("page_start")
        if isinstance(page, int) and page > 0:
            parts.append(f"p.{page}")
        return one_line(" · ".join(parts))

    def _is_media_directory(self, dir_uri: str) -> bool:
        """True when every file inside this child directory is media."""

        from openviking.parse.parsers.media.utils import get_media_type

        prefix = dir_uri.rstrip("/") + "/"
        files = [p for p in (self._node_files or []) if p.startswith(prefix)]
        if not files:
            return False
        return all(get_media_type(p.rsplit("/", 1)[-1], None) == "image" for p in files)

    def _media_listing_for(self, child_uri: str) -> str:
        """Listing description for an images/ child; empty unless all-media."""

        from openviking.parse.parsers.media.utils import get_media_type

        prefix = child_uri.rstrip("/") + "/"
        names = [
            p.rsplit("/", 1)[-1]
            for p in (self._node_files or [])
            if p.startswith(prefix) and get_media_type(p.rsplit("/", 1)[-1], None) == "image"
        ]
        if not names:
            return ""
        head = ", ".join(names[:4]) + ("…" if len(names) > 4 else "")
        return f"{len(names)} images: {head}"

    async def _write_navigation_index(self, dir_uri: str, node: DirNode) -> None:
        """Publish `{dir}/index.md`: every direct child with its viking URI."""

        from openviking.core.namespace import context_type_for_uri

        if context_type_for_uri(dir_uri) != "resource":
            # Memory and skill trees have their own discovery contracts; a
            # generated catalogue would collide with those directories' bodies.
            return

        manifest = await self._chapter_manifest()
        self._chapter_manifest_cache = manifest
        self._node_files = list(node.file_paths or [])

        summaries = {
            path.rsplit("/", 1)[-1]: item
            for path, item in zip(node.file_paths, node.file_summaries, strict=True)
            if item
        }
        abstracts = {
            path.rsplit("/", 1)[-1]: item
            for path, item in zip(node.children_dirs, node.children_abstracts, strict=True)
            if item
        }

        root = str(self._root_uri).rstrip("/") + "/"
        directories: List[DirectoryIndexEntry] = []
        for child_uri in node.children_dirs:
            name = child_uri.rstrip("/").rsplit("/", 1)[-1]
            child = abstracts.get(name) or {}
            hint = self._manifest_hint(child_uri[len(root) :] if child_uri.startswith(root) else name)
            directories.append(
                DirectoryIndexEntry(
                    name=name,
                    uri=child_uri,
                    description=hint
                    or self._media_listing_for(child_uri)
                    or navigation_description(child.get("abstract")),
                    kind="directory",
                ).normalized()
            )

        files: List[DirectoryIndexEntry] = []
        for file_uri in node.file_paths:
            name = file_uri.rsplit("/", 1)[-1]
            if name == DIRECTORY_INDEX_FILENAME:
                continue
            child = summaries.get(name) or {}
            hint = self._manifest_hint(file_uri[len(root) :] if file_uri.startswith(root) else name)
            files.append(
                DirectoryIndexEntry(
                    name=name,
                    uri=file_uri,
                    description=hint or navigation_description(child.get("summary")),
                    kind="file",
                ).normalized()
            )

        try:
            wrote = await write_directory_index(
                viking_fs=self._viking_fs,
                dir_uri=dir_uri,
                directories=directories,
                files=files,
                ctx=self._ctx,
                title=dir_uri.rstrip("/").rsplit("/", 1)[-1] or dir_uri,
                generated_by={
                    "component": "SemanticDagExecutor",
                    "trigger": self._generation_trigger,
                },
                lock=self._lock,
            )
            if wrote.reason == "foreign_body":
                logger.info(
                    "[SemanticDag] Kept authored %s as chapter content; no navigation index",
                    wrote.uri,
                )
        except Exception as exc:
            # Navigation is an affordance; it must never fail a semantic batch.
            logger.warning("[SemanticDag] Navigation index write failed for %s: %s", dir_uri, exc)

    async def _write_directory_semantics(
        self,
        dir_uri: str,
        overview: str,
        abstract: str,
        *,
        total_entries: int,
        sampled_entries: int,
        consume_pending: int,
        missing_summary_entries: Optional[int] = None,
    ) -> AbstractOverviewWriteResult:
        metadata: Dict[str, Any] = {
            "generated_by": {
                "component": "SemanticProcessor",
                "trigger": self._generation_trigger,
            },
            "freshness": freshness_metadata(
                total_entries,
                sampled_entries,
                missing_summary_entries=missing_summary_entries,
            ),
        }
        if dir_uri == self._root_uri and self._source:
            metadata["source"] = self._source
        wrote = await write_abstract_overview(
            viking_fs=self._viking_fs,
            dir_uri=dir_uri,
            overview=overview,
            abstract=abstract,
            ctx=self._ctx,
            is_stale=self._is_stale,
            metadata=metadata,
            consume_pending=consume_pending,
            lock=self._lock,
            log_prefix="[SemanticDag]",
        )
        if not wrote.wrote:
            self._stale = True
        return wrote

    async def _overview_task(self, dir_uri: str) -> None:
        node = self._nodes.get(dir_uri)
        if not node:
            return
        if not self._aggregate_directory:
            # Deferred aggregation still ran changed-file work. Finish without
            # touching directory sidecars or their vectors.
            self._stats.done_nodes += 1
            self._stats.in_progress_nodes = max(0, self._stats.in_progress_nodes - 1)
            self._release_dir_node(dir_uri)
            if dir_uri == self._root_uri and self._root_done:
                self._root_done.set()
            return
        need_vectorize = True
        children_changed = True
        should_write = True
        abstract: Optional[str] = None
        overview: Optional[str] = None
        total_entries = (
            node.total_entries
            if node.total_entries is not None
            else len(node.file_paths) + len(node.children_dirs)
        )
        sampled_inputs: List[tuple[str, Dict[str, str]]] = []
        try:
            if self._generation_trigger == "content_copy" and not node.transfer_inputs_ready:
                need_vectorize = False
                should_write = False
            elif self._incremental_update and self._generation_trigger != "content_copy":
                children_changed = await self._check_dir_children_changed(
                    dir_uri, node.file_paths, node.children_dirs
                )

                if not children_changed:
                    overview, abstract = await self._read_existing_overview_abstract(dir_uri)
                    # A placeholder sidecar is not content: treat it as missing so
                    # the directory is regenerated instead of skipped forever.
                    overview, abstract = strip_placeholder_sidecars(overview, abstract)
                    should_write = overview is None or abstract is None
                    # Rebuilt sidecars must also replace their stale vectors.
                    need_vectorize = should_write
                    children_changed = should_write
            if should_write and (overview is None or abstract is None):
                async with node.lock:
                    file_summaries = self._finalize_file_summaries(node)
                    children_abstracts = await self._finalize_children_abstracts(node)
                # Freshness describes the directory itself, including direct
                # entries whose summaries failed. Those entries remain visible
                # as unsampled coverage instead of disappearing from the count.
                # Sampling happened in _dispatch_dir, before summary work was
                # scheduled. Transfer refreshes similarly prepare only target
                # entries whose existing L2 summaries are ready.
                sampled_inputs = sorted(
                    [("file", item) for item in file_summaries]
                    + [("directory", item) for item in children_abstracts],
                    key=lambda tagged: str(tagged[1].get("name") or ""),
                )
                sampled_entries = len(sampled_inputs)
                if self._generation_trigger != "content_copy":
                    overview = self._select_direct_media_overview(node, file_summaries)
                if overview is None:
                    async with self._llm_sem:
                        overview = await self._processor._generate_overview(
                            dir_uri,
                            file_summaries,
                            children_abstracts,
                            total_files=len(node.file_paths),
                            total_children=len(node.children_dirs),
                        )
                if overview is None:
                    # The model produced nothing for this directory. Writing a
                    # placeholder would overwrite a good .abstract.md /
                    # .overview.md with "not generated" text. When the sidecars
                    # are already on disk, keeping them is the whole fix: a
                    # transient model hiccup must not fail the reindex, so the
                    # directory is left untouched and counted as skipped.
                    existing_overview, existing_abstract = await self._read_existing_overview_abstract(dir_uri)
                    stale = placeholder_sidecar_names(existing_overview, existing_abstract)
                    # Always hand this directory to the post-walk sweep: the walk's
                    # own read can miss sidecars the published view still serves
                    # (live: "no sidecars exist on disk" while the content API read
                    # the placeholder back), and a delete attempted here races the
                    # lock the walk itself holds. The sweep runs once the walk is
                    # over, re-reads every listed directory and deletes only files
                    # whose body is still the marker, so listing an extra
                    # directory costs one read and can never remove live content.
                    self._stats.placeholder_sidecar_dirs.append(dir_uri)
                    if stale:
                        # Sidecars written before the no-placeholder rule carry
                        # only the "not generated" marker. There is nothing worth
                        # keeping in them, so drop the stale files and leave the
                        # directory without L0/L1 - the same end state as the
                        # no-sidecar branch below.
                        sidecar_dir = self._sidecar_dir_path(dir_uri)
                        removed = 0
                        for name in stale:
                            if await self._remove_stale_sidecar(f"{sidecar_dir}/{name}", dir_uri):
                                removed += 1
                        if removed:
                            logger.warning(
                                f"[SemanticDag] {dir_uri}: removed {removed} placeholder "
                                "sidecar(s) left by the pre-fix overview path"
                            )
                    if (
                        existing_overview is not None
                        and existing_abstract is not None
                        and not stale
                    ):
                        self._stats.overview_skipped_nodes += 1
                        logger.warning(
                            f"[SemanticDag] {dir_uri}: overview generation returned no "
                            "content; keeping the existing sidecars and continuing"
                        )
                        should_write = False
                        need_vectorize = False
                    else:
                        # Nothing to keep and nothing to write: the directory is
                        # left without L0/L1 rather than carrying a placeholder or
                        # failing the run. Raising here is what re-enqueued the
                        # same directory forever (image-only directories have no
                        # text for the model to summarise), so the reindex never
                        # finished.
                        self._stats.overview_skipped_nodes += 1
                        logger.warning(
                            f"[SemanticDag] {dir_uri}: overview generation returned no "
                            "content and no sidecars exist on disk; leaving the "
                            "directory without L0/L1 instead of writing a placeholder"
                        )
                        should_write = False
                        need_vectorize = False
                else:
                    overview, abstract = self._processor._normalize_overview_generation(overview)

            if self._closed:
                return

            # Persist sidecars before publishing their directory vectors.
            if should_write:
                assert overview is not None and abstract is not None
                try:
                    wrote = await self._write_directory_semantics(
                        dir_uri,
                        overview,
                        abstract,
                        total_entries=total_entries,
                        sampled_entries=sampled_entries,
                        consume_pending=node.pending_snapshot,
                        missing_summary_entries=node.missing_summary_entries,
                    )
                    if dir_uri == self._root_uri:
                        self._root_write_result = wrote
                    if not wrote.wrote:
                        need_vectorize = False
                except AbstractOverviewFormatError:
                    raise
                except Exception:
                    need_vectorize = False
                    logger.info(f"[SemanticDag] {dir_uri} write failed, skipping")
                await self._write_navigation_index(dir_uri, node)

        except AbstractOverviewFormatError:
            raise
        except Exception as e:
            # Propagate to the worker loop, which routes it through fail():
            # root_done is set and run() re-raises, so no DAG path hangs, and
            # callers observe a real VLM failure instead of a false success.
            logger.error(f"Failed to generate overview for {dir_uri}: {e}", exc_info=True)
            raise
        else:
            if need_vectorize and not self._skip_vectorization:
                assert overview is not None and abstract is not None
                try:
                    await self._processor._vectorize_directory(
                        dir_uri,
                        context_type=self._context_type,
                        abstract=abstract,
                        overview=overview,
                        ctx=self._ctx,
                        ingest_options=self._ingest_options_for_directory(),
                        creator_acl_grant=self._creator_acl_grant(dir_uri),
                    )
                except Exception as e:
                    logger.error(
                        "Failed to schedule vectorization for %s: %s",
                        dir_uri,
                        e,
                        exc_info=True,
                    )
                    raise
        finally:
            self._stats.done_nodes += 1
            self._stats.in_progress_nodes = max(0, self._stats.in_progress_nodes - 1)

        self._dir_change_status[dir_uri] = children_changed

        parent_uri = self._parent.get(dir_uri)
        if parent_uri is None:
            self._release_dir_node(dir_uri)
            if self._root_done:
                self._root_done.set()
            return

        await self._on_child_done(parent_uri, dir_uri, abstract or "")
        self._release_dir_node(dir_uri)

    def get_stats(self) -> DagStats:
        return DagStats(
            total_nodes=self._stats.total_nodes,
            pending_nodes=self._stats.pending_nodes,
            in_progress_nodes=self._stats.in_progress_nodes,
            done_nodes=self._stats.done_nodes,
            lock_skipped_nodes=self._stats.lock_skipped_nodes,
            overview_skipped_nodes=self._stats.overview_skipped_nodes,
            placeholder_sidecar_dirs=list(self._stats.placeholder_sidecar_dirs),
        )

    @property
    def root_write_result(self) -> AbstractOverviewWriteResult:
        """Visible-body changes produced for the executor root."""

        return self._root_write_result


if False:  # pragma: no cover - for type checkers only
    from openviking.storage.queuefs.semantic_processor import SemanticProcessor
