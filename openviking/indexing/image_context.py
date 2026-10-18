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

import asyncio
import hashlib
import json
import logging
import posixpath
import re
from contextlib import contextmanager
from contextvars import ContextVar
from html.parser import HTMLParser
from urllib.parse import unquote

from markdown_it import MarkdownIt

from openviking.indexing.content import draft_sources
from openviking.indexing.record_meta import extract_inline_metadata
from openviking.parse.image_rewrite import VIKINGSRC_ATTR
from openviking_cli.exceptions import NotFoundError
from openviking.utils.token_estimation import estimate_text_tokens

logger = logging.getLogger(__name__)

VERSION = "image-context-v5"

# Conservative default when no model tokenizer is configured. Kept in sync with
# the legacy 2048-token occurrence budget, but callers that know the real VLM
# tokenizer must pass it so the budget is measured, not guessed.
DEFAULT_CONTEXT_TOKEN_BUDGET = 2048

# How many directory entries to request per VikingFS listing page. Pagination
# continues until a short page is returned, so no reference is ever dropped.
LIST_PAGE_SIZE = 256

_MARKDOWN_SUFFIXES = (".md", ".markdown")


class _ParsedSource:
    """One markdown source parsed once and reused across every cited image.

    A chapter's markdown is identical for every asset it references, so parsing
    it per image multiplies an O(md) cost by the image count. The token stream
    and source lines are target-independent and cached here.
    """

    __slots__ = ("tokens", "lines")

    def __init__(self, markdown: str):
        # The figcaption is the authoritative description (generated offline
        # before upload); it is read as the occurrence's caption and also stays
        # in the neighboring source text. Untrusted upstream bodies never reach
        # this point: they are discarded at the corpus boundary, not on ingest.
        self.tokens = MarkdownIt("commonmark", {"html": True}).parse(markdown)
        self.lines = markdown.splitlines(keepends=True)


class _ImageContextCache:
    """Run-scoped memo of source discovery, reads and parses.

    Scoped to one reindex invocation: sources are fenced against concurrent
    writes for its duration, so a single read is the run's consistent view.
    """

    def __init__(self):
        self.sources: dict[str, tuple[str, ...]] = {}
        self.texts: dict[str, str] = {}
        self.parsed: dict[str, _ParsedSource] = {}


_context_cache: ContextVar = ContextVar("image_context_cache", default=None)


@contextmanager
def image_context_cache():
    """Enable per-run memoization of chapter markdown reads and parses."""
    cache = _ImageContextCache()
    token = _context_cache.set(cache)
    try:
        yield cache
    finally:
        _context_cache.reset(token)


# The corpus is machine-generated upstream and almost every ``<img>`` tag in it
# is emitted WITHOUT its closing ``>``: live, 151,230 of 152,603 tags end at
# ``..."/`` and the next line opens ``<figcaption>``. HTMLParser then keeps
# consuming that sibling as if it were still part of the img tag, so the
# caption -- the authoritative description this whole recovery path exists to
# read -- is silently lost and the image is reported as undescribed. Terminate
# such a tag before parsing instead of trusting the corpus to be well-formed.
_UNCLOSED_IMG = re.compile(r"<img\b[^<>]*?(?=<|$)", re.IGNORECASE | re.DOTALL)


def _terminate_unclosed_img(fragment: str) -> str:
    """Add the missing ``>`` to ``<img>`` tags that were emitted without one."""
    if "<img" not in fragment.lower():
        return fragment
    return _UNCLOSED_IMG.sub(lambda match: match.group(0).rstrip() + ">", fragment)


class _Images(HTMLParser):
    """Collect ``<img>`` references and their figcaption from an HTML fragment.

    Each reference is returned as ``(src, caption)``. The caption is the body of
    the ``<figcaption>`` that follows the ``<img>`` inside the same figure block:
    it is the authoritative description, generated offline before upload. An
    ``<img>`` with no figcaption (or an empty one) yields an empty caption, which
    the summary path treats as "regenerate".
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.images = []
        self._in_caption = False
        self._caption_parts = []

    def feed(self, data: str) -> None:
        super().feed(_terminate_unclosed_img(data))

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "img":
            attrs = dict(attrs)
            src = attrs.get("src") or attrs.get(VIKINGSRC_ATTR)
            if src:
                self.images.append([src, ""])
        elif tag == "figcaption":
            self._in_caption = True
            self._caption_parts = []

    def handle_endtag(self, tag):
        if tag.lower() != "figcaption":
            return
        self._in_caption = False
        caption = "".join(self._caption_parts).strip()
        self._caption_parts = []
        if caption and self.images:
            # Attach to the most recent reference that has no caption yet.
            for entry in reversed(self.images):
                if not entry[1]:
                    entry[1] = caption
                    break

    def handle_data(self, data):
        if self._in_caption:
            self._caption_parts.append(data)


def _viking_parts(uri):
    """Split ``viking://<authority>/<path>`` without URL fragment/query parsing.

    A Viking path is a filesystem path, so ``#`` and ``?`` are ordinary filename
    characters. ``urlsplit`` would truncate at them (e.g. an authored
    ``..._nozzle_#1_....md`` source), so the path is taken verbatim here.
    """
    rest = uri[len("viking://") :]
    slash = rest.find("/")
    if slash < 0:
        return rest, ""
    return rest[:slash], rest[slash:]


def resolve_reference(source_uri, reference):
    if reference.startswith("viking://"):
        netloc, path = _viking_parts(reference)
    elif "://" in reference or reference.startswith("//"):
        # A non-Viking absolute scheme or a protocol-relative reference cannot
        # name a local asset.
        return None
    else:
        netloc, base_path = _viking_parts(source_uri)
        path = posixpath.join(posixpath.dirname(base_path), reference)
    # Resolve only a local URI; VikingFS still enforces current source ACL.
    path = posixpath.normpath(unquote(path))
    return f"viking://{netloc}{path}"


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


def extract_occurrences(
    markdown,
    source_uri,
    image_uri,
    token_budget=2048,
    count_tokens=None,
    parsed: "_ParsedSource | None" = None,
):
    if parsed is None:
        parsed = _ParsedSource(markdown)
    tokens = parsed.tokens
    lines = parsed.lines
    # The chapter's own ``- document_id: / - page: / - 机型:`` block is read from
    # the full source once, here, and travels with every occurrence. A figure's
    # context window is token-budgeted and header-decorated, so the block can be
    # outside the window (a late figure) or unreachable by its leading-line scan
    # (the ``Source URI:`` header); without this the image row could never be
    # filtered by the machine model of the chapter it illustrates.
    source_meta = extract_inline_metadata(markdown)
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
                    # A markdown embed carries only alt text, never a trusted
                    # figcaption: the caption lives in the <figure> HTML form.
                    images.append((child.attrGet("src") or "", ""))
                elif child.type == "html_inline":
                    parser = _Images()
                    parser.feed(child.content)
                    images.extend(parser.images)
        elif token.type == "html_block":
            parser = _Images()
            parser.feed(token.content)
            images.extend(parser.images)
        for reference, caption in images:
            if not _reference_matches(reference, source_uri, target, token.map, lines):
                continue
            start, end = token.map or (0, 0)
            # Neighboring prose remains centered on this reference, including
            # captions after the figure. Never use only the document beginning.
            nearby = "".join(lines[max(0, start - 12) : min(len(lines), end + 12)])
            context = truncate_to_token_budget(
                f"Source URI: {source_uri}\nChapter: {' > '.join(s for _, s in heading)}\nNeighboring source text:\n{nearby}",
                token_budget,
                count_tokens,
            )
            caption = (caption or "").strip()
            digest = hashlib.sha256(
                json.dumps(
                    [VERSION, source_uri, index, len(occurrences), context, caption],
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            occurrences.append(
                {
                    "context_uri": source_uri,
                    "heading_path": [s for _, s in heading],
                    "context": context,
                    "context_hash": digest,
                    "ordinal": len(occurrences),
                    "context_version": VERSION,
                    "context_token_budget": token_budget,
                    "context_token_count": (count_tokens or estimate_text_tokens)(context),
                    "context_budget_kind": "measured" if count_tokens else "estimated",
                    "caption": caption,
                    "source_meta": dict(source_meta),
                }
            )
    return occurrences


def _reference_matches(reference, source_uri, target, token_map, lines):
    """Figure form keeps the authoring ``src`` beside the storage pointer, so a
    match on either wins: relative src resolves to the sibling image; when the
    src path was never materialized (sidecar layouts renamed it), the explicit
    viking:// pointer is authoritative."""
    if resolve_reference(source_uri, reference) == target:
        return True
    if reference.startswith("viking://"):
        return reference == target
    pointer = _find_vikingsrc_nearby(reference, lines, token_map)
    return bool(pointer) and pointer == target


def _find_vikingsrc_nearby(reference, lines, token_map):
    """Locate the vikingsrc pointer of the <img> whose src is *reference*.

    HTMLParser collapses multi-attribute tags before we see raw text, so scan
    the token's source lines for the tag carrying this exact src.
    """
    if not token_map:
        return None
    start, end = token_map
    for line in lines[start:end]:
        marker = f'src="{reference}"'
        pos = line.find(marker)
        if pos < 0:
            continue
        tail = line[pos + len(marker) :]
        v_pos = tail.find(f'{VIKINGSRC_ATTR}="')
        if v_pos < 0:
            continue
        tail = tail[v_pos + len(VIKINGSRC_ATTR) + 2 :]
        close = tail.find('"')
        if close < 0:
            continue
        netloc, path = _viking_parts(tail[:close])
        return f"viking://{netloc}{posixpath.normpath(unquote(path))}"
    return None


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

    cache = _context_cache.get()
    result = []
    with draft_sources():
        source_uris = (
            cache.sources[chapter]
            if cache is not None and chapter in cache.sources
            else None
        )
        if source_uris is None:
            discovered = []
            async for source_uri in _discover_markdown_sources(fs, chapter, ctx):
                discovered.append(source_uri)
            source_uris = tuple(discovered)
            if cache is not None:
                cache.sources[chapter] = source_uris

        for source_uri in source_uris:
            from openviking.indexing.publication import configured_store

            registry = configured_store()
            # ``observe_draft`` opens a write transaction. Calling it inline on
            # the event loop made every image-source summary queue behind the
            # single publication writer (observed live as multi-second loop
            # stalls with this exact frame at publication.py observe_draft).
            if registry and getattr(ctx, "account_id", None):
                epoch = await asyncio.to_thread(
                    registry.observe_draft, ctx.account_id, source_uri
                )
            else:
                epoch = None
            text = cache.texts.get(source_uri) if cache is not None else None
            parsed = cache.parsed.get(source_uri) if cache is not None else None
            if text is None:
                try:
                    content = await fs.read_file(source_uri, ctx=ctx)
                except NotFoundError:
                    # A source listed before deletion (or blocked by a tombstone on a
                    # re-created tree) must not abort the chapter's remaining images.
                    logger.warning("Skipping vanished image context source: %s", source_uri)
                    continue
                text = content.decode("utf-8") if isinstance(content, bytes) else content
                if cache is not None:
                    cache.texts[source_uri] = text
            if parsed is None:
                parsed = _ParsedSource(text)
                if cache is not None:
                    cache.parsed[source_uri] = parsed
            found = extract_occurrences(
                text,
                source_uri,
                image_uri,
                token_budget,
                count_tokens=count_tokens,
                parsed=parsed,
            )
            if epoch is not None:
                for occurrence in found:
                    occurrence["context_epoch"] = epoch
            result.extend(found)
    return result