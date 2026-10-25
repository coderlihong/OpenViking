# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""A batched embedding commit must settle the request's wait roots.

``TextEmbeddingHandler.on_dequeue_batch`` defers its writes: each member's
``_process_message`` appends to ``collector["upserts"]`` and returns success
*without* recording an outcome, because at that moment nothing has been written.
The outcome has to be recorded once the batch's vectors have landed.

It was not. Every chunk indexed through the batch path therefore left its
``pending_embedding_roots`` entry set forever. The reindex waiting on that
telemetry id has no whole-request bound -- a large tree legitimately queues work
for hours -- so it could only ever end at the stall bound, and it reported the
run as stalled with one embedding root pending.

Measured live (2026-10-25) before the fix:

* ``reindex viking://resources/quick/d.md`` -> HTTP 200 after **308s**,
  ``failed_records: 1``, warning "no queue progress ... 1 embedding root(s)
  still pending";
* the same file's vector had in fact been written and was already returning
  search hits (score 0.936) -- the work was done, only the bookkeeping was lost;
* ``mark_embedding_done`` was never called once for the whole process lifetime.

After the fix the same request completes in ~8.8s with ``failed_records: 0`` and
no warnings.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from openviking.storage.collection_schemas import TextEmbeddingHandler
from openviking.telemetry.request_wait_tracker import get_request_wait_tracker

pytestmark = pytest.mark.asyncio


class _Embedder:
    """Minimal embedder double: the handler applies this guard before calling out."""

    supports_multimodal = False
    max_input_tokens = None

    def prepare_embedding_input(self, content):
        return content

    async def embed_async(self, _text, is_query: bool = False):
        from openviking.models.embedder.base import EmbedResult

        return EmbedResult(dense_vector=[0.1, 0.2])

    async def embed(self, *_args, **_kwargs):
        from openviking.models.embedder.base import EmbedResult

        return EmbedResult(dense_vector=[0.1, 0.2])


class _Config:
    storage = SimpleNamespace(vectordb=SimpleNamespace(name="ctx"))
    embedding = SimpleNamespace(
        dimension=2,
        get_embedder=lambda: _Embedder(),
        circuit_breaker=SimpleNamespace(
            failure_threshold=5, reset_timeout=300.0, max_reset_timeout=300.0
        ),
    )


class _RecordingVikingDB:
    """Records upserts so the test can assert the vector really was written."""

    is_closing = False
    uses_content_field = False
    has_queue_manager = False

    def __init__(self):
        self.upserted = []

    async def upsert(self, data, *, ctx=None, options=None):
        self.upserted.append(data)
        return "rec"

    async def upsert_many(self, data_list, *, ctx=None, options=None):
        self.upserted.extend(data_list)
        return ["rec"] * len(data_list)


def _payload(telemetry_id: str, msg_id: str) -> dict:
    return {
        "data": json.dumps(
            {
                "id": msg_id,
                "message": "hydraulic pump pressure",
                "telemetry_id": telemetry_id,
                "context_data": {
                    "id": "ctx-1",
                    "uri": "viking://resources/batch/a.md",
                    "account_id": "default",
                    "abstract": "hydraulic pump pressure",
                },
            }
        )
    }


@pytest.fixture()
def handler(monkeypatch):
    monkeypatch.setattr(
        "openviking_cli.utils.config.get_openviking_config",
        lambda: _Config(),
    )
    db = _RecordingVikingDB()
    h = TextEmbeddingHandler(db)
    return h, db


def _reset_tracker(telemetry_id: str) -> None:
    tracker = get_request_wait_tracker()
    tracker.register_request(telemetry_id)


async def test_batched_commit_settles_the_wait_root(handler):
    """The regression: a batch member must not leave its root pending."""
    h, db = handler
    telemetry_id = "tm_batch_settle_1"
    _reset_tracker(telemetry_id)
    tracker = get_request_wait_tracker()
    tracker.register_embedding_root(telemetry_id, "msg-1")
    assert tracker.is_complete(telemetry_id) is False

    await h.on_dequeue_batch([_payload(telemetry_id, "msg-1")])

    # The vector really was written...
    assert db.upserted, "the batch path must persist the vector"
    # ...and the request must learn that its work finished.
    assert tracker.is_complete(telemetry_id) is True, (
        "batched commit left the request's embedding root pending: the reindex "
        "waiting on it can then only end at its stall bound"
    )
    tracker.cleanup(telemetry_id)


async def test_a_batch_of_several_members_settles_every_root(handler):
    """Every member of the batch settles, not just the first."""
    h, db = handler
    telemetry_id = "tm_batch_settle_2"
    _reset_tracker(telemetry_id)
    tracker = get_request_wait_tracker()
    ids = ["msg-a", "msg-b", "msg-c"]
    for mid in ids:
        tracker.register_embedding_root(telemetry_id, mid)
    assert tracker.pending_counts(telemetry_id) == (0, 3)

    await h.on_dequeue_batch([_payload(telemetry_id, mid) for mid in ids])

    assert len(db.upserted) == 3
    assert tracker.is_complete(telemetry_id) is True
    tracker.cleanup(telemetry_id)


async def test_the_single_message_path_still_settles(handler):
    """``on_dequeue`` records its own outcome; the fix must not disturb it."""
    h, db = handler
    telemetry_id = "tm_single_settle"
    _reset_tracker(telemetry_id)
    tracker = get_request_wait_tracker()
    tracker.register_embedding_root(telemetry_id, "msg-single")

    await h.on_dequeue(_payload(telemetry_id, "msg-single"))

    assert db.upserted
    assert tracker.is_complete(telemetry_id) is True
    tracker.cleanup(telemetry_id)
