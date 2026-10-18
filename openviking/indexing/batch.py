# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""One durable text publication barrier per recursive producer invocation.

The context only coordinates producers. Queue consumers rely on SQL membership,
so receipts remain effective after the producer process has exited.
"""

import asyncio
import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)

_batch = ContextVar("text_publication_batch", default=None)

_CHUNK_ENV = "OPENVIKING_PUBLICATION_CHUNK_MEMBERS"
_CHUNK_SECONDS_ENV = "OPENVIKING_PUBLICATION_CHUNK_SECONDS"
_DEFAULT_CHUNK_MEMBERS = 1500
_DEFAULT_CHUNK_SECONDS = 300.0
# The store's own wording for "this batch already sealed"; matched instead of the
# exception type so the same recovery works over the authority socket proxy.
_SEALED_BATCH_ERROR = "no longer accepts members"


def _env_int(name, default):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(0, value)


def publication_chunk_members() -> int:
    """Members one publication batch accepts before the producer rolls over.

    A recursive producer used to bind its whole tree to a single batch, so
    nothing became visible until the last file finished, a restart left every
    receipt invisible, and the eventual publish scanned the entire tree at once.
    Sealing a filled chunk keeps publication incremental without changing the
    per-member receipt rule: a chunk still publishes only once every one of its
    members has all of its receipts. ``0`` disables rollover and keeps the
    legacy one-batch-per-producer behavior.
    """
    return _env_int(_CHUNK_ENV, _DEFAULT_CHUNK_MEMBERS)


def publication_chunk_seconds() -> float:
    """Age at which a partly filled chunk is sealed at the next member.

    The member count alone would leave the last partial chunk unpublished until
    the producer returns, which for a multi-hour tree is the original stall. A
    time bound keeps the tail chunk bounded as well. ``0`` disables it.
    """
    raw = os.environ.get(_CHUNK_SECONDS_ENV)
    if raw is None or not raw.strip():
        return _DEFAULT_CHUNK_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_CHUNK_SECONDS
    return max(0.0, value)


class TextPublicationBatch:
    def __init__(self):
        self.registry = None
        self.id = None
        self.closed = False
        self.members = 0
        self.opened_at = 0.0
        # ``join`` runs inside worker threads (async producers reach it through
        # ``asyncio.to_thread``), so the batch has to be created under a lock.
        # Without it a second producer could observe ``self.registry`` already
        # assigned but ``self.id`` still None, join with ``None``, and escape the
        # batch entirely -- publishing on its own while its siblings are still
        # building, which is exactly the atomicity the batch exists to provide.
        self._lock = threading.Lock()

    def join(self, registry, account, namespace):
        with self._lock:
            if self.closed:
                raise RuntimeError("Text publication producer already closed")
            if self.registry is None:
                self.registry = registry
                self.id = registry.begin_batch(account, namespace)
                self.opened_at = time.monotonic()
            elif self.registry.path != registry.path:
                raise ValueError("Text publication batch cannot cross registries")
            chunk = publication_chunk_members()
            age = publication_chunk_seconds()
            filled = chunk and self.members >= chunk
            expired = age and self.members and time.monotonic() - self.opened_at >= age
            if self.members and (filled or expired):
                # The filled (or aged) chunk is sealed here; it publishes on its own
                # as soon as its last member reports every receipt. This member
                # continues in a fresh batch, so an interrupted producer loses at
                # most one chunk.
                registry.seal_batch(self.id)
                self.id = registry.begin_batch(account, namespace)
                self.members = 0
                self.opened_at = time.monotonic()
            self.members += 1
            return self.id

    def reopen(self, registry, account, namespace):
        """Replace a batch that already sealed, so a late member still publishes.

        A producer still in flight when its chunk rolls over (or when a sibling
        task finishes the producer) holds the sealed id in its context, and the
        store refuses members on a sealed batch.  Failing there dropped the
        member for good -- observed live: 20 "Failed chunk reindex ...
        Publication batch no longer accepts members" records on one image.
        """
        with self._lock:
            if self.registry is not None and self.registry.path != registry.path:
                raise ValueError("Text publication batch cannot cross registries")
            self.registry = registry
            self.id = registry.begin_batch(account, namespace)
            self.members = 0
            self.opened_at = time.monotonic()
            return self.id

    def finish(self, success):
        with self._lock:
            self.closed = True
            if self.id is None:
                return
            if not success:
                self.registry.fail_batch(self.id, "Text batch producer did not complete successfully")
                return
            self.registry.seal_batch(self.id)


def producer_batch_context():
    return _batch.get()


@contextmanager
def bind_text_batch(batch):
    token = _batch.set(batch)
    try:
        yield
    finally:
        _batch.reset(token)


def current_batch_id(registry, account, namespace):
    batch = _batch.get()
    return batch.join(registry, account, namespace) if batch else None


def begin_batched(registry, account, namespace, *args, **kwargs):
    """``registry.begin`` with the running text publication batch attached.

    ``current_batch_id`` can roll the batch over, and rolling over seals the
    full chunk -- a write transaction against the single-writer store.  Async
    producers call this through ``asyncio.to_thread``: resolving the batch on
    the event loop would put that transaction in front of every waiting
    request, which is the convoy the loop watchdog kept catching.

    The batch can also seal before a still-running member reaches the store
    (chunk rollover, or a sibling finishing the producer).  The store refuses a
    sealed batch, so continue in a fresh chunk instead of dropping the member.
    """
    kwargs["batch"] = current_batch_id(registry, account, namespace)
    try:
        return registry.begin(account, namespace, *args, **kwargs)
    except RuntimeError as exc:
        if _SEALED_BATCH_ERROR not in str(exc):
            raise
        batch = _batch.get()
        if batch is not None:
            kwargs["batch"] = batch.reopen(registry, account, namespace)
        else:
            kwargs["batch"] = registry.begin_batch(account, namespace)
        logger.warning(
            "publication batch was sealed before this member arrived; "
            "continuing in a fresh chunk %s",
            kwargs["batch"],
        )
        return registry.begin(account, namespace, *args, **kwargs)


def text_publication_batch(method):
    """Seal the batch once the producer returns, unless it raised.

    A member the producer could not build is marked failed by that member's own
    path; it drops out of the publication while the rest of the batch publishes.
    Nested calls and concurrently created asyncio tasks share the outer batch.
    No publication registry means this decorator preserves the legacy behavior.
    A queued/sealed batch is pending, never a claim that its task is ready.
    """
    @wraps(method)
    async def wrapped(*args, **kwargs):
        if _batch.get() is not None:
            return await method(*args, **kwargs)
        batch = TextPublicationBatch()
        token = _batch.set(batch)
        try:
            result = await method(*args, **kwargs)
            # Sealing the final chunk is a write transaction; keep it off the
            # event loop like every other publication write on this path.
            await asyncio.to_thread(batch.finish, True)
            return result
        except BaseException:
            await asyncio.to_thread(batch.finish, False)
            raise
        finally:
            _batch.reset(token)

    return wrapped
