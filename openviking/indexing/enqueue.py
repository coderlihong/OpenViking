# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Enqueue a single shared IndexPlan through the existing embedding worker."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import hashlib

from openviking.indexing.batch import begin_batched
from openviking.indexing.plan import chunk_fields, plan_resource
from openviking.storage.queuefs.embedding_msg import EmbeddingMsg


async def enqueue_text_plan(base_message, content, embedding_config, enqueue) -> int:
    """No queue work starts until the entire lossless plan validates its budgets.

    A configured registry seals the producer only after all messages enqueue.
    Consumers publish after every expected child write. The returned count is
    planned work, not proof of publication. GC remains a separate dependency gate.
    """
    base = dict(base_message.context_data)
    source_ref = base.pop("_source_snapshot", None)
    from openviking.indexing.record_meta import TEXT_CATEGORY, stamp_record_meta

    # Text parent/child chunks are stamped as the ``text`` category and carry the
    # chapter's own provenance block, so a hit can be filtered by modality and
    # cited back to document/chapter/page.
    stamp_record_meta(base, category=TEXT_CATEGORY, contents=(content,), source_uri=base.get("uri"))
    plan = plan_resource(content, base["uri"], base["account_id"], embedding_config)
    if embedding_config.chunking.publish_content:
        if source_ref is None:
            raise ValueError(
                "Content publication requires a verified source revision before enqueue"
            )
        if source_ref.get("text_sha256") != plan.content_revision:
            raise ValueError("Source revision does not match the chunk index plan")
    if not plan.chunks:
        if embedding_config.chunking.publish_content:
            raise ValueError("Empty content has no index plan; explicit clear/delete is required")
        return 0
    from openviking.indexing.publication import configured_store

    registry = configured_store(embedding_config)
    from openviking.indexing.metadata_integration import prepare_meta, bind_meta, write_meta
    prepared = (
        await asyncio.to_thread(
            prepare_meta, base, registry, plan.document_id, plan.content_revision
        )
        if registry
        else None
    )
    if prepared:
        plan = replace(plan, plan_id=hashlib.sha256((plan.plan_id + prepared["fingerprint"]).encode()).hexdigest())
    lexical = bool(getattr(embedding_config.chunking, "lexical_enabled", False))
    expected = [c.chunk_id for c in plan.chunks]
    if lexical:
        expected.append("lexical:" + plan.plan_id)
    if prepared:
        expected.append("metadata:" + plan.plan_id)
    generation = (
        await asyncio.to_thread(
            begin_batched,
            registry,
            base["account_id"],
            base.get("namespace_id", "resources"),
            base["uri"],
            plan.plan_id,
            expected,
            source_ref=source_ref,
        )
        if registry
        else None
    )
    try:
        if prepared:
            base.update(bind_meta(prepared, generation))
            await asyncio.to_thread(write_meta, registry, generation, prepared)
        await _enqueue_children(base_message, base, plan, enqueue, generation)
        if registry:
            if lexical:
                scalar = {key: base[key] for key in (
                    "search_tags", "tags", "created_at", "updated_at", "type", "name", "description", "owner_user_id"
                ) if key in base}
                await asyncio.to_thread(registry.lexical_write, generation, [
                    {**scalar, **chunk_fields(plan, chunk), "uri": base["uri"], "chunk_text": chunk.embedding_text}
                    for chunk in plan.chunks
                ])
            await asyncio.to_thread(registry.seal, generation)
            state = await asyncio.to_thread(registry.state, generation)
            if state["state"] in {"failed", "superseded"}:
                raise RuntimeError(state.get("error") or "Generation cannot publish")
    except BaseException:
        if registry:
            await asyncio.to_thread(
                registry.fail, generation, "Producer failed before publication seal"
            )
        raise
    return len(plan.chunks)


async def _enqueue_children(base_message, base, plan, enqueue, generation):
    from openviking.utils.embedding_utils import _enqueue_embedding_message

    for chunk in plan.chunks:
        fields = deepcopy(base)
        fields.update(chunk_fields(plan, chunk))
        fields["abstract"] = chunk.embedding_text
        fields["level"] = 2
        if generation:
            fields["index_generation"] = generation
        message = EmbeddingMsg(chunk.embedding_text, fields, telemetry_id=base_message.telemetry_id)
        # Adapter queue implements the same enqueue contract as ingestion/reindex.
        if not await _enqueue_embedding_message(
            enqueue, message, failure_message=f"Failed to enqueue chunk for {base['uri']}"
        ):
            raise RuntimeError(f"Chunk enqueue failed for {base['uri']}; index is not ready")
