# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Authoritative business meta storage (KB-018 / spec 7.2).

Ingest ``meta`` is optional. For every accepted business key the **raw** value,
the **canonical** value, the **source** and the **normalizer version** are stored
separately and bound to the document revision. A rebuild can therefore prove
which normalizer produced which canonical form, and a rebuild that omits ``meta``
never clears previously stored values.

System fields (account/namespace/ACL/version/vector columns) can never be
overwritten by user supplied meta. Chunk, image and semantic records inherit the
authoritative values instead of re-deriving them, so a meta edit is visible in
the same index generation as the content edit that carries it.

Filters produced here are exact-ID expressions (``Eq`` / ``Or`` / ``And``). The
dictionary layer (``openviking.retrieve.meta_dictionary``) resolves fuzzy user
input to a finite canonical set first; we never ask a storage adapter to run a
regex over every vector row.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping

from openviking.storage.expr import And, Eq, FilterExpr, Or

# Bumped whenever the canonicalization rules below change. It is persisted with
# every value so a later rebuild can tell which rules produced a canonical form.
NORMALIZER_VERSION = "business-meta-v1"

# Missing-meta sentinel. The context collection declares string fields with an
# empty default, so an absent business field reads back as "".
MISSING_META_SENTINEL = ""

# Column prefixes used when a business field is projected onto an index record.
CANONICAL_PREFIX = "meta__"
RAW_PREFIX = "meta_raw__"
SOURCE_PREFIX = "meta_source__"
NORMALIZER_PREFIX = "meta_norm__"
FIELDS_COLUMN = "meta_fields"

# System columns that user meta must never shadow. Comparison is done on the
# NFKC/casefolded key so ``Account_ID`` is rejected too.
SYSTEM_FIELDS = frozenset(
    {
        "id",
        "uri",
        "type",
        "context_type",
        "vector",
        "sparse_vector",
        "created_at",
        "updated_at",
        "active_count",
        "level",
        "name",
        "description",
        "tags",
        "search_tags",
        "abstract",
        "content",
        "chunk_id",
        "parent_chunk_id",
        "chunk_ordinal",
        "chunk_continuation",
        "document_id",
        "content_revision",
        "embedding_space_id",
        "index_plan_id",
        "index_generation",
        "chunker_version",
        "chunk_text",
        "parent_text",
        "image_context_uri",
        "image_context_hash",
        "account_id",
        "namespace_id",
        "owner_user_id",
        "acl",
        "acl_mode",
        "acl_grants",
        "acl_owner",
        "acl_read",
        "acl_write",
        "acl_manage",
    }
)

# Any key with one of these prefixes is reserved for internal projections.
_RESERVED_KEY_PREFIXES = ("meta_", "acl_", "_", "system_", "business_meta_")


class MetaError(ValueError):
    """Base class for rejected business meta."""


class SystemFieldOverwriteError(MetaError):
    """Raised when user meta tries to shadow a system field."""


class MetaLimitError(MetaError):
    """Raised when a meta key or value exceeds the configured limits."""


class MetaBindingError(MetaError):
    """Raised when a record does not match the meta's document revision."""


@dataclass(frozen=True)
class MetaLimits:
    """Type/length limits applied to the *extra* user keys."""

    max_keys: int = 32
    max_key_length: int = 64
    max_value_length: int = 512
    allowed_types: tuple[type, ...] = (str, int, float, bool)


def normalize_key(key: object) -> str:
    """NFKC/casefolded key used for system-field comparison and storage."""
    if not isinstance(key, str):
        raise MetaError(f"meta key must be a string, got {type(key).__name__}")
    return unicodedata.normalize("NFKC", key).strip().casefold()


def normalize_business_value(field: str, raw: object) -> str:
    """Canonicalize a business value for a field.

    Codes and model identifiers are case-insensitive in practice, so the
    canonical form is NFKC-normalized (full-width to half-width), whitespace
    collapsed and upper-cased. ``raw`` is preserved verbatim alongside it.
    """
    if raw is None:
        return MISSING_META_SENTINEL
    text = unicodedata.normalize("NFKC", str(raw))
    text = " ".join(text.split())
    return text.upper()


@dataclass(frozen=True)
class MetaValue:
    """One business field: raw value, canonical value, source and rules version."""

    field: str
    raw: object
    canonical: str
    source: str
    normalizer_version: str

    def raw_text(self) -> str:
        return "" if self.raw is None else str(self.raw)

    def to_columns(self) -> dict[str, Any]:
        return {
            CANONICAL_PREFIX + self.field: self.canonical,
            RAW_PREFIX + self.field: self.raw_text(),
            SOURCE_PREFIX + self.field: self.source,
            NORMALIZER_PREFIX + self.field: self.normalizer_version,
        }


@dataclass
class AuthoritativeMeta:
    """Authoritative meta bound to one ``(document, revision, generation)``."""

    document_id: str
    content_revision: str
    source: str
    normalizer_version: str
    index_generation: str | None = None
    values: dict[str, MetaValue] = field(default_factory=dict)

    def fields(self) -> tuple[str, ...]:
        return tuple(sorted(self.values))

    def canonical(self, field_name: str) -> str:
        value = self.values.get(normalize_key(field_name))
        return value.canonical if value is not None else MISSING_META_SENTINEL

    def raw(self, field_name: str) -> object:
        value = self.values.get(normalize_key(field_name))
        return None if value is None else value.raw

    def provenance(self, field_name: str) -> tuple[str, str] | None:
        value = self.values.get(normalize_key(field_name))
        if value is None:
            return None
        return value.source, value.normalizer_version

    def is_bound_to(self, document_id: str, content_revision: str) -> bool:
        return self.document_id == document_id and self.content_revision == content_revision

    def fingerprint(self) -> str:
        payload = [
            self.document_id,
            self.content_revision,
            self.index_generation,
            self.normalizer_version,
            [
                [f, v.raw_text(), v.canonical, v.source, v.normalizer_version]
                for f, v in sorted(self.values.items())
            ],
        ]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()

    def to_columns(self) -> dict[str, Any]:
        """Projection for chunk/image/semantic index records."""
        columns: dict[str, Any] = {
            "document_id": self.document_id,
            "content_revision": self.content_revision,
            FIELDS_COLUMN: list(self.fields()),
        }
        if self.index_generation is not None:
            columns["index_generation"] = self.index_generation
        for value in self.values.values():
            columns.update(value.to_columns())
        return columns

    def to_payload(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "content_revision": self.content_revision,
            "index_generation": self.index_generation,
            "source": self.source,
            "normalizer_version": self.normalizer_version,
            "values": [
                {
                    "field": v.field,
                    "raw": v.raw,
                    "canonical": v.canonical,
                    "source": v.source,
                    "normalizer_version": v.normalizer_version,
                }
                for v in self.values.values()
            ],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AuthoritativeMeta":
        meta = cls(
            document_id=payload["document_id"],
            content_revision=payload["content_revision"],
            source=payload.get("source", "ingest"),
            normalizer_version=payload.get("normalizer_version", NORMALIZER_VERSION),
            index_generation=payload.get("index_generation"),
        )
        for entry in payload.get("values", []):
            meta.values[entry["field"]] = MetaValue(
                field=entry["field"],
                raw=entry.get("raw", ""),
                canonical=entry["canonical"],
                source=entry.get("source", meta.source),
                normalizer_version=entry.get("normalizer_version", meta.normalizer_version),
            )
        return meta


def validate_meta_key(key: object, limits: MetaLimits) -> str:
    """Return the normalized key, or raise if it is reserved / oversized."""
    normalized = normalize_key(key)
    if not normalized:
        raise MetaError("meta key must not be empty")
    if normalized in SYSTEM_FIELDS:
        raise SystemFieldOverwriteError(
            f"meta key {key!r} would overwrite the system field {normalized!r}"
        )
    if normalized.startswith(_RESERVED_KEY_PREFIXES):
        raise SystemFieldOverwriteError(f"meta key {key!r} uses a reserved prefix")
    if len(normalized) > limits.max_key_length:
        raise MetaLimitError(
            f"meta key {key!r} exceeds the {limits.max_key_length} character limit"
        )
    return normalized


def validate_meta_value(key: str, raw: object, limits: MetaLimits) -> None:
    if raw is None:
        raise MetaLimitError(f"meta value for {key!r} must not be null")
    if isinstance(raw, bool) or not isinstance(raw, limits.allowed_types):
        raise MetaLimitError(
            f"meta value for {key!r} has unsupported type {type(raw).__name__}"
        )
    if isinstance(raw, float) and not math.isfinite(raw):
        raise MetaLimitError(f"meta value for {key!r} must be finite")
    if isinstance(raw, str) and len(raw) > limits.max_value_length:
        raise MetaLimitError(
            f"meta value for {key!r} exceeds the {limits.max_value_length} character limit"
        )


def build_meta(
    raw_meta: Mapping[str, object] | None,
    *,
    document_id: str,
    content_revision: str,
    source: str = "ingest",
    index_generation: str | None = None,
    limits: MetaLimits | None = None,
) -> AuthoritativeMeta:
    """Parse user supplied meta into an authoritative, revision-bound record.

    ``raw_meta=None`` (or an empty mapping) is valid and yields an empty record;
    the caller decides whether that means "no meta" or "keep the previous meta"
    (see :func:`merge_meta`).
    """
    limits = limits or MetaLimits()
    meta = AuthoritativeMeta(
        document_id=document_id,
        content_revision=content_revision,
        source=source,
        normalizer_version=NORMALIZER_VERSION,
        index_generation=index_generation,
    )
    if not raw_meta:
        return meta
    if len(raw_meta) > limits.max_keys:
        raise MetaLimitError(
            f"meta carries {len(raw_meta)} keys, above the {limits.max_keys} key limit"
        )
    for key, raw in raw_meta.items():
        normalized = validate_meta_key(key, limits)
        validate_meta_value(normalized, raw, limits)
        if normalized in meta.values:
            raise MetaError(f"meta key {key!r} is duplicated after normalization")
        meta.values[normalized] = MetaValue(
            field=normalized,
            raw=raw,
            canonical=normalize_business_value(normalized, raw),
            source=source,
            normalizer_version=NORMALIZER_VERSION,
        )
    return meta


def merge_meta(
    existing: AuthoritativeMeta | None,
    raw_meta: Mapping[str, object] | None,
    *,
    document_id: str,
    content_revision: str,
    source: str = "ingest",
    index_generation: str | None = None,
    limits: MetaLimits | None = None,
) -> AuthoritativeMeta:
    """Rebuild helper: never clear previously stored meta.

    ``raw_meta=None`` keeps every existing value (only re-bound to the new
    revision/generation). A non-empty mapping overwrites the keys it names and
    preserves every other key, so a rebuild that supplies only part of the meta
    cannot silently drop the rest.
    """
    limits = limits or MetaLimits()
    if raw_meta is None:
        return AuthoritativeMeta(
            document_id=document_id,
            content_revision=content_revision,
            source=source,
            normalizer_version=NORMALIZER_VERSION,
            index_generation=index_generation,
            values=dict(existing.values) if existing is not None else {},
        )
    incoming = build_meta(
        raw_meta,
        document_id=document_id,
        content_revision=content_revision,
        source=source,
        index_generation=index_generation,
        limits=limits,
    )
    values = dict(existing.values) if existing is not None else {}
    values.update(incoming.values)
    return AuthoritativeMeta(
        document_id=document_id,
        content_revision=content_revision,
        source=source,
        normalizer_version=NORMALIZER_VERSION,
        index_generation=index_generation,
        values=values,
    )


def attach_meta(
    record: Mapping[str, Any],
    meta: AuthoritativeMeta,
    *,
    strict: bool = True,
) -> dict[str, Any]:
    """Return ``record`` with the authoritative meta projected onto it.

    Chunk, image and semantic records carry the document identity; when
    ``strict`` is set a mismatching document/revision is rejected rather than
    letting a record inherit another revision's meta.
    """
    result = dict(record)
    document_id = result.get("document_id")
    content_revision = result.get("content_revision")
    if strict and document_id is not None and document_id != meta.document_id:
        raise MetaBindingError(
            f"record document {document_id!r} does not match meta {meta.document_id!r}"
        )
    if strict and content_revision is not None and content_revision != meta.content_revision:
        raise MetaBindingError(
            f"record revision {content_revision!r} does not match meta {meta.content_revision!r}"
        )
    for key, value in meta.to_columns().items():
        if key in result and result[key] != value:
            raise MetaBindingError(f"record carries conflicting authoritative meta field {key!r}")
        result[key] = value
    result["meta_fingerprint"] = meta.fingerprint()
    return result


class MetaStore:
    """In-memory authoritative store keyed by ``(document, generation)``.

    This is the reference implementation of the contract: it keeps every value
    bound to a revision and exposes the exact-ID filter used by recall paths.
    A persistent backend would mirror the same methods.
    """

    def __init__(self) -> None:
        self._by_document: dict[str, dict[str | None, AuthoritativeMeta]] = {}

    def get(self, document_id: str, generation: str | None = None) -> AuthoritativeMeta | None:
        return self._by_document.get(document_id, {}).get(generation)

    def latest(self, document_id: str) -> AuthoritativeMeta | None:
        generations = self._by_document.get(document_id)
        if not generations:
            return None
        return next(reversed(generations.values()))

    def record(
        self,
        raw_meta: Mapping[str, object] | None,
        *,
        document_id: str,
        content_revision: str,
        source: str = "ingest",
        index_generation: str | None = None,
        limits: MetaLimits | None = None,
    ) -> AuthoritativeMeta:
        """Ingest or rebuild a document's meta without clearing omitted keys."""
        existing = self.get(document_id, index_generation)
        if existing is None and index_generation is not None:
            existing = self.latest(document_id)
        meta = merge_meta(
            existing,
            raw_meta,
            document_id=document_id,
            content_revision=content_revision,
            source=source,
            index_generation=index_generation,
            limits=limits,
        )
        self._by_document.setdefault(document_id, {})[index_generation] = meta
        return meta

    def documents(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_document))


def canonical_filter(
    field_name: str,
    values: list[str] | tuple[str, ...],
    *,
    include_unknown: bool = False,
) -> FilterExpr:
    """Exact-ID filter for one field.

    ``values`` must already be canonical IDs from the dictionary layer; this
    function never widens them. With ``include_unknown`` the empty sentinel is
    OR-ed in so documents that never declared the field still participate, and
    the caller is expected to mark those hits.
    """
    key = normalize_key(field_name)
    column = CANONICAL_PREFIX + key
    candidates = [normalize_business_value(key, value) for value in values]
    conds: list[FilterExpr] = [Eq(column, value) for value in candidates]
    if include_unknown:
        conds.append(Eq(column, MISSING_META_SENTINEL))
    if not conds:
        # No candidate at all: an unsatisfiable constraint, never a wildcard.
        return Eq(column, "\x00__no_candidate__")
    if len(conds) == 1:
        return conds[0]
    return Or(conds)


def combined_filter(
    field_values: Mapping[str, list[str] | tuple[str, ...]],
    *,
    include_unknown: bool = False,
) -> FilterExpr | None:
    """Same-field candidates OR, cross-field AND (spec 7.2).

    Returns ``None`` when no field carries a candidate; the caller then falls
    back to unfiltered recall rather than to an implicit constraint.
    """
    parts: list[FilterExpr] = []
    for field_name, values in field_values.items():
        if not values:
            continue
        parts.append(canonical_filter(field_name, values, include_unknown=include_unknown))
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return And(parts)


__all__ = [
    "AuthoritativeMeta",
    "CANONICAL_PREFIX",
    "FIELDS_COLUMN",
    "MISSING_META_SENTINEL",
    "MetaBindingError",
    "MetaError",
    "MetaLimitError",
    "MetaLimits",
    "MetaStore",
    "MetaValue",
    "NORMALIZER_VERSION",
    "RAW_PREFIX",
    "SOURCE_PREFIX",
    "NORMALIZER_PREFIX",
    "SYSTEM_FIELDS",
    "SystemFieldOverwriteError",
    "attach_meta",
    "build_meta",
    "canonical_filter",
    "combined_filter",
    "merge_meta",
    "normalize_business_value",
    "normalize_key",
    "validate_meta_key",
    "validate_meta_value",
]
