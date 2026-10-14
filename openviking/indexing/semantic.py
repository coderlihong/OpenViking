# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Publish directory navigation vectors in the same barrier as text children."""

import hashlib
import json

from openviking.indexing.batch import current_batch_id
from openviking.indexing.publication import configured_store
from openviking.storage.vector_ids import publication_member_id


async def enqueue_semantic_plan(messages, embedding_config, queue, semantic_refs=None):
    registry = configured_store(embedding_config)
    if registry is None or not messages:
        raise ValueError("Semantic publication requires a registry and nonempty members")
    if embedding_config.chunking.publish_content and semantic_refs is None:
        raise ValueError("Semantic content publication requires verified sidecar revisions")
    first = messages[0].context_data
    identity = (first["account_id"], first["uri"], first.get("namespace_id", "resources"))
    for message in messages:
        fields = message.context_data
        if fields.get("context_type") != "resource" or fields.get("is_leaf"):
            raise ValueError("Semantic publication only supports resource directory records")
        if (
            fields["account_id"],
            fields["uri"],
            fields.get("namespace_id", "resources"),
        ) != identity:
            raise ValueError("Directory semantic plan must have one source identity")
    members = [publication_member_id(m.context_data) for m in messages]
    body = json.dumps([(m.context_data["level"], m.message) for m in messages], ensure_ascii=False)
    revision = hashlib.sha256(body.encode()).hexdigest()
    from openviking.indexing.metadata_integration import prepare_meta, bind_meta, write_meta
    from openviking.indexing.record_meta import TEXT_CATEGORY, stamp_record_meta

    # L0/L1 directory summaries are text-modality vectors: they carry the same
    # ``category=text`` discriminator as the chunks below them, so a query can
    # exclude every non-image row without guessing from ``level``.
    stamp_record_meta(
        first, category=TEXT_CATEGORY, contents=(m.message for m in messages), source_uri=first["uri"]
    )
    document_id = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    prepared = prepare_meta(first, registry, document_id, revision)
    for message in messages[1:]:
        message.context_data.pop("_business_meta_input", None)
    config = embedding_config.model_dump(mode="json")
    config["chunking"].pop("publication_path", None)
    config["chunking"].pop("publication_authority_socket", None)
    config["chunking"].pop("publication_authority_token_file", None)
    config["chunking"].pop("tokenizer_path", None)
    plan = hashlib.sha256(
        json.dumps(["directory-semantic-v1", identity, revision, config, prepared["fingerprint"] if prepared else None], sort_keys=True).encode()
    ).hexdigest()
    if prepared:
        members.append("metadata:" + plan)
    generation = registry.begin(
        identity[0],
        identity[2],
        identity[1],
        plan,
        members,
        batch=current_batch_id(registry, identity[0], identity[2]),
        semantic_refs=semantic_refs,
    )
    try:
        write_meta(registry, generation, prepared)
        from openviking.utils.embedding_utils import _enqueue_embedding_message

        for message in messages:
            message.context_data.update(
                index_plan_id=plan, index_generation=generation, content_revision=revision
            )
            message.context_data.update(bind_meta(prepared, generation))
            if not await _enqueue_embedding_message(
                queue,
                message,
                failure_message=f"Failed directory semantic enqueue for {identity[1]}",
            ):
                raise RuntimeError("Directory semantic enqueue was rejected")
        registry.seal(generation)
        state = registry.state(generation)
        if state["state"] in {"failed", "superseded"}:
            raise RuntimeError(state.get("error") or "Directory generation cannot publish")
    except BaseException:
        registry.fail(generation, "Directory producer failed before publication seal")
        raise
    return len(messages)
