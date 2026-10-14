# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Machine-generated per-directory navigation index (``index.md``).

An agent locating knowledge has two cheap moves: list a directory and grep its
navigation index.  ``index.md`` is that index -- one entry per direct child,
each carrying the literal ``viking://`` URI an agent hands to
``content/read``.

Unlike ``.abstract.md`` / ``.overview.md`` this document is *navigation only*:
it is never summarized into its parent and never turned into vectors or lexical
rows.  A generated index lists every child title, so indexing it would let the
catalogue outrank the chapters it points at -- the exact failure already
documented for the parser's ``toc.md``.

The filename is not the authority for that exclusion.  Chapter corpora published
before this feature store a branch chapter's own body at ``{dir}/index.md``, and
that prose is indexed L2 content.  So a generated index is identified by its
frontmatter ``kind: directory_index`` marker, and an existing body is never
overwritten by a catalogue.  The name is closed against *new* authored bodies at
the content-write boundary (see ``authored_index_collision``); a legacy corpus is
moved to the canonical chapter name by the maintenance pass, not silently renamed
while it is being indexed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

import yaml

DIRECTORY_INDEX_FILENAME = "index.md"
DIRECTORY_INDEX_KIND = "directory_index"
_ABSTRACT_CHAR_LIMIT = 220
_BODY_CHAR_LIMIT = 512_000
# Only a leading markdown marker is decoration; inner spacing is content and an
# agent greps for it ("10.2.1 拆卸步骤"), so this must stay anchored and require
# at least one marker character.
_SUMMARY_LINE = re.compile(r"^[ \t]*(?:[#>*\-]|\*\*)+[ \t]*")

#: Write outcome so the semantic pipeline can log a skip instead of raising.
_SKIPPED_FOREIGN_BODY = "foreign_body"


@dataclass(frozen=True)
class DirectoryIndexEntry:
    """One navigable child of a directory."""

    name: str
    uri: str
    description: str = ""
    kind: str = "file"

    def normalized(self) -> "DirectoryIndexEntry":
        name = str(self.name or "").strip()
        uri = str(self.uri or "").strip()
        description = one_line(self.description)
        return DirectoryIndexEntry(name=name, uri=uri, description=description, kind=self.kind)


@dataclass(frozen=True)
class DirectoryIndexWriteResult:
    """Result of one navigation-index writeback."""

    wrote: bool
    uri: str
    reason: str = ""
    entries: int = 0

    def __bool__(self) -> bool:
        return self.wrote


def directory_index_uri(dir_uri: str) -> str:
    """Return the navigation index URI that describes ``dir_uri``."""

    value = str(dir_uri or "").rstrip("/")
    if not value:
        raise ValueError("directory index requires a non-empty directory URI")
    return f"{value}/{DIRECTORY_INDEX_FILENAME}"


def is_directory_index_uri(uri: str) -> bool:
    """Cheap filename test; content marker remains the authority."""

    return str(uri or "").rstrip("/").rsplit("/", 1)[-1] == DIRECTORY_INDEX_FILENAME


def authored_index_collision(uri: str, content: str | bytes | None) -> Optional[str]:
    """Return where an authored body must live when it claims ``{dir}/index.md``.

    A directory's ``index.md`` belongs to the generated catalogue: it is the file
    an agent greps to list the directory, and it is kept out of the vector and
    summary chains.  Chapter prose that takes the name instead leaves that
    directory with no catalogue at all, and the indexer will not overwrite the
    prose to get one.  The canonical chapter name is ``{dir}/{dir}.md``, which is
    what the parser produces for a branch chapter, so the caller gets the exact
    URI to write to rather than a rule to reverse-engineer.

    Only the resource namespace is reserved; memory and skill trees have their own
    discovery contracts and the indexer never writes a catalogue there.
    """

    from openviking.core.namespace import context_type_for_uri

    if not is_directory_index_uri(uri) or context_type_for_uri(uri) != "resource":
        return None
    if is_generated_directory_index(content):
        return None
    parent = str(uri).rstrip("/").rsplit("/", 1)[0]
    if not parent:
        return None
    directory_name = parent.rsplit("/", 1)[-1]
    return f"{parent}/{directory_name}.md"


def one_line(text: Any) -> str:
    """Collapse a summary into one grep-friendly line without markdown noise."""

    if text is None:
        return ""
    value = str(text).replace("\r\n", "\n").replace("\r", "\n")
    parts: list[str] = []
    for raw in value.split("\n"):
        cleaned = _SUMMARY_LINE.sub("", raw).strip()
        if cleaned:
            parts.append(cleaned)
    line = " ".join(parts)
    if len(line) > _ABSTRACT_CHAR_LIMIT:
        line = line[: _ABSTRACT_CHAR_LIMIT - 1].rstrip() + "…"
    return line


# HTML fragments and table markup are cell noise in a one-line description;
# table cell text rides on them and cannot be reassembled, so the whole tag
# run goes rather than a mangled fragment.
_HTML_TAG_RUN = re.compile(r"(?:<[^>]+>[ \t]*)+")
# `- key: value` header blocks exported alongside chapter prose (document_id,
# chapter_uri, page) provenance lines, not content.
_META_BULLET = re.compile(r"^[ \t]*-[ \t]*\w+[ \t]*:[ \t]*\S")
_INLINE_TAG = re.compile(r"</?[a-zA-Z][^>]*>")


def navigation_description(text: Any) -> str:
    """One-line description of a child's *own* content for a navigation index.

    Navigation lines are grep anchors an agent scans to pick a URI, so they
    carry prose only: markdown heading markers and bullet marks are stripped,
    HTML/table fragments (which would survive as mangled tag runs once
    newlines collapse) are dropped, and provenance bullets like
    ``- document_id: ...`` are excluded -- those describe the export, not the
    directory's content. Falls back to whatever prose remains; empty when a
    child carries no presentable text.
    """

    if text is None:
        return ""
    value = str(text).replace("\r\n", "\n").replace("\r", "\n")
    parts: list[str] = []
    for raw in value.split("\n"):
        if not _HTML_TAG_RUN.sub("", raw).strip() and "<" in raw and ">" in raw:
            continue
        cleaned = _META_BULLET.match(raw)
        if cleaned:
            continue
        cleaned = _SUMMARY_LINE.sub("", _INLINE_TAG.sub(" ", raw)).strip()
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        if cleaned:
            parts.append(cleaned)
    line = " ".join(parts).strip()
    if len(line) > _ABSTRACT_CHAR_LIMIT:
        line = line[: _ABSTRACT_CHAR_LIMIT - 1].rstrip() + "…"
    return line


def entries_from_children(
    children: Sequence[Mapping[str, Any]],
) -> list[DirectoryIndexEntry]:
    """Build directory entries from ``{"name", "abstract"}`` summaries."""

    out: list[DirectoryIndexEntry] = []
    for item in children:
        name = str(item.get("name") or "").strip()
        uri = str(item.get("uri") or "").strip()
        if not name or not uri:
            continue
        out.append(
            DirectoryIndexEntry(
                name=name,
                uri=uri,
                description=item.get("abstract") or item.get("description") or "",
                kind="directory",
            ).normalized()
        )
    return out


def entries_from_files(
    files: Sequence[Mapping[str, Any]],
) -> list[DirectoryIndexEntry]:
    """Build file entries from ``{"name", "uri", "summary"}`` summaries."""

    out: list[DirectoryIndexEntry] = []
    for item in files:
        name = str(item.get("name") or "").strip()
        uri = str(item.get("uri") or "").strip()
        if not name or not uri:
            continue
        out.append(
            DirectoryIndexEntry(
                name=name,
                uri=uri,
                description=item.get("summary") or item.get("description") or "",
                kind="file",
            ).normalized()
        )
    return out


def _link(entry: DirectoryIndexEntry) -> str:
    label = entry.name.replace("[", "").replace("]", "")
    line = f"- [{label}]({entry.uri})"
    if entry.description:
        line = f"{line} — {entry.description}"
    return line


def render_index_body(
    dir_uri: str,
    directories: Iterable[DirectoryIndexEntry],
    files: Iterable[DirectoryIndexEntry],
    *,
    title: Optional[str] = None,
) -> str:
    """Render the visible body: one literal URI per direct child."""

    listing = [str(p or "").strip("/").rsplit("/", 1)[-1] for p in [dir_uri] if p]
    name = title or (listing[0] if listing else "根目录")
    dirs = [e for e in directories if e.uri]
    leaves = [e for e in files if e.uri]
    lines: list[str] = [
        f"# 目录索引 · {name}",
        "",
        "本文件由 OpenViking 自动生成，是本目录的导航清单：每个直接子条目一行，带完整 viking URI。",
        "它不建向量，也不进 BM25 召回，所以检索命中里不会出现它，正文不会被清单挤掉。",
        "定位方法两条：按目录 grep 本文件（grep 的 uri 填目录，pattern 填关键词），或直接读 目录/index.md。",
        "拿到 URI 后用 content/read 精读；分层摘要与向量索引都在各自的章节文件里。",
        "",
    ]
    if dirs:
        lines += ["## 子目录", ""]
        lines += [_link(e) for e in dirs]
        lines.append("")
    if leaves:
        lines += ["## 文件", ""]
        lines += [_link(e) for e in leaves]
        lines.append("")
    if not dirs and not leaves:
        lines += ["（此目录暂无可列出的条目。）", ""]
    body = "\n".join(lines).rstrip() + "\n"
    if len(body) > _BODY_CHAR_LIMIT:
        body = body[:_BODY_CHAR_LIMIT].rstrip() + "\n\n（条目过多，索引在此处截断。）\n"
    return body


def render_directory_index(
    dir_uri: str,
    directories: Iterable[DirectoryIndexEntry],
    files: Iterable[DirectoryIndexEntry],
    *,
    title: Optional[str] = None,
    generated_by: Optional[Mapping[str, Any]] = None,
) -> str:
    """Render OKF markdown for a generated navigation index."""

    dirs = [e.normalized() for e in directories]
    leaves = [e.normalized() for e in files]
    frontmatter: dict[str, Any] = {
        "kind": DIRECTORY_INDEX_KIND,
        "directory": str(dir_uri or "").rstrip("/") + "/",
        "entry_count": len(dirs) + len(leaves),
    }
    if generated_by:
        frontmatter["generated_by"] = dict(generated_by)
    header = yaml.safe_dump(frontmatter, allow_unicode=True, default_flow_style=False).rstrip()
    body = render_index_body(dir_uri, dirs, leaves, title=title)
    return f"---\n{header}\n---\n\n{body}"


def is_generated_directory_index(raw: str | bytes | None) -> bool:
    """Return whether ``raw`` is a generated navigation index.

    Reads only the frontmatter block, so a large legacy body named ``index.md``
    is decided by its own header and stays indexed as chapter content.
    """

    if raw is None:
        return False
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return False
    if not raw.startswith("---"):
        return False
    lines = raw.split("\n", 40)
    block: list[str] = []
    for index in range(1, len(lines)):
        if lines[index].rstrip("\r") == "---":
            block = lines[1:index]
            break
    else:
        return False
    for line in block:
        key, _, value = line.partition(":")
        if key.strip() == "kind" and value.strip().strip("'\"") == DIRECTORY_INDEX_KIND:
            return True
    return False


def directory_index_entry_uris(raw: str | bytes | None) -> list[str]:
    """Extract every URI a generated index advertises (audit and test helper)."""

    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", "replace")
        except Exception:  # pragma: no cover - defensive
            return []
    if not isinstance(raw, str):
        return []
    return re.findall(r"\]\((viking://[^)\s]+)\)", raw)


async def read_head(viking_fs: Any, uri: str, *, ctx: Any = None) -> Optional[str]:
    """Read a document for marker detection; ``None`` when it does not exist."""

    from openviking.server.error_mapping import is_not_found_error

    read_file = getattr(viking_fs, "read_file", None)
    if read_file is None:
        return None
    try:
        from openviking.indexing.content import draft_sources

        with draft_sources():
            content = await read_file(uri, ctx=ctx)
    except KeyError:
        return None
    except Exception as exc:
        if is_not_found_error(exc):
            return None
        raise
    if isinstance(content, bytes):
        return content.decode("utf-8", "replace")
    return str(content or "")


async def is_generated_index_at(
    viking_fs: Any,
    uri: str,
    *,
    ctx: Any = None,
    cache: Optional[dict[str, bool]] = None,
) -> bool:
    """Content-authoritative check used by enumeration sites."""

    if not is_directory_index_uri(uri):
        return False
    if cache is not None and uri in cache:
        return cache[uri]
    result = is_generated_directory_index(await read_head(viking_fs, uri, ctx=ctx))
    if cache is not None:
        cache[uri] = result
    return result


async def navigation_index_uris(
    viking_fs: Any,
    uris: Sequence[str],
    *,
    ctx: Any = None,
) -> set[str]:
    """Partition ``uris`` into the set that are generated navigation indexes."""

    cache: dict[str, bool] = {}
    return {
        uri
        for uri in uris
        if await is_generated_index_at(viking_fs, uri, ctx=ctx, cache=cache)
    }


async def write_directory_index(
    *,
    viking_fs: Any,
    dir_uri: str,
    directories: Sequence[DirectoryIndexEntry],
    files: Sequence[DirectoryIndexEntry],
    ctx: Any = None,
    title: Optional[str] = None,
    generated_by: Optional[Mapping[str, Any]] = None,
    lock: Optional[dict[str, Any]] = None,
) -> DirectoryIndexWriteResult:
    """Write ``{dir}/index.md``, refusing to overwrite an authored body.

    A legacy chapter corpus keeps its branch-chapter prose at ``{dir}/index.md``.
    That prose is indexed L2 content and must never be replaced by a catalogue,
    so the write is skipped with a reason instead of forced.
    """

    target = directory_index_uri(dir_uri)
    current = await read_head(viking_fs, target, ctx=ctx)
    if current is not None and not is_generated_directory_index(current):
        return DirectoryIndexWriteResult(
            wrote=False, uri=target, reason=_SKIPPED_FOREIGN_BODY
        )
    rendered = render_directory_index(
        dir_uri,
        directories,
        files,
        title=title,
        generated_by=generated_by,
    )
    if current is not None and current == rendered:
        return DirectoryIndexWriteResult(
            wrote=False,
            uri=target,
            reason="unchanged",
            entries=len(directories) + len(files),
        )
    await viking_fs.write_file(target, rendered, ctx=ctx, lease_ref=lock)
    return DirectoryIndexWriteResult(
        wrote=True, uri=target, entries=len(directories) + len(files)
    )
