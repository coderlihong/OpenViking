# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Lossless source partitioning with model-tokenizer parent/child budgets.

Internal source intervals prove coverage; public citations remain the original URI.
No model download, guessed tokenizer, or silent tail truncation is permitted.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

from markdown_it import MarkdownIt

VERSION = "markdown-parents-v2"


def digest(*values) -> str:
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


@lru_cache(maxsize=8)
def load_tokenizer(path: str, expected_sha: str):
    from tokenizers import Tokenizer

    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("Chunk tokenizer SHA256 does not match the configured model tokenizer")
    return Tokenizer.from_str(raw.decode())


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    parent_chunk_id: str
    ordinal: int
    source_text: str
    embedding_text: str
    parent_text: str
    token_count: int
    continuation: bool


@dataclass(frozen=True)
class IndexPlan:
    source_uri: str
    document_id: str
    content_revision: str
    embedding_space_id: str
    plan_id: str
    chunks: tuple[Chunk, ...]
    # Recall-time parent context budget. A plan is only expanded into parent
    # text up to this token count; ``0`` means the producer declared no budget
    # and the consumer must fall back to its own default. Kept on the plan so a
    # reader never has to re-derive the chunker config to bound expansion.
    parent_token_budget: int = 0

    def validate_coverage(self, source: str) -> None:
        if "".join(c.source_text for c in self.chunks) != source:
            raise ValueError("IndexPlan lost or duplicated source content")
        if len({c.chunk_id for c in self.chunks}) != len(self.chunks):
            raise ValueError("IndexPlan contains duplicate chunk identities")

    def validate_parent_budget(self, count: Callable[[str], int]) -> None:
        """Every parent context must fit the declared recall-time budget.

        Parent text is returned as context only; the child embedding is never
        the parent. A parent that exceeds the budget is a hard plan error, not
        something the reader may silently truncate.
        """
        if self.parent_token_budget <= 0:
            return
        for chunk in self.chunks:
            if count(chunk.parent_text) > self.parent_token_budget:
                raise ValueError("Parent context exceeds the configured parent token budget")


def ast_blocks(source: str):
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    ranges = []
    for token in MarkdownIt("commonmark").enable("table").parse(source):
        if token.level == 0 and token.map and token.nesting >= 0:
            start, end = (offsets[n] for n in token.map)
            if ranges and start < ranges[-1][1]:
                continue
            ranges.append((start, end, token.type))
    cursor = 0
    for _start, end, kind in ranges:
        if end <= cursor:
            continue
        yield source[cursor:end], kind
        cursor = end
    if cursor < len(source):
        yield source[cursor:], "paragraph"


def prefix_piece(text: str, budget: int, count: Callable[[str], int]) -> str:
    """Split on Unicode character boundaries, never decoded partial byte tokens."""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if count(text[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    while low and count(text[:low]) > budget:
        low -= 1
    if low == 0:
        raise ValueError("Token budget cannot accommodate one source character")
    return text[:low]


def table_pieces(block: str, budget: int, count: Callable[[str], int]):
    """Preserve source exactly once while repeating headers in derived inputs.

    Row groups remain Markdown tables. A single oversized row is explicitly
    continued as literal Markdown with the same header, never silently cut off.
    """
    tokens = MarkdownIt("commonmark").enable("table").parse(block)
    table = next(t for t in tokens if t.type == "table_open")
    body = next((t for t in tokens if t.type == "tbody_open"), None)
    if not body or body.map[0] >= body.map[1]:
        raise ValueError("Oversized table header cannot be split into semantic row groups")
    lines = block.splitlines(keepends=True)
    header = "".join(lines[table.map[0] : body.map[0]])
    leading = "".join(lines[: table.map[0]])
    if count("\n\n" + header) >= budget:
        raise ValueError("Table header exceeds the chunk source budget")
    result = []
    own = leading + header
    rows = ""
    first = True

    def emit():
        nonlocal own, rows, first
        if rows or (first and own):
            result.append((own, "\n\n" + header + rows + "\n\n", True, "table_open"))
            first = False
            own, rows = "", ""

    for line in lines[body.map[0] : body.map[1]]:
        if count("\n\n" + header + rows + line + "\n\n") <= budget:
            own += line
            rows += line
            continue
        emit()
        if count("\n\n" + header + line + "\n\n") <= budget:
            own, rows = line, line
            continue
        # No valid row group can contain this row. Keep all Unicode characters
        # and mark each literal continuation; the header stays visible.
        remainder = line
        while remainder:
            fence = "`" * max(
                3, max((len(m.group()) for m in re.finditer(r"`+", remainder)), default=0) + 1
            )
            template = "\n\n" + header + "\n" + fence + "markdown\n" + "\n" + fence + "\n\n"
            room = budget - count(template)
            if room < 1:
                raise ValueError("Table header leaves no room for an oversized row continuation")
            fragment = prefix_piece(remainder, room, count)
            rendered = (
                "\n\n" + header + "\n" + fence + "markdown\n" + fragment + "\n" + fence + "\n\n"
            )
            while count(rendered) > budget:
                fragment = fragment[:-1]
                if not fragment:
                    raise ValueError("Table continuation exceeds the token budget")
                rendered = (
                    "\n\n" + header + "\n" + fence + "markdown\n" + fragment + "\n" + fence + "\n\n"
                )
            result.append((fragment, rendered, True, "table_open"))
            remainder = remainder[len(fragment) :]
    emit()
    trailing = "".join(lines[body.map[1] :])
    if trailing:
        # ast_blocks generally ends at table_end, but preserve any residual text.
        result.append((trailing, trailing, True, "paragraph"))
    if "".join(p[0] for p in result) != block:
        raise ValueError("Table partition lost source content")
    return result


def build_plan(
    source: str, uri: str, account_id: str, config, space: str, count: Callable[[str], int]
) -> IndexPlan:
    revision = hashlib.sha256(source.encode()).hexdigest()
    document_id = digest(account_id, uri)
    signature = config.model_dump(exclude={
        "tokenizer_path", "publication_path",
        "publication_authority_socket", "publication_authority_token_file",
    })
    plan_id = digest(VERSION, document_id, revision, space, signature)
    if not source:
        return IndexPlan(
            uri,
            document_id,
            revision,
            space,
            plan_id,
            (),
            parent_token_budget=config.parent_max_tokens,
        )
    title = uri.rsplit("/", 1)[-1]
    prefix = "# " + title + "\n\n"
    if count(prefix) >= config.child_target_tokens // 2:
        prefix = ""  # Long filenames must not consume the source budget.
    capacity = config.child_target_tokens - config.special_token_reserve - count(prefix) - 16
    if capacity < 1:
        raise ValueError("Chunk target is too small for tokenizer/template reserve")
    positions, heading_paths = [], []
    line_offsets = [0]
    for line in source.splitlines(keepends=True):
        line_offsets.append(line_offsets[-1] + len(line))
    headings = []
    for token in MarkdownIt("commonmark").parse(source):
        if token.type == "heading_open" and token.level == 0 and token.map:
            level = int(token.tag[1:])
            raw = source[line_offsets[token.map[0]] : line_offsets[token.map[1]]].strip()
            headings = [(n, text) for n, text in headings if n < level] + [(level, raw)]
            positions.append(line_offsets[token.map[0]])
            heading_paths.append("\n".join(text for _, text in headings) + "\n\n")
    heading_reserve = min(96, config.parent_max_tokens // 4)

    def heading_at(offset):
        index = bisect.bisect_right(positions, offset) - 1
        if index < 0:
            return ""
        value = heading_paths[index]
        return (
            value
            if count(value) <= heading_reserve
            else prefix_piece(value, heading_reserve, count)
        )

    pieces = []
    for block, kind in ast_blocks(source):
        if kind == "table_open" and count(block) > capacity:
            pieces.extend(table_pieces(block, capacity, count))
            continue
        remainder = block
        # Prefer intact Markdown blocks. Oversize blocks split at complete lines
        # where possible; fragments are explicitly marked for downstream QA.
        fragmented = count(block) > capacity
        while remainder:
            if count(remainder) <= capacity:
                piece = remainder
            else:
                piece = prefix_piece(remainder, capacity, count)
                last_line = piece.rfind("\n")
                if last_line > len(piece) // 2:
                    piece = piece[: last_line + 1]
            rendered = piece
            if fragmented and kind in {"fence", "code_block", "table_open"}:
                fence = "`" * max(
                    3,
                    max((len(m.group()) for m in re.finditer(r"`+", piece)), default=0) + 1,
                )
                rendered = fence + "markdown\n" + piece + "\n" + fence + "\n"
            pieces.append((piece, rendered, fragmented, kind))
            remainder = remainder[len(piece) :]
    # Pack short adjacent blocks into a child; a heading should not receive an
    # isolated vector merely because it was a separate AST token.
    packed = []
    current = []
    for piece in pieces:
        if current and (
            piece[3] == "heading_open" or count("".join(p[1] for p in current + [piece])) > capacity
        ):
            packed.append(
                (
                    "".join(p[0] for p in current),
                    "".join(p[1] for p in current),
                    any(p[2] for p in current),
                )
            )
            current = []
        current.append(piece)
    if current:
        packed.append(
            (
                "".join(p[0] for p in current),
                "".join(p[1] for p in current),
                any(p[2] for p in current),
            )
        )
    pieces = packed
    # Group children into bounded parents; parent text is returned as context,
    # rather than creating a redundant parent embedding.
    parents = []
    group = []
    for piece in pieces:
        if (
            group
            and count(prefix + "".join(p[1] for p in group + [piece]))
            > config.parent_max_tokens - heading_reserve
        ):
            parents.append(group)
            group = []
        group.append(piece)
        if (
            count(prefix + "".join(p[1] for p in group))
            > config.parent_max_tokens - heading_reserve
        ):
            raise ValueError("Parent token budget exceeded")
    if group:
        parents.append(group)
    chunks = []
    source_offset = 0
    for parent_ordinal, group in enumerate(parents):
        parent_text = prefix + heading_at(source_offset) + "".join(p[1] for p in group)
        parent_id = digest(plan_id, "parent", parent_ordinal)
        previous = ""
        for own, rendered, continuation in group:
            overlap = ""
            if previous and config.overlap_tokens:
                # Suffix measured with the same model tokenizer as the child.
                low, high = 0, len(previous)
                while low < high:
                    middle = (low + high + 1) // 2
                    if count(previous[-middle:]) <= config.overlap_tokens:
                        low = middle
                    else:
                        high = middle - 1
                overlap = previous[-low:] if low else ""
                while overlap and count(overlap) > config.overlap_tokens:
                    overlap = overlap[1:]
            heading = heading_at(source_offset)
            text = prefix + heading + overlap + ("\n" if overlap else "") + rendered
            while overlap and count(text) + config.special_token_reserve > config.child_max_tokens:
                overlap = overlap[1:]
                text = prefix + heading + overlap + ("\n" if overlap else "") + rendered
            while heading and count(text) + config.special_token_reserve > config.child_max_tokens:
                heading = heading[:-1]
                text = prefix + heading + overlap + ("\n" if overlap else "") + rendered
            if len(text.encode()) > 50000 or len(parent_text.encode()) > 50000:
                raise ValueError("Tokenizer partition exceeds vector scalar byte limits")
            tokens = count(text)
            if tokens + config.special_token_reserve > config.child_max_tokens:
                raise ValueError("Child token budget exceeded; no truncation allowed")
            ordinal = len(chunks)
            chunks.append(
                Chunk(
                    digest(plan_id, "child", ordinal),
                    parent_id,
                    ordinal,
                    own,
                    text,
                    parent_text,
                    tokens,
                    continuation,
                )
            )
            previous = own
            source_offset += len(own)
    plan = IndexPlan(
        uri,
        document_id,
        revision,
        space,
        plan_id,
        tuple(chunks),
        parent_token_budget=config.parent_max_tokens,
    )
    plan.validate_coverage(source)
    plan.validate_parent_budget(count)
    return plan


def plan_resource(source: str, uri: str, account_id: str, embedding) -> IndexPlan:
    config = embedding.chunking
    tokenizer = load_tokenizer(config.tokenizer_path, config.tokenizer_sha256)
    model = embedding.hybrid or embedding.dense
    credentials = model.credentials
    first = credentials[0] if credentials else None
    provider = first.provider if first and first.provider else model.provider
    model_name = first.model if first and first.model else model.model
    if any(
        (c.provider or model.provider) != provider or (c.model or model.model) != model_name
        for c in credentials
    ):
        raise ValueError("Chunk embedding failover must stay in the same declared vector space")
    space = digest(provider, model_name, model.version, model.dimension, config.tokenizer_sha256)
    if model.wire_format != "input":
        space = digest(space, "wire", model.wire_format)
    return build_plan(
        source,
        uri,
        account_id,
        config,
        space,
        lambda text: len(tokenizer.encode(text, add_special_tokens=False).ids),
    )


def chunk_fields(plan: IndexPlan, chunk: Chunk) -> dict:
    return {
        "chunk_id": chunk.chunk_id,
        "parent_chunk_id": chunk.parent_chunk_id,
        # The citation a client opens is always the ORIGINAL source URI, never a
        # derived chunk file or a position/span. No extra location burden.
        "source_uri": plan.source_uri,
        "document_id": plan.document_id,
        "content_revision": plan.content_revision,
        "embedding_space_id": plan.embedding_space_id,
        "index_plan_id": plan.plan_id,
        "chunker_version": VERSION,
        "chunk_ordinal": chunk.ordinal,
        "chunk_text": chunk.embedding_text,
        "parent_text": chunk.parent_text,
        "chunk_continuation": int(chunk.continuation),
    }


def children_by_parent(chunks) -> dict[str, tuple]:
    """Group a plan's children by parent identity.

    One parent legitimately owns many children; recall must never collapse
    them into a single record just because they share a source URI.
    """
    groups: dict[str, list] = {}
    for chunk in chunks:
        groups.setdefault(chunk.parent_chunk_id, []).append(chunk)
    return {parent: tuple(members) for parent, members in groups.items()}


def _parent_text_of(candidate) -> str:
    if isinstance(candidate, dict):
        return candidate.get("parent_text", "") or ""
    return getattr(candidate, "parent_text", "") or ""


def expand_parent_budget(
    candidates,
    *,
    budget_tokens: int,
    count: Callable[[str], int],
):
    """Attach parent context in rank order without exceeding a token budget.

    Each surviving child keeps its own ``chunk_text`` and ``source_uri``; the
    parent body is only context. Once the budget is exhausted further parents
    are dropped, but their child hits remain, so a distant tail hit sharing a
    URI with a head hit is never lost to parent expansion.

    ``budget_tokens <= 0`` disables expansion and returns the hits unchanged.
    Dict hits that lose their parent get ``parent_text`` cleared in a copy;
    object hits are returned as-is so callers keep identity-based semantics.
    """
    if budget_tokens <= 0:
        return list(candidates)
    remaining = budget_tokens
    expanded = []
    for candidate in candidates:
        parent_text = _parent_text_of(candidate)
        if not parent_text:
            expanded.append(candidate)
            continue
        cost = count(parent_text)
        if cost <= remaining:
            remaining -= cost
            expanded.append(candidate)
        elif isinstance(candidate, dict):
            expanded.append({**candidate, "parent_text": ""})
        else:
            expanded.append(candidate)
    return expanded
