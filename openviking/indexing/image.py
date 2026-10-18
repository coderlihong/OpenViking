# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Stage a joint image vector and its original asset behind the shared barrier."""

import asyncio
from copy import deepcopy
from functools import partial
import hashlib
import json

from openviking.indexing.batch import begin_batched
from openviking.indexing.publication import configured_store

VERSION = "image-occurrences-v3"


async def enqueue_image_plan(message, embedding_config, queue, source_ref=None, occurrences=None, fs=None, ctx=None):
    fields = message.context_data
    parts = message.message
    if not fields.get("_require_joint_image_embedding") or not isinstance(parts, list):
        raise ValueError("Image plan requires a strict joint embedding envelope")
    if not any(p.get("type") == "image_url" for p in parts) or not any(
        p.get("type") == "text" and p.get("text", "").strip() for p in parts
    ):
        raise ValueError("Image plan requires summary and image input")
    if embedding_config.chunking.publish_content and source_ref is None:
        raise ValueError("Image content publication requires a verified asset revision")
    if source_ref:
        source_ref = dict(source_ref, image_plan_version=VERSION)
    identity = (fields["account_id"], fields.get("namespace_id", "resources"), fields["uri"])
    config = embedding_config.model_dump(mode="json")
    config["chunking"].pop("publication_path", None)
    config["chunking"].pop("publication_authority_socket", None)
    config["chunking"].pop("publication_authority_token_file", None)
    config["chunking"].pop("tokenizer_path", None)
    model_config = config.get("hybrid") or config.get("dense")
    if model_config is None:
        raise ValueError("Joint image indexing requires a dense embedding space")
    space = hashlib.sha256(
        json.dumps(["joint-image-space-v1", model_config], sort_keys=True).encode()
    ).hexdigest()
    records = occurrences or [{"summary": fields["abstract"], "context_status": "unreferenced"}]
    for record in records:
        if not record.get("summary", "").strip():
            raise ValueError("Every image occurrence requires a nonempty summary")
        if source_ref and record.get("asset_sha256") and record["asset_sha256"] != source_ref["sha256"]:
            raise ValueError("Image changed after its VLM summary was generated")
    if source_ref:
        records = [dict(record, asset_sha256=source_ref["sha256"]) for record in records]
        from openviking.indexing.image_cache import store_image_semantics

        if fs is None or ctx is None:
            raise ValueError("Image cache publication requires authorized asset storage")
        source_ref["image_semantics"] = await store_image_semantics(fs, ctx, source_ref, records)
        epochs = {}
        for record in records:
            if record.get("context_uri") and "context_epoch" in record:
                uri, epoch = record["context_uri"], record["context_epoch"]
                if uri in epochs and epochs[uri] != epoch:
                    raise ValueError("Image context changed while preparing occurrences")
                epochs[uri] = epoch
        source_ref["image_context_epochs"] = epochs
    image_parts = [p for p in parts if p.get("type") == "image_url"]
    revision = hashlib.sha256(json.dumps([image_parts, records], sort_keys=True).encode()).hexdigest()
    registry = configured_store(embedding_config)
    from openviking.indexing.metadata_integration import prepare_meta, bind_meta, write_meta
    from openviking.indexing.record_meta import IMAGE_CATEGORY, stamp_record_meta

    # Joint image embeddings are stamped as the ``images`` category and point back
    # at the chapter they illustrate, so an image hit never gets mistaken for a
    # text chunk (or filtered out of one) at query time. The chapter's own
    # provenance fields ride along from the occurrence, otherwise a query narrowed
    # to a machine model would answer with its body text and silently drop its
    # figures.
    provenance = {}
    for record in records:
        for key, value in (record.get("source_meta") or {}).items():
            provenance.setdefault(key, value)
    stamp_record_meta(
        fields,
        category=IMAGE_CATEGORY,
        contents=[record.get("context") or record.get("summary") for record in records],
        source_uri=next((r.get("context_uri") for r in records if r.get("context_uri")), None),
        image_uri=fields.get("uri"),
        values=provenance,
    )
    document_id = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    prepared = (
        # metadata_latest() takes a query lease (a write transaction); keeping it
        # on the event loop froze every in-flight request while a bulk publish
        # held the write lock.
        await asyncio.to_thread(
            prepare_meta,
            fields,
            registry,
            document_id,
            source_ref["sha256"] if source_ref else revision,
        )
        if registry
        else None
    )
    plan = hashlib.sha256(
        json.dumps([VERSION, identity, revision, config, prepared["fingerprint"] if prepared else None], sort_keys=True).encode()
    ).hexdigest()
    from openviking.storage.queuefs.embedding_msg import EmbeddingMsg
    from openviking.utils.embedding_utils import _truncate_abstract_bytes

    members = []
    messages = []
    for ordinal, record in enumerate(records):
        member = "image:" + hashlib.sha256(
            json.dumps([plan, ordinal, record], sort_keys=True).encode()
        ).hexdigest()
        data = deepcopy(fields)
        data.update(
            chunk_id=member,
            parent_chunk_id="",
            document_id=hashlib.sha256(json.dumps(identity).encode()).hexdigest(),
            abstract=_truncate_abstract_bytes(record["summary"]),
            chunk_text=record["summary"],
            parent_text=record.get("context", ""),
            image_context_uri=record.get("context_uri", ""),
            image_context_hash=record.get("context_hash", ""),
            chunk_ordinal=ordinal,
            chunker_version=VERSION,
            embedding_space_id=space,
            content_revision=source_ref["sha256"] if source_ref else revision,
            index_plan_id=plan,
        )
        joint = [{"type": "text", "text": record["summary"]},
                 *[deepcopy(p) for p in parts if p.get("type") == "image_url"]]
        messages.append(EmbeddingMsg(joint, data, telemetry_id=message.telemetry_id))
        members.append(member)
    generation = None
    if registry:
        if prepared:
            members.append("metadata:" + plan)
        # The registry serialises every writer behind one SQLite write lock, and
        # a bulk publication can hold it for seconds.  Running this transaction
        # inline stalled the event loop for that whole window (the loop watchdog
        # caught the main thread inside publication.db `BEGIN IMMEDIATE` reached
        # from here, with /health at 7s).  The store keeps a per-thread
        # connection, so a worker thread waits on the lock without freezing
        # every in-flight VLM and embedding request.
        generation = await asyncio.to_thread(
            begin_batched,
            registry,
            identity[0],
            identity[1],
            *identity[2:],
            plan,
            members,
            source_ref=source_ref,
        )
        for item in messages:
            item.context_data["index_generation"] = generation
            item.context_data.update(bind_meta(prepared, generation))
    try:
        await asyncio.to_thread(write_meta, registry, generation, prepared)
        from openviking.utils.embedding_utils import _enqueue_embedding_message

        for item in messages:
            if not await _enqueue_embedding_message(
                queue, item, failure_message=f"Failed joint image enqueue for {identity[2]}"
            ):
                raise RuntimeError("Image enqueue failed; index is not ready")
        if registry:
            await asyncio.to_thread(registry.seal, generation)
            state = await asyncio.to_thread(registry.state, generation)
            if state["state"] in {"failed", "superseded"}:
                raise RuntimeError(state.get("error") or "Image generation cannot publish")
    except BaseException:
        if registry:
            await asyncio.to_thread(
                registry.fail, generation, "Image producer failed before publication seal"
            )
        raise
    return len(messages)
