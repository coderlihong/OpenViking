# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
QueueManager: Encapsulates AGFS QueueFS plugin operations.
All queues are managed through NamedQueue.
"""

import asyncio
import atexit
import threading
import time
import traceback
from typing import Any, Dict, Optional, Sequence, Set, Union

from openviking.service.task_work_index import TaskWorkIndex
from openviking_cli.utils.logger import get_logger

from .embedding_queue import EmbeddingQueue
from .named_queue import DequeueHandlerBase, NamedQueue, QueueStatus
from .queue_middleware import QueueMiddleware
from .semantic_queue import SemanticQueue

logger = get_logger(__name__)

DEFAULT_MAX_CONCURRENT_SESSION_COMMIT = 8

# ========== Singleton Pattern ==========
_instance: Optional["QueueManager"] = None


def init_queue_manager(
    agfs: Any,
    timeout: int = 10,
    mount_point: str = "/queue",
    max_concurrent_embedding: int = 10,
    max_concurrent_semantic: int = 32,
    max_concurrent_external_parse: int = 4,
    max_concurrent_add_resource: int = 4,
    max_concurrent_session_commit: int = DEFAULT_MAX_CONCURRENT_SESSION_COMMIT,
    max_concurrent_external_task: int = 10,
    *,
    middlewares: Sequence[QueueMiddleware] = (),
) -> "QueueManager":
    """Initialize QueueManager singleton.

    Args:
        agfs: Pre-initialized AGFS client (HTTP or Binding).
        timeout: Request timeout in seconds.
        mount_point: Path where QueueFS is mounted.
        max_concurrent_embedding: Max concurrent embedding tasks.
        max_concurrent_semantic: Max concurrent semantic node work.
        max_concurrent_external_parse: Max concurrent ExternalParse tasks.
        max_concurrent_add_resource: Max concurrent AddResource tasks.
        max_concurrent_session_commit: Max concurrent SessionCommit tasks.
        middlewares: Additional middleware, fixed at construction for all queues.
    """
    global _instance
    _instance = QueueManager(
        agfs=agfs,
        timeout=timeout,
        mount_point=mount_point,
        max_concurrent_embedding=max_concurrent_embedding,
        max_concurrent_semantic=max_concurrent_semantic,
        max_concurrent_external_parse=max_concurrent_external_parse,
        max_concurrent_add_resource=max_concurrent_add_resource,
        max_concurrent_session_commit=max_concurrent_session_commit,
        max_concurrent_external_task=max_concurrent_external_task,
        middlewares=middlewares,
    )
    return _instance


def get_queue_manager() -> "QueueManager":
    """Get QueueManager singleton."""
    if _instance is None:
        raise RuntimeError("QueueManager is not initialized. Call init_queue_manager() first.")
    return _instance


class QueueManager:
    """
    QueueManager: Encapsulates AGFS QueueFS plugin operations.
    Integrates NamedQueue to manage multiple named queues.
    """

    # Standard queue names
    EMBEDDING = "Embedding"
    SEMANTIC = "Semantic"
    # Keep the on-disk name stable so pre-upgrade jobs remain recoverable.
    EXTERNAL_PARSE = "ExternalParse"
    ADD_RESOURCE = "AddResource"
    SESSION_COMMIT = "SessionCommit"
    EXTERNAL_TASK = "ExternalTask"
    # Account and user cleanup share one consumer. Retain the persisted name
    # so user cleanup messages queued before this change resume in place.
    DATA_CLEANUP = "UserDeletion"
    # Deferred work re-enqueues itself; throttle the next scheduling round.
    _REQUEUE_POLL_INTERVAL = 1.0

    def __init__(
        self,
        agfs: Any,
        timeout: int = 10,
        mount_point: str = "/queue",
        max_concurrent_embedding: int = 10,
        max_concurrent_semantic: int = 32,
        max_concurrent_external_parse: int = 4,
        max_concurrent_add_resource: int = 4,
        max_concurrent_session_commit: int = DEFAULT_MAX_CONCURRENT_SESSION_COMMIT,
        max_concurrent_external_task: int = 10,
        *,
        middlewares: Sequence[QueueMiddleware] = (),
    ):
        """Initialize QueueManager."""
        self._agfs = agfs
        self.timeout = timeout
        self.mount_point = mount_point
        self._max_concurrent_embedding = max_concurrent_embedding
        # Optional override for how many worker threads the embedding queue runs.
        # 0/None derives it from max_concurrent (see _worker_thread_count).
        self._embedding_worker_threads = int(
            getattr(self, "_config_embedding_worker_threads", 0) or 0
        )
        self._max_concurrent_semantic = max_concurrent_semantic
        self._max_concurrent_external_parse = max_concurrent_external_parse
        self._max_concurrent_add_resource = max_concurrent_add_resource
        self._max_concurrent_session_commit = max_concurrent_session_commit
        self._max_concurrent_external_task = max_concurrent_external_task
        self._queues: Dict[str, NamedQueue] = {}
        self._started = False
        self._queue_threads: Dict[str, list] = {}
        self._queue_stop_events: Dict[str, threading.Event] = {}
        self._poll_interval = 0.2
        self._task_work_index = TaskWorkIndex()
        # Import at composition time to avoid a service <-> queue package cycle.
        from openviking.service.task_queue_middleware import TaskWorkQueueMiddleware

        self._middlewares: tuple[QueueMiddleware, ...] = (
            TaskWorkQueueMiddleware(self._task_work_index),
            *middlewares,
        )

        atexit.register(self.stop)
        logger.info(
            f"[QueueManager] Initialized with agfs={type(agfs).__name__}, mount_point={mount_point}"
        )

    def start(self) -> None:
        """Start QueueManager workers."""
        if self._started:
            return

        self._started = True

        # Start queue workers for existing queues
        for queue in list(self._queues.values()):
            self._start_queue_worker(queue)

        logger.info(f"[QueueManager] mount_point={self.mount_point} Started")

    async def prepare_task_tracking(self, tracker: Any) -> list[Any]:
        """Rebuild task work from QueueFS before any consumer starts."""
        snapshots = {name: await queue.snapshot() for name, queue in self._queues.items()}
        owners = self._task_work_index.rebuild(snapshots)
        tracker.attach_work_index(self._task_work_index)
        return await tracker.restore_work_tasks(owners)

    def setup_standard_queues(self, vector_store: Any, start: bool = True) -> None:
        """
        Setup standard queues (Embedding and Semantic) with their handlers.

        Args:
            vector_store: Vector store instance for handlers to write results.
            start: Whether to start worker threads immediately (default True).
                   Pass False when the consumer depends on resources that are
                   not yet initialized (e.g. VikingFS); call start() manually
                   after those resources are ready.
        """
        # Import handlers here to avoid circular dependencies
        from openviking.storage.collection_schemas import TextEmbeddingHandler
        from openviking.storage.queuefs import SemanticProcessor

        # Embedding Queue
        embedding_handler = TextEmbeddingHandler(vector_store)
        self.get_queue(
            self.EMBEDDING,
            dequeue_handler=embedding_handler,
            allow_create=True,
        )
        logger.info("Embedding queue initialized with TextEmbeddingHandler")

        # Semantic Queue
        semantic_processor = SemanticProcessor(max_concurrent_llm=self._max_concurrent_semantic)
        self.get_queue(
            self.SEMANTIC,
            dequeue_handler=semantic_processor,
            allow_create=True,
        )
        logger.info("Semantic queue initialized with SemanticProcessor")

        if start:
            self.start()

    def _embedding_batch_target(self) -> int:
        """How many embedding messages one dequeue should try to take.

        The batched commit is the whole point of collecting a batch, and its
        benefit grows with the batch. ``max_concurrent`` bounds how many run at
        once, not how many may be committed together, so the target is decoupled
        from it and capped to keep one commit's work bounded.
        """
        configured = int(getattr(self, "_embedding_batch_size", 0) or 0)
        if configured > 0:
            return configured
        return max(1, min(128, self._max_concurrent_embedding * 4))

    def _worker_thread_count(self, queue_name: str) -> int:
        """How many worker threads to run for a queue.

        Each worker owns a private event loop, and the embedder's concurrency
        semaphore is keyed by event loop -- so one thread caps the whole queue at
        one semaphore's worth of in-flight requests no matter how large the batch
        is. Measured on the 1935 deployment: the embedder sustained 17,170
        docs/min in isolation while the queue delivered 128, a 134x loss that
        came entirely from serialising requests behind a single loop.

        Embedding gets several threads so the semaphore is no longer the
        ceiling; queues that mutate shared bookkeeping stay single-threaded.
        """
        if queue_name != self.EMBEDDING:
            return 1
        configured = int(getattr(self, "_embedding_worker_threads", 0) or 0)
        if configured > 0:
            return configured
        # Each thread brings its own event loop and therefore its own semaphore
        # pool, so thread count multiplies the achievable in-flight requests.
        # Default to 4: enough to saturate the model endpoint without turning the
        # queue into a thread farm.
        return max(1, min(8, int(getattr(self, "_default_embedding_worker_threads", 4))))

    def _start_queue_worker(self, queue: NamedQueue) -> None:
        """Start dedicated worker thread(s) for a queue if not already running."""
        existing = self._queue_threads.get(queue.name)
        if existing and any(thread.is_alive() for thread in existing):
            return

        max_concurrent = self._max_concurrent_for_queue(queue.name)
        stop_event = threading.Event()
        self._queue_stop_events[queue.name] = stop_event
        threads = []
        count = self._worker_thread_count(queue.name)
        for index in range(count):
            thread = threading.Thread(
                target=self._queue_worker_loop,
                args=(queue, stop_event, max_concurrent),
                name=f"{queue.name}-worker-{index}",
                daemon=True,
            )
            threads.append(thread)
        self._queue_threads[queue.name] = threads
        for thread in threads:
            thread.start()
        if count > 1:
            logger.info(
                "[QueueManager] Started %d worker threads for %s (max_concurrent=%d)",
                count,
                queue.name,
                max_concurrent,
            )

    def _max_concurrent_for_queue(self, queue_name: str) -> int:
        """Return the worker concurrency limit for a named queue."""
        if queue_name == self.DATA_CLEANUP:
            return 1
        if queue_name == self.EMBEDDING:
            return self._max_concurrent_embedding
        if queue_name == self.EXTERNAL_PARSE:
            return self._max_concurrent_external_parse
        if queue_name == self.ADD_RESOURCE:
            return self._max_concurrent_add_resource
        if queue_name == self.SESSION_COMMIT:
            return self._max_concurrent_session_commit
        if queue_name == self.EXTERNAL_TASK:
            return self._max_concurrent_external_task
        return self._max_concurrent_semantic

    def _queue_worker_loop(
        self, queue: NamedQueue, stop_event: threading.Event, max_concurrent: int = 1
    ) -> None:
        """Worker loop for a single queue.

        When max_concurrent > 1, items are fetched and processed in parallel
        (up to max_concurrent at a time). Otherwise items are processed one by one.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        poll_interval = (
            self._REQUEUE_POLL_INTERVAL
            if queue.name in {self.SESSION_COMMIT, self.EXTERNAL_TASK}
            else self._poll_interval
        )
        try:
            if max_concurrent > 1:
                loop.run_until_complete(
                    self._worker_async_concurrent(queue, stop_event, max_concurrent)
                )
            else:
                while not stop_event.is_set():
                    try:
                        queue_size = loop.run_until_complete(queue.size())
                        if queue.has_dequeue_handler() and queue_size > 0:
                            data = loop.run_until_complete(queue.dequeue())
                            if data is not None:
                                logger.debug("[QueueManager] Dequeued message from %s", queue.name)
                            if queue.name in {self.SESSION_COMMIT, self.EXTERNAL_TASK}:
                                stop_event.wait(poll_interval)
                        else:
                            stop_event.wait(poll_interval)
                    except Exception as e:
                        logger.error(f"[QueueManager] Worker error for {queue.name}: {e}")
                        traceback.print_exc()
                        stop_event.wait(poll_interval)
        finally:
            loop.close()

    async def _worker_async_concurrent(
        self, queue: NamedQueue, stop_event: threading.Event, max_concurrent: int
    ) -> None:
        """Concurrent worker: drains the queue and processes items in parallel.

        A Semaphore caps inflight tasks at max_concurrent.
        """
        poll_interval = (
            self._REQUEUE_POLL_INTERVAL
            if queue.name in {self.SESSION_COMMIT, self.EXTERNAL_TASK}
            else self._poll_interval
        )
        sem = asyncio.Semaphore(max_concurrent)
        active_tasks: Set[asyncio.Task] = set()

        async def process_batch(batch: list[Dict[str, Any]]) -> None:
            """Process a dequeued batch, then ack each member that settled.

            The handler commits the whole batch's vectors and publication
            receipts under one lock each, which is what keeps the store's single
            writer from serialising the queue. Members still ack individually,
            and a failed member is left unacked for recovery exactly as the
            per-message path left it.
            """
            try:
                results = await queue.process_dequeued_batch(batch)
            except Exception as e:  # noqa: BLE001
                logger.error(f"[QueueManager] Batched worker error for {queue.name}: {e}")
                return
            for data, result in zip(batch, results):
                msg_id = data.get("id", "") if isinstance(data, dict) else ""
                if result.outcome.value == "failed":
                    logger.error(
                        f"[QueueManager] Member failed in batch for {queue.name}: {result.error}"
                    )
                    continue
                try:
                    await queue.ack(msg_id, data)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"[QueueManager] Ack failed for {queue.name}: {e}")

        async def process_one(data: Dict[str, Any]) -> None:
            async with sem:
                msg_id = data.get("id", "") if isinstance(data, dict) else ""
                try:
                    await queue.process_dequeued(data)
                    # Ack after successful processing (delete from persistent storage).
                    await queue.ack(msg_id, data)
                except Exception as e:
                    # The message remains in processing and will be recovered.
                    logger.error(f"[QueueManager] Concurrent worker error for {queue.name}: {e}")

        while not stop_event.is_set():
            # Prune completed tasks
            active_tasks = {t for t in active_tasks if not t.done()}

            # While capacity remains, keep draining the queue. Fill the window
            # with one batched dequeue instead of one AGFS round-trip per
            # message: a serial per-message dequeue capped dispatch at
            # ~1/deq_latency tasks/s and left most of the concurrency idle.
            while len(active_tasks) < max_concurrent:
                if not queue.has_dequeue_handler():
                    break
                # Ask for a batch sized by the concurrency window, not by the
                # number of free task slots: with several worker threads each
                # requesting only its own remaining slots, the queue is split
                # into many tiny batches (measured: 57 batches of 2) and every
                # batch pays the per-batch commit cost. A batch amortises that
                # cost over its members, so take as many as the window allows.
                want = max_concurrent - len(active_tasks)
                if queue.name == self.EMBEDDING:
                    want = max(want, self._embedding_batch_target())
                batch = await queue.dequeue_batch_raw(want)
                if not batch:
                    break
                task = asyncio.create_task(process_batch(batch))
                active_tasks.add(task)
                logger.debug(
                    f"[QueueManager] Dispatched {len(batch)} tasks from batch dequeue "
                    f"for {queue.name} (active={len(active_tasks)})"
                )
                if len(batch) < 2:
                    # Either the queue is nearly drained or the backend lacks
                    # the batch operation; avoid a tight spin on it.
                    await asyncio.sleep(poll_interval)

            await asyncio.sleep(poll_interval)

        # Drain remaining in-flight tasks on shutdown (with timeout)
        if active_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*active_tasks, return_exceptions=True),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[QueueManager] Drain timeout for {queue.name}, "
                    f"cancelling {len(active_tasks)} in-flight task(s)"
                )
                for t in active_tasks:
                    t.cancel()
                await asyncio.gather(*active_tasks, return_exceptions=True)

    def stop(self) -> None:
        """Stop QueueManager and release resources."""
        global _instance
        if not self._started:
            return

        # Stop queue workers
        for stop_event in self._queue_stop_events.values():
            stop_event.set()
        for name, threads in self._queue_threads.items():
            for thread in threads:
                thread.join(timeout=10.0)
                if thread.is_alive():
                    logger.warning(
                        f"[QueueManager] Worker thread {name} did not exit in time"
                    )
        self._queue_threads.clear()
        self._queue_stop_events.clear()

        self._agfs = None
        self._queues.clear()
        self._started = False

        if _instance is self:
            _instance = None

        logger.info("[QueueManager] Stopped")

    def is_running(self) -> bool:
        """Check if QueueManager is running."""
        return self._started

    def get_queue(
        self,
        name: str,
        dequeue_handler: Optional[DequeueHandlerBase] = None,
        allow_create: bool = False,
    ) -> NamedQueue:
        """Get or create a named queue object."""
        if name not in self._queues:
            if not allow_create:
                raise RuntimeError(f"Queue {name} does not exist and allow_create is False")
            if name == self.EMBEDDING:
                self._queues[name] = EmbeddingQueue(
                    self._agfs,
                    self.mount_point,
                    name,
                    dequeue_handler=dequeue_handler,
                    middlewares=self._middlewares,
                )
            elif name == self.SEMANTIC:
                self._queues[name] = SemanticQueue(
                    self._agfs,
                    self.mount_point,
                    name,
                    dequeue_handler=dequeue_handler,
                    middlewares=self._middlewares,
                )
            else:
                self._queues[name] = NamedQueue(
                    self._agfs,
                    self.mount_point,
                    name,
                    dequeue_handler=dequeue_handler,
                    middlewares=self._middlewares,
                )
            if self._started:
                self._start_queue_worker(self._queues[name])
        else:
            if dequeue_handler is not None:
                self._queues[name].set_dequeue_handler(dequeue_handler)
            if self._started:
                # Ensure existing queue has a worker running
                self._start_queue_worker(self._queues[name])
        return self._queues[name]

    # ========== Compatibility convenience methods ==========

    async def enqueue(self, queue_name: str, data: Union[str, Dict[str, Any]]) -> str:
        """Send message to queue (enqueue)."""
        return await self.get_queue(queue_name).enqueue(data)

    async def dequeue(self, queue_name: str) -> Optional[Dict[str, Any]]:
        """Get message from specified queue."""
        return await self.get_queue(queue_name).dequeue()

    async def peek(self, queue_name: str) -> Optional[Dict[str, Any]]:
        """Peek at the head message of specified queue."""
        return await self.get_queue(queue_name).peek()

    async def size(self, queue_name: str) -> int:
        """Get the size of specified queue."""
        return await self.get_queue(queue_name).size()

    async def clear(self, queue_name: str) -> bool:
        """Clear specified queue."""
        return await self.get_queue(queue_name).clear()

    # ========== Status check interface ==========

    async def check_status(self, queue_name: Optional[str] = None) -> Dict[str, QueueStatus]:
        """Check queue status."""
        if queue_name:
            if queue_name not in self._queues:
                return {}
            return {queue_name: await self._queues[queue_name].get_status()}
        return {name: await q.get_status() for name, q in self._queues.items()}

    def has_errors(self, queue_name: Optional[str] = None) -> bool:
        """Check if there are errors."""
        if queue_name:
            if queue_name not in self._queues:
                return False
            return self._queues[queue_name]._error_count > 0
        return any(q._error_count > 0 for q in self._queues.values())

    async def is_all_complete(self, queue_name: Optional[str] = None) -> bool:
        """Check if all processing is complete."""
        statuses = await self.check_status(queue_name)
        return all(s.is_complete for s in statuses.values())

    async def wait_complete(
        self,
        queue_name: Optional[str] = None,
        timeout: Optional[float] = None,
        poll_interval: float = 0.5,
    ) -> Dict[str, QueueStatus]:
        """Wait for completion and return final status."""
        start = time.time()
        while True:
            if await self.is_all_complete(queue_name):
                return await self.check_status(queue_name)
            if timeout and (time.time() - start) > timeout:
                raise TimeoutError(f"Queue processing not complete after {timeout}s")
            await asyncio.sleep(poll_interval)
