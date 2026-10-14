# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Keyword search over generated directory catalogues (``index.md``).

The agent locate leg of retrieval: given a directory URI and keywords
(``|`` = OR), walk the subtree, read the generated navigation indexes and
rank matching entries. Reads only catalogue files -- no body content, no
vectors, no hybrid BM25 rows -- so every hit is an entry URI to hand to
``content/read``.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Any, Dict, Iterable, List, Optional

from openviking.core.directory_index import (
    DIRECTORY_INDEX_FILENAME,
    is_generated_directory_index,
)
from openviking.server.error_mapping import is_not_found_error
from openviking_cli.exceptions import InvalidArgumentError, NotFoundError

_ENTRY_LINE = re.compile(
    r"^[-*]\s+\[(?P<name>[^\]]*)\]\((?P<uri>viking://[^)\s]+)\)\s*(?:—\s*(?P<description>.*))?$"
)
_HEADING = re.compile(r"^##\s+(?P<title>.+?)\s*$")
_KIND_BY_SECTION = {"子目录": "directory", "文件": "file"}
_NAME_WEIGHT = 2
_DESC_WEIGHT = 1
_DIR_SCAN_LIMIT = 4096


def parse_keywords(query: str) -> List[str]:
    """Split ``a|b|c`` into keywords; empty keywords are errors, not no-ops."""
    keywords = [part.strip() for part in str(query or "").split("|")]
    keywords = [keyword for keyword in keywords if keyword]
    if not keywords:
        raise InvalidArgumentError("query must contain at least one non-empty keyword")
    return keywords


def _join_uri(base: str, name: str) -> str:
    if base.endswith("/"):
        return f"{base}{name}"
    return f"{base}/{name}"


def _parse_entries(raw: str) -> Iterable[Dict[str, Any]]:
    """Yield catalogue entries (line number, name, uri, description, kind)."""
    section_kind: Optional[str] = None
    for line_no, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        heading = _HEADING.match(stripped)
        if heading:
            section_kind = _KIND_BY_SECTION.get(heading.group("title"))
            continue
        match = _ENTRY_LINE.match(stripped)
        if not match:
            continue
        name = match.group("name").strip()
        entry_uri = match.group("uri").strip()
        if not name or not entry_uri:
            continue
        yield {
            "line": line_no,
            "name": name,
            "uri": entry_uri,
            "description": (match.group("description") or "").strip(),
            "kind": section_kind,
            "content": line.rstrip("\r\n"),
        }


def _score_entry(
    entry: Dict[str, Any],
    keywords: List[str],
    *,
    case_insensitive: bool,
) -> Optional[Dict[str, Any]]:
    name = entry["name"]
    description = entry["description"]
    if case_insensitive:
        name_probe = name.lower()
        desc_probe = description.lower()
    else:
        name_probe = name
        desc_probe = description
    credit = 0
    name_hits = 0
    for keyword in keywords:
        probe = keyword.lower() if case_insensitive else keyword
        if probe in name_probe:
            credit += _NAME_WEIGHT
            name_hits += 1
        elif probe in desc_probe:
            credit += _DESC_WEIGHT
    if credit == 0:
        return None
    scored = dict(entry)
    scored["score"] = round(credit / (_NAME_WEIGHT * len(keywords)), 4)
    scored["_name_hits"] = name_hits
    return scored


async def _resolve_kind(viking_fs: Any, uri: str, ctx: Optional[Any]) -> str:
    try:
        info = await viking_fs.stat(uri, ctx=ctx, skip_count=True)
    except Exception:
        return "file"
    if isinstance(info, dict) and info.get("isDir"):
        return "directory"
    return "file"


async def search_navigation_indexes(
    *,
    viking_fs: Any,
    ctx: Optional[Any] = None,
    uri: str,
    query: str,
    limit: int = 20,
    case_insensitive: bool = False,
    level_limit: int = 10,
    include_content: bool = False,
) -> Dict[str, Any]:
    """Rank catalogue entries under ``uri`` matching ``query`` keywords.

    ``level_limit`` counts directory hops from ``uri``: 0 reads only
    ``uri``'s own ``index.md``, N walks down N levels.
    """
    keywords = parse_keywords(query)
    base = str(uri or "").strip()
    if not base:
        raise InvalidArgumentError("uri is required")
    if base != "viking://" and base.endswith("/"):
        base = base.rstrip("/")

    try:
        info = await viking_fs.stat(base, ctx=ctx, skip_count=True)
    except Exception as exc:
        if is_not_found_error(exc):
            raise NotFoundError(base, resource_type="directory") from exc
        raise
    if not isinstance(info, dict) or not info.get("isDir"):
        raise InvalidArgumentError(f"uri is not a directory: {base}")

    hits: List[Dict[str, Any]] = []
    index_files_scanned = 0
    queue = deque([(base, 0)])
    visited = {base}
    while queue:
        dir_uri, depth = queue.popleft()
        raw: Optional[Any] = None
        try:
            raw = await viking_fs.read(_join_uri(dir_uri, DIRECTORY_INDEX_FILENAME), ctx=ctx)
        except Exception:
            raw = None
        if raw is not None and is_generated_directory_index(raw):
            index_files_scanned += 1
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            for entry in _parse_entries(text):
                scored = _score_entry(entry, keywords, case_insensitive=case_insensitive)
                if scored is None:
                    continue
                scored["index_uri"] = _join_uri(dir_uri, DIRECTORY_INDEX_FILENAME)
                hits.append(scored)
        if depth >= level_limit or len(visited) >= _DIR_SCAN_LIMIT:
            continue
        try:
            children = await viking_fs.ls(dir_uri, output="original", ctx=ctx)
        except Exception:
            children = []
        for child in children or []:
            if not isinstance(child, dict) or not child.get("isDir"):
                continue
            child_uri = str(child.get("uri") or "").rstrip("/")
            if not child_uri or child_uri in visited:
                continue
            visited.add(child_uri)
            queue.append((child_uri, depth + 1))

    hits.sort(key=lambda hit: (-hit["score"], -hit["_name_hits"], hit["index_uri"], hit["line"]))
    matches: List[Dict[str, Any]] = []
    for hit in hits[:limit]:
        hit.pop("_name_hits", None)
        if hit["kind"] is None:
            hit["kind"] = await _resolve_kind(viking_fs, hit["uri"], ctx)
        if not include_content:
            hit.pop("content", None)
        matches.append(hit)
    return {
        "matches": matches,
        "count": len(matches),
        "index_files_scanned": index_files_scanned,
    }
