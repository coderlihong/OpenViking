# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Lossless chapter planning independent of PDF pages and model-specific output.

Markdown AST ranges are internal implementation details; published citations use
only Viking URIs. Ambiguous/missing bookmark anchors trigger a whole-document
fallback, never a manual-review state or a partially merged bookmark tree.
"""

from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from markdown_it import MarkdownIt

from openviking.utils.token_estimation import estimate_text_tokens


@dataclass(frozen=True)
class Heading:
    start: int
    end: int
    title: str
    level: int


@dataclass
class Chapter:
    chapter_id: str
    title: str
    level: int
    content: str
    start: int | None
    end: int | None
    parent_id: str | None = None
    page_start: int | None = None
    match_status: str = "matched"
    alias_of: str | None = None


@dataclass
class ChapterPlan:
    chapters: list[Chapter]
    structure_source: str
    alignment_status: str
    bookmarks: list[dict[str, Any]]
    info: list[dict[str, Any]] = field(default_factory=list)

    def validate_coverage(self, content: str) -> None:
        """Every original character belongs to exactly one own-body interval."""
        seen = set()
        for chapter in self.chapters:
            if not chapter.chapter_id or chapter.chapter_id in seen:
                raise ValueError("Chapter identities must be unique")
            if chapter.parent_id is not None and chapter.parent_id not in seen:
                raise ValueError("Chapter parent must precede its child")
            if chapter.alias_of is not None and chapter.alias_of not in seen:
                raise ValueError("Chapter alias must reference an earlier chapter")
            if chapter.start is None:
                if chapter.end is not None or chapter.content:
                    raise ValueError("Structural or alias chapter cannot duplicate own-body content")
            elif (isinstance(chapter.start, bool) or not isinstance(chapter.start, int) or
                  isinstance(chapter.end, bool) or not isinstance(chapter.end, int) or
                  chapter.start < 0 or chapter.end > len(content)):
                raise ValueError("Chapter interval is invalid")
            seen.add(chapter.chapter_id)
        cursor = 0
        for chapter in sorted(
            (c for c in self.chapters if c.start is not None), key=lambda c: c.start
        ):
            if chapter.start != cursor or chapter.end is None or chapter.end < cursor:
                raise ValueError("Chapter coverage has a gap or overlap")
            if chapter.content != content[chapter.start : chapter.end]:
                raise ValueError("Chapter content does not match its original interval")
            cursor = chapter.end
        if cursor != len(content):
            raise ValueError("Chapter coverage does not reach the document tail")


def _normalize(title: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", title)).casefold()


#: A line that *reads* like a chapter anchor: an Arabic section number (``10.2``)
#: or a Chinese chapter marker (``第 10 章``).  Spacing between the marker and the
#: number/title is not under the corpus author's control -- extracted PDF text
#: renders ``第 10 章 故障诊断`` with the very spaces the typeset title carries --
#: so the marker is matched with explicit whitespace tolerance instead of the
#: unspelled form only.  ``\s*\S`` at the end keeps a bare ``第 1 章`` line, which
#: states nothing, from becoming an anchor.
_NUMBERED_TITLE = re.compile(
    r"(?:"
    r"\d+(?:[.．]\d+)*[.、．]?"
    r"|第\s*[0-9０-９]+\s*[章节篇部]"
    r"|第\s*[一二三四五六七八九十百零两]+\s*[章节篇部]"
    r")\s*\S"
)


def _ast(content: str) -> tuple[list[Any], list[int]]:
    offsets = [0]
    for line in content.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return MarkdownIt("commonmark").enable("table").parse(content), offsets


def _headings(tokens: list[Any], offsets: list[int]) -> list[Heading]:
    return [
        Heading(offsets[t.map[0]], offsets[t.map[1]], tokens[i + 1].content, int(t.tag[1:]))
        for i, t in enumerate(tokens)
        if t.type == "heading_open" and t.level == 0 and t.map and tokens[i + 1].content.strip()
    ]


def _id(prefix: str, ordinal: int, title: str) -> str:
    digest = hashlib.sha256(f"{prefix}:{ordinal}:{title}".encode()).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _tree_parents(levels: list[int]) -> list[int | None]:
    stack: list[int] = []
    parents: list[int | None] = []
    for i, level in enumerate(levels):
        while stack and levels[stack[-1]] >= level:
            stack.pop()
        parents.append(stack[-1] if stack else None)
        stack.append(i)
    return parents


def _bookmark_plan(
    content: str,
    bookmarks: list[dict[str, Any]],
    tokens: list[Any],
    offsets: list[int],
    *,
    deadline: float,
    max_candidates: int,
) -> tuple[list[Chapter] | None, str, int]:
    titles = {_normalize(b["title"]) for b in bookmarks}
    headings = _headings(tokens, offsets)
    candidates = list(headings)
    starts = {h.start for h in candidates}
    # Local text/OCR may have plain numbered titles rather than Markdown headings.
    # Only standalone paragraph lines matching an entire bookmark title qualify.
    for token in tokens:
        if time.monotonic() >= deadline:
            return None, "alignment_budget_exceeded", len(bookmarks)
        if token.type != "paragraph_open" or token.level != 0 or not token.map:
            continue
        first, last = token.map
        lines = content[offsets[first] : offsets[last]].splitlines(keepends=True)
        pos = offsets[first]
        for line in lines:
            title = line.strip()
            numbered = _NUMBERED_TITLE.match(title)
            if _normalize(title) in titles and (numbered or len(lines) == 1) and pos not in starts:
                candidates.append(Heading(pos, pos + len(line), title, 1))
                starts.add(pos)
            pos += len(line)
    candidates.sort(key=lambda h: h.start)
    if len(candidates) > max_candidates:
        return None, "alignment_candidate_limit", len(bookmarks)
    by_title: dict[str, list[Heading]] = {}
    for candidate in candidates:
        by_title.setdefault(_normalize(candidate.title), []).append(candidate)
    levels = [int(b["level"]) for b in bookmarks]
    parents = _tree_parents(levels)
    paths: list[tuple[str, ...]] = []
    canonical: dict[tuple[str, ...], int] = {}
    aliases: dict[int, int] = {}
    groups: list[tuple[int, list[Heading]]] = []
    branch_indices = {p for p in parents if p is not None}
    missing = 0
    for i, bookmark in enumerate(bookmarks):
        parent = parents[i]
        path = (
            (*paths[parent], _normalize(bookmark["title"]))
            if parent is not None
            else (_normalize(bookmark["title"]),)
        )
        paths.append(path)
        anchors = by_title.get(path[-1], [])
        # A repeated title alone does not prove an alias: distinct sections may
        # legitimately have identical titles. Only one actual anchor plus the
        # same known target page can validate a duplicate outline entry.
        previous = canonical.get(path)
        if (
            previous is not None
            and bookmark.get("page_num") is not None
            and bookmark.get("page_num") == bookmarks[previous].get("page_num")
            and len(anchors) == 1
        ):
            aliases[i] = previous
            continue
        canonical.setdefault(path, i)
        if anchors:
            groups.append((i, anchors))
        elif i not in branch_indices:
            missing += 1
    if missing or not groups:
        return None, "missing_heading_matches", max(missing, 1)
    # Count all monotonic full alignments (capped at two); no greedy first match.
    # Prefix sums over sorted anchors keep repeated titles from quadratic growth.
    layers: list[list[tuple[Heading, int, int | None]]] = []
    for _, anchors in groups:
        if time.monotonic() >= deadline:
            return None, "alignment_budget_exceeded", len(bookmarks)
        previous = layers[-1] if layers else []
        layer: list[tuple[Heading, int, int | None]] = []
        count = 0
        predecessor = None
        prev_idx = 0
        for anchor in anchors:
            if not layers:
                layer.append((anchor, 1, None))
                continue
            while prev_idx < len(previous) and previous[prev_idx][0].start < anchor.start:
                ways = previous[prev_idx][1]
                if ways:
                    predecessor = prev_idx if count == 0 and ways == 1 else None
                    count = min(2, count + ways)
                prev_idx += 1
            layer.append((anchor, count, predecessor if count == 1 else None))
        layers.append(layer)
    total = min(2, sum(item[1] for item in layers[-1]))
    if total != 1:
        reason = "ambiguous_heading_matches" if total else "heading_order_conflict"
        return None, reason, len(bookmarks)
    chosen: dict[int, Heading] = {}
    idx = next(i for i, item in enumerate(layers[-1]) if item[1])
    for group_idx in range(len(groups) - 1, -1, -1):
        anchor, _, previous_idx = layers[group_idx][idx]
        chosen[groups[group_idx][0]] = anchor
        idx = previous_idx if previous_idx is not None else 0
    ordered = sorted(chosen, key=lambda i: chosen[i].start)
    ends = {
        i: chosen[ordered[k + 1]].start if k + 1 < len(ordered) else len(content)
        for k, i in enumerate(ordered)
    }
    ids = [_id("bookmark", i, b["title"]) for i, b in enumerate(bookmarks)]
    chapters = []
    first = chosen[ordered[0]].start
    if first:
        chapters.append(
            Chapter(_id("preface", 0, "前置内容"), "前置内容", 1, content[:first], 0, first)
        )
    for i, bookmark in enumerate(bookmarks):
        anchor = chosen.get(i)
        alias = aliases.get(i)
        chapters.append(
            Chapter(
                chapter_id=ids[i],
                title=bookmark["title"],
                level=levels[i],
                content=content[anchor.start : ends[i]] if anchor else "",
                start=anchor.start if anchor else None,
                end=ends[i] if anchor else None,
                parent_id=ids[parents[i]] if parents[i] is not None else None,
                page_start=bookmark.get("page_num"),
                match_status="validated_alias"
                if alias is not None
                else "matched"
                if anchor
                else "structural_only",
                alias_of=ids[alias] if alias is not None else None,
            )
        )
    return chapters, "", 0


def split_chapters(
    content: str,
    bookmarks: list[dict[str, Any]],
    *,
    section_target_tokens: int = 4096,
    alignment_timeout: float = 10.0,
    max_alignment_candidates: int = 20000,
) -> ChapterPlan:
    """Try bookmark alignment, then AST headings, then whole-block grouping."""
    if not content.strip():
        raise ValueError("Parser returned empty document content")
    if (isinstance(section_target_tokens, bool) or not isinstance(section_target_tokens, int) or section_target_tokens < 1 or
            isinstance(alignment_timeout, bool) or not isinstance(alignment_timeout, (int, float)) or
            not 0 < alignment_timeout < float("inf") or isinstance(max_alignment_candidates, bool) or
            not isinstance(max_alignment_candidates, int) or max_alignment_candidates < 1):
        raise ValueError("Chapter splitting budgets must be positive")
    deadline = time.monotonic() + alignment_timeout
    tokens, offsets = _ast(content)
    headings = _headings(tokens, offsets)
    chapters, reason, unmatched = (
        _bookmark_plan(
            content,
            bookmarks,
            tokens,
            offsets,
            deadline=deadline,
            max_candidates=max_alignment_candidates,
        )
        if bookmarks
        else (None, "bookmarks_unavailable", 0)
    )
    info = []
    if chapters is not None:
        structure, status = "pdf_bookmarks", "matched"
    else:
        status = "failed" if bookmarks else "not_available"
        if headings:
            structure = "markdown_headings"
            parents = _tree_parents([h.level for h in headings])
            ids = [_id("heading", i, h.title) for i, h in enumerate(headings)]
            chapters = []
            if headings[0].start:
                chapters.append(
                    Chapter(
                        _id("preface", 0, "前置内容"),
                        "前置内容",
                        1,
                        content[: headings[0].start],
                        0,
                        headings[0].start,
                    )
                )
            for i, heading in enumerate(headings):
                end = headings[i + 1].start if i + 1 < len(headings) else len(content)
                chapters.append(
                    Chapter(
                        ids[i],
                        heading.title,
                        heading.level,
                        content[heading.start : end],
                        heading.start,
                        end,
                        ids[parents[i]] if parents[i] is not None else None,
                    )
                )
        else:
            structure = "rule_blocks"
            starts = sorted({0, *(offsets[t.map[0]] for t in tokens if t.map and t.level == 0)})
            starts.append(len(content))
            chapters = []
            begin = 0
            size = 0
            for left, right in zip(starts, starts[1:], strict=False):
                block_size = estimate_text_tokens(content[left:right])
                if size and size + block_size > section_target_tokens:
                    n = len(chapters) + 1
                    chapters.append(
                        Chapter(
                            _id("rule", n, "section"),
                            f"section_{n:04d}",
                            1,
                            content[begin:left],
                            begin,
                            left,
                        )
                    )
                    begin, size = left, 0
                size += block_size
            n = len(chapters) + 1
            chapters.append(
                Chapter(
                    _id("rule", n, "section"),
                    f"section_{n:04d}",
                    1,
                    content[begin:],
                    begin,
                    len(content),
                )
            )
        info.append(
            {
                "code": "BOOKMARK_ALIGNMENT_FAILED" if bookmarks else "BOOKMARKS_UNAVAILABLE",
                "message": "书签对齐未成功，已按Markdown标题或规则切分继续入库"
                if bookmarks
                else "PDF无可用书签，已按Markdown标题或规则切分继续入库",
                "reason": reason,
                "fallback_strategy": structure,
                "unmatched_count": unmatched,
            }
        )
    plan = ChapterPlan(chapters, structure, status, bookmarks, info)
    plan.validate_coverage(content)
    return plan
