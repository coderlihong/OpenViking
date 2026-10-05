# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""One durable text publication barrier per recursive producer invocation.

The context only coordinates producers. Queue consumers rely on SQL membership,
so receipts remain effective after the producer process has exited.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import signature

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
        state = self.registry.batch_state(self.id)
        if state["state"] == "failed":
            raise RuntimeError(state["error"] or "Text publication batch failed")


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
    """Seal only after all recursive producers succeed, including counter errors.

    Nested calls and concurrently created asyncio tasks share the outer batch.
    No publication registry means this decorator preserves the legacy behavior.
    A queued/sealed batch is pending, never a claim that its task is ready.
    """
    sig = signature(method)

    @wraps(method)
    async def wrapped(*args, **kwargs):
        if _batch.get() is not None:
            return await method(*args, **kwargs)
        counters = sig.bind_partial(*args, **kwargs).arguments.get("counters")
        failed_before = getattr(counters, "failed_records", 0)
        batch = TextPublicationBatch()
        token = _batch.set(batch)
        try:
            result = await method(*args, **kwargs)
            outcome = getattr(result, "outcome", None)
            success = (outcome is None or outcome.value == "success") and getattr(
                counters, "failed_records", 0
            ) == failed_before
            batch.finish(success)
            return result
        except BaseException:
            batch.finish(False)
            raise
        finally:
            _batch.reset(token)

    return wrapped
