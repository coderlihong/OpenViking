# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Data consistency checks between VikingFS content and vector index records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openviking.core.context import ResourceContentType
from openviking.core.directory_index import is_generated_index_at
from openviking.core.namespace import is_session_uri
from openviking.server.identity import RequestContext
from openviking.storage.expr import Eq
from openviking.storage.internal_names import CHAPTER_METADATA_FILENAMES
from openviking.storage.ovpack.index import normalize_uri_key, walk_uri_record_batches
from openviking.utils.embedding_utils import get_resource_content_type
from openviking_cli.utils.logger import get_logger
from openviking_cli.utils.uri import VikingURI

logger = get_logger(__name__)

NON_INDEX_SCOPES = frozenset({"session"})
PUBLIC_MISSING_RECORD_LIMIT = 20
ERROR_DETAILS_MISSING_RECORD_LIMIT = 1
INDEX_RECORD_PAGE_LIMIT = 100
INDEX_RECORD_SCAN_CAP = 1000


@dataclass(frozen=True)
class IndexExpectation:
    """One vector index record expected from filesystem content."""

    uri: str
    rel_path: str
    level: int

    @property
    def key(self) -> str:
        path = self.rel_path or "."
        return f"{path}#level={self.level}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "path": self.rel_path,
            "level": self.level,
            "key": self.key,
        }


@dataclass(frozen=True)
class IndexConsistencyReport:
    """Result of checking filesystem/index consistency for a subtree."""

    expected: tuple[IndexExpectation, ...]
    missing_records: tuple[IndexExpectation, ...]

    @property
    def ok(self) -> bool:
        return not self.missing_records

    def details(self, limit: int = ERROR_DETAILS_MISSING_RECORD_LIMIT) -> dict[str, Any]:
        limited = self.missing_records[:limit]
        return {
            "expected_count": len(self.expected),
            "missing_record_count": len(self.missing_records),
            "missing_records": [item.key for item in limited],
            "missing_records_truncated": len(self.missing_records) > len(limited),
        }

    def to_dict(self, limit: int = PUBLIC_MISSING_RECORD_LIMIT) -> dict[str, Any]:
        limited = self.missing_records[:limit]
        return {
            "ok": self.ok,
            "expected_count": len(self.expected),
            "missing_record_count": len(self.missing_records),
            "missing_records": [item.to_dict() for item in limited],
            "missing_records_truncated": len(self.missing_records) > len(limited),
        }


def _join_uri(base_uri: str, rel_path: str) -> str:
    return VikingURI(base_uri).join(rel_path).uri


def _entry_uri(root_uri: str, entry: dict[str, Any]) -> str:
    uri = entry.get("uri")
    if isinstance(uri, str) and uri:
        return uri
    rel_path = str(entry.get("rel_path") or "")
    return _join_uri(root_uri, rel_path)


def _is_index_scope(uri: str) -> bool:
    try:
        return VikingURI(uri).scope not in NON_INDEX_SCOPES and not is_session_uri(uri)
    except Exception:
        return False


def _leaf_name(uri_or_path: str) -> str:
    return uri_or_path.rstrip("/").split("/")[-1]


async def _read_text_if_exists(viking_fs, uri: str, ctx: RequestContext) -> str:
    try:
        if not await viking_fs.exists(uri, ctx=ctx):
            return ""
        content = await viking_fs.read_file(uri, ctx=ctx)
        return content.decode("utf-8") if isinstance(content, bytes) else str(content)
    except Exception:
        return ""


def _directory_candidates(
    root_uri: str,
    entries: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    if root_uri != "viking://" and _is_index_scope(root_uri):
        candidates.append((root_uri, ""))

    for entry in entries:
        if not entry.get("isDir"):
            continue
        uri = _entry_uri(root_uri, entry)
        if not _is_index_scope(uri):
            continue
        rel_path = str(entry.get("rel_path") or "")
        candidates.append((uri, rel_path))
    return candidates


def _file_candidates(
    root_uri: str,
    entries: list[dict[str, Any]],
) -> list[tuple[str, str, str]]:
    candidates: list[tuple[str, str, str]] = []
    for entry in entries:
        if entry.get("isDir"):
            continue
        size = entry.get("size")
        if isinstance(size, int) and not isinstance(size, bool) and size == 0:
            continue
        uri = _entry_uri(root_uri, entry)
        if not _is_index_scope(uri):
            continue
        rel_path = str(entry.get("rel_path") or "")
        name = str(entry.get("name") or _leaf_name(rel_path))
        if name.startswith(".") or name in CHAPTER_METADATA_FILENAMES:
            continue
        candidates.append((uri, rel_path, name))
    return candidates


async def build_index_expectations(
    viking_fs,
    root_uri: str,
    entries: list[dict[str, Any]],
    ctx: RequestContext,
) -> tuple[IndexExpectation, ...]:
    """Build the index records expected from current filesystem content."""
    expectations: list[IndexExpectation] = []

    for uri, rel_path in _directory_candidates(root_uri, entries):
        abstract = await _read_text_if_exists(viking_fs, f"{uri}/.abstract.md", ctx)
        overview = await _read_text_if_exists(viking_fs, f"{uri}/.overview.md", ctx)
        if abstract:
            expectations.append(IndexExpectation(uri=uri, rel_path=rel_path, level=0))
        if overview:
            expectations.append(IndexExpectation(uri=uri, rel_path=rel_path, level=1))

    for uri, rel_path, name in _file_candidates(root_uri, entries):
        if get_resource_content_type(name) != ResourceContentType.TEXT:
            continue
        # The pipeline never vectorizes a generated navigation catalogue (its
        # entry list would outrank the entries), so the index owes it no L2 row.
        # The frontmatter marker is the authority, not the name: a legacy
        # chapter body stored at index.md stays expected.
        if await is_generated_index_at(viking_fs, uri, ctx=ctx):
            continue
        expectations.append(IndexExpectation(uri=uri, rel_path=rel_path, level=2))

    return tuple(sorted(expectations, key=lambda item: (item.rel_path, item.level)))


def _record_level(record: dict[str, Any]) -> int | None:
    level = record.get("level")
    if isinstance(level, bool) or not isinstance(level, int):
        return None
    return level


async def _fetch_index_records(vector_store, uri: str, ctx: RequestContext) -> list[dict[str, Any]]:
    """Fetch every record stored under the exact URI.

    A URI keeps one row per level per generation for its whole lifetime, so a
    fixed small limit truncates the level map on URIs with build history and
    reports a present level as missing. Paginate until the store is exhausted.
    """
    if not vector_store or not hasattr(vector_store, "filter"):
        return []

    records: list[dict[str, Any]] = []
    offset = 0
    while len(records) < INDEX_RECORD_SCAN_CAP:
        kwargs = {
            "filter": Eq("uri", uri),
            "limit": INDEX_RECORD_PAGE_LIMIT,
            "output_fields": ["uri", "level"],
        }
        if offset:
            kwargs["offset"] = offset
        try:
            page = await vector_store.filter(**kwargs, ctx=ctx)
        except TypeError:
            try:
                page = await vector_store.filter(**kwargs)
            except Exception as exc:
                logger.warning(f"Failed to check vector index records for {uri}: {exc}")
                return records
        except Exception as exc:
            logger.warning(f"Failed to check vector index records for {uri}: {exc}")
            return records
        records.extend(page)
        if len(page) < INDEX_RECORD_PAGE_LIMIT:
            break
        offset += len(page)
    return records


async def check_index_consistency(
    viking_fs,
    vector_store,
    root_uri: str,
    entries: list[dict[str, Any]],
    ctx: RequestContext,
) -> IndexConsistencyReport:
    """Check that filesystem content has the expected vector index records."""
    expectations = await build_index_expectations(viking_fs, root_uri, entries, ctx)
    missing_records: list[IndexExpectation] = []

    records_by_uri: dict[str, dict[int, dict[str, Any]]] = {}
    candidate_uris = sorted({expectation.uri for expectation in expectations})
    if candidate_uris and vector_store is not None and hasattr(vector_store, "filter"):
        # Batched fetch: one subtree query per directory group instead of one
        # Eq query per candidate URI (the per-URI loop dominates runtime on
        # large subtrees). Same rows, same level maps.
        fetched: dict[str, list[dict[str, Any]]] = {}

        async def consume(
            uris: list[str], by_uri: dict[str, list[dict[str, Any]]]
        ) -> None:
            for uri in uris:
                fetched[normalize_uri_key(uri)] = by_uri.get(normalize_uri_key(uri), [])

        await walk_uri_record_batches(
            vector_store,
            root_uri,
            candidate_uris,
            ctx,
            output_fields=["uri", "level"],
            consume=consume,
        )
        for uri, records in fetched.items():
            records_by_uri[uri] = {
                level: record
                for record in records
                if (level := _record_level(record)) is not None
            }
    for expectation in expectations:
        if expectation.uri not in records_by_uri:
            records = await _fetch_index_records(vector_store, expectation.uri, ctx)
            records_by_uri[expectation.uri] = {
                level: record for record in records if (level := _record_level(record)) is not None
            }
        record = records_by_uri[expectation.uri].get(expectation.level)
        if record is None:
            missing_records.append(expectation)

    return IndexConsistencyReport(
        expected=expectations,
        missing_records=tuple(missing_records),
    )
