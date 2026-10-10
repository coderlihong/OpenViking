# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Private image semantics cache bound to a published asset and source context."""

import asyncio
import hashlib
import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from openviking.indexing.content import archive_bytes, blob_path, draft_sources
from openviking.indexing.image_context import VERSION as CONTEXT_VERSION, collect_image_contexts
from openviking.indexing.publication import configured_store
from openviking_cli.utils.config import get_openviking_config

logger = logging.getLogger(__name__)

VERSION = "image-semantics-cache-v2"


class _TwinCache:
    """Run-scoped memo of the published asset_sha256 -> twin index.

    One manifest capture serves every uncited copy in a run; rebuilding it per
    image would re-parse a multi-megabyte manifest thousands of times. Summaries
    are memoized per twin cache blob so N copies of one asset read it once.
    """

    __slots__ = ("index", "summaries", "lock")

    def __init__(self):
        self.index: dict[str, tuple[str, dict]] | None = None
        self.summaries: dict[str, dict] = {}
        self.lock = asyncio.Lock()


_twin_cache: ContextVar = ContextVar("image_twin_cache", default=None)


@contextmanager
def image_twin_cache():
    """Enable per-run memoization of the published twin index."""
    token = _twin_cache.set(_TwinCache())
    try:
        yield
    finally:
        _twin_cache.reset(token)


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


async def _published_twin_index(registry, account):
    """Map every published image asset_sha256 to (uri, reference) once per run.

    Only entries carrying an ``image_semantics`` cache are indexed: those are
    the assets whose summary can be reused for a byte-identical copy without
    calling the VLM again.
    """
    cache = _twin_cache.get()
    if cache is not None and cache.index is not None:
        return cache.index
    # Concurrent file tasks share one capture: without the lock each would
    # re-read and re-parse the manifest before any of them stores the index.
    if cache is not None:
        async with cache.lock:
            if cache.index is not None:
                return cache.index
            index = await _capture_twin_index(registry, account)
            cache.index = index
            return index
    return await _capture_twin_index(registry, account)


async def _capture_twin_index(registry, account):
    lease, manifest = registry.capture(account)
    try:
        index: dict[str, tuple[str, dict]] = {}
        for uri, item in manifest.items():
            content = (item or {}).get("content") or {}
            sha = content.get("sha256")
            if not sha or not content.get("image_semantics"):
                continue
            index.setdefault(sha, (uri, content))
        return index
    finally:
        registry.release(lease)


async def load_twin_image_semantics(fs, uri, ctx, asset_sha256=None):
    """Reuse the published summary of a byte-identical asset for an uncited copy.

    The menus corpus stores the same image bytes under several directories, but
    only one copy's citing markdown carries the figcaption. The other copies
    resolve no occurrence, so the caption-first path yields nothing and they
    would fall through to a VLM call. Their bytes are identical to a published
    asset whose summary is already trusted, so that summary is reused verbatim.

    ``asset_sha256`` lets a caller that already read the bytes skip a second
    read. Returns ``None`` when no published twin exists, leaving the caller to
    regenerate through the normal (VLM) path.
    """
    registry = configured_store()
    if registry is None or not getattr(ctx, "account_id", None):
        return None
    if registry.is_tombstoned(ctx.account_id, uri):
        raise ValueError("SEMANTICS_REQUIRED: image asset was deleted")
    if asset_sha256 is None:
        with draft_sources():
            raw = await fs.read_file_bytes(uri, ctx=ctx)
        asset_sha256 = hashlib.sha256(raw).hexdigest()
    index = await _published_twin_index(registry, ctx.account_id)
    twin = index.get(asset_sha256)
    if twin is None:
        return None
    _twin_uri, content = twin
    cache_ref = content.get("image_semantics")
    if not cache_ref:
        return None
    cache = _twin_cache.get()
    if cache is not None and asset_sha256 in cache.summaries:
        return cache.summaries[asset_sha256]
    try:
        raw_payload = fs._handle_agfs_read(
            await fs._async_agfs.read(blob_path(fs, ctx, cache_ref["sha256"]))
        )
    except Exception:
        return None
    if (
        len(raw_payload) != cache_ref["size"]
        or hashlib.sha256(raw_payload).hexdigest() != cache_ref["sha256"]
    ):
        # A twin is opportunistic: an unreadable cache blob means this copy
        # simply cannot be served from a twin, so it regenerates normally.
        return None
    payload = json.loads(raw_payload)
    signature = semantics_signature()
    if (
        payload.get("version") != VERSION
        or payload.get("signature") != signature
        or payload.get("asset_sha256") != asset_sha256
    ):
        # A twin built under a different model/prompt signature cannot be
        # reused; fall through to regeneration instead of raising, since the
        # uncited copy has no context of its own to protect.
        return None
    records = payload.get("occurrences") or []
    if payload.get("cache_key") != cache_key(signature, records):
        return None
    summary = "\n\n".join((r.get("summary") or "") for r in records).strip()
    if not summary:
        return None
    # The copy cites no markdown of its own, so the reused record carries no
    # context_uri: attributing it to the twin's chapter would fabricate a
    # dependency and make caption backfill write into an unrelated source.
    result = {
        "name": uri.rsplit("/", 1)[-1],
        "summary": summary,
        "image_occurrences": [
            {
                "summary": summary,
                "context_status": "twin_reuse",
                "asset_sha256": asset_sha256,
                "semantics_signature": signature,
            }
        ],
    }
    if cache is not None:
        cache.summaries[asset_sha256] = result
    logger.debug("Reused published twin semantics for %s (asset %s)", uri, asset_sha256[:12])
    return result


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
