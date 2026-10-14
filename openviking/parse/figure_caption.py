# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Backfill VLM captions into ``<figure>`` blocks of referencing markdown.

After the semantic stage generates an image summary (``image_occurrences``
carry one entry per referencing markdown file), each occurrence's caption is
written into the ``<figcaption>`` of the figure block whose ``vikingsrc`` points
at that image. Occurrences keep per-context distinctness: one asset cited by
two chapters gets two figure blocks, each filled with its own caption.
"""

import logging
from typing import Any, Dict, Optional

from openviking.parse.image_rewrite import _protected_ranges, VIKINGSRC_ATTR
from openviking_cli.utils import get_logger

logger = get_logger(__name__)

_FIGURE_OPEN = "<figure>"
_FIGURE_CLOSE = "</figure>"
_CAPTION_OPEN = "<figcaption>"
_CAPTION_CLOSE = "</figcaption>"

# Sent to the VLM when a figure block was found but carries no caption text.
_EMPTY_CAPTION_PLACEHOLDER = "（未生成图片描述）"


def _find_attr_value(tag: str, attr: str) -> Optional[str]:
    marker = f'{attr}="'
    start = tag.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = tag.find('"', start)
    if end < 0:
        return None
    return tag[start:end]


def _unescape_xml_attr(value: str) -> str:
    return value.replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def find_figure_spans(content: str):
    """Yield ``(start, end, block_text)`` for every top-level ``<figure>`` block."""
    spans = []
    cursor = 0
    while True:
        start = content.find(_FIGURE_OPEN, cursor)
        if start < 0:
            return spans
        end = content.find(_FIGURE_CLOSE, start)
        if end < 0:
            return spans
        end += len(_FIGURE_CLOSE)
        spans.append((start, end, content[start:end]))
        cursor = end


def match_occurrence_to_figure(block_text: str, vikingsrc_uri: str) -> bool:
    decoded = _unescape_xml_attr(_find_attr_value(block_text, VIKINGSRC_ATTR) or "")
    return decoded == vikingsrc_uri


def fill_figure_caption(block_text: str, caption: str) -> str:
    """Set the figcaption content of one figure block, replacing any prior body."""
    open_end = block_text.find(_CAPTION_OPEN)
    close_start = block_text.rfind(_CAPTION_CLOSE)
    if open_end < 0 or close_start < 0 or close_start < open_end:
        return block_text
    open_end += len(_CAPTION_OPEN)
    caption = caption.strip() or _EMPTY_CAPTION_PLACEHOLDER
    body_lines = [f"    {line}" for line in caption.splitlines() if line.strip()]
    body = "\n".join(body_lines) if body_lines else f"    {_EMPTY_CAPTION_PLACEHOLDER}"
    return f"{block_text[:open_end]}\n{body}\n{block_text[close_start:]}"


def _caption_is_empty(body: str) -> bool:
    """True when the figcaption body holds no real caption yet.

    Empty bodies and the low-information placeholder are both (re)fillable;
    any other non-empty text was written by an earlier backfill pass and wins
    (first-write-wins): rewriting it would bump the markdown draft epoch and
    re-trigger the joint-index publication gate for sibling images citing the
    same file.
    """
    text = body.replace(_EMPTY_CAPTION_PLACEHOLDER, "").strip()
    return not text


async def backfill_image_captions(
    viking_fs,
    image_uri: str,
    summary_dict: Dict[str, Any],
    ctx=None,
    lock: Optional[Dict[str, Any]] = None,
) -> list[str]:
    """Write each occurrence's caption into the citing markdown's figure block.

    ``summary_dict["image_occurrences"]`` entries carry ``context_uri`` (the
    referencing markdown URI) and ``summary`` (the VLM description). Returns
    the markdown URIs actually rewritten: a caller that indexes content must
    know which files owe a fresh revision. Failures are logged, never raised:
    a caption backfill must not break the semantic pipeline.
    """
    occurrences = summary_dict.get("image_occurrences") or []
    by_file: Dict[str, list] = {}
    for occurrence in occurrences:
        context_uri = occurrence.get("context_uri") or ""
        caption = (occurrence.get("summary") or "").strip()
        if not context_uri.endswith(".md"):
            continue
        by_file.setdefault(context_uri, []).append(caption)

    updated: list[str] = []
    for md_uri, captions in by_file.items():
        try:
            content = await viking_fs.read_file(md_uri, ctx=ctx)
        except Exception as e:
            logger.warning(f"[figure_caption] Failed to read {md_uri}: {e}")
            continue

        new_content, filled = _backfill_content(content, image_uri, captions)
        if filled <= 0:
            continue
        try:
            await viking_fs.write_file(md_uri, new_content, ctx=ctx, lease_ref=lock)
            updated.append(md_uri)
            logger.debug(f"[figure_caption] Filled {filled} caption(s) in {md_uri}")
        except Exception as e:
            logger.warning(f"[figure_caption] Failed to write {md_uri}: {e}")
    return updated


def _backfill_content(content: str, image_uri: str, captions: list) -> tuple[str, int]:
    """Fill up to len(captions) matching figure blocks, in document order.

    A block matches when its vikingsrc URI equals *image_uri* or when its
    vikingsrc filename equals the image's filename (occurrence contexts resolve
    relative references to the stored sibling). Extra occurrences beyond
    matching blocks are dropped.
    """
    protected = _protected_ranges(content)
    spans = find_figure_spans(content)
    if not spans:
        return content, 0

    image_name = image_uri.rsplit("/", 1)[-1]
    pending = list(captions)
    pieces = []
    cursor = 0
    filled = 0
    skipped_filled = 0
    for start, end, block in spans:
        if any(start <= pos < end for pos, _ in protected):
            continue
        attr_raw = _find_attr_value(block, VIKINGSRC_ATTR)
        if attr_raw is None:
            continue
        decoded = _unescape_xml_attr(attr_raw)
        if decoded != image_uri and decoded.rsplit("/", 1)[-1] != image_name:
            continue
        open_end = block.find(_CAPTION_OPEN)
        close_start = block.rfind(_CAPTION_CLOSE)
        already_filled = False
        if open_end >= 0 and close_start > open_end:
            body = block[open_end + len(_CAPTION_OPEN) : close_start]
            already_filled = not _caption_is_empty(body)
        if already_filled:
            # First-write-wins: keep the existing caption, don't rewrite the file.
            skipped_filled += 1
            continue
        if not pending:
            break
        caption = pending.pop(0)
        pieces.append(content[cursor:start])
        pieces.append(fill_figure_caption(block, caption))
        cursor = end
        filled += 1
    if not filled:
        return content, 0
    pieces.append(content[cursor:])
    return "".join(pieces), filled
