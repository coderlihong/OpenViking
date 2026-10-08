# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Private image semantics cache bound to a published asset and source context."""

import hashlib
import json
from pathlib import Path

from openviking.indexing.content import archive_bytes, blob_path, draft_sources
from openviking.indexing.image_context import VERSION as CONTEXT_VERSION, collect_image_contexts
from openviking.indexing.publication import configured_store
from openviking_cli.utils.config import get_openviking_config

VERSION = "image-semantics-cache-v2"


def semantics_signature():
    settings = get_openviking_config()
    vlm = getattr(settings, "vlm", None)
    config = vlm.model_dump(mode="json") if hasattr(vlm, "model_dump") else None
    if hasattr(settings, "prompts"):
        from openviking.parse.parsers.media.utils import render_prompt
    else:
        # Minimal test configurations omit prompts; production config always
        # uses the active renderer, including custom templates and its cache.
        from openviking.prompts.manager import PromptManager
        render_prompt = PromptManager(Path(__file__).parents[1] / "prompts/templates").render
    prompt = render_prompt("parsing.image_summary", {"context": "<image-cache-signature>"})
    return hashlib.sha256(json.dumps(
        [VERSION, CONTEXT_VERSION, config, hashlib.sha256(prompt.encode()).hexdigest()],
        sort_keys=True,
    ).encode()).hexdigest()


def occurrence_identity(record):
    """Identity of one reference: its source, context hash and write epoch."""
    return (
        record.get("context_uri", ""),
        record.get("context_hash", ""),
        record.get("context_epoch"),
    )


def cache_key(signature, occurrences):
    """Content-addressed cache key binding model/prompt to every reference context.

    Including each occurrence's context hash means a change to the referencing
    chapter (or its epoch) yields a different key, so a stale summary can never be
    reused for a new context.
    """
    return hashlib.sha256(
        json.dumps(
            [VERSION, signature, [occurrence_identity(r) for r in occurrences]],
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


async def store_image_semantics(fs, ctx, asset_ref, occurrences):
    signature = semantics_signature()
    if any(r.get("semantics_signature", signature) != signature for r in occurrences):
        raise ValueError("Image semantics model or prompt changed after summary generation")
    payload = {
        "version": VERSION,
        "signature": signature,
        "cache_key": cache_key(signature, occurrences),
        "asset_sha256": asset_ref["sha256"],
        "occurrences": occurrences,
    }
    return await archive_bytes(fs, json.dumps(payload, ensure_ascii=False, sort_keys=True).encode(), ctx)


async def load_image_semantics(fs, uri, ctx):
    """Only published caches may be reused; source ACL and working bytes rechecked."""
    registry = configured_store()
    if registry is None:
        return None
    if registry.is_tombstoned(ctx.account_id, uri):
        raise ValueError("SEMANTICS_REQUIRED: image asset was deleted")
    lease, manifest = registry.capture(ctx.account_id)
    try:
        reference = manifest.get(uri, {}).get("content", {})
        cache_ref = reference.get("image_semantics")
        if not cache_ref:
            if reference.get("image_plan_version"):
                raise ValueError("SEMANTICS_REQUIRED: published image has no occurrence cache")
            # A joint build that failed before publication leaves the URI in
            # desired with an image: member but out of every snapshot manifest.
            # That is a semantics-requiring state, not "never published as
            # joint": silently returning None would re-publish the stale
            # text-only abstract as the joint input.
            if registry.declared_image_plan(ctx.account_id, uri):
                raise ValueError("SEMANTICS_REQUIRED: unpublished joint image plan for asset")
            return None
        # Authorization happens before reading a private cache; all sources are
        # then read with the caller context through collect_image_contexts.
        with draft_sources():
            raw_image = await fs.read_file_bytes(uri, ctx=ctx)
        if hashlib.sha256(raw_image).hexdigest() != reference["sha256"]:
            raise ValueError("SEMANTICS_REQUIRED: image asset changed")
        raw = fs._handle_agfs_read(await fs._async_agfs.read(blob_path(fs, ctx, cache_ref["sha256"])))
        if len(raw) != cache_ref["size"] or hashlib.sha256(raw).hexdigest() != cache_ref["sha256"]:
            raise RuntimeError("Image semantics cache is missing or corrupted")
        payload = json.loads(raw)
        signature = semantics_signature()
        if (payload.get("version") != VERSION or payload.get("signature") != signature
                or payload.get("asset_sha256") != reference["sha256"]):
            raise ValueError("SEMANTICS_REQUIRED: image semantics configuration changed")
        current = await collect_image_contexts(fs, uri, ctx)
        records = payload["occurrences"]
        if [occurrence_identity(r) for r in current] != [
            occurrence_identity(r) for r in records if r.get("context_uri")
        ]:
            raise ValueError("SEMANTICS_REQUIRED: image referencing context changed")
        # The cache key binds the model/prompt signature to every reference
        # context; a mismatch means the cached summary no longer describes the
        # current referencing context and must not be reused.
        if payload.get("cache_key") != cache_key(signature, records):
            raise ValueError("SEMANTICS_REQUIRED: image summary cache key does not match context")
        registry.renew(lease)
        return {"name": uri.rsplit("/", 1)[-1], "summary": "\n\n".join(r["summary"] for r in records), "image_occurrences": records}
    finally:
        registry.release(lease)
