# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""One durable text publication barrier per recursive producer invocation.

The context only coordinates producers. Queue consumers rely on SQL membership,
so receipts remain effective after the producer process has exited.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

_batch = ContextVar("text_publication_batch", default=None)


class TextPublicationBatch:
    def __init__(self):
        self.registry = None
        self.id = None
        self.closed = False

    def join(self, registry, account, namespace):
        if self.closed:
            raise RuntimeError("Text publication producer already closed")
        if self.registry is None:
            self.registry = registry
            self.id = registry.begin_batch(account, namespace)
        elif self.registry.path != registry.path:
            raise ValueError("Text publication batch cannot cross registries")
        return self.id

    def finish(self, success):
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
            batch.finish(True)
            return result
        except BaseException:
            batch.finish(False)
            raise
        finally:
            _batch.reset(token)

    return wrapped
