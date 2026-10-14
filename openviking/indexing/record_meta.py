# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Derived, file-scoped meta for every vector record.

A vector row is only citable and filterable if it states two things about
itself:

* **which modality it is** -- an image VL joint embedding or a text
  parent/child chunk embedding;
* **where it came from** -- the viking URI of the source file, plus whatever
  document/chapter/page identifiers the corpus itself declares.

Neither is guessed.  Authored Markdown already carries its provenance as a
bullet block right under the chapter heading::

    # 10.2 进行操作时的注意事项

    - document_id: 797105495448853
    - chapter_uri: kb://chapters/843907453002281
    - page: p.638-641

This module reads that block once per record at enqueue time and feeds it
through the *existing* business-meta pipeline (``indexing.metadata_integration``),
so filtering uses the same exact-ID equality as every other business field.
The category enum is deliberately literal: image rows are ``images``, text rows
are ``text``.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Dict, Iterable, Mapping, Optional

CATEGORY_FIELD = "category"
IMAGE_CATEGORY = "images"
TEXT_CATEGORY = "text"
SOURCE_URI_FIELD = "source_uri"
IMAGE_URI_FIELD = "image_uri"

#: Keys a record always owns.  A directory-level sidecar describes the corpus;
#: it must never outvote the file's own chapter/page provenance.
FILE_OWNED_KEYS = frozenset(
    {
        CATEGORY_FIELD,
        SOURCE_URI_FIELD,
        IMAGE_URI_FIELD,
        "source_document_id",
        "chapter_uri",
        "page",
    }
)

# Chinese corpora spell the same fields in their own language.  Aliasing here
# keeps one canonical filter surface instead of teaching every reader two.
# ``document_id`` is a reserved system column (the meta binding key), so the
# corpus's own document number is stored as ``source_document_id`` instead.
# The query side resolves through the *same* table, so a field an author wrote
# in Chinese is also a field an agent can filter on in Chinese.
AUTHORED_KEY_ALIASES: Dict[str, str] = {
    "document_id": "source_document_id",
    "source_document_id": "source_document_id",
    "documentid": "source_document_id",
    "文档id": "source_document_id",
    "文档编号": "source_document_id",
    "chapter_uri": "chapter_uri",
    "章节uri": "chapter_uri",
    "章节": "chapter_uri",
    "page": "page",
    "页码": "page",
    "页": "page",
    "machine_model": "machine_model",
    "机型": "machine_model",
    "型号": "machine_model",
    "keywords": "keywords",
    "关键词": "keywords",
    "source_uri": "source_uri",
    "溯源": "source_uri",
}

#: Back-compat spelling kept for the module's earlier internal name.
_KEY_ALIASES = AUTHORED_KEY_ALIASES


def canonical_field_name(field: str) -> str:
    """Resolve an authored or canonical field name to its canonical form.

    Names the corpus never authored pass through untouched, so a tenant's own
    business fields keep working while ``机型`` and ``machine_model`` stay one
    filter surface rather than two.
    """

    normalized = unicodedata.normalize("NFKC", str(field or "")).strip().casefold()
    return AUTHORED_KEY_ALIASES.get(normalized, normalized)


_BULLET_META = re.compile(
    r"^[ \t]*[-*+][ \t]+(?P<key>[^:：\n]{1,64}?)[ \t]*[:：][ \t]*(?P<value>\S[^\n]{0,511}?)\s*$"
)
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]")
_MAX_SCAN_LINES = 80


def _canonical_key(raw: str) -> Optional[str]:
    """Map an authored key onto its canonical field name, or ``None``.

    Only Markdown emphasis decoration is removed; an intra-word underscore is
    part of the key (``chapter_uri``), and lookup uses the same NFKC/casefold
    shape as the business-meta normalizer, so ``Page`` and ``ｐａｇｅ`` both land
    on ``page``.
    """

    stripped = re.sub(r"[*`]", "", raw).strip().casefold()
    return _KEY_ALIASES.get(stripped)


def extract_inline_metadata(text: str, *, max_lines: int = _MAX_SCAN_LINES) -> Dict[str, str]:
    """Read the leading ``- key: value`` provenance block of one chapter.

    Only the leading block counts: scanning stops at the first heading, prose
    paragraph, or bullet whose key is not a known provenance field, so an
    ordinary list inside the chapter never becomes filterable meta.
    """

    values: Dict[str, str] = {}
    if not isinstance(text, str) or not text.strip():
        return values
    for line in text.splitlines()[:max_lines]:
        if not line.strip():
            continue
        if _HEADING.match(line):
            continue
        match = _BULLET_META.match(line)
        if not match:
            break
        field = _canonical_key(match.group("key"))
        if field is None:
            break
        values.setdefault(field, match.group("value"))
    return values


def _merge_values(base: Mapping[str, Any], derived: Mapping[str, str]) -> Dict[str, Any]:
    """Let the file's own provenance win for the keys the file actually owns."""

    merged = dict(base)
    for key, value in derived.items():
        if key in FILE_OWNED_KEYS or merged.get(key) in (None, ""):
            merged[key] = value
    return merged


def _as_bundle(fields: Dict[str, Any], values: Mapping[str, str], source: str) -> Dict[str, Any]:
    existing = fields.get("_business_meta_input")
    if isinstance(existing, Mapping) and isinstance(existing.get("values"), Mapping):
        dependencies = list(existing.get("dependencies") or [])
        base_values = dict(existing["values"])
        fingerprint = existing.get("fingerprint") or ""
    else:
        dependencies = []
        base_values = {}
        fingerprint = ""
    merged = _merge_values(base_values, values)
    digest = hashlib.sha256(
        f"{fingerprint}|{source}|{sorted(merged.items())}".encode("utf-8")
    ).hexdigest()
    return {
        "values": merged,
        # The sidecar stays the identity of the corpus-level source; a derived
        # block is recorded as its own source so provenance is never laundered.
        "source": f"{existing['source']}+{source}" if base_values and existing else source,
        "dependencies": dependencies,
        "fingerprint": digest,
    }


def stamp_record_meta(
    fields: Dict[str, Any],
    *,
    category: str,
    contents: Iterable[Optional[str]] = (),
    source_uri: Optional[str] = None,
    image_uri: Optional[str] = None,
    values: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Attach the modality category and source provenance to one record base.

    ``contents`` are the texts the record was built from, in order; the first
    value seen for a key wins, which makes an image that repeats across chapters
    deterministic (its metadata comes from its first occurrence) without letting
    one occurrence's page shadow another's.

    ``values`` are provenance **already read from the source the record cites**
    by the caller that holds that source.  They come first, because an image row
    is built from a VLM summary plus a token-budgeted context window: the window
    is decorated with its own ``Source URI:``/``Chapter:`` header, and a chapter
    whose figure sits far from the heading keeps the provenance block outside the
    window entirely.  Re-parsing either of those to recover ``page`` would be a
    guess about the window's shape, so the document's own block is handed over
    instead.  Nothing is invented: a caller without provenance passes nothing.

    Returns the derived values so callers can assert on them in tests and logs.
    """

    if category not in {IMAGE_CATEGORY, TEXT_CATEGORY}:
        raise ValueError(f"unsupported vector record category: {category!r}")

    derived: Dict[str, str] = {CATEGORY_FIELD: category}
    for key, value in (values or {}).items():
        if isinstance(value, str) and value.strip():
            derived.setdefault(canonical_field_name(key), value.strip())
    for text in contents:
        for key, value in extract_inline_metadata(text or "").items():
            derived.setdefault(key, value)
    resolved_source = source_uri or fields.get("uri") or ""
    if resolved_source:
        derived[SOURCE_URI_FIELD] = resolved_source
    if image_uri:
        derived[IMAGE_URI_FIELD] = image_uri

    bundle_source = f"inline:{derived.get(SOURCE_URI_FIELD, fields.get('uri', ''))}"
    fields["_business_meta_input"] = _as_bundle(fields, derived, bundle_source)
    return derived


def meta_view(payload: Any) -> Dict[str, str]:
    """Read a stored business-meta payload back into ``field -> value``.

    The column is written for the token index, where one value is an entry with
    a raw and a canonical form.  A response should carry the authored text: the
    canonical form upper-cases values that a filter accepts either way, so
    echoing it would report the modality as ``IMAGES`` to a corpus that wrote
    ``images``.  An absent or malformed payload yields an empty view; a hit never
    claims metadata it cannot show.
    """

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return {}
    if not isinstance(payload, Mapping):
        return {}
    view: Dict[str, str] = {}
    for entry in payload.get("values") or []:
        if not isinstance(entry, Mapping):
            continue
        field = str(entry.get("field") or "").strip()
        value = str(entry.get("raw") or entry.get("canonical") or "").strip()
        if field and value:
            view.setdefault(field, value)
    return view


def provenance_view(row: Mapping[str, Any]) -> Dict[str, Any]:
    """The citable trail of one stored vector row, shaped for a retrieval hit.

    ``chapter_uri`` is the field an agent acts on: an image row's context source
    is the markdown that embeds it, so a figure hit arrives with the file to open
    next instead of a picture whose page is lost.  Every value is copied out of
    the row or its own stamped meta; nothing is inferred from a URI's shape.
    """

    from openviking.indexing.metadata_integration import PAYLOAD_COLUMN

    meta = meta_view(row.get(PAYLOAD_COLUMN))
    view: Dict[str, Any] = {}
    for key, value in (
        ("category", meta.get(CATEGORY_FIELD, "")),
        ("source_uri", meta.get(SOURCE_URI_FIELD, "") or str(row.get("uri") or "")),
        ("image_uri", meta.get(IMAGE_URI_FIELD, "")),
        ("chapter_uri", str(row.get("image_context_uri") or "") or meta.get(SOURCE_URI_FIELD, "")),
        ("content_revision", str(row.get("content_revision") or "")),
        ("chunk_id", str(row.get("chunk_id") or "")),
        ("parent_chunk_id", str(row.get("parent_chunk_id") or "")),
    ):
        if value:
            view[key] = value
    return view

