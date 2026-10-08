# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Image references and bounded chapter context from Markdown ASTs.

Occurrences remain separate: one asset can have different meanings in different
source files. The containing chapter tree is enumerated through the same authorized
VikingFS APIs as indexing; remote references are never fetched.

Reference discovery is paginated and cross-directory: every legal reference is
kept, including 17+ occurrences of the same asset and references that live in a
sibling/parent directory. No occurrence count is hardcoded; the context budget is
measured with the real model tokenizer when one is configured.
"""

import hashlib
import json
import logging
import posixpath
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

from openviking.indexing.content import draft_sources
from openviking_cli.exceptions import NotFoundError
from openviking.utils.token_estimation import estimate_text_tokens

logger = logging.getLogger(__name__)

VERSION = "image-context-v3"

# Conservative default when no model tokenizer is configured. Kept in sync with
# the legacy 2048-token occurrence budget, but callers that know the real VLM
# tokenizer must pass it so the budget is measured, not guessed.
DEFAULT_CONTEXT_TOKEN_BUDGET = 2048

# How many directory entries to request per VikingFS listing page. Pagination
# continues until a short page is returned, so no reference is ever dropped.
LIST_PAGE_SIZE = 256

_MARKDOWN_SUFFIXES = (".md", ".markdown")


class _Images(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "img":
            attrs = dict(attrs)
            if attrs.get("src"):
                self.images.append((attrs["src"], attrs.get("alt", "")))


def resolve_reference(source_uri, reference):
    parsed = urlsplit(reference)
    if parsed.scheme:
        if parsed.scheme != "viking":
            return None
        base = parsed
        path = parsed.path
    else:
        if parsed.netloc:
            return None
        base = urlsplit(source_uri)
        path = posixpath.join(posixpath.dirname(base.path), parsed.path)
    # Resolve only a local URI; VikingFS still enforces current source ACL.
    path = posixpath.normpath(unquote(path))
    return f"viking://{base.netloc}{path}"


def load_vlm_token_counter():
    """Count with pinned VLM text tokenizers, never an embedding tokenizer.

    Every configured model (including backup/credential overrides) must be
    represented. Invalid or incomplete explicit configuration fails rather
    than silently presenting an estimate as a measured VLM budget. Without
    any explicit VLM tokenizers the caller uses the documented estimator.
    This counts occurrence text, not the complete prompt or image tokens.
    """
    from openviking_cli.utils.config import get_openviking_config

    try:
        settings = get_openviking_config()
    except Exception:
        return None
    vlm = getattr(settings, "vlm", None)
    entries = getattr(vlm, "context_tokenizers", ()) or ()
    if not entries:
        return None
    models = {getattr(vlm, "model", None)}
    backup = getattr(vlm, "backup", None)
    models.add(getattr(backup, "model", None))
    for credential in getattr(vlm, "credentials", ()) or ():
        models.add(getattr(credential, "model", None))
    configured_models = {entry.model for entry in entries}
    if {model for model in models if model} - configured_models:
        raise ValueError("VLM context tokenizers must cover every configured model")
    from pathlib import Path
    from tokenizers import Tokenizer

    tokenizers = []
    for entry in entries:
        raw = Path(entry.path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry.sha256:
            raise ValueError("VLM context tokenizer SHA256 does not match")
        tokenizers.append(Tokenizer.from_str(raw.decode("utf-8")))

    def count(text: str) -> int:
        return max(len(tokenizer.encode(text, add_special_tokens=False).ids)
                   for tokenizer in tokenizers)

    return count


def truncate_to_token_budget(text, max_tokens, count_tokens=None, *, marker="\n…\n"):
    """Fit text to a budget measured by ``count_tokens``.

    When a real tokenizer counter is supplied the budget is exact; otherwise the
    shared conservative estimator is used. Head and tail are retained so a late
    figure keeps its surrounding prose.
    """
    count = count_tokens or estimate_text_tokens
    if not text or count(text) <= max_tokens:
        return text
    if max_tokens <= 0:
        return ""

    marker_tokens = count(marker)
    if marker_tokens >= max_tokens:
        marker = ""
        marker_tokens = 0
    content_tokens = max_tokens - marker_tokens
    head_tokens = int(content_tokens * 0.75)
    tail_tokens = content_tokens - head_tokens

    def _prefix_length(value, budget):
        low, high = 0, len(value)
        while low < high:
            mid = (low + high + 1) // 2
            if count(value[:mid]) <= budget:
                low = mid
            else:
                high = mid - 1
        return low

    head_end = _prefix_length(text, head_tokens)
    remaining = text[head_end:]
    tail_length = _prefix_length(remaining[::-1], tail_tokens)
    tail = remaining[len(remaining) - tail_length :] if tail_length else ""
    head = text[:head_end].rstrip()
    tail = tail.lstrip()
    candidate = head + marker + tail
    # Token counts need not be additive or monotone at a concatenation boundary,
    # and counting a reversed suffix is only an initial approximation. Verify
    # the actual forward string; geometrically shrink until it really fits.
    while candidate and count(candidate) > max_tokens:
        if len(head) >= len(tail) and head:
            head = head[:len(head) // 2]
        elif tail:
            tail = tail[len(tail) - len(tail) // 2:]
        else:
            marker = ""
        candidate = head + marker + tail
    return candidate


def extract_occurrences(markdown, source_uri, image_uri, token_budget=2048, count_tokens=None):
    tokens = MarkdownIt("commonmark", {"html": True}).parse(markdown)
    lines = markdown.splitlines(keepends=True)
    heading = []
    occurrences = []
    target = resolve_reference(image_uri, image_uri)
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            level = int(token.tag[1:])
            title = tokens[index + 1].content
            heading = [(n, s) for n, s in heading if n < level] + [(level, title)]
        images = []
        if token.type == "inline":
            for child in token.children or []:
                if child.type == "image":
                    images.append((child.attrGet("src") or "", child.content))
                elif child.type == "html_inline":
                    parser = _Images()
                    parser.feed(child.content)
                    images.extend(parser.images)
        elif token.type == "html_block":
            parser = _Images()
            parser.feed(token.content)
            images.extend(parser.images)
        for reference, caption in images:
            if resolve_reference(source_uri, reference) != target:
                continue
            start, end = token.map or (0, 0)
            # Neighboring prose remains centered on this reference, including
            # captions after the figure. Never use only the document beginning.
            nearby = "".join(lines[max(0, start - 12) : min(len(lines), end + 12)])
            context = truncate_to_token_budget(
                f"Source URI: {source_uri}\nChapter: {' > '.join(s for _, s in heading)}\nCaption: {caption}\nNeighboring source text:\n{nearby}",
                token_budget,
                count_tokens,
            )
            digest = hashlib.sha256(
                json.dumps(
                    [VERSION, source_uri, index, len(occurrences), caption, context],
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            occurrences.append(
                {
                    "context_uri": source_uri,
                    "heading_path": [s for _, s in heading],
                    "caption": caption,
                    "context": context,
                    "context_hash": digest,
                    "ordinal": len(occurrences),
                    "context_version": VERSION,
                    "context_token_budget": token_budget,
                    "context_token_count": (count_tokens or estimate_text_tokens)(context),
                    "context_budget_kind": "measured" if count_tokens else "estimated",

                }
            )
    return occurrences


def _is_markdown_source(entry):
    if entry.get("isDir") or entry.get("type") == "directory":
        return False
    name = entry.get("name", "")
    if not name or name.startswith("."):
        return False
    return name.lower().endswith(_MARKDOWN_SUFFIXES)


async def _list_all_entries(fs, directory, ctx):
    """Page through a directory until the listing is exhausted."""
    entries = []
    offset = 0
    while True:
        page = await fs.ls(directory, node_limit=LIST_PAGE_SIZE, offset=offset, ctx=ctx)
        if not page:
            break
        entries.extend(page)
        if len(page) < LIST_PAGE_SIZE:
            break
        offset += len(page)
    return entries


async def _discover_markdown_sources(fs, chapter, ctx):
    """Iteratively enumerate the authorized tree without a silent depth cap.

    A malformed listing cannot redirect discovery outside the requested tree.
    The visited set also prevents duplicate sources and directory cycles.
    """
    pending = [chapter]
    visited = set()
    root = chapter.rstrip("/")
    while pending:
        directory = pending.pop()
        if directory in visited:
            continue
        visited.add(directory)
        for entry in await _list_all_entries(fs, directory, ctx):
            name = entry.get("name", "")
            if not name or name.startswith("."):
                continue
            child = entry.get("uri") or f"{directory}/{name}"
            normalized = resolve_reference(child, child)
            if (normalized != child or not child.startswith(root + "/")
                    or posixpath.dirname(child) != directory):
                raise ValueError("Image context listing contains an invalid child URI")
            if entry.get("isDir") or entry.get("type") == "directory":
                pending.append(child)
            elif _is_markdown_source(entry) and child not in visited:
                visited.add(child)
                yield child


async def collect_image_contexts(
    fs,
    image_uri,
    ctx,
    token_budget=None,
    count_tokens=None,
):
    """Collect every occurrence of ``image_uri`` with a tokenizer-bounded context.

    The old per-image cap of 16 references is removed. Discovery paginates the
    containing directory tree so cross-directory references and 17+ occurrences
    are all retained. ``token_budget`` defaults to the model-tokenizer-measured
    budget when ``count_tokens`` is not supplied.
    """
    parent = image_uri.rsplit("/", 1)[0]
    chapter = parent.rsplit("/", 1)[0] if parent.rsplit("/", 1)[-1] == "images" else parent

    if count_tokens is None:
        count_tokens = load_vlm_token_counter()
    if token_budget is None:
        token_budget = DEFAULT_CONTEXT_TOKEN_BUDGET

    result = []
    with draft_sources():
        async for source_uri in _discover_markdown_sources(fs, chapter, ctx):
            from openviking.indexing.publication import configured_store

            registry = configured_store()
            epoch = (
                registry.observe_draft(ctx.account_id, source_uri)
                if registry and getattr(ctx, "account_id", None)
                else None
            )
            try:
                content = await fs.read_file(source_uri, ctx=ctx)
            except NotFoundError:
                # A source listed before deletion (or blocked by a tombstone on a
                # re-created tree) must not abort the chapter's remaining images.
                logger.warning("Skipping vanished image context source: %s", source_uri)
                continue
            if isinstance(content, bytes):
                content = content.decode("utf-8")
            found = extract_occurrences(
                content, source_uri, image_uri, token_budget, count_tokens=count_tokens
            )
            if epoch is not None:
                for occurrence in found:
                    occurrence["context_epoch"] = epoch
            result.extend(found)
    return result