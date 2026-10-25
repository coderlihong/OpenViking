# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Orphaned queue work must stop once its task is terminal.

A restart settles the previous generation's in-flight task as ``failed``
(``_fail_orphaned``). The QueueFS messages that task had already enqueued are
durable and survive the restart -- by design, because that is what makes queued
work recoverable. The problem was that nothing told them to stop: the queue
middleware asks ``TaskTracker.is_cancellation_requested`` before processing each
message, and that predicate only answered for ``CANCELLING``/``CANCELLED``.

So a failed task's messages kept draining. Measured live on 2026-10-25:

* the task showed ``failed`` ("orphaned: owning server process exited"),
* 22,391 Semantic-Nodes were still pending, draining at ~0.8 nodes/s (~7.6h),
* those nodes write L0/L1 sidecars, so they held the target tree's pathlock,
* every later reindex of that tree returned ``409 path_busy``, and
* ``POST /tasks/{id}/cancel`` answered **412** because the record was already
  terminal -- there was no API left to stop it.

``FAILED`` is terminal, and a terminal task has no consumer left to publish for,
so its remaining queue work is orphaned by definition. These tests pin that the
predicate says so.
"""

from __future__ import annotations

import pytest

from openviking.server.identity import RequestContext, Role
from openviking.service.task_store import PersistentTaskStore
from openviking.service.task_tracker import TaskStatus, TaskTracker, set_task_tracker
from openviking_cli.session.user_id import UserIdentifier

pytestmark = pytest.mark.asyncio

ACCOUNT = "acme"
USER = "alice"


class _FakeAgfs:
    """Minimal AGFS double for the persistent task store (mirrors test_task_tracker)."""

    def __init__(self):
        self.files = {}
        self.dirs = {"/", "/local"}

    def mkdir(self, path, mode="755"):
        self.dirs.add(path.rstrip("/") or "/")
        return {"message": "created", "mode": mode}

    def write(self, path, data, max_retries=3, *, ctx=None):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.files[path] = data
        self.dirs.add(path.rsplit("/", 1)[0] or "/")
        return "OK"

    def read(self, path, offset=0, size=-1, stream=False):
        if path not in self.files:
            from openviking.pyagfs.exceptions import AGFSNotFoundError

            raise AGFSNotFoundError(path)
        raw = self.files[path]
        return raw[offset:] if size == -1 else raw[offset : offset + size]

    def exists(self, path):
        return path in self.files or path in self.dirs

    def stat(self, path):
        if path in self.dirs:
            return {"isDir": True, "size": 0}
        if path in self.files:
            return {"isDir": False, "size": len(self.files[path])}
        from openviking.pyagfs.exceptions import AGFSNotFoundError

        raise AGFSNotFoundError(path)

    def ls(self, path, **kwargs):
        prefix = path.rstrip("/") + "/"
        return [p for p in list(self.files) + list(self.dirs) if p.startswith(prefix)]

    def rm(self, path, **kwargs):
        self.files.pop(path, None)
        self.dirs.discard(path)
        return "OK"


@pytest.fixture(autouse=True)
def clean_singleton():
    set_task_tracker(None)
    yield
    set_task_tracker(None)


@pytest.fixture
def tracker() -> TaskTracker:
    return TaskTracker(store=PersistentTaskStore(_FakeAgfs()))


def _ctx() -> RequestContext:
    return RequestContext(user=UserIdentifier(ACCOUNT, USER), role=Role.ADMIN)


async def test_a_failed_task_stops_its_queued_work(tracker: TaskTracker):
    """The regression: a terminal (failed) task must read as "stop working"."""
    task = await tracker.create("admin_reindex", account_id=ACCOUNT, user_id=USER)
    await tracker.start(task.task_id, stage="queued")
    assert tracker.is_cancellation_requested(task.task_id) is False

    await tracker.fail(task.task_id, "orphaned: owning server process exited")

    recorded = await tracker.get(task.task_id)
    assert recorded.status == TaskStatus.FAILED
    assert tracker.is_cancellation_requested(task.task_id) is True


async def test_completed_tasks_need_no_stop_and_cancelled_ones_do(tracker: TaskTracker):
    """``COMPLETED`` is only reachable once its work has drained, so it is moot.

    ``complete`` goes through ``_finalize_task_on_owner``, which returns early
    while ``has_work()`` is true -- a task can only become ``COMPLETED`` after
    every message it owns has settled. ``CANCELLED`` is the opposite case and
    must stop the work.
    """
    done = await tracker.create("admin_reindex", account_id=ACCOUNT, user_id=USER)
    await tracker.start(done.task_id)
    await tracker.complete(done.task_id, {})
    # No work was ever registered for it in this unit test, so it finalizes now.
    assert (await tracker.get(done.task_id)).status == TaskStatus.COMPLETED

    cancelled = await tracker.create("admin_reindex", account_id=ACCOUNT, user_id=USER)
    await tracker.start(cancelled.task_id)
    await tracker.record_cancelled(cancelled.task_id)
    assert tracker.is_cancellation_requested(cancelled.task_id) is True


async def test_a_running_task_keeps_working(tracker: TaskTracker):
    """Only terminal states stop work; a live task must not be mistaken for one."""
    task = await tracker.create("admin_reindex", account_id=ACCOUNT, user_id=USER)
    await tracker.start(task.task_id, stage="semantic:viking://resources/raw")
    assert tracker.is_cancellation_requested(task.task_id) is False

    await tracker.update_stage(task.task_id, "vectors:viking://resources/raw")
    assert tracker.is_cancellation_requested(task.task_id) is False


async def test_an_unknown_task_does_not_stop_work(tracker: TaskTracker):
    """No record is not the same as a terminal record: leave the work alone."""
    assert tracker.is_cancellation_requested("no-such-task") is False
