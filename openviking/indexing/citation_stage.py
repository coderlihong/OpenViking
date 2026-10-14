# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Citation normalization stage: figure pointers and captions before reindex.

External markdown reaches OpenViking with authoring ``src`` references and (at
best) an untrusted upstream caption. This stage turns every image occurrence
under a target into the published citation form:

1. ``rewrite_image_uris`` completes each figure's ``vikingsrc`` pointer and
   materializes remote references into owned storage.
2. Images whose occurrences carry no figcaption are described by the VLM from
   the asset plus its bounded chapter context; the description is written back
   into the citing markdown.
3. Descriptions stay in the citing markdown's ``<figcaption>`` bodies: the
   reindex vector phase reads them via ``collect_image_contexts``, publishes
   the joint image records, and chunks the captioned markdown. This stage
   itself enqueues no embedding message.

Reindex is a pure compute stage over the bytes this stage publishes: it must
run AFTER citation normalization, or the vector phase archives markdown whose
captions do not exist yet.
"""

import asyncio
from typing import Any, Optional

from openviking.server.identity import RequestContext
from openviking_cli.utils import get_logger

logger = get_logger(__name__)


def _content_owner_ctx(uri: str, ctx: RequestContext) -> RequestContext:
    from openviking.core.namespace import content_owner_context_for_uri

    return content_owner_context_for_uri(uri, ctx)


async def normalize_citations(
    uri: str,
    *,
    ctx: RequestContext,
    recursive: bool = True,
    lock: Optional[dict] = None,
) -> dict[str, Any]:
    """Normalize figure citations under *uri* and settle their captions.

    Args:
        uri: Target md URI or directory URI (walked when *recursive*).
        ctx: Actor context; writes use the target's content-owner context.
        recursive: Walk directory descendants collecting markdown and images.
        lock: Caller-held pathlock lease forwarding; when None a tree lease is
            acquired here for the duration of the run.

    Returns:
        Counters describing what the stage did.
    """
    from openviking.indexing.content import draft_sources
    from openviking.parse.figure_caption import backfill_image_captions
    from openviking.parse.image_rewrite import rewrite_image_uris

    service = _service()
    viking_fs = service.viking_fs
    stat = await viking_fs.stat(uri, ctx=ctx, skip_count=True)
    is_dir = bool(stat.get("isDir", stat.get("is_dir")))
    if not is_dir:
        # A single file target normalizes only itself.
        recursive = False

    own_lease = lock is None
    lease = None
    if own_lease:
        acquire = (
            viking_fs._async_agfs.pathlock_acquire_tree
            if is_dir
            else viking_fs._async_agfs.pathlock_acquire_exact
        )
        path = viking_fs._uri_to_path(uri, ctx=ctx)
        lease = await acquire(path)
        lock = await viking_fs._async_agfs.pathlock_as_borrowed(lease)
    try:
        counters: dict[str, Any] = {
            "uri": uri,
            "files_normalized": 0,
            "images_described": 0,
            "captions_backfilled": 0,
            "warnings": [],
        }
        async with asyncio.Semaphore(1):
            with draft_sources():
                # rewrite_image_uris globs markdown under the root itself, so a
                # single-file URI normalizes just that file and a directory
                # walks its whole subtree.
                stats = await rewrite_image_uris(uri, ctx=ctx, lease_ref=lock)
                counters["files_normalized"] = int(stats.get("files_processed") or 0)
                md_uris = [uri]
                if is_dir:
                    glob_result = await viking_fs.glob(
                        "**/*.md" if recursive else "*.md", uri=uri, ctx=ctx
                    )
                    md_uris = list(glob_result.get("matches") or [])
                image_summaries = await _describe_images(viking_fs, md_uris, ctx)
                counters["images_described"] = len(image_summaries)
                for image_uri, summary_dict in image_summaries.items():
                    updated = await backfill_image_captions(
                        viking_fs,
                        image_uri,
                        summary_dict,
                        ctx=_content_owner_ctx(image_uri, ctx),
                        lock=lock,
                    )
                    counters["captions_backfilled"] += len(updated)
        return counters
    finally:
        if own_lease and lease is not None:
            await viking_fs._async_agfs.pathlock_release(lease)


async def _describe_images(viking_fs, md_uris: list[str], ctx: RequestContext) -> dict[str, dict]:
    """Describe every image cited by *md_uris* whose figcaption is still empty.

    Occurrences carry the figcaption body read from the published markdown —
    the boundary that discards untrusted upstream captions has already run at
    corpus fetch — so a non-empty caption here is trusted and the VLM skipped.
    """
    from openviking.indexing.image_context import collect_image_contexts
    from openviking.parse.parsers.media.utils import generate_image_summary

    vlm = getattr(_config(), "vlm", None)
    llm_sem = asyncio.Semaphore(max(1, getattr(vlm, "max_concurrent", None) or 1))

    cited: dict[str, None] = {}
    for md_uri in md_uris:
        try:
            content = await viking_fs.read_file(md_uri, ctx=ctx)
        except Exception as exc:
            logger.warning("[citation-stage] Failed to read %s: %s", md_uri, exc)
            continue
        for image_uri in _cited_image_uris(content):
            cited.setdefault(image_uri, None)

    summaries: dict[str, dict] = {}
    for image_uri in cited:
        try:
            contexts = await collect_image_contexts(viking_fs, image_uri, ctx)
        except Exception as exc:
            logger.warning("[citation-stage] Context collection failed for %s: %s", image_uri, exc)
            contexts = []
        try:
            summary_dict = await generate_image_summary(
                image_uri,
                image_uri.rsplit("/", 1)[-1],
                llm_sem=llm_sem,
                ctx=ctx,
                contexts=contexts,
                allow_vlm=True,
            )
        except Exception as exc:
            logger.warning("[citation-stage] Image description failed for %s: %s", image_uri, exc)
            continue
        if (summary_dict.get("summary") or "").strip():
            summaries[image_uri] = summary_dict
    return summaries


def _cited_image_uris(content: str) -> list[str]:
    """Extract the vikingsrc URIs of the figure blocks in *content*."""
    from openviking.parse.figure_caption import find_figure_spans
    from openviking.parse.image_rewrite import VIKINGSRC_ATTR, _unescape_xml_attr

    uris = []
    for _, _, block in find_figure_spans(content):
        marker = f'{VIKINGSRC_ATTR}="'
        start = block.find(marker)
        if start < 0:
            continue
        start += len(marker)
        end = block.find('"', start)
        if end < 0:
            continue
        uris.append(_unescape_xml_attr(block[start:end]))
    return uris


def _service():
    from openviking.server.dependencies import get_service

    return get_service()


def _config():
    from openviking_cli.utils.config import get_openviking_config

    return get_openviking_config()
