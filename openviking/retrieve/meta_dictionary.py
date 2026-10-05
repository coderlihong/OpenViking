# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Fuzzy business-value dictionary and meta query resolution (KB-019 / spec 7.2).

The dictionary indexes **business values**, never vector rows. A query is parsed
into a finite set of canonical IDs, and each recall path (BM25 / dense / image)
is then filtered by exact ID equality through
:mod:`openviking.indexing.business_meta`. No adapter is asked to run a regex over
every row, and :class:`Contains` adapter differences are irrelevant because the
dictionary already reduced the problem to ``Eq`` / ``Or`` / ``And``.

Two kinds of query input are kept strictly separate:

``meta_filters``
    An explicit user constraint. ``exact`` / ``prefix`` / ``contains`` /
    ``fuzzy`` are allowed, same-field candidates OR, cross-field AND. A field
    whose constraint resolves to no candidate yields an **empty** result (or an
    ambiguity note); a hard constraint is never silently dropped.

``meta_hints``
    Inferred from the query text. By default they only boost. Only a
    high-confidence unique mapping may *tighten* the candidate set, and only when
    the caller opted in via ``tighten_hints``; a low-confidence hint always
    returns its candidates plus an explanation.

Different fields get different fuzzy rules: model expressions may be fuzzed, but
subject codes / BOM / DTC are strict, because a single-character difference
there can mean a **different configuration**. ACL, namespace and the
applicability three-state are never fuzzy-interpreted at all.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from openviking.indexing.business_meta import (
    CANONICAL_PREFIX,
    MISSING_META_SENTINEL,
    normalize_business_value,
    normalize_key,
)
from openviking.storage.expr import And, Eq, FilterExpr, Or

EXACT = "exact"
PREFIX = "prefix"
CONTAINS = "contains"
FUZZY = "fuzzy"
OPS = (EXACT, PREFIX, CONTAINS, FUZZY)

HIGH = "high"
MEDIUM = "medium"
LOW = "low"

# Fields that must never be fuzzy-interpreted. ACL, namespace and applicability
# are authoritative three-state/identity inputs, not business descriptions.
NON_FUZZY_FIELDS = frozenset(
    {
        "account_id",
        "namespace_id",
        "owner_user_id",
        "acl",
        "acl_mode",
        "acl_grants",
        "applicability",
        "context_type",
        "level",
    }
)

# A model expression is a short letter run, a short digit run and an optional
# dash-suffixed variant (``SY215-C``). The trailing word boundary keeps longer
# alphanumeric identifiers such as subject codes (``SY021NCB62788``) out.
_MODEL_TOKEN = re.compile(
    r"(?<![A-Za-z0-9-])[A-Za-z]{1,4}\d{1,3}(?:-?[A-Za-z]{1,4})?(?![A-Za-z0-9-])"
)


class MetaDictionaryError(ValueError):
    """Base class for dictionary/query errors."""


class MetaOpNotAllowedError(MetaDictionaryError):
    """Raised when a requested operator is forbidden for the field."""


class ProtectedFieldFuzzyError(MetaDictionaryError):
    """Raised when a non-fuzzy field (ACL/namespace/applicability) is fuzzed."""


@dataclass(frozen=True)
class FuzzyRule:
    """Per-field fuzzy policy."""

    field: str
    exact: bool = True
    prefix: bool = True
    contains: bool = True
    fuzzy: bool = True
    max_edit_distance: int = 1
    min_fuzzy_length: int = 5

    def allows(self, op: str) -> bool:
        if op == EXACT:
            return self.exact
        if op == PREFIX:
            return self.prefix
        if op == CONTAINS:
            return self.contains
        if op == FUZZY:
            return self.fuzzy
        raise MetaOpNotAllowedError(f"unknown meta operator {op!r}")

    def effective_distance(self, value: str) -> int:
        """Short queries are too small to fuzz safely; return 0 to force exact."""
        if len(value) < self.min_fuzzy_length:
            return 0
        return self.max_edit_distance


STRICT_RULE = FuzzyRule(
    field="*strict*", exact=True, prefix=False, contains=False, fuzzy=False
)
MODEL_RULE = FuzzyRule(
    field="*model*", exact=True, prefix=True, contains=True, fuzzy=True,
    max_edit_distance=2, min_fuzzy_length=4,
)
GENERIC_RULE = FuzzyRule(
    field="*", exact=True, prefix=True, contains=True, fuzzy=True,
    max_edit_distance=1, min_fuzzy_length=5,
)

# A one-character difference in these fields can mean a different configuration.
STRICT_FIELDS = frozenset({"subject_code", "bom", "dtc", "config_spec", "profile_id"})
MODEL_FIELDS = frozenset({"modelid", "model", "model_id", "machine_model"})

DEFAULT_RULES: dict[str, FuzzyRule] = {}


def rule_for(field_name: str, rules: Mapping[str, FuzzyRule] | None = None) -> FuzzyRule:
    key = normalize_key(field_name)
    if rules and key in rules:
        return rules[key]
    if key in NON_FUZZY_FIELDS:
        return STRICT_RULE
    if key in STRICT_FIELDS:
        return STRICT_RULE
    if key in MODEL_FIELDS:
        return MODEL_RULE
    return GENERIC_RULE


def _trigrams(value: str) -> set[str]:
    if len(value) < 3:
        return {value} if value else set()
    return {value[i : i + 3] for i in range(len(value) - 2)}


def bounded_edit_distance(left: str, right: str, max_distance: int) -> int:
    """Levenshtein distance capped at ``max_distance`` (returns max+1 if above)."""
    if max_distance < 0:
        return len(left) + len(right)
    if abs(len(left) - len(right)) > max_distance:
        return max_distance + 1
    if left == right:
        return 0
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, 1):
        current = [i]
        best = i
        for j, right_char in enumerate(right, 1):
            cost = 0 if left_char == right_char else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            current.append(value)
            best = min(best, value)
        if best > max_distance:
            return max_distance + 1
        previous = current
    return previous[-1]


@dataclass(frozen=True)
class MetaFilter:
    """An explicit user constraint on one business field."""

    field: str
    values: tuple[str, ...]
    op: str = EXACT
    include_unknown: bool = False

    def __post_init__(self) -> None:
        if self.op not in OPS:
            raise MetaOpNotAllowedError(f"unknown meta operator {self.op!r}")
        if not self.values:
            raise MetaDictionaryError(f"meta filter for {self.field!r} has no values")


@dataclass(frozen=True)
class MetaHint:
    """A query-inferred suggestion for one business field (boost by default)."""

    field: str
    value: str
    op: str = FUZZY

    def __post_init__(self) -> None:
        if self.op not in OPS:
            raise MetaOpNotAllowedError(f"unknown meta operator {self.op!r}")


@dataclass(frozen=True)
class ResolvedValue:
    """Outcome of resolving one filter/hint value against the dictionary."""

    canonical: tuple[str, ...]
    matched_via: str
    scan_volume: int = 0


@dataclass
class QueryResolution:
    """The exact-ID filter plus boosts, ambiguity and explanations."""

    hard_filter: FilterExpr | None = None
    hard_fields: dict[str, tuple[str, ...]] = field(default_factory=dict)
    boosts: dict[str, tuple[str, ...]] = field(default_factory=dict)
    tightened: dict[str, tuple[str, ...]] = field(default_factory=dict)
    include_unknown_fields: tuple[str, ...] = ()
    unsatisfied: tuple[str, ...] = ()
    ambiguous: bool = False
    explanations: tuple[str, ...] = ()
    scan_volume: int = 0

    def is_empty(self) -> bool:
        """True when a hard constraint cannot be satisfied (never relaxed)."""
        return bool(self.unsatisfied)


class BusinessDictionary:
    """Per-namespace business-value dictionary with an inverted index."""

    def __init__(
        self,
        namespace_id: str,
        rules: Mapping[str, FuzzyRule] | None = None,
    ) -> None:
        self.namespace_id = namespace_id
        self._rules = dict(rules or {})
        self._canonical: dict[str, set[str]] = {}
        self._alias: dict[str, dict[str, str]] = {}
        self._strings: dict[str, dict[str, set[str]]] = {}
        self._prefix: dict[str, list[tuple[str, str]]] = {}
        self._trigram: dict[str, dict[str, set[str]]] = {}
        self._documents: set[str] = set()
        self._dirty: set[str] = set()

    # -- population --------------------------------------------------------
    def add_value(self, field_name: str, canonical: str, aliases: Iterable[str] = ()) -> str:
        """Register a canonical business value plus optional human/business aliases."""
        key = normalize_key(field_name)
        canonical_value = normalize_business_value(key, canonical)
        if not canonical_value:
            raise MetaDictionaryError(f"empty canonical value for {field_name!r}")
        self._canonical.setdefault(key, set()).add(canonical_value)
        self._strings.setdefault(key, {}).setdefault(canonical_value, set()).add(
            canonical_value
        )
        alias_map = self._alias.setdefault(key, {})
        for alias in aliases:
            alias_value = normalize_business_value(key, alias)
            if not alias_value:
                continue
            existing = alias_map.get(alias_value)
            if existing is not None and existing != canonical_value:
                raise MetaDictionaryError(
                    f"alias {alias!r} maps to both {existing!r} and {canonical_value!r}"
                )
            alias_map[alias_value] = canonical_value
            self._strings[key][canonical_value].add(alias_value)
        self._dirty.add(key)
        return canonical_value

    def add_meta(self, meta) -> None:
        """Seed the dictionary from an authoritative meta record."""
        if getattr(meta, "document_id", None):
            self._documents.add(meta.document_id)
        for value in meta.values.values():
            if value.canonical:
                self.add_value(value.field, value.canonical)

    def rebuild_indexes(self) -> None:
        for key in list(self._dirty):
            entries: list[tuple[str, str]] = []
            postings: dict[str, set[str]] = {}
            for canonical_value, strings in self._strings.get(key, {}).items():
                for text in strings:
                    entries.append((text, canonical_value))
                    for trigram in _trigrams(text):
                        postings.setdefault(trigram, set()).add(canonical_value)
            entries.sort()
            self._prefix[key] = entries
            self._trigram[key] = postings
            self._dirty.discard(key)

    def _ensure(self, key: str) -> None:
        if key in self._dirty:
            self.rebuild_indexes()

    # -- introspection -----------------------------------------------------
    def cardinality(self) -> dict[str, int]:
        return {key: len(values) for key, values in sorted(self._canonical.items())}

    def total_values(self) -> int:
        return sum(len(values) for values in self._canonical.values())

    def fields(self) -> tuple[str, ...]:
        return tuple(sorted(self._canonical))

    def known_documents(self) -> tuple[str, ...]:
        return tuple(sorted(self._documents))

    def rule(self, field_name: str) -> FuzzyRule:
        return rule_for(field_name, self._rules)

    # -- lookups (all bounded by dictionary cardinality) -------------------
    def lookup_exact(self, field_name: str, value: str) -> ResolvedValue:
        key = normalize_key(field_name)
        self._ensure(key)
        probe = normalize_business_value(key, value)
        hits: set[str] = set()
        if probe in self._canonical.get(key, set()):
            hits.add(probe)
        alias_hit = self._alias.get(key, {}).get(probe)
        if alias_hit is not None:
            hits.add(alias_hit)
        via = EXACT if hits else "none"
        return ResolvedValue(tuple(sorted(hits)), via, scan_volume=1)

    def lookup_prefix(self, field_name: str, value: str) -> ResolvedValue:
        key = normalize_key(field_name)
        self._ensure(key)
        probe = normalize_business_value(key, value)
        entries = self._prefix.get(key, [])
        if not entries or not probe:
            return ResolvedValue((), "none", scan_volume=0)
        low = bisect_left(entries, (probe, ""))
        high = bisect_right(entries, (probe + "\uffff", ""))
        hits = {canonical for _, canonical in entries[low:high]}
        return ResolvedValue(tuple(sorted(hits)), PREFIX, scan_volume=high - low)

    def lookup_contains(self, field_name: str, value: str) -> ResolvedValue:
        key = normalize_key(field_name)
        self._ensure(key)
        probe = normalize_business_value(key, value)
        if not probe:
            return ResolvedValue((), "none", scan_volume=0)
        candidates, scanned = self._candidate_set(key, probe)
        hits: set[str] = set()
        for canonical_value, strings in self._strings.get(key, {}).items():
            if canonical_value not in candidates:
                continue
            for text in strings:
                scanned += 1
                if probe in text:
                    hits.add(canonical_value)
                    break
        return ResolvedValue(tuple(sorted(hits)), CONTAINS, scan_volume=scanned)

    def lookup_fuzzy(self, field_name: str, value: str) -> ResolvedValue:
        key = normalize_key(field_name)
        self._ensure(key)
        rule = rule_for(key, self._rules)
        probe = normalize_business_value(key, value)
        distance = rule.effective_distance(probe)
        if not probe:
            return ResolvedValue((), "none", scan_volume=0)
        if distance <= 0:
            resolved = self.lookup_exact(key, value)
            return ResolvedValue(resolved.canonical, "exact", resolved.scan_volume)
        candidates, scanned = self._candidate_set(key, probe, union=True)
        hits: set[str] = set()
        for canonical_value, strings in self._strings.get(key, {}).items():
            if canonical_value not in candidates:
                continue
            for text in strings:
                scanned += 1
                if bounded_edit_distance(probe, text, distance) <= distance:
                    hits.add(canonical_value)
                    break
        return ResolvedValue(tuple(sorted(hits)), FUZZY, scan_volume=scanned)

    def lookup(self, field_name: str, value: str, op: str = EXACT) -> ResolvedValue:
        key = normalize_key(field_name)
        if key in NON_FUZZY_FIELDS and op == FUZZY:
            raise ProtectedFieldFuzzyError(
                f"{field_name!r} is not fuzzy-interpreted (ACL/namespace/applicability)"
            )
        rule = rule_for(key, self._rules)
        if not rule.allows(op):
            raise MetaOpNotAllowedError(
                f"operator {op!r} is not allowed for field {field_name!r}; "
                "single-character differences may denote a different configuration"
            )
        if op == EXACT:
            return self.lookup_exact(key, value)
        if op == PREFIX:
            return self.lookup_prefix(key, value)
        if op == CONTAINS:
            return self.lookup_contains(key, value)
        return self.lookup_fuzzy(key, value)

    def _candidate_set(
        self, key: str, probe: str, *, union: bool = False
    ) -> tuple[set[str], int]:
        """Trigram candidates; fall back to a bounded scan for very short probes."""
        postings = self._trigram.get(key, {})
        grams = _trigrams(probe)
        if len(probe) < 3 or not grams:
            return set(self._canonical.get(key, set())), 0
        scanned = 0
        result: set[str] | None = None
        for trigram in grams:
            bucket = postings.get(trigram, set())
            scanned += len(bucket)
            if result is None:
                result = set(bucket)
            elif union:
                result |= bucket
            else:
                result &= bucket
        if not result:
            return set(), scanned
        return result, scanned

    # -- performance reporting (KB-019 / TC-7.7 hook) ----------------------
    def report(
        self,
        *,
        documents: int | None = None,
        chunks: int | None = None,
        candidates: int = 0,
        matched: int = 0,
        scan_volume: int = 0,
        accuracy: float | None = None,
    ) -> "PerformanceReport":
        cardinality = self.cardinality()
        inflation = float(candidates) / matched if matched else 0.0
        return PerformanceReport(
            namespace_id=self.namespace_id,
            dictionary_cardinality=cardinality,
            total_values=self.total_values(),
            documents=documents if documents is not None else len(self._documents),
            chunks=chunks or 0,
            candidates=candidates,
            matched=matched,
            candidate_inflation=inflation,
            scan_volume=scan_volume,
            accuracy=accuracy,
        )


@dataclass(frozen=True)
class PerformanceReport:
    """Measurable dictionary/recall numbers; no benchmark is implied here."""

    namespace_id: str
    dictionary_cardinality: dict[str, int]
    total_values: int
    documents: int
    chunks: int
    candidates: int
    matched: int
    candidate_inflation: float
    scan_volume: int
    accuracy: float | None = None

    def to_dict(self) -> dict:
        return {
            "namespace_id": self.namespace_id,
            "dictionary_cardinality": dict(self.dictionary_cardinality),
            "total_values": self.total_values,
            "documents": self.documents,
            "chunks": self.chunks,
            "candidates": self.candidates,
            "matched": self.matched,
            "candidate_inflation": self.candidate_inflation,
            "scan_volume": self.scan_volume,
            "accuracy": self.accuracy,
        }


def parse_model_expressions(query: str) -> tuple[str, ...]:
    """Extract model-like tokens from free query text.

    Only model-shaped tokens (letters followed by digits, e.g. ``SY215-C``) are
    returned; this is deliberately conservative so subject codes and DTCs are
    not mistaken for model expressions.
    """
    return tuple(sorted({match.group().upper() for match in _MODEL_TOKEN.finditer(query or "")}))


def _resolve_one(
    dictionary: BusinessDictionary,
    field_name: str,
    value: str,
    op: str,
) -> ResolvedValue:
    return dictionary.lookup(field_name, value, op)


def resolve_query(
    dictionary: BusinessDictionary,
    *,
    meta_filters: Sequence[MetaFilter] = (),
    meta_hints: Sequence[MetaHint] = (),
    tighten_hints: bool = False,
    confidence_threshold: float = 0.85,
) -> QueryResolution:
    """Turn explicit filters and inferred hints into an exact-ID query.

    Hard constraints (``meta_filters``) are resolved to canonical IDs. A field
    whose values all resolve to nothing is recorded in ``unsatisfied`` and the
    resolution becomes empty; it is never widened or dropped.

    Hints (``meta_hints``) are resolved to canonical candidates. A hint may only
    tighten the candidate set when ``tighten_hints`` is enabled **and** the
    mapping is unique with confidence at or above ``confidence_threshold``;
    otherwise it is a boost and the candidates plus an explanation are returned.
    """
    resolution = QueryResolution()
    hard_fields: dict[str, list[str]] = {}
    scan_total = 0

    for meta_filter in meta_filters:
        key = normalize_key(meta_filter.field)
        if key in NON_FUZZY_FIELDS and meta_filter.op == FUZZY:
            raise ProtectedFieldFuzzyError(
                f"{meta_filter.field!r} is not fuzzy-interpreted"
            )
        resolved: set[str] = set()
        for value in meta_filter.values:
            outcome = _resolve_one(dictionary, key, value, meta_filter.op)
            scan_total += outcome.scan_volume
            if not outcome.canonical:
                resolution.explanations = resolution.explanations + (
                    f"no canonical value for {key}={value!r} via {meta_filter.op}",
                )
                continue
            resolved.update(outcome.canonical)
        if meta_filter.include_unknown:
            resolution.include_unknown_fields = resolution.include_unknown_fields + (key,)
        hard_fields.setdefault(key, [])
        hard_fields[key].extend(sorted(resolved))

    for key, values in hard_fields.items():
        if not values and key not in resolution.include_unknown_fields:
            resolution.unsatisfied += tuple(f"{key}={value}" for item in meta_filters
                                             if normalize_key(item.field) == key for value in item.values)

    if resolution.unsatisfied:
        # A hard constraint with no candidate yields an empty result. Do not
        # build a filter that would silently relax it.
        resolution.hard_fields = {}
        resolution.scan_volume = scan_total
        resolution.ambiguous = False
        return resolution

    for key, values in hard_fields.items():
        deduped = tuple(sorted(set(values)))
        resolution.hard_fields[key] = deduped
    resolution.hard_filter = _build_filter(
        resolution.hard_fields, resolution.include_unknown_fields
    )

    for hint in meta_hints:
        key = normalize_key(hint.field)
        if key in NON_FUZZY_FIELDS:
            raise ProtectedFieldFuzzyError(f"{hint.field!r} is not fuzzy-interpreted")
        outcome = _resolve_one(dictionary, key, hint.value, hint.op)
        scan_total += outcome.scan_volume
        if not outcome.canonical:
            resolution.explanations = resolution.explanations + (
                f"hint {key}={hint.value!r} matched no dictionary value; ignored",
            )
            continue
        confidence = 1.0 / len(outcome.canonical)
        unique = len(outcome.canonical) == 1
        if tighten_hints and unique and confidence >= confidence_threshold:
            resolution.tightened[key] = outcome.canonical
            resolution.explanations = resolution.explanations + (
                f"hint {key}={hint.value!r} uniquely mapped to "
                f"{outcome.canonical[0]!r}; candidates tightened",
            )
            continue
        resolution.boosts[key] = tuple(
            sorted(set(resolution.boosts.get(key, ())) | set(outcome.canonical))
        )
        if unique:
            resolution.explanations = resolution.explanations + (
                f"hint {key}={hint.value!r} -> {outcome.canonical[0]!r} (boost only)",
            )
        else:
            resolution.ambiguous = True
            resolution.explanations = resolution.explanations + (
                f"hint {key}={hint.value!r} is ambiguous across "
                f"{len(outcome.canonical)} candidates; boost only",
            )

    resolution.scan_volume = scan_total
    return resolution


def _build_filter(
    hard_fields: Mapping[str, Sequence[str]],
    include_unknown_fields: Sequence[str],
) -> FilterExpr | None:
    parts: list[FilterExpr] = []
    for key, values in hard_fields.items():
        column = CANONICAL_PREFIX + key
        conds: list[FilterExpr] = [Eq(column, value) for value in values]
        if key in include_unknown_fields:
            conds.append(Eq(column, MISSING_META_SENTINEL))
        parts.append(conds[0] if len(conds) == 1 else Or(conds))
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return And(parts)


def resolve_model_query(
    dictionary: BusinessDictionary,
    query: str,
    *,
    field: str = "modelid",
    tighten_hints: bool = False,
) -> QueryResolution:
    """Convenience wrapper: parse model expressions from a query as hints."""
    hints = [
        MetaHint(field=field, value=token, op=FUZZY)
        for token in parse_model_expressions(query)
    ]
    return resolve_query(dictionary, meta_hints=hints, tighten_hints=tighten_hints)


__all__ = [
    "BusinessDictionary",
    "CONTAINS",
    "DEFAULT_RULES",
    "EXACT",
    "FUZZY",
    "FuzzyRule",
    "GENERIC_RULE",
    "HIGH",
    "LOW",
    "MEDIUM",
    "MODEL_FIELDS",
    "MODEL_RULE",
    "MetaDictionaryError",
    "MetaFilter",
    "MetaHint",
    "MetaOpNotAllowedError",
    "NON_FUZZY_FIELDS",
    "OPS",
    "PREFIX",
    "PerformanceReport",
    "ProtectedFieldFuzzyError",
    "QueryResolution",
    "ResolvedValue",
    "STRICT_FIELDS",
    "STRICT_RULE",
    "bounded_edit_distance",
    "parse_model_expressions",
    "resolve_model_query",
    "resolve_query",
    "rule_for",
]
