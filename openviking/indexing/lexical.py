# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Real BM25 lexical index over child text and image summaries (KB-016, F6).

This is a retrieval *adapter component*, not a second user-facing search API.
It is the lexical recall path that the unified retrieval adapter calls alongside
dense recall; RRF fusion lives in the retrieval layer (KB-017).

Design points required by the enterprise KB spec (section 7.1):

- Index and query both pass through one version-locked Chinese tokenizer that
  preserves model numbers (``SY215-C``) and DTC codes (``P0087``).
- The inverted index is built from child text *and* image summaries, and is
  organised by ``namespace`` / ``document_id`` / ``index_generation``.
- Query snapshot mapping and authorization are applied *before* candidate
  ranking, never by over-fetching then discarding.
- Only derived content is stored; the index participates in generation-based
  publication by exposing a deterministic receipt child id that the caller can
  include in ``PublicationStore`` child sets.
- FTS5 ``bm25()`` returns lower-is-better negative scores; this adapter layer
  normalizes the direction so that higher is better. The raw score is kept for
  audit. Fusion (RRF) consumes the normalized rank, not the raw score.
- Sparse embedding is *not* a substitute for BM25 and is not used here.

First-version provider selection is fixed by the spec: local backends use
``local_fts5``; ``native`` is only permitted for deployments that already pass
the contract tests and is rejected here.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

# Locked tokenizer rule version. Any change to segmentation or to the
# model-number/DTC preservation rules must bump this string, because the
# inverted index is only comparable between documents indexed with the same
# version.
TOKENIZER_VERSION = "zh-lexical-v2"

# Inverted-index layout version (tables, triggers, tokenizer wiring).
INDEX_VERSION = "lexical-fts5-v1"

# Portable fallback layout version, used only when SQLite lacks FTS5.
FALLBACK_INDEX_VERSION = "lexical-portable-bm25-v1"

# BM25 constants for the portable fallback (FTS5 uses its own defaults).
_BM25_K1 = 1.2
_BM25_B = 0.75

MODEL_NUMBER_RE = re.compile(r"[A-Za-z]{1,6}\d{2,6}(?:[-/][A-Za-z0-9]{1,6})*")
DTC_RE = re.compile(r"(?<![A-Za-z0-9])[PBCU]\d{4}(?![0-9])")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_LATIN_RE = re.compile(r"[a-z0-9]+(?:[-_/][a-z0-9]+)*")

_PROVIDERS = ("local_fts5",)


@lru_cache(maxsize=1)
def fts5_available() -> bool:
    """Whether this Python's SQLite build exposes the FTS5 extension."""
    try:
        connection = sqlite3.connect(":memory:")
    except sqlite3.Error:  # pragma: no cover - interpreter cannot open SQLite
        return False
    try:
        connection.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(x)")
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()
    return True


def _cjk_tokens(run: str) -> list[str]:
    """Unigrams plus bigrams so Chinese terms match without a word dictionary."""
    tokens = list(run)
    tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def tokenize(text: str) -> tuple[str, ...]:
    """Version-locked tokenization for both indexing and querying.

    Rules (``TOKENIZER_VERSION`` = ``zh-lexical-v2``):

    1. Model numbers are preserved and expanded, case-folded:
       ``SY215-C`` -> ``sy215-c``, ``sy215``, ``sy215c``.
    2. DTC codes (``P0087``) are preserved verbatim, case-folded.
    3. CJK runs are segmented into unigrams and bigrams.
    4. Latin/digit words keep ``-``/``_``/``/`` joins as single tokens.

    Order and repeated occurrences are preserved for BM25 term frequency.
    Query expressions deduplicate terms separately.
    """
    if not isinstance(text, str):
        raise TypeError("Lexical tokenization requires text")
    emitted: list[str] = []
    lowered = text.lower()
    # Model numbers and DTC codes are extracted before generic latin scanning so
    # that their compound form is never split by the CJK/word rules.
    preserved: list[tuple[int, str]] = []
    for match in MODEL_NUMBER_RE.finditer(text):
        if DTC_RE.fullmatch(match.group()):
            continue
        preserved.append((match.start(), match.group().lower()))
    for match in DTC_RE.finditer(text):
        preserved.append((match.start(), match.group().lower()))
    preserved.sort()

    def emit(token: str) -> None:
        if token:
            emitted.append(token)

    def emit_word(word: str) -> None:
        emit(word)
        head = word.split("-")[0].split("/")[0].split("_")[0]
        if head != word:
            emit(head)
        compact = word.replace("-", "").replace("/", "").replace("_", "")
        if compact != word and compact != head:
            emit(compact)

    cursor = 0
    for start, token in preserved:
        if start < cursor:
            continue
        for word in _LATIN_RE.findall(lowered[cursor:start]):
            emit_word(word)
        for run in _CJK_RUN_RE.findall(lowered[cursor:start]):
            for piece in _cjk_tokens(run):
                emit(piece)
        emit_word(token)
        cursor = start + len(token)
    for word in _LATIN_RE.findall(lowered[cursor:]):
        emit_word(word)
    for run in _CJK_RUN_RE.findall(lowered[cursor:]):
        for piece in _cjk_tokens(run):
            emit(piece)
    return tuple(emitted)


def encode(text: str) -> str:
    """Flatten text into the whitespace-separated token stream FTS5 stores."""
    return " ".join(tokenize(text))


def encode_query(text: str) -> str:
    """Build a conjunctive FTS5 MATCH expression from the same token stream."""
    tokens = tuple(dict.fromkeys(tokenize(text)))
    if not tokens:
        raise ValueError("Lexical query produced no tokens; refusing a match-all query")
    # Quote every token: tokens may contain '-', which is an FTS5 operator.
    return " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)


def lexical_child_id(document_id: str, index_generation: str) -> str:
    """Deterministic publication receipt child id for one document generation.

    The caller adds this id to the ``children`` set passed to
    ``PublicationStore.begin`` and acknowledges it only after the lexical write
    is durable, so an incomplete lexical index cannot switch a generation.
    """
    if not document_id or not index_generation:
        raise ValueError("Lexical receipt requires document_id and index_generation")
    return "lexical:" + hashlib.sha256(
        json.dumps([INDEX_VERSION, document_id, index_generation]).encode()
    ).hexdigest()


@dataclass(frozen=True)
class LexicalEntry:
    """One derived-content unit placed in the inverted index."""

    uri: str
    document_id: str
    index_generation: str
    chunk_id: str
    text: str
    content_kind: str = "text"
    content_revision: str = ""

    def __post_init__(self) -> None:
        if self.content_kind not in {"text", "image_summary"}:
            raise ValueError("Lexical entry content_kind must be text or image_summary")
        if not self.uri or not self.document_id or not self.index_generation:
            raise ValueError("Lexical entry requires uri, document_id and index_generation")
        if not self.text.strip():
            raise ValueError("Lexical entry requires nonempty derived content")


@dataclass(frozen=True)
class LexicalResult:
    """A ranked lexical candidate; ``score`` is higher-is-better."""

    uri: str
    document_id: str
    chunk_id: str
    index_generation: str
    content_kind: str
    rank: int
    score: float
    score_normalized: float
    raw_score: float
    provider: str
    tokenizer_version: str = TOKENIZER_VERSION

    def as_dict(self) -> dict:
        return {
            "uri": self.uri,
            "document_id": self.document_id,
            "chunk_id": self.chunk_id,
            "index_generation": self.index_generation,
            "content_kind": self.content_kind,
            "rank": self.rank,
            "score": self.score,
            "score_normalized": self.score_normalized,
            "score_components": {
                "lexical_raw": self.raw_score,
                "lexical": self.score,
            },
            "provider": self.provider,
            "tokenizer_version": self.tokenizer_version,
        }


@dataclass
class SearchScope:
    """Pre-ranking filter: snapshot mapping plus authorization."""

    document_ids: tuple[str, ...] | None = None
    generations: tuple[str, ...] | None = None
    authorized_uris: frozenset[str] | None = None
    content_kinds: tuple[str, ...] | None = None

    def as_json(self) -> dict:
        return {
            "document_ids": sorted(self.document_ids) if self.document_ids else None,
            "generations": sorted(self.generations) if self.generations else None,
            "authorized_uris": (
                sorted(self.authorized_uris) if self.authorized_uris is not None else None
            ),
            "content_kinds": list(self.content_kinds) if self.content_kinds else None,
        }


def entries_from_chunks(plan, chunks: list[dict] | tuple[dict, ...]) -> list[LexicalEntry]:
    """Build lexical entries from ``plan.chunk_fields``-shaped child records.

    Only the documented derived fields are read: ``chunk_id``, ``chunk_text``,
    ``document_id`` and ``content_revision``. The source URI is the citation
    identity and is never rewritten.
    """
    entries = []
    for fields in chunks:
        entries.append(
            LexicalEntry(
                uri=plan.source_uri,
                document_id=fields.get("document_id") or plan.document_id,
                index_generation=fields.get("index_generation", ""),
                chunk_id=fields["chunk_id"],
                text=fields.get("chunk_text", ""),
                content_kind="text",
                content_revision=fields.get("content_revision", plan.content_revision),
            )
        )
    return entries


def entries_from_image_records(
    records,
    *,
    uri: str,
    document_id: str,
    index_generation: str,
    content_revision: str = "",
) -> list[LexicalEntry]:
    """Build lexical entries from image occurrence summaries.

    Matches the ``summary`` field produced by ``openviking/indexing/image.py``;
    the image summary is derived content and belongs in the same inverted index
    as child text so a textual query can reach an image.
    """
    entries = []
    for ordinal, record in enumerate(records):
        entries.append(
            LexicalEntry(
                uri=record.get("context_uri") or uri,
                document_id=document_id,
                index_generation=index_generation,
                chunk_id=f"image:{ordinal}",
                text=record["summary"],
                content_kind="image_summary",
                content_revision=content_revision,
            )
        )
    return entries


class _Transaction:
    """Small explicit transaction helper over a short-lived SQLite connection."""

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection

    def __enter__(self) -> sqlite3.Connection:
        self._connection.execute("BEGIN IMMEDIATE")
        return self._connection

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            if exc_type is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        finally:
            self._connection.close()
        return False


class LexicalIndex:
    """SQLite-backed BM25 lexical index scoped by namespace/document/generation."""

    def __init__(self, path: str, *, provider: str = "local_fts5"):
        if provider not in _PROVIDERS:
            raise ValueError(
                f"Lexical provider {provider!r} is not allowed; local backends use local_fts5 "
                "and native requires a contract-tested deployment"
            )
        self.path = str(Path(path).expanduser().resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._fts5 = fts5_available()
        self._provider = provider if self._fts5 else "portable_bm25"
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS lexical_rows (
                  rowid INTEGER PRIMARY KEY,
                  account TEXT NOT NULL, namespace TEXT NOT NULL,
                  document_id TEXT NOT NULL, index_generation TEXT NOT NULL,
                  uri TEXT NOT NULL, chunk_id TEXT NOT NULL,
                  content_kind TEXT NOT NULL, content_revision TEXT NOT NULL,
                  content_sha TEXT NOT NULL, tokens TEXT NOT NULL,
                  UNIQUE(account, namespace, document_id, index_generation, chunk_id));
                CREATE INDEX IF NOT EXISTS lexical_scope
                  ON lexical_rows(account, namespace, document_id, index_generation);
                CREATE INDEX IF NOT EXISTS lexical_uri ON lexical_rows(account, uri);
                """
            )
            db.execute("CREATE TABLE IF NOT EXISTS lexical_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            version = db.execute("SELECT value FROM lexical_metadata WHERE key='tokenizer_version'").fetchone()
            populated = db.execute("SELECT EXISTS(SELECT 1 FROM lexical_rows)").fetchone()[0]
            if (version and version[0] != TOKENIZER_VERSION) or (not version and populated):
                raise ValueError("LEXICAL_REBUILD_REQUIRED: tokenizer version changed; build a new index before switching publication")
            db.execute("INSERT OR IGNORE INTO lexical_metadata VALUES ('tokenizer_version', ?)", (TOKENIZER_VERSION,))
            if self._fts5:
                db.executescript(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS lexical_fts USING fts5(
                      tokens, content='lexical_rows', content_rowid='rowid',
                      tokenize="unicode61 tokenchars '-_/'");
                    CREATE TRIGGER IF NOT EXISTS lexical_rows_ai AFTER INSERT ON lexical_rows
                    BEGIN INSERT INTO lexical_fts(rowid, tokens)
                      VALUES (new.rowid, new.tokens); END;
                    CREATE TRIGGER IF NOT EXISTS lexical_rows_ad AFTER DELETE ON lexical_rows
                    BEGIN INSERT INTO lexical_fts(lexical_fts, rowid, tokens)
                      VALUES ('delete', old.rowid, old.tokens); END;
                    CREATE TRIGGER IF NOT EXISTS lexical_rows_au AFTER UPDATE ON lexical_rows
                    BEGIN
                      INSERT INTO lexical_fts(lexical_fts, rowid, tokens)
                        VALUES ('delete', old.rowid, old.tokens);
                      INSERT INTO lexical_fts(rowid, tokens) VALUES (new.rowid, new.tokens);
                    END;
                    """
                )

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def index_version(self) -> str:
        return INDEX_VERSION if self._fts5 else FALLBACK_INDEX_VERSION

    def _connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def _tx(self):
        return _Transaction(self._connect())

    def build(
        self,
        account: str,
        namespace: str,
        entries,
        *,
        replace_scope: tuple[str, ...] | None = None,
    ) -> dict:
        """Write derived content and return a deterministic index receipt.

        ``replace_scope`` is a ``(document_id, index_generation)`` pair; when
        given, the existing rows for that document generation are removed in the
        same transaction so a rebuild cannot mix old and new derived content.
        """
        rows = list(entries)
        for entry in rows:
            if not isinstance(entry, LexicalEntry):
                raise TypeError("LexicalIndex.build accepts LexicalEntry instances")
        with self._tx() as db:
            if replace_scope is not None:
                document_id, index_generation = replace_scope
                db.execute(
                    "DELETE FROM lexical_rows WHERE account=? AND namespace=? AND "
                    "document_id=? AND index_generation=?",
                    (account, namespace, document_id, index_generation),
                )
            for entry in rows:
                tokens = encode(entry.text)
                db.execute(
                    "INSERT OR REPLACE INTO lexical_rows"
                    "(account, namespace, document_id, index_generation, uri, chunk_id,"
                    " content_kind, content_revision, content_sha, tokens)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        account,
                        namespace,
                        entry.document_id,
                        entry.index_generation,
                        entry.uri,
                        entry.chunk_id,
                        entry.content_kind,
                        entry.content_revision,
                        hashlib.sha256(entry.text.encode()).hexdigest(),
                        tokens,
                    ),
                )
            documents = sorted({(e.document_id, e.index_generation) for e in rows})
            stored = db.execute(
                "SELECT count(*) FROM lexical_rows WHERE account=? AND namespace=?",
                (account, namespace),
            ).fetchone()[0]
        return {
            "provider": self.provider,
            "index_version": self.index_version,
            "tokenizer_version": TOKENIZER_VERSION,
            "namespace": namespace,
            "entries": len(rows),
            "documents": [{"document_id": d, "index_generation": g} for d, g in documents],
            "receipts": [lexical_child_id(d, g) for d, g in documents],
            "stored_rows": stored,
        }

    def drop(
        self,
        account: str,
        namespace: str,
        *,
        document_id: str | None = None,
        index_generation: str | None = None,
    ) -> int:
        """Remove derived rows for a scope; returns the number of rows dropped."""
        query = "DELETE FROM lexical_rows WHERE account=? AND namespace=?"
        params: list[str] = [account, namespace]
        if document_id is not None:
            query += " AND document_id=?"
            params.append(document_id)
        if index_generation is not None:
            query += " AND index_generation=?"
            params.append(index_generation)
        with self._tx() as db:
            return db.execute(query, params).rowcount

    def generations(self, account: str, namespace: str, document_id: str) -> list[str]:
        with self._tx() as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT index_generation FROM lexical_rows "
                    "WHERE account=? AND namespace=? AND document_id=?",
                    (account, namespace, document_id),
                )
            ]

    def scoped_rowids(self, account: str, namespace: str, scope: SearchScope) -> list[int]:
        """Resolve the pre-ranking filter to rowids (authorization before ranking)."""
        query = "SELECT rowid, document_id, index_generation, uri, content_kind FROM lexical_rows"
        query += " WHERE account=? AND namespace=?"
        params: list[str] = [account, namespace]
        with self._tx() as db:
            rows = db.execute(query, params).fetchall()
        allowed = []
        for rowid, document_id, generation, uri, content_kind in rows:
            if scope.document_ids is not None and document_id not in scope.document_ids:
                continue
            if scope.generations is not None and generation not in scope.generations:
                continue
            if scope.authorized_uris is not None and uri not in scope.authorized_uris:
                continue
            if scope.content_kinds is not None and content_kind not in scope.content_kinds:
                continue
            allowed.append(rowid)
        return allowed

    def search(
        self,
        query: str,
        account: str,
        namespace: str,
        *,
        scope: SearchScope | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[LexicalResult]:
        """Return BM25 candidates with higher-is-better normalized scores.

        Scope (snapshot mapping + authorization) is resolved to rowids *before*
        any scoring query runs. An empty scope short-circuits to no candidates
        rather than falling back to an unfiltered match.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("Lexical limit must be a positive integer")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Lexical offset must be a nonnegative integer")
        scope = scope or SearchScope()
        allowed = self.scoped_rowids(account, namespace, scope)
        if not allowed:
            return []
        match = encode_query(query)
        if self._fts5:
            hits = self._fts5_hits(match, allowed)
        else:
            hits = self._portable_hits(query, allowed)
        if not hits:
            return []
        # bm25() is negative-is-better; the adapter layer flips the direction so
        # that higher is better and later RRF fusion is not presented a
        # second, differently-signed score scale.
        best = max(score for score, _ in hits)
        results = []
        for rank, (score, row) in enumerate(hits[offset : offset + limit], start=offset + 1):
            normalized = 1.0 if best <= 0 else max(0.0, min(1.0, score / best))
            results.append(
                LexicalResult(
                    uri=row["uri"],
                    document_id=row["document_id"],
                    chunk_id=row["chunk_id"],
                    index_generation=row["index_generation"],
                    content_kind=row["content_kind"],
                    rank=rank,
                    score=score,
                    score_normalized=normalized,
                    raw_score=-score,
                    provider=self.provider,
                )
            )
        return results

    def _row_lookup(self, rowids: list[int]) -> dict[int, dict]:
        marks = ",".join("?" * len(rowids))
        with self._tx() as db:
            rows = db.execute(
                f"SELECT rowid, uri, document_id, chunk_id, index_generation, content_kind "
                f"FROM lexical_rows WHERE rowid IN ({marks})",
                rowids,
            ).fetchall()
        return {
            row[0]: {
                "uri": row[1],
                "document_id": row[2],
                "chunk_id": row[3],
                "index_generation": row[4],
                "content_kind": row[5],
            }
            for row in rows
        }

    def _fts5_hits(self, match: str, allowed: list[int]) -> list[tuple[float, dict]]:
        marks = ",".join("?" * len(allowed))
        with self._tx() as db:
            rows = db.execute(
                "SELECT f.rowid, bm25(lexical_fts) AS raw FROM lexical_fts f "
                f"WHERE lexical_fts MATCH ? AND f.rowid IN ({marks}) "
                "ORDER BY raw ASC, f.rowid ASC",
                [match, *allowed],
            ).fetchall()
        if not rows:
            return []
        lookup = self._row_lookup([row[0] for row in rows])
        # Negate the native lower-is-better FTS5 score so higher is better.
        return [(-float(raw), lookup[rowid]) for rowid, raw in rows if rowid in lookup]

    def _portable_hits(self, query: str, allowed: list[int]) -> list[tuple[float, dict]]:
        """Portable Okapi BM25 used only when SQLite lacks FTS5."""
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        marks = ",".join("?" * len(allowed))
        with self._tx() as db:
            rows = db.execute(
                "SELECT rowid, uri, document_id, chunk_id, index_generation, content_kind, tokens "
                f"FROM lexical_rows WHERE rowid IN ({marks})",
                allowed,
            ).fetchall()
        documents = []
        for row in rows:
            tokens = row[6].split()
            documents.append(
                (
                    row[0],
                    {
                        "uri": row[1],
                        "document_id": row[2],
                        "chunk_id": row[3],
                        "index_generation": row[4],
                        "content_kind": row[5],
                    },
                    tokens,
                )
            )
        total = len(documents)
        if not total:
            return []
        average = sum(len(tokens) for _, _, tokens in documents) / total
        hits = []
        for rowid, meta, tokens in documents:
            counts: dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            if not set(query_tokens).issubset(counts):
                continue  # Same conjunctive query contract as native FTS5.
            score = 0.0
            for token in set(query_tokens):
                frequency = counts.get(token, 0)
                if not frequency:
                    continue
                document_frequency = sum(1 for _, _, t in documents if token in set(t))
                ratio = (total - document_frequency + 0.5) / (document_frequency + 0.5)
                inverse = math.log(1 + ratio)
                denominator = frequency + _BM25_K1 * (
                    1 - _BM25_B + _BM25_B * len(tokens) / (average or 1)
                )
                score += inverse * frequency * (_BM25_K1 + 1) / denominator
            if score > 0:
                hits.append((score, meta, rowid))
        hits.sort(key=lambda item: (-item[0], item[2]))
        return [(score, meta) for score, meta, _ in hits]

    def stats(self, account: str, namespace: str) -> dict:
        with self._tx() as db:
            rows = db.execute(
                "SELECT document_id, index_generation, content_kind, count(*) "
                "FROM lexical_rows WHERE account=? AND namespace=? "
                "GROUP BY document_id, index_generation, content_kind",
                (account, namespace),
            ).fetchall()
        return {
            "provider": self.provider,
            "index_version": self.index_version,
            "tokenizer_version": TOKENIZER_VERSION,
            "namespace": namespace,
            "groups": [
                {
                    "document_id": row[0],
                    "index_generation": row[1],
                    "content_kind": row[2],
                    "entries": row[3],
                }
                for row in rows
            ],
        }


def lexical_receipt(document_id: str, index_generation: str) -> str:
    """Public alias for the publication receipt child id of one generation."""
    return lexical_child_id(document_id, index_generation)


def provider_for(deployment: str = "local") -> str:
    """First-version provider selection fixed by spec section 7.1.

    ``native`` is only allowed for deployments that already pass the lexical
    contract tests; this build does not certify that, so it is rejected.
    """
    if deployment == "local":
        return "local_fts5"
    raise ValueError(
        f"Lexical provider for deployment {deployment!r} is not certified; "
        "native requires a deployment that already passes the lexical contract tests"
    )


def index_path(base: str, account: str, namespace: str) -> str:
    """Namespace-scoped on-disk location; workers never share a mutable copy."""
    digest = hashlib.sha256(json.dumps([account, namespace]).encode()).hexdigest()[:16]
    return os.path.join(base, f"lexical-{digest}.db")


__all__ = [
    "FALLBACK_INDEX_VERSION",
    "INDEX_VERSION",
    "TOKENIZER_VERSION",
    "LexicalEntry",
    "LexicalIndex",
    "LexicalResult",
    "SearchScope",
    "encode",
    "encode_query",
    "entries_from_chunks",
    "entries_from_image_records",
    "fts5_available",
    "index_path",
    "lexical_child_id",
    "lexical_receipt",
    "provider_for",
    "tokenize",
]