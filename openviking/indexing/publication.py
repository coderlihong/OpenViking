# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Durable text-generation publication with immutable query views.

SQLite transactions fence producer attempts and preserve unrelated documents.
A local store is a single-host compatibility mode. Multi-process deployments
use the authenticated authority repository; SQLite remains local to its owner.
Text and directory semantic records/content can publish in one batch.
Images, directory layout and BM25 are not yet part of this barrier.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os

if os.name == "posix":
    import fcntl
import json
import math
import sqlite3
import threading
import time
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache, wraps
from pathlib import Path
from uuid import uuid4

from openviking.indexing.manifest_codec import (
    decode_manifest,
    decode_manifest_delta,
    encode_manifest,
    encode_manifest_delta,
    is_delta_manifest,
)
from openviking.indexing.query_leases import QueryLeaseStore
from openviking.indexing.registry_lexical import RegistryLexicalMixin
from openviking.indexing.registry_metadata import RegistryMetadataMixin
from openviking.indexing.registry_gc import (
    RegistryGCMixin,
    _seconds,
    _snapshot_manifest,
    _tombstone_covers,
    record_snapshot_manifest,
)
from openviking.indexing.registry_domains import RegistryDomains
from openviking.indexing.jobs import DurableJobsMixin
from openviking.indexing.write_recovery import WriteRecoveryMixin
from openviking.storage.expr import And, Eq, Or, RawDSL
from openviking.storage.vector_ids import publication_member_id
from openviking_cli.exceptions import FailedPreconditionError

logger = logging.getLogger(__name__)

# How many delta snapshots may chain before a full manifest is written. Bounds
# the replay a reader pays: resolving a snapshot walks to the nearest full
# manifest, so depth is the read cost.
_MANIFEST_DELTA_MAX_DEPTH = 16


def _with_delta_ancestors(db, snapshot_ids):
    """Extend *snapshot_ids* with every ancestor reachable through deltas.

    A delta snapshot stores only its change set, so it is unreadable once its
    parent is gone. Any set of snapshots that must stay readable therefore has to
    include the whole chain behind each of them.
    """
    keep = set(snapshot_ids)
    pending = list(keep)
    while pending:
        current = pending.pop()
        row = db.execute("SELECT manifest FROM snapshots WHERE id=?", (current,)).fetchone()
        if row is None:
            continue
        raw = row[0]
        if not is_delta_manifest(raw):
            continue
        try:
            parent = decode_manifest_delta(raw).get("parent")
        except Exception:  # noqa: BLE001
            continue
        if parent and parent not in keep:
            keep.add(parent)
            pending.append(parent)
    return keep


def _delta_depth(db, snapshot):
    """How many deltas sit between *snapshot* and its nearest full manifest."""
    depth = 0
    current = snapshot
    seen = set()
    while current and depth <= _MANIFEST_DELTA_MAX_DEPTH:
        if current in seen:
            return _MANIFEST_DELTA_MAX_DEPTH
        seen.add(current)
        row = db.execute("SELECT manifest FROM snapshots WHERE id=?", (current,)).fetchone()
        if row is None:
            return _MANIFEST_DELTA_MAX_DEPTH
        raw = row[0]
        if not is_delta_manifest(raw):
            return depth
        try:
            current = decode_manifest_delta(raw).get("parent")
        except Exception:  # noqa: BLE001
            return _MANIFEST_DELTA_MAX_DEPTH
        depth += 1
    return depth

_view = ContextVar("text_index_query_view", default=None)

_QUERY_CAPTURE_ATTEMPTS = 3
_QUERY_LEASE_STORE_LOCK = threading.Lock()

# Tombstone rows are tiny; one statement per this many rows keeps a batch
# inside SQLite's host-parameter budget while the caller still pays one writer
# transaction per call (and one authority frame, whose size bounds a bulk
# account-scope tombstone call the same way).
TOMBSTONE_BATCH = 500


class PublicationStore(RegistryDomains, DurableJobsMixin, WriteRecoveryMixin, RegistryLexicalMixin, RegistryMetadataMixin, RegistryGCMixin):
    def __init__(self, path: str, *, authority_owned=False):
        self.path = str(Path(path).expanduser().resolve())
        if (Path(self.path).parent / 'RESTORE_PENDING.json').exists():
            from openviking_cli.exceptions import FailedPreconditionError
            raise FailedPreconditionError('RESTORE_PENDING: joint restore validation must finish before registry startup')
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._authority_owned = authority_owned
        # Query-view leases live in their own tiny store (see _query_leases);
        # created lazily so tests and read-only tooling pay nothing for it.
        self._query_leases_store: QueryLeaseStore | None = None
        # One long-lived connection per thread. Opening a connection per call
        # forced SQLite to re-read and re-parse the entire schema on every
        # statement prepare (sqlite3InitOne), which pinned the single event-loop
        # thread at 100% CPU under the reindex load and stretched write-lock
        # hold times until publication query leases lapsed.
        self._local = threading.local()
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots (
                  id TEXT PRIMARY KEY, account TEXT NOT NULL, namespace TEXT NOT NULL,
                  manifest TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS heads (
                  account TEXT, namespace TEXT, snapshot TEXT,
                  PRIMARY KEY(account,namespace));
                CREATE TABLE IF NOT EXISTS builds (
                  generation TEXT PRIMARY KEY, account TEXT, namespace TEXT, uri TEXT,
                  plan TEXT, expected TEXT, state TEXT, created REAL, error TEXT);
                -- "Newest generation for this URI" is the acceptance gate, the
                -- interrupted-generation scan and the retention prune; without
                -- this index each of them sorts the whole append-only history
                -- (653,922 rows live) on every call.
                CREATE INDEX IF NOT EXISTS builds_uri_created ON builds(uri, created);
                CREATE TABLE IF NOT EXISTS desired (
                  account TEXT, namespace TEXT, uri TEXT, generation TEXT,
                  PRIMARY KEY(account,namespace,uri));
                -- Reverse lookup by generation: GC and lexical retention ask
                -- "is this generation still the desired one?" once per candidate
                -- row, and the primary key cannot answer it (a full scan of
                -- ``desired`` per row made that check take minutes).
                CREATE INDEX IF NOT EXISTS desired_generation ON desired(generation);
                CREATE TABLE IF NOT EXISTS drafts (
                  account TEXT, uri TEXT, epoch INTEGER NOT NULL, sha TEXT,
                  PRIMARY KEY(account,uri));
                CREATE TABLE IF NOT EXISTS sources (
                  generation TEXT PRIMARY KEY, reference TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS source_writes (
                  id TEXT PRIMARY KEY, account TEXT, uri TEXT, state TEXT, created REAL);
                CREATE INDEX IF NOT EXISTS source_writes_uri ON source_writes(account,uri);
                CREATE TABLE IF NOT EXISTS wiki_schemas (
                  account TEXT, schema_id TEXT, version TEXT, digest TEXT, reference TEXT,
                  state TEXT, PRIMARY KEY(account,schema_id,version));
                CREATE TABLE IF NOT EXISTS wiki_collections (
                  account TEXT, collection_id TEXT, binding TEXT,
                  PRIMARY KEY(account,collection_id));
                CREATE TABLE IF NOT EXISTS wiki_build_plans (
                  id TEXT PRIMARY KEY, account TEXT NOT NULL, namespace TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL, digest TEXT NOT NULL, manifest TEXT NOT NULL,
                  state TEXT NOT NULL, created REAL NOT NULL,
                  UNIQUE(account,namespace,idempotency_key));
                CREATE TABLE IF NOT EXISTS wiki_collection_domains (
                  account TEXT, collection_id TEXT, state TEXT NOT NULL,
                  target_root TEXT, raw_namespace TEXT, raw_root TEXT,
                  PRIMARY KEY(account,collection_id));
                CREATE TABLE IF NOT EXISTS wiki_entity_catalog (
                  account TEXT, namespace_id TEXT, canonical_id TEXT,
                  entity_type TEXT NOT NULL, schema_id TEXT NOT NULL,
                  schema_version TEXT NOT NULL, schema_digest TEXT NOT NULL,
                  aliases TEXT NOT NULL, source_uri TEXT NOT NULL, registered REAL NOT NULL,
                  PRIMARY KEY(account,namespace_id,canonical_id));
                CREATE TABLE IF NOT EXISTS wiki_entity_aliases (
                  account TEXT, namespace_id TEXT, alias TEXT, canonical_id TEXT NOT NULL,
                  PRIMARY KEY(account,namespace_id,alias));
                CREATE TABLE IF NOT EXISTS kb_namespaces (
                  account TEXT, namespace TEXT, root_uri TEXT,
                  PRIMARY KEY(account,namespace), UNIQUE(account,root_uri));
                CREATE TABLE IF NOT EXISTS kb_exports (
                  account TEXT, source_namespace TEXT, target_namespace TEXT, principal TEXT,
                  PRIMARY KEY(account,source_namespace,target_namespace,principal));
                CREATE TABLE IF NOT EXISTS kb_policy_revisions (
                  account TEXT PRIMARY KEY, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS semantic_sources (
                  generation TEXT PRIMARY KEY, reference TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts (
                  generation TEXT, child TEXT, PRIMARY KEY(generation,child));
                CREATE TABLE IF NOT EXISTS batches (
                  id TEXT PRIMARY KEY, account TEXT, namespace TEXT, state TEXT, error TEXT,
                  remaining INTEGER);
                CREATE TABLE IF NOT EXISTS batch_members (
                  generation TEXT PRIMARY KEY, batch TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS batch_members_batch ON batch_members(batch);
                CREATE TABLE IF NOT EXISTS leases (
                  id TEXT PRIMARY KEY, snapshots TEXT, expires REAL);
                CREATE TABLE IF NOT EXISTS tombstones (
                  account TEXT, uri TEXT, created REAL NOT NULL,
                  PRIMARY KEY(account,uri));
                CREATE TABLE IF NOT EXISTS gc_marks (
                  account TEXT, sha TEXT, kind TEXT, revision INTEGER NOT NULL,
                  state TEXT NOT NULL, created REAL NOT NULL,
                  PRIMARY KEY(account,sha,kind));
                CREATE TABLE IF NOT EXISTS gc_leases (
                  id TEXT PRIMARY KEY, account TEXT NOT NULL, snapshots TEXT NOT NULL,
                  expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS gc_audit (
                  id TEXT PRIMARY KEY, account TEXT, sha TEXT, kind TEXT, action TEXT,
                  reason TEXT, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS gc_audit_account ON gc_audit(account);
            """)
            # ``CREATE TABLE IF NOT EXISTS`` never retrofits a column onto an
            # existing database, so a registry built before the batch counter
            # existed needs the column added here (same idiom as
            # server/oauth/storage.py). NULL marks those legacy batches: they
            # keep the original whole-batch scan.
            existing = {row[1] for row in db.execute("PRAGMA table_info(batches)")}
            if "remaining" not in existing:
                db.execute("ALTER TABLE batches ADD COLUMN remaining INTEGER")
            # Same retrofit for the fence owner: a fence stamped with another
            # process generation belongs to a server that already exited.
            write_columns = {row[1] for row in db.execute("PRAGMA table_info(source_writes)")}
            if "owner" not in write_columns:
                db.execute("ALTER TABLE source_writes ADD COLUMN owner TEXT")

        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO wiki_collection_domains(account,collection_id,state) SELECT account,collection_id,'legacy_domain_pending' FROM wiki_collections")
        self.init_lexical()
        self.init_metadata()
        self.init_jobs()
        self.init_write_recovery()
        self.init_physical_gc()

    @contextmanager
    def db(self, readonly: bool = False, busy_timeout_ms: int | None = None):
        lock = open(self.path + ".authority.lock", "a+b")
        if os.name == "posix" and not self._authority_owned:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close()
                raise RuntimeError("Registry is owned by an authority; use its socket repository")
        try:
            db = self._local.conn
        except AttributeError:
            try:
                # 120s: under the vector phase dozens of workers race for the
                # single write lock and each transaction re-reads cold b-tree
                # pages from this multi-GB database; a 30s busy timeout surfaced
                # as spurious "database is locked" failures at the tail of the
                # convoy. Waiting is safe -- every caller retries or re-enqueues.
                db = sqlite3.connect(self.path, timeout=120)
            except BaseException:
                lock.close()
                raise
            db.row_factory = sqlite3.Row
            # WAL lets readers proceed while a writer commits; the default
            # rollback journal serialises them, which surfaced as
            # "database is locked" once concurrent writers reached the vector
            # phase. NORMAL is the standard WAL pairing (no fsync per commit).
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("PRAGMA foreign_keys=ON")
            # The default 2MB page cache thrashes against a multi-GB database:
            # hot b-tree pages of the receipts/builds indexes fall out between
            # transactions and every small lookup degenerates into pread storms,
            # stretching write-lock hold times (see stack samples). This is a
            # per-thread-local connection, so the budget is threads x this cap
            # (~72 x 16MB ~= 1.1GB worst case) -- not a single shared quota.
            db.execute("PRAGMA cache_size=-16384")  # 16MB per connection
            self._local.conn = db
        # A reused connection keeps its prepared-statement cache and loaded
        # schema; only the outermost frame opens a transaction, so callers that
        # pass `db` down (e.g. the batch walk) join the same transaction instead
        # of trying to BEGIN inside one.
        depth = getattr(self._local, "depth", 0)
        self._local.depth = depth + 1
        started = time.perf_counter()
        acquired = started
        try:
            if depth == 0:
                if busy_timeout_ms is not None:
                    # Bounded wait for callers that run on the event loop: a query
                    # lease renewal used to queue behind the publisher's commit
                    # queue for 200+s, freezing the whole server (watchdog stacks:
                    # db -> renew -> QueryView.read -> visible_filter -> search).
                    # A short busy timeout turns that freeze into a fast retry.
                    db.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
                if readonly:
                    # Pure readers must not queue on the writer lock: in WAL a
                    # deferred read transaction never blocks writers and writers
                    # never block it. This halves the BEGIN IMMEDIATE demand of
                    # the vector phase (can_write/state/is_superseded).
                    db.execute("BEGIN DEFERRED")
                else:
                    db.execute("BEGIN IMMEDIATE")
                acquired = time.perf_counter()
            yield db
            if depth == 0 and not readonly:
                db.commit()
        except BaseException:
            if depth == 0:
                db.rollback()
                # A rolled-back transaction must not publish anything.
                self._local.pending_publications = []
            raise
        finally:
            self._local.depth = depth
            if depth == 0 and readonly:
                # End the read snapshot so it never pins the WAL head.
                try:
                    db.execute("COMMIT")
                except sqlite3.OperationalError:
                    pass
            if depth == 0 and busy_timeout_ms is not None:
                # Restore the connection default for the next transaction.
                try:
                    db.execute("PRAGMA busy_timeout=120000")
                except sqlite3.OperationalError:
                    pass
            lock.close()
            if depth == 0:
                total_ms = (time.perf_counter() - started) * 1000.0
                threshold = _slow_tx_threshold_ms()
                if threshold and total_ms >= threshold:
                    wait_ms = (acquired - started) * 1000.0
                    # Walk raw frames instead of traceback.extract_stack(): the
                    # latter pulls every frame through linecache.checkcache(),
                    # which stats every source file in the cache.  Under the
                    # vector phase most transactions cross the threshold, and
                    # that stat storm ran on the event loop and stalled it for
                    # 15-56s (watchdog: MainThread -> db -> extract_stack ->
                    # linecache.checkcache).  f_lineno needs no linecache.
                    frames = []
                    frame = sys._getframe(1)
                    while frame is not None and len(frames) < 24:
                        if "publication.py" not in frame.f_code.co_filename:
                            frames.append(
                                f"{Path(frame.f_code.co_filename).name}:"
                                f"{frame.f_lineno}:{frame.f_code.co_name}"
                            )
                        frame = frame.f_back
                    logger.warning(
                        "slow publication transaction: %.0fms (lock wait %.0fms, readonly=%s) caller=%s",
                        total_ms,
                        wait_ms,
                        readonly,
                        " <- ".join(frames[:6]),
                    )
        if depth == 0 and not readonly:
            # Publish only once this transaction has committed. The expensive part
            # of a publication (walks, manifest merge, json.dumps + gzip over a
            # ~35MB snapshot) used to run under BEGIN IMMEDIATE and held the single
            # writer for ~0.7s, queueing every other writer behind it. It now runs
            # in a read-only pass followed by a short write transaction.
            self._drain_deferred_publications()

    def begin(
        self,
        account,
        namespace,
        uri,
        plan,
        children,
        source_ref=None,
        batch=None,
        semantic_refs=None,
    ):
        if source_ref is not None:
            sha = source_ref.get("sha256", "")
            if (
                len(sha) != 64
                or any(c not in "0123456789abcdef" for c in sha)
                or not isinstance(source_ref.get("size"), int)
                or source_ref["size"] < 0
            ):
                raise ValueError("Invalid source revision reference")
            cached = source_ref.get("image_semantics")
            if cached is not None and (
                len(cached.get("sha256", "")) != 64
                or any(c not in "0123456789abcdef" for c in cached["sha256"])
                or not isinstance(cached.get("size"), int) or cached["size"] < 0
            ):
                raise ValueError("Invalid image semantics reference")
            deps = source_ref.get("image_context_epochs", {})
            if not isinstance(deps, dict) or any(
                not uri.startswith("viking://") or not isinstance(epoch, int)
                for uri, epoch in deps.items()
            ):
                raise ValueError("Invalid image context dependencies")
            contributors = source_ref.get("wiki_contributors", [])
            if not isinstance(contributors, list) or any(
                not isinstance(uri, str) or not uri.startswith("viking://resources/")
                for uri in contributors
            ):
                raise ValueError("Invalid wiki contributor dependencies")
        if semantic_refs is not None:
            if not semantic_refs or set(semantic_refs) - {"0", "1"}:
                raise ValueError("Invalid semantic source levels")
            for reference in semantic_refs.values():
                sha = reference.get("sha256", "")
                if (
                    len(sha) != 64
                    or any(c not in "0123456789abcdef" for c in sha)
                    or not isinstance(reference.get("size"), int)
                    or reference["size"] < 0
                    or not isinstance(reference.get("draft_epoch"), int)
                ):
                    raise ValueError("Invalid semantic source revision")
        expected = list(children)
        if not expected or len(expected) != len(set(expected)):
            raise ValueError("Publication requires a nonempty unique child set")
        if semantic_refs is not None and set(expected) - {'metadata:' + plan} != {
            f"semantic:{level}" for level in semantic_refs
        }:
            raise ValueError("Semantic source levels do not match expected members")
        generation = uuid4().hex
        with self.db() as db:
            self._assert_not_writing(db, account, uri)
            if batch is not None:
                group = db.execute("SELECT * FROM batches WHERE id=?", (batch,)).fetchone()
                if not group or group["state"] != "building":
                    raise RuntimeError("Publication batch no longer accepts members")
                if (group["account"], group["namespace"]) != (account, namespace):
                    raise ValueError("Publication batch cannot cross account/namespace")
                duplicate = db.execute(
                    "SELECT m.generation,b.state FROM batch_members m "
                    "JOIN builds b ON b.generation=m.generation WHERE m.batch=? AND b.uri=?",
                    (batch, uri),
                ).fetchall()
                published = [row for row in duplicate if row["state"] == "published"]
                if published and len(published) == len(duplicate):
                    # Every attempt for this URI inside the batch already landed in a
                    # snapshot; a second member would rewrite a published manifest entry
                    # mid-batch, which the batch cannot reconcile. The caller owns this
                    # case (the source is already published) and must open a new batch.
                    raise ValueError("Duplicate source URI in publication batch")
                for stale in duplicate:
                    if stale["state"] == "published":
                        continue
                    # A newer index attempt for the same source replaces the batch's
                    # earlier member rather than being refused. The caption backfill
                    # rewrites a chapter's markdown just after that chapter's first
                    # build was enqueued, so its rebuild necessarily arrives in the same
                    # batch; raising here left `desired` pointing at the generation that
                    # the gate had already condemned ("Source changed during index
                    # build"), so the chapter and the figure it cites stayed invisible
                    # to retrieval forever while the ingest still reported success.
                    ready = self._member_ready(db, stale["generation"])
                    changed = db.execute(
                        "UPDATE builds SET state='superseded',error=? WHERE generation=?",
                        (
                            "Superseded by a newer index attempt in the same batch",
                            stale["generation"],
                        ),
                    ).rowcount
                    # A member that had already gone ready was counted down when it
                    # did; only a still-outstanding one owes the batch a decrement.
                    if changed and not ready:
                        self._decrement_batch(db, batch)
            overlap = db.execute(
                "SELECT 1 FROM desired WHERE account=? AND uri=? AND namespace!=?",
                (account, uri, namespace),
            ).fetchone()
            if overlap:
                raise ValueError("Source URI already belongs to another publication namespace")
            if source_ref is not None and "draft_epoch" in source_ref:
                draft = db.execute(
                    "SELECT epoch FROM drafts WHERE account=? AND uri=?", (account, uri)
                ).fetchone()
                if draft is None or draft[0] != source_ref["draft_epoch"]:
                    raise RuntimeError("Source changed while its index plan was being prepared")
                db.execute(
                    "UPDATE drafts SET sha=? WHERE account=? AND uri=?",
                    (source_ref["sha256"], account, uri),
                )
            if source_ref is not None:
                for source_uri, epoch in source_ref.get("image_context_epochs", {}).items():
                    self._assert_not_writing(db, account, source_uri)
                    draft = db.execute(
                        "SELECT epoch FROM drafts WHERE account=? AND uri=?", (account, source_uri)
                    ).fetchone()
                    if draft is None or draft[0] != epoch:
                        raise RuntimeError("Image context changed before index enqueue")
            if semantic_refs is not None:
                for level, reference in semantic_refs.items():
                    sidecar = uri.rstrip("/") + (
                        "/.abstract.md" if level == "0" else "/.overview.md"
                    )
                    self._assert_not_writing(db, account, sidecar)
                    draft = db.execute(
                        "SELECT epoch FROM drafts WHERE account=? AND uri=?", (account, sidecar)
                    ).fetchone()
                    if draft is None or draft[0] != reference["draft_epoch"]:
                        raise RuntimeError("Semantic source changed while preparing its index")
                    db.execute(
                        "UPDATE drafts SET sha=? WHERE account=? AND uri=?",
                        (reference["sha256"], account, sidecar),
                    )
            db.execute(
                "INSERT INTO builds VALUES (?,?,?,?,?,?,?, ?,NULL)",
                (
                    generation,
                    account,
                    namespace,
                    uri,
                    plan,
                    json.dumps(expected),
                    "building",
                    time.time(),
                ),
            )
            if batch is not None:
                db.execute("INSERT INTO batch_members VALUES (?,?)", (generation, batch))
                # NULL stays NULL for batches created before the counter existed:
                # those keep the original whole-batch scan.
                db.execute("UPDATE batches SET remaining=remaining+1 WHERE id=?", (batch,))
            if source_ref is not None:
                db.execute("INSERT INTO sources VALUES (?,?)", (generation, json.dumps(source_ref)))
            if semantic_refs is not None:
                db.execute(
                    "INSERT INTO semantic_sources VALUES (?,?)",
                    (generation, json.dumps(semantic_refs)),
                )
            db.execute(
                "INSERT OR REPLACE INTO desired VALUES (?,?,?,?)",
                (account, namespace, uri, generation),
            )
        return generation

    def observe_draft(self, account, uri):
        with self.db() as db:
            self._assert_not_writing(db, account, uri)
            db.execute("INSERT OR IGNORE INTO drafts VALUES (?,?,0,NULL)", (account, uri))
            return db.execute(
                "SELECT epoch FROM drafts WHERE account=? AND uri=?", (account, uri)
            ).fetchone()[0]

    @staticmethod
    def _assert_not_writing(db, account, uri):
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM source_writes WHERE account=? AND uri=?", (account, uri))]
        if not rows:
            return
        if PublicationStore._settle_dead_fences(db, account, uri, rows):
            return
        # Point-named so queue workers settle the message permanently instead
        # of re-enqueueing an unknown error forever (observed live: a single
        # unbacked uncertain fence re-enqueued one sidecar write 96 times).
        raise FailedPreconditionError(
            "WRITE_RECOVERY_REQUIRED: source has an in-flight or uncertain write; "
            "retry after verified recovery or operator fence clear"
        )

    @staticmethod
    def _settle_dead_fences(db, account, uri, rows):
        """Settle unbacked uncertain fences whose writer is long gone.

        ``source_write`` (openviking/indexing/content.py:167) leaves an
        ``uncertain`` fence whenever a storage write fails or is cancelled, and
        nothing ever expires it.  With no ``source_write_refs`` row no verifier
        can prove terminality -- that case is exactly what the operator path
        ``write_recovery.write_fence_clear`` exists for -- so a dead fence
        blocked this URI's directory generation for good.  Observed live: 154
        new fences in two minutes of fleet reindex and 74 "Failed directory
        generation" records on an otherwise complete manual.

        Once every row is unbacked and older than its state's limit
        (``AUTO_SETTLE_FENCE_SECONDS`` for ``uncertain``,
        ``AUTO_SETTLE_WRITING_SECONDS`` for an abandoned ``writing`` row), the
        writer cannot still be running, so settle it with the same audited
        bookkeeping the operator path uses and let the caller proceed.
        """
        now = time.time()
        settle_after = {
            "uncertain": AUTO_SETTLE_FENCE_SECONDS,
            "writing": AUTO_SETTLE_WRITING_SECONDS,
        }
        # A fence stamped with another process generation belongs to a server
        # that already exited, so it settles at once; legacy rows with no owner
        # (or a mix that includes this generation) keep the time window.
        generation = current_process_generation()
        if not all(row.get("owner") and row.get("owner") != generation for row in rows):
            for row in rows:
                limit = settle_after.get(row["state"])
                if limit is None or now - row["created"] < limit:
                    return False
        tokens = [row["id"] for row in rows]
        placeholders = ",".join("?" for _ in tokens)
        if db.execute(
            "SELECT 1 FROM source_write_refs WHERE token IN (" + placeholders + ")", tokens
        ).fetchone():
            return False
        epoch_row = db.execute("SELECT epoch FROM drafts WHERE account=? AND uri=?", (account, uri)).fetchone()
        epoch = epoch_row["epoch"] if epoch_row else 0
        db.execute(
            "INSERT INTO source_fence_clears (account,uri,actor,reason,fences,old_epoch,new_epoch,confirmed) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (account, uri, AUTO_SETTLE_FENCE_ACTOR, AUTO_SETTLE_FENCE_REASON,
             json.dumps([{"id": row["id"], "state": row["state"], "created": row["created"]} for row in rows],
                        sort_keys=True),
             epoch, epoch + 1, now),
        )
        db.execute("DELETE FROM source_writes WHERE account=? AND uri=?", (account, uri))
        db.execute("UPDATE drafts SET epoch=epoch+1,sha=NULL WHERE account=? AND uri=?", (account, uri))
        logger.warning(
            "auto-settled %d dead write fence(s) for %s "
            "(unbacked, owners=%s; a different process generation settles at once, "
            "otherwise older than %.0fs uncertain / %.0fs writing): %s",
            len(rows), uri,
            ",".join(sorted({str(row.get("owner")) for row in rows})),
            AUTO_SETTLE_FENCE_SECONDS, AUTO_SETTLE_WRITING_SECONDS,
            ", ".join(sorted({row["state"] for row in rows})),
        )
        return True

    def begin_write(self, account, uri, backend_ref=None):
        if backend_ref is not None and (
            not isinstance(backend_ref, dict) or backend_ref.get('account') != account
            or backend_ref.get('uri') != uri or not isinstance(backend_ref.get('backend'), str)
            or not backend_ref.get('backend') or not isinstance(backend_ref.get('operation_id'), str)
            or not backend_ref.get('operation_id')
        ):
            raise ValueError('Backend operation must be bound to this account and source URI')
        token = uuid4().hex
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO drafts VALUES (?,?,0,NULL)", (account, uri))
            db.execute("UPDATE drafts SET epoch=epoch+1,sha=NULL WHERE account=? AND uri=?", (account, uri))
            db.execute("INSERT INTO source_writes VALUES (?,?,?,'writing',?,?)",
                       (token, account, uri, time.time(), current_process_generation()))
            if backend_ref is not None:
                db.execute('INSERT INTO source_write_refs VALUES (?,?,?,?)',
                           (token, backend_ref['backend'], backend_ref['operation_id'], json.dumps(backend_ref, sort_keys=True)))
        return token

    def finish_write(self, token):
        with self.db() as db:
            row = db.execute("SELECT * FROM source_writes WHERE id=?", (token,)).fetchone()
            if row is None or row["state"] != "writing":
                raise RuntimeError("Only a confirmed live write may complete its fence")
            db.execute("UPDATE drafts SET epoch=epoch+1,sha=NULL WHERE account=? AND uri=?", (row["account"], row["uri"]))
            db.execute("DELETE FROM source_writes WHERE id=?", (token,))
            db.execute("DELETE FROM source_write_refs WHERE token=?", (token,))

    def uncertain_write(self, token):
        with self.db() as db:
            db.execute("UPDATE source_writes SET state='uncertain' WHERE id=?", (token,))

    def pending_writes(self, account, uri):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM source_writes WHERE account=? AND uri=?", (account, uri))]

    def invalidate_draft(self, account, uri):
        # Legacy explicit invalidation is not a write fence. Storage mutations
        # must use begin_write/finish_write to cover their entire await window.
        with self.db() as db:
            db.execute(
                "UPDATE drafts SET epoch=epoch+1,sha=NULL WHERE account=? AND uri=?", (account, uri)
            )

    def _build(self, db, generation):
        row = db.execute("SELECT * FROM builds WHERE generation=?", (generation,)).fetchone()
        if row is None:
            raise ValueError("Unknown index generation")
        return row

    def begin_batch(self, account, namespace):
        batch = uuid4().hex
        with self.db() as db:
            db.execute(
                "INSERT INTO batches (id,account,namespace,state,error,remaining) "
                "VALUES (?,?,?,'building',NULL,0)",
                (batch, account, namespace),
            )
        return batch

    def _fail_batch(self, db, batch, reason):
        db.execute(
            "UPDATE batches SET state='failed',error=?,remaining=0 WHERE id=? AND state!='published'",
            (reason, batch),
        )
        db.execute(
            "UPDATE builds SET state='failed',error=? WHERE generation IN "
            "(SELECT generation FROM batch_members WHERE batch=?) "
            "AND state IN ('building','queued')",
            (reason, batch),
        )

    def fail_batch(self, batch, reason):
        with self.db() as db:
            self._fail_batch(db, batch, reason)

    def seal_batch(self, batch):
        outcome = {}
        with self.db() as db:
            db.execute(
                "UPDATE batches SET state='queued' WHERE id=? AND state='building'", (batch,)
            )
            self._settle_batch(db, batch, outcome=outcome)
        # The publish itself runs when the transaction above commits, so the
        # answer is only known now.
        return bool(outcome.get("published"))

    def batch_state(self, batch):
        with self.db() as db:
            row = db.execute("SELECT * FROM batches WHERE id=?", (batch,)).fetchone()
            if row is None:
                raise ValueError("Unknown publication batch")
            return dict(row)

    def _has_all_receipts(self, db, row):
        actual = {
            r[0]
            for r in db.execute(
                "SELECT child FROM receipts WHERE generation=?", (row["generation"],)
            )
        }
        return row["state"] == "queued" and actual == set(json.loads(row["expected"]))

    def _member_batch(self, db, generation):
        row = db.execute(
            "SELECT batch FROM batch_members WHERE generation=?", (generation,)
        ).fetchone()
        return row[0] if row else None

    def _member_ready(self, db, generation):
        """True when a member was already counted down: sealed with every receipt.

        No member may decrement the batch counter twice, so every decrement site
        asks this first.
        """
        return self._has_all_receipts(db, self._build(db, generation))

    def _decrement_batch(self, db, batch):
        # ``remaining>0`` also leaves legacy NULL batches untouched (NULL>0 is not
        # true), so a member added to a pre-counter batch stays on the original
        # whole-batch scan.
        db.execute(
            "UPDATE batches SET remaining=remaining-1 WHERE id=? AND remaining>0", (batch,)
        )

    def _receipt_recorded(self, db, row, inserted, outcome=None):
        """Advance the batch counter after a receipt or a seal lands.

        Shared by every acknowledge path (vector children, the lexical marker,
        the metadata marker) and by ``seal``, so the counter stays exact no
        matter which child of a member arrives last.
        """
        batch = self._member_batch(db, row["generation"])
        if batch is None:
            return self._publish_if_complete(db, row, outcome)
        ready = self._member_ready(db, row["generation"])
        if inserted and ready:
            self._decrement_batch(db, batch)
        # The counter replaced the whole-batch scan a receipt used to run, so
        # the member this receipt belongs to is validated on its own. Without
        # it an obsolete chunk stayed 'queued' -- the chunk path reads that
        # state to report the write as superseded or failed -- and its batch
        # waited for a publication that could never include it.
        self._mark_invalid_member(db, self._build(db, row["generation"]), batch)
        return self._settle_batch(db, batch, outcome=outcome)

    def _mark_invalid_member(self, db, row, batch):
        """Retire a member the publication gate refuses, on its own receipt.

        Only sealed batches are validated, exactly as the whole-batch scan
        gated on ``state == 'queued'``: before the producer finishes, a source
        write is still expected to be in flight. The counter is left alone --
        the member is still counted until its receipts complete, and the
        caller's ready check does that decrement.
        """
        state_row = db.execute("SELECT state FROM batches WHERE id=?", (batch,)).fetchone()
        if state_row is None or state_row[0] != "queued":
            return False
        if row["state"] not in {"building", "queued"}:
            return False
        if not self._has_all_receipts(db, row):
            # Still producing: ``_invalid_reason`` also reports in-flight source
            # writes and draft mismatches, which resolve on their own. Retiring
            # the member here published its siblings without it (observed as an
            # order-dependent flake in the batch tests), so a member is judged
            # only once its receipts are complete.
            return False
        reason = self._invalid_reason(db, row)
        if not reason:
            return False
        state = "superseded" if reason.startswith("Newer") else "failed"
        return bool(
            db.execute(
                "UPDATE builds SET state=?,error=? WHERE generation=? AND state IN ('building','queued')",
                (state, reason, row["generation"]),
            ).rowcount
        )

    def _settle_batch(self, db, batch, reason=None, outcome=None):
        """Publish only when the counter proves nothing is outstanding.

        The counter replaces a full scan of the batch -- thousands of members,
        each costing several queries -- that every single receipt used to run
        inside the write transaction, which is what capped concurrent batch
        builds. ``remaining`` is NULL on batches created before the counter
        existed and on members added to them; those keep the original scan.
        """
        group = db.execute(
            "SELECT remaining,state FROM batches WHERE id=?", (batch,)
        ).fetchone()
        if group is None:
            raise ValueError("Unknown publication batch")
        if group["remaining"] is None:
            # Legacy batch (written before the counter existed): no O(1) readiness
            # signal, so keep the original synchronous whole-batch scan.
            published = self._publish_legacy_batch(db, batch)
            if outcome is not None:
                outcome["published"] = published
            return published
        if group["remaining"]:
            return False
        if group["state"] == "queued":
            # The counter proves nothing is outstanding; whether anything is
            # actually publishable (an empty batch is not, a batch left with no
            # live member fails) is decided by the deferred pass, which reports
            # its answer through ``outcome``.
            self._defer_publication(batch=batch, outcome=outcome)
            return True
        if group["state"] == "building":
            # Nothing is outstanding, but the batch is unsealed: either every
            # member is already ready (the producer's seal will publish them) or
            # every member was retired. Only the second case is a dead batch --
            # and it must be aborted on the spot, because a rejected enqueue
            # relies on the abort to stop the remaining members from being
            # built. Retiring the last live member used to fail the batch here.
            live = db.execute(
                "SELECT COUNT(*) FROM batch_members m JOIN builds b ON b.generation=m.generation "
                "WHERE m.batch=? AND b.state NOT IN ('failed','superseded')",
                (batch,),
            ).fetchone()[0]
            if not live:
                self._fail_batch(db, batch, reason or "No batch member could be published")
        return False

    def _invalid_reason(self, db, row):
        try:
            self._assert_not_writing(db, row["account"], row["uri"])
        except FailedPreconditionError as exc:
            return str(exc)
        desired = db.execute(
            "SELECT generation FROM desired WHERE account=? AND namespace=? AND uri=?",
            (row["account"], row["namespace"], row["uri"]),
        ).fetchone()
        if not desired or desired[0] != row["generation"]:
            return "Newer index attempt superseded batch member"
        source = db.execute(
            "SELECT reference FROM sources WHERE generation=?", (row["generation"],)
        ).fetchone()
        if source:
            reference = json.loads(source[0])
            for source_uri, epoch in reference.get("image_context_epochs", {}).items():
                try:
                    self._assert_not_writing(db, row["account"], source_uri)
                except FailedPreconditionError as exc:
                    return str(exc)
                draft = db.execute(
                    "SELECT epoch FROM drafts WHERE account=? AND uri=?", (row["account"], source_uri)
                ).fetchone()
                if draft is None or draft[0] != epoch:
                    return "Image context changed during index build"
            if "draft_epoch" in reference:
                draft = db.execute(
                    "SELECT epoch,sha FROM drafts WHERE account=? AND uri=?",
                    (row["account"], row["uri"]),
                ).fetchone()
                if (
                    draft is None
                    or draft[0] != reference["draft_epoch"]
                    or draft[1] != reference["sha256"]
                ):
                    return "Source changed during index build"
        semantic = db.execute(
            "SELECT reference FROM semantic_sources WHERE generation=?", (row["generation"],)
        ).fetchone()
        if semantic:
            for level, reference in json.loads(semantic[0]).items():
                sidecar = row["uri"].rstrip("/") + (
                    "/.abstract.md" if level == "0" else "/.overview.md"
                )
                try:
                    self._assert_not_writing(db, row["account"], sidecar)
                except FailedPreconditionError as exc:
                    return str(exc)
                draft = db.execute(
                    "SELECT epoch,sha FROM drafts WHERE account=? AND uri=?",
                    (row["account"], sidecar),
                ).fetchone()
                if (
                    draft is None
                    or draft[0] != reference["draft_epoch"]
                    or draft[1] != reference["sha256"]
                ):
                    return "Semantic source changed during index build"
        return self.metadata_invalid_reason(db, row)

    def _publish_legacy_rows(self, db, rows):
        """Publish rows from a batch created before the ``remaining`` counter.

        Kept for databases whose batches predate the counter: they carry no O(1)
        readiness signal, so they publish with the original synchronous walk
        (all of it inside the caller's write transaction).
        """
        first = rows[0]
        head = db.execute(
            "SELECT snapshot FROM heads WHERE account=? AND namespace=?",
            (first["account"], first["namespace"]),
        ).fetchone()
        manifest = (
            # Copy the memoized manifest before mutating: the cache holds the
            # exact mapping that snapshot decodes to, and re-decoding it here
            # cost ~154ms inside the publication write transaction.
            dict(_snapshot_manifest(db, head[0]))
            if head
            else {}
        )
        # A tree-scale batch publishes tens of thousands of members in one
        # transaction; two lookups per member inside that transaction multiplied
        # into ~90k statement prepares on a 10GB database and held the write lock
        # for the whole walk. Prefetch both reference tables in two indexed scans,
        # chunked under SQLite's host-parameter ceiling.
        sources = {}
        semantics = {}
        generations = [row["generation"] for row in rows]
        for chunk_start in range(0, len(generations), 900):
            chunk = generations[chunk_start:chunk_start + 900]
            placeholders = ",".join("?" * len(chunk))
            sources.update(
                (row[0], row[1])
                for row in db.execute(
                    f"SELECT generation,reference FROM sources WHERE generation IN ({placeholders})",
                    chunk,
                )
            )
            semantics.update(
                (row[0], row[1])
                for row in db.execute(
                    f"SELECT generation,reference FROM semantic_sources WHERE generation IN ({placeholders})",
                    chunk,
                )
            )
        for row in rows:
            item = {"generation": row["generation"], "plan": row["plan"]}
            source = sources.get(row["generation"])
            if source:
                item["content"] = json.loads(source)
            semantic = semantics.get(row["generation"])
            if semantic:
                item["semantic_contents"] = json.loads(semantic)
            manifest[row["uri"]] = item
        # One UPDATE per member multiplied into a statement per published row
        # while the write lock was held; chunk them under SQLite's parameter
        # ceiling instead (generations are already deduplicated above).
        for chunk_start in range(0, len(generations), 900):
            chunk = generations[chunk_start:chunk_start + 900]
            placeholders = ",".join("?" * len(chunk))
            db.execute(
                f"UPDATE builds SET state='published' WHERE generation IN ({placeholders})",
                chunk,
            )
        snapshot = uuid4().hex
        db.execute(
            "INSERT INTO snapshots VALUES (?,?,?,?,?)",
            (snapshot, first["account"], first["namespace"], encode_manifest(manifest), time.time()),
        )
        db.execute(
            "INSERT OR REPLACE INTO heads VALUES (?,?,?)",
            (first["account"], first["namespace"], snapshot),
        )
        # Hand the mapping we just stored to the read caches: every publish mints
        # a new snapshot id, so readers would otherwise re-gunzip this ~13MB blob
        # (~154ms of CPU each, once per file during reindex).
        record_snapshot_manifest(snapshot, manifest)
    def _publish_legacy_batch(self, db, batch):
        """Synchronous publication for a batch with ``remaining IS NULL``."""
        group = db.execute("SELECT state FROM batches WHERE id=?", (batch,)).fetchone()
        if group is None:
            raise ValueError("Unknown publication batch")
        if group[0] != "queued":
            return False
        rows = db.execute(
            "SELECT b.* FROM builds b JOIN batch_members m ON m.generation=b.generation "
            "WHERE m.batch=?",
            (batch,),
        ).fetchall()
        if not rows:
            return False
        # A member that failed or was superseded is dropped from this publication;
        # the remaining members still publish once every one of them is ready.
        # Only a batch with no publishable member fails as a whole.
        healthy = []
        for row in rows:
            reason = self._invalid_reason(db, row)
            if row["state"] in {"failed", "superseded"} or reason:
                if reason and row["state"] not in {"failed", "superseded"}:
                    state = "superseded" if reason.startswith("Newer") else "failed"
                    db.execute(
                        "UPDATE builds SET state=?,error=? WHERE generation=?",
                        (state, reason, row["generation"]),
                    )
                continue
            healthy.append(row)
        if not all(self._has_all_receipts(db, row) for row in healthy):
            return False
        if not healthy:
            self._fail_batch(db, batch, "No batch member could be published")
            return False
        self._publish_legacy_rows(db, healthy)
        db.execute("UPDATE batches SET state='published' WHERE id=?", (batch,))
        return True
    def _publish_if_complete(self, db, row, outcome=None):
        member = db.execute(
            "SELECT batch FROM batch_members WHERE generation=?", (row["generation"],)
        ).fetchone()
        if member:
            # Delegate to the batch even when this member is not ready: a member
            # that turned invalid is dropped there, letting healthy members publish.
            return self._settle_batch(db, member[0], outcome=outcome)
        if not self._has_all_receipts(db, row):
            return False
        reason = self._invalid_reason(db, row)
        if reason:
            state = "superseded" if reason.startswith("Newer") else "failed"
            db.execute(
                "UPDATE builds SET state=?,error=? WHERE generation=?",
                (state, reason, row["generation"]),
            )
            return False
        self._defer_publication(generation=row["generation"], outcome=outcome)
        return True

    def _defer_publication(self, batch=None, generation=None, outcome=None):
        """Publish after the current write transaction commits.

        Everything expensive about publishing -- the member walk, the manifest
        merge and the json/gzip encode of a ~35MB snapshot -- used to happen inside
        BEGIN IMMEDIATE, so one publish held the single writer for ~0.7s and every
        other writer queued behind it. The work is deferred to a read-only pass
        plus a short commit; a later receipt (or the periodic sweep) retries if
        this process dies in between.
        """
        pending = getattr(self._local, "pending_publications", None)
        if pending is None:
            pending = []
            self._local.pending_publications = pending
        pending.append((batch, generation, outcome))
        return True

    def _drain_deferred_publications(self):
        pending = getattr(self._local, "pending_publications", None)
        if not pending:
            return
        self._local.pending_publications = []
        for batch, generation, outcome in pending:
            try:
                if batch is not None:
                    published = self._publish_ready_batch(batch)
                elif generation is not None:
                    published = self._publish_ready_build(generation)
                else:
                    published = False
            except Exception:
                # Never fail the write that triggered this: the next receipt, the
                # next seal or the sweep publishes the batch instead.
                logger.exception(
                    "deferred publication failed (batch=%s generation=%s)",
                    batch,
                    generation,
                )
                published = False
            if outcome is not None:
                outcome["published"] = published

    def _prepare_publication(self, batch=None, generation=None):
        try:
            with self.db(readonly=True) as db:
                return self._collect_publication(db, batch=batch, generation=generation)
        except sqlite3.OperationalError as exc:
            if "readonly" not in str(exc).lower():
                raise
            # The read-only pass tried to write (settling a dead write fence).
            # Rare; redo the preparation inside a write transaction.
            with self.db() as db:
                return self._collect_publication(db, batch=batch, generation=generation)

    def _publish_ready_batch(self, batch):
        """Publish a ready batch, rebuilding the plan if the head moved.

        The plan is collected outside the writer lock (that is what keeps the
        lock hold short in the common case); if another publish landed in
        between, the plan is *rebuilt inside the lock* rather than dropped, so a
        ready publication is never lost to the read/write gap. Once the lock is
        held no other writer can move the head, so a second round always settles.
        """
        plan = self._prepare_publication(batch=batch)
        if plan is None:
            return False
        with self.db() as db:
            if self._apply_publication(db, plan):
                return True
            refreshed = self._collect_publication(db, batch=batch)
            if refreshed is None:
                return False
            if self._apply_publication(db, refreshed):
                return True
        logger.warning("Publication of batch %s kept losing the head race; deferring to the sweep", batch)
        return False

    def _publish_ready_build(self, generation):
        plan = self._prepare_publication(generation=generation)
        if plan is None:
            return False
        with self.db() as db:
            if self._apply_publication(db, plan):
                return True
            refreshed = self._collect_publication(db, generation=generation)
            if refreshed is None:
                return False
            if self._apply_publication(db, refreshed):
                return True
        logger.warning("Publication of generation %s kept losing the head race", generation)
        return False

    def _collect_publication(self, db, batch=None, generation=None):
        """Read-only: decide what a batch (or one build) would publish.

        Returns ``None`` when there is nothing to publish yet, or a plan carrying
        everything the short commit needs: the members to mark published the
        members that turned invalid, the new snapshot id and its encoded blob.
        """
        invalid = []
        if batch is not None:
            group = db.execute("SELECT state FROM batches WHERE id=?", (batch,)).fetchone()
            if group is None or group[0] != "queued":
                return None
            rows = db.execute(
                "SELECT b.* FROM builds b JOIN batch_members m ON m.generation=b.generation "
                "WHERE m.batch=?",
                (batch,),
            ).fetchall()
            if not rows:
                return None
            healthy = []
            for row in rows:
                if row["state"] in {"failed", "superseded"}:
                    continue
                if not self._has_all_receipts(db, row):
                    # Still producing: judging it now would let a transient
                    # ``_invalid_reason`` drop a member that is simply not done
                    # yet, and the batch would publish without it.
                    return None
                reason = self._invalid_reason(db, row)
                if reason:
                    state = "superseded" if reason.startswith("Newer") else "failed"
                    invalid.append((row["generation"], state, reason))
                    continue
                healthy.append(row)
            if not healthy:
                # Only a batch with no publishable member fails as a whole; the
                # commit pass applies the abort so nothing else was published.
                return {"batch": batch, "fail": "No batch member could be published"}
            if not all(self._has_all_receipts(db, row) for row in healthy):
                return None
            account = healthy[0]["account"]
            namespace = healthy[0]["namespace"]
        else:
            row = self._build(db, generation)
            if row is None or not self._has_all_receipts(db, row):
                return None
            reason = self._invalid_reason(db, row)
            if reason:
                state = "superseded" if reason.startswith("Newer") else "failed"
                return {"batch": None, "invalid": [(generation, state, reason)]}
            healthy = [row]
            account = row["account"]
            namespace = row["namespace"]

        head = db.execute(
            "SELECT snapshot FROM heads WHERE account=? AND namespace=?", (account, namespace)
        ).fetchone()
        base = head[0] if head else None
        # A publish changes a few URIs but the manifest is tens of megabytes, so
        # track what changed and store a delta against the base. The full
        # manifest is still built here for the cache and for callers that need
        # it, but the *stored* blob is the delta: re-serialising the full
        # manifest measured 184ms of JSON + 398ms of gzip (586ms) on a 188k-entry
        # account, per publish, for a change set of a few entries.
        manifest = dict(_snapshot_manifest(db, base)) if base else {}
        # Snapshot the base keys *before* the loop mutates ``manifest``. Comparing
        # against the mutated dict would alias it and report an empty change set
        # every time.
        base_keys = frozenset(manifest) if base else frozenset()

        sources = {}
        semantics = {}
        generations = [row["generation"] for row in healthy]
        for chunk_start in range(0, len(generations), 900):
            chunk = generations[chunk_start:chunk_start + 900]
            placeholders = ",".join("?" * len(chunk))
            sources.update(
                (row[0], row[1])
                for row in db.execute(
                    f"SELECT generation,reference FROM sources WHERE generation IN ({placeholders})",
                    chunk,
                )
            )
            semantics.update(
                (row[0], row[1])
                for row in db.execute(
                    f"SELECT generation,reference FROM semantic_sources WHERE generation IN ({placeholders})",
                    chunk,
                )
            )
        for row in healthy:
            item = {"generation": row["generation"], "plan": row["plan"]}
            source = sources.get(row["generation"])
            if source:
                item["content"] = json.loads(source)
            semantic = semantics.get(row["generation"])
            if semantic:
                item["semantic_contents"] = json.loads(semantic)
            manifest[row["uri"]] = item
        base_manifest = _snapshot_manifest(db, base) if base else {}
        changed = {
            uri: item
            for uri, item in manifest.items()
            if base_manifest.get(uri) != item
        }
        dropped = [uri for uri in base_keys if uri not in manifest]
        # A delta chains onto its parent, so an unbounded run of publishes would
        # build an unbounded chain that every read has to replay. Cap the depth
        # and fall back to a full manifest periodically; that keeps reads O(1) in
        # the common case and O(depth) at worst.
        use_delta = base and (changed or dropped) and _delta_depth(db, base) < _MANIFEST_DELTA_MAX_DEPTH
        blob = (
            encode_manifest_delta(base, changed, dropped)
            if use_delta
            else encode_manifest(manifest)
        )
        return {
            "batch": batch,
            "account": account,
            "namespace": namespace,
            "base": base,
            "snapshot": uuid4().hex,
            "manifest": manifest,
            "blob": blob,
            "published": generations,
            "invalid": invalid,
        }

    def _apply_publication(self, db, plan):
        """Short write transaction: apply a plan prepared outside the write lock."""
        batch = plan.get("batch")
        if plan.get("fail"):
            self._fail_batch(db, batch, plan["fail"])
            return False
        account = plan["account"]
        namespace = plan["namespace"]
        if batch is not None:
            group = db.execute("SELECT state FROM batches WHERE id=?", (batch,)).fetchone()
            if group is None or group[0] != "queued":
                return False
        head = db.execute(
            "SELECT snapshot FROM heads WHERE account=? AND namespace=?", (account, namespace)
        ).fetchone()
        if (head[0] if head else None) != plan.get("base"):
            # Another publisher moved the head while this plan was prepared; the
            # stale manifest cannot be committed. A retry re-prepares against it.
            return False
        for generation, state, reason in plan.get("invalid") or []:
            db.execute(
                "UPDATE builds SET state=?,error=? WHERE generation=?",
                (state, reason, generation),
            )
        snapshot = plan.get("snapshot")
        if snapshot is None:
            return False
        generations = plan["published"]
        for chunk_start in range(0, len(generations), 900):
            chunk = generations[chunk_start:chunk_start + 900]
            placeholders = ",".join("?" * len(chunk))
            db.execute(
                f"UPDATE builds SET state='published' WHERE generation IN ({placeholders})",
                chunk,
            )
        db.execute(
            "INSERT INTO snapshots VALUES (?,?,?,?,?)",
            (snapshot, account, namespace, plan["blob"], time.time()),
        )
        db.execute(
            "INSERT OR REPLACE INTO heads VALUES (?,?,?)",
            (account, namespace, snapshot),
        )
        if batch is not None:
            db.execute("UPDATE batches SET state='published' WHERE id=?", (batch,))
        # Hand the mapping we just stored to the read caches: every publish mints
        # a new snapshot id, so readers would otherwise re-gunzip this blob.
        record_snapshot_manifest(snapshot, plan["manifest"])
        return True

    def can_write(self, generation, fields):
        with self.db(readonly=True) as db:
            row = self._build(db, generation)
            if (
                row["account"] != fields.get("account_id")
                or row["uri"] != fields.get("uri")
                or row["plan"] != fields.get("index_plan_id")
            ):
                raise ValueError("Generation identity does not match queued chunk")
            if publication_member_id(fields) not in json.loads(row["expected"]):
                raise ValueError("Unexpected generation chunk")
            desired = db.execute(
                "SELECT generation FROM desired WHERE account=? AND namespace=? AND uri=?",
                (row["account"], row["namespace"], row["uri"]),
            ).fetchone()
            return row["state"] in {"building", "queued"} and desired and desired[0] == generation

    def seal(self, generation):
        outcome = {}
        with self.db() as db:
            row = self._build(db, generation)
            sealed = 0
            if row["state"] == "building":
                sealed = db.execute(
                    "UPDATE builds SET state='queued' WHERE generation=? AND state='building'",
                    (generation,),
                ).rowcount
            self._receipt_recorded(db, self._build(db, generation), sealed, outcome)
        return bool(outcome.get("published"))

    def acknowledge(self, generation, child):
        outcome = {}
        with self.db() as db:
            row = self._build(db, generation)
            if child not in json.loads(row["expected"]):
                raise ValueError("Unexpected child cannot satisfy publication")
            if row["state"] not in {"building", "queued"}:
                return False
            inserted = db.execute(
                "INSERT OR IGNORE INTO receipts VALUES (?,?)", (generation, child)
            ).rowcount
            self._receipt_recorded(db, self._build(db, generation), inserted, outcome)
        return bool(outcome.get("published"))

    def record_receipt(self, generation, child):
        """Insert one receipt row without advancing or publishing.

        The embedding queue writes the vector first and then records the
        receipt. On the single-writer store each of those is a
        ``BEGIN IMMEDIATE``, and the lock -- not the work -- is the ceiling: a
        100-document drain took 415 write transactions whose lock wait summed to
        383s while the model needed 15ms per document.

        This is the batched-write half. The caller inserts rows for a whole
        dequeued batch in one transaction, then settles each touched generation
        with ``settle_after_receipts`` outside that transaction. Splitting it
        keeps the expensive publish walk and the correctness checks off the
        writer lock, exactly as the single-receipt path already does.
        """
        with self.db() as db:
            row = self._build(db, generation)
            if child not in json.loads(row["expected"]):
                raise ValueError("Unexpected child cannot satisfy publication")
            if row["state"] not in {"building", "queued"}:
                return False
            return bool(
                db.execute(
                    "INSERT OR IGNORE INTO receipts VALUES (?,?)", (generation, child)
                ).rowcount
            )

    def record_and_settle_receipts(self, entries):
        """Record a whole batch's receipts and settle them in ONE transaction.

        The embedding queue's hot path. ``entries`` is a sequence of
        ``(generation, child)``. Compared with the per-chunk path -- two lock
        acquisitions per chunk -- this is two per batch, and now one: recording
        and settling are safe to combine because settling does not publish
        inline. ``_receipt_recorded`` -> ``_settle_batch`` -> ``_defer_publication``
        only *queues* the publication on the thread-local list, which
        ``_drain_deferred_publications`` runs after this transaction commits, so
        the expensive manifest walk and encode still happen outside the writer.

        The outer ``db()`` block is what makes that work: ``settle_many_after_receipts``
        opens its own ``db()``, which joins the enclosing transaction because
        ``db()`` is depth-aware per thread.

        Returns ``{generation: state}`` for every generation the caller should
        report on. A child the generation does not expect raises, exactly as the
        single-receipt path raises: the batch is rejected as a whole rather than
        silently dropping a member.
        """
        with self.db():
            recorded, touched = self.record_receipts(entries)
            if not touched:
                return {}
            return self.settle_many_after_receipts(touched)

    def record_receipts(self, entries):
        """Insert receipts for many generations in ONE write transaction.

        This is the batched form of ``record_receipt``: the embedding queue
        records a whole dequeued batch's receipts under a single
        ``BEGIN IMMEDIATE`` instead of one per chunk. ``entries`` is a sequence
        of ``(generation, child)``.

        Each row is validated exactly as the single-receipt path validates it --
        an unexpected child still raises, a non-writable generation is still
        skipped -- so what lands in ``receipts`` is identical. Only the number of
        lock acquisitions changes. Returns ``(recorded, touched)``: the number of
        rows inserted and the set of generations whose receipts were touched and
        therefore need ``settle_after_receipts``.

        Callers must settle every touched generation afterwards; that is where
        the counter advances and the deferred publication is scheduled, and it
        must happen outside this transaction so a publish never holds the lock.
        """
        recorded = 0
        touched = []
        with self.db() as db:
            for generation, child in entries:
                row = self._build(db, generation)
                if child not in json.loads(row["expected"]):
                    raise ValueError("Unexpected child cannot satisfy publication")
                if row["state"] not in {"building", "queued"}:
                    continue
                inserted = db.execute(
                    "INSERT OR IGNORE INTO receipts VALUES (?,?)", (generation, child)
                ).rowcount
                if inserted:
                    recorded += 1
                    touched.append(generation)
        # One entry per generation, not per receipt. A generation owns several
        # chunks, so appending it once per inserted receipt would make
        # ``settle_many_after_receipts`` advance its batch counter several times
        # for one member -- the counter went negative and the batch never
        # settled, which surfaced as "Newer index attempt superseded batch
        # member" retries on an otherwise healthy chunk.
        return recorded, list(dict.fromkeys(touched))

    def settle_many_after_receipts(self, generations):
        """Advance counters and schedule publication for many generations at once.

        The settle half of the batched receipt path. Every generation whose
        receipts were just inserted needs its batch counter advanced exactly
        once and its publication deferred; doing that per generation would put
        the single-writer lock back in the loop for each one. The per-generation
        work is unchanged -- ``_receipt_recorded`` still runs for each -- only
        the enclosing transaction is shared.

        Returns ``{generation: state}`` for the caller to report.
        """
        outcome = {}
        states = {}
        with self.db() as db:
            for generation in generations:
                row = self._build(db, generation)
                if row["state"] in {"building", "queued"}:
                    # ``inserted=1`` means "this call recorded at least one new
                    # receipt for the member"; ``_receipt_recorded`` only
                    # decrements the batch counter when the member is also fully
                    # ready, so a partly-received generation leaves it alone.
                    self._receipt_recorded(db, row, 1, outcome)
                states[generation] = dict(self._build(db, generation))
        return states

    def settle_after_receipts(self, generation):
        """Advance the counter and publish for a generation whose receipts landed.

        The second half of the batched receipt path: called once per touched
        generation after the bulk insert commits, so the counter advance and the
        deferred publication still happen exactly once per generation instead of
        once per receipt.
        """
        outcome = {}
        with self.db() as db:
            row = self._build(db, generation)
            if row["state"] not in {"building", "queued"}:
                return dict(row)
            self._receipt_recorded(db, row, 1, outcome)
            return dict(self._build(db, generation))

    def acknowledge_and_state(self, generation, child):
        """``acknowledge`` followed by ``state`` inside one transaction.

        The chunk write path calls both back to back for every indexed chunk,
        and each ``db()`` block is its own ``BEGIN IMMEDIATE`` on the single
        writer, so the pair paid two lock acquisitions per chunk while a bulk
        publication held the writer.  Folding them halves that on the hottest
        path; the returned mapping is what a following ``state`` call returned.
        """
        outcome = {}
        with self.db() as db:
            row = self._build(db, generation)
            if child not in json.loads(row["expected"]):
                raise ValueError("Unexpected child cannot satisfy publication")
            if row["state"] in {"building", "queued"}:
                inserted = db.execute(
                    "INSERT OR IGNORE INTO receipts VALUES (?,?)", (generation, child)
                ).rowcount
                self._receipt_recorded(db, self._build(db, generation), inserted, outcome)
            return dict(self._build(db, generation))

    def prune_snapshots(self, older_than_seconds=3600.0, limit=64):
        """Delete manifest snapshots nothing can reach any more.

        Every publication writes a *complete* gzipped manifest, so the table
        grows by roughly ten megabytes per publish while only one snapshot per
        account is reachable from ``heads``.  The live store reached 1,497
        snapshots holding 30GB of manifest text, of which 1,494 were unreachable
        history (``heads`` has one row per account, ``leases`` was empty).

        A snapshot is droppable only when no head points at it, no unexpired
        lease pins it, and it is older than ``older_than_seconds`` -- well past
        the query-lease TTL -- so a reader that captured it cannot lose its view
        mid-query.  Deletion is bounded per call: the sweep must never hold the
        single writer for long.
        """
        now = time.time()
        cutoff = now - max(0.0, float(older_than_seconds))
        with self.db() as db:
            keep = {row["snapshot"] for row in db.execute("SELECT snapshot FROM heads")}
            if not keep:
                # An empty registry is a bug, not a licence to delete history.
                return 0
            for table in ("leases", "gc_leases"):
                for row in db.execute(f"SELECT snapshots, expires FROM {table}"):
                    expires = row["expires"]
                    if expires is None or float(expires) <= now:
                        continue
                    pinned = row["snapshots"]
                    if not pinned:
                        continue
                    try:
                        keep.update(json.loads(pinned))
                    except (TypeError, ValueError):
                        keep.add(pinned)
            # Query-view leases moved to the dedicated store; their pins must
            # protect exactly like the legacy rows above.
            keep.update(self._query_leases.live_snapshot_ids())
            # capture() no longer sweeps expired gc_leases (it must not take a
            # publication write transaction at all), so the periodic prune owns
            # that housekeeping now.
            db.execute("DELETE FROM gc_leases WHERE expires<?", (now,))
            # A recovery record names the snapshot the rollback must restore, so
            # it pins its manifest until the recovery itself is settled.
            keep.update(
                row["snapshot"]
                for row in db.execute("SELECT snapshot FROM source_recoveries")
            )
            # A snapshot stored as a delta needs its parent chain to resolve, so
            # every ancestor of a kept snapshot must survive too. Without this,
            # pruning deleted a parent that a live head still chained through and
            # every read of that head raised "Pinned snapshot dependency is
            # missing".
            keep = _with_delta_ancestors(db, keep)
            victims = [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM snapshots WHERE created < ? ORDER BY created LIMIT ?",
                    (cutoff, max(1, int(limit))),
                )
                if row["id"] not in keep
            ]
            if not victims:
                return 0
            marks = ",".join("?" for _ in victims)
            db.execute(f"DELETE FROM snapshots WHERE id IN ({marks})", victims)
        return len(victims)

    def _reap_stale_batch_members(self, db, max_age_seconds: float) -> int:
        """Fail batch members whose producer never finished their chunk set.

        A member stuck in ``building`` means the process writing its chunks
        exited without confirming them (restart, killed job), so
        ``remaining`` never reaches zero and the batch can never settle: every
        *other* member of that batch - often hundreds - stays unpublished for
        good.  Only members older than ``max_age_seconds`` whose batch has seen
        no new member for that same window are failed: a live producer keeps
        adding members, and an embedding backlog can legitimately delay a young
        member for tens of minutes.
        """

        cutoff = time.time() - max(0.0, float(max_age_seconds))
        rows = db.execute(
            """
            SELECT m.batch AS batch, m.generation AS generation
            FROM batch_members m
            JOIN builds b ON b.generation = m.generation
            JOIN batches bt ON bt.id = m.batch
            WHERE b.state = 'building'
              AND b.created < ?
              AND bt.state = 'building'
              AND COALESCE((
                  SELECT MAX(b2.created) FROM batch_members m2
                    JOIN builds b2 ON b2.generation = m2.generation
                   WHERE m2.batch = m.batch), 0) < ?
            """,
            (cutoff, cutoff),
        ).fetchall()
        for row in rows:
            reaped = db.execute(
                "UPDATE builds SET state='failed', error=? "
                "WHERE generation=? AND state='building'",
                (
                    "stale build: the producer exited before its batch could settle",
                    row["generation"],
                ),
            ).rowcount
            if not reaped:
                continue
            db.execute(
                "UPDATE batches SET remaining=remaining-1 WHERE id=? AND remaining>0",
                (row["batch"],),
            )
        # A *sealed* member can be stuck the same way: its batch counted it ready
        # (remaining already decremented) but some receipts never arrived, so
        # `_has_all_receipts` is False forever and `_collect_publication` returns
        # None for the whole batch. Every sibling then waits for a publication
        # that can never happen -- observed as "Retried N queued publication
        # batch(es) whose publish never landed" repeating every 15s, with one
        # semantic message blocked behind it. Fail those members so the batch can
        # settle without them.
        stuck = db.execute(
            """
            SELECT m.batch AS batch, m.generation AS generation
            FROM batch_members m
            JOIN builds b ON b.generation = m.generation
            JOIN batches bt ON bt.id = m.batch
            WHERE b.state = 'queued'
              AND b.created < ?
              AND bt.state IN ('queued', 'building')
              AND (SELECT COUNT(*) FROM receipts r WHERE r.generation = b.generation)
                  < json_array_length(json_extract(b.expected, '$'))
            LIMIT 500
            """,
            (cutoff,),
        ).fetchall()
        for row in stuck:
            reaped = db.execute(
                "UPDATE builds SET state='failed', error=? "
                "WHERE generation=? AND state='queued'",
                (
                    "stale member: receipts never completed, batch could not settle",
                    row["generation"],
                ),
            ).rowcount
            if reaped:
                db.execute(
                    "UPDATE batches SET remaining=MAX(remaining-1,0) WHERE id=?",
                    (row["batch"],),
                )
        return len(rows) + len(stuck)

    # Errors a producer leaves behind when it is killed rather than failing.
    INTERRUPTED_GENERATION_ERRORS = (
        "producer interrupted",
        "server restart",
        "stale build",
        "orphaned",
        "owning server process exited",
    )

    def interrupted_uris(self, limit=2000):
        """URIs whose newest generation died with its producer.

        A restart orphans whatever was in flight: those generations stay
        ``failed`` and nothing retries them, so a tree can sit half-indexed until
        an operator notices.  Live: one interrupted run left 14,688 URIs for
        40 hours, and the acceptance run needed a hand-written re-fire script.
        Returns ``(account, uri, error, created)`` newest-first so a repair pass
        can be bounded and reported.
        """
        clauses = " OR ".join("b.error LIKE ?" for _ in self.INTERRUPTED_GENERATION_ERRORS)
        params = [f"%{marker}%" for marker in self.INTERRUPTED_GENERATION_ERRORS]
        params.append(int(limit))
        with self.db(readonly=True) as db:
            rows = db.execute(
                f"""
                SELECT b.account, b.uri, b.error, b.created
                FROM builds b
                JOIN (SELECT uri, MAX(created) AS newest FROM builds GROUP BY uri) m
                  ON m.uri = b.uri AND m.newest = b.created
                WHERE b.state = 'failed' AND ({clauses})
                ORDER BY b.created DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def published_chunks(self, account, uri):
        """``(generation, [(chunk_id, content_revision)])`` for a URI's live generation.

        ``content_revision`` is the sha256 of the source text a generation was
        built from, so comparing it with the file's current hash answers "has this
        content changed?" without spending an embedding call. Returns ``None``
        when the URI has no live generation or no published chunks.
        """
        with self.db(readonly=True) as db:
            row = db.execute(
                "SELECT generation FROM desired WHERE account=? AND uri=? LIMIT 1",
                (account, uri),
            ).fetchone()
            if row is None:
                return None
            generation = row["generation"]
            rows = db.execute(
                "SELECT child, json_extract(payload,'$.content_revision') AS revision "
                "FROM publication_lexical WHERE generation=? AND uri=?",
                (generation, uri),
            ).fetchall()
        if not rows:
            return None
        return generation, [(row["child"], row["revision"]) for row in rows]

    def retired_generations(self, older_than_seconds=86400.0, limit=2000):
        """Generations whose vectors and rows nothing can reach any more.

        A generation is retired once it is no longer the desired generation of
        its URI, no snapshot a live lease pins still references it, and its build
        settled longer ago than the grace period.

        Live measurement on the enterprise tree: 530,826 of 714,532 generations
        were retired, and their 957,757 vectors were 73.5% of a 78 GB store --
        and because retrieval never filters by generation they were also still
        *searchable*, so stale copies of a URI competed with the current one.
        """
        if (
            isinstance(older_than_seconds, bool)
            or not isinstance(older_than_seconds, (int, float))
            or not math.isfinite(older_than_seconds)
            or older_than_seconds < 0
        ):
            raise ValueError('Generation grace period must be finite and nonnegative')
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError('Generation scan must be a positive integer')
        cutoff = time.time() - float(older_than_seconds)
        with self.db(readonly=True) as db:
            protected = self._protected_generations(db)
            rows = db.execute(
                "SELECT b.generation, b.account, b.uri FROM builds b "
                "WHERE b.created < ? AND b.state IN ('published','superseded','failed') "
                "  AND NOT EXISTS (SELECT 1 FROM desired d WHERE d.generation=b.generation) "
                "ORDER BY b.created LIMIT ?",
                (cutoff, limit),
            ).fetchall()
        return [dict(row) for row in rows if row['generation'] not in protected]

    def prune_settled_builds(self, older_than_seconds=604800.0, limit=20000):
        """Drop failed/superseded build rows that only history can still want.

        ``builds`` is append-only: live, 653,922 rows / 337 MB, and every pass
        over a tree adds a superseded row per record on top. What the acceptance
        gate, the interrupted-generation scan and the resume pass read is the
        *newest* row per URI, so a settled row is droppable once a newer row
        exists for its URI and it is past the retention window.

        ``failed`` rows are kept while they are the newest for their URI: that is
        exactly the state the resume pass retries.
        """
        if (
            isinstance(older_than_seconds, bool)
            or not isinstance(older_than_seconds, (int, float))
            or not math.isfinite(older_than_seconds)
            or older_than_seconds < 0
        ):
            raise ValueError('Build retention must be finite and nonnegative')
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError('Build prune batch must be a positive integer')
        cutoff = time.time() - float(older_than_seconds)
        with self.db() as db:
            removed = db.execute(
                "DELETE FROM builds WHERE generation IN ("
                "  SELECT generation FROM builds "
                "  WHERE state IN ('failed','superseded') AND created < ? "
                "    AND EXISTS (SELECT 1 FROM builds newer "
                "                WHERE newer.uri = builds.uri AND newer.created > builds.created) "
                "  ORDER BY created LIMIT ?)",
                (cutoff, limit),
            ).rowcount
        return removed or 0

    def _reap_abandoned_members(self, db, max_idle_seconds: float) -> int:
        """Fail members whose receipt set stopped growing.

        Distinct from ``_reap_stale_batch_members``: that one waits for
        ``STALE_BUILD_SECONDS`` because a live producer legitimately holds a
        member for a long time. This one targets the narrower, unambiguous case --
        a member that received *some* receipts and then nothing -- because such a
        member can never complete, and while it sits there its whole batch is
        unpublished.
        """
        cutoff = time.time() - max(0.0, float(max_idle_seconds))
        rows = db.execute(
            """
            SELECT m.batch AS batch, m.generation AS generation
            FROM batch_members m
            JOIN builds b ON b.generation = m.generation
            JOIN batches bt ON bt.id = m.batch
            WHERE b.state = 'queued'
              AND b.created < ?
              AND bt.state IN ('queued', 'building')
              AND (SELECT COUNT(*) FROM receipts r WHERE r.generation = b.generation) > 0
              AND (SELECT COUNT(*) FROM receipts r WHERE r.generation = b.generation)
                  < json_array_length(json_extract(b.expected, '$'))
            LIMIT 500
            """,
            (cutoff,),
        ).fetchall()
        reaped = 0
        for row in rows:
            changed = db.execute(
                "UPDATE builds SET state='failed', error=? "
                "WHERE generation=? AND state='queued'",
                (
                    "abandoned member: receipts stopped arriving, batch could not settle",
                    row["generation"],
                ),
            ).rowcount
            if changed:
                reaped += changed
                db.execute(
                    "UPDATE batches SET remaining=MAX(remaining-1,0) WHERE id=?",
                    (row["batch"],),
                )
        return reaped

    def recover_stranded_batches(self, limit=200, idle_seconds=0.0):
        """Seal batches a lost producer left 'building' with every receipt in.

        ``seal_batch`` is the producer's last act, so a producer that dies first
        (server restart, cancelled job, killed client) leaves a batch nothing
        ever seals again: its members hold their receipts and look ready, but
        ``_apply_publication`` only publishes a 'queued' batch, so every
        chapter in it stays invisible to retrieval for good.  Only batches whose
        receipts are complete (``remaining`` 0) and whose members have all left
        'building' are sealed, which is exactly the work the dead producer had
        already finished -- a partially indexed chapter cannot become visible.

        ``idle_seconds`` guards the periodic sweep against a live producer that
        is between its last receipt and ``seal_batch``: a batch still growing
        (newest member younger than the idle window) is left for its producer.

        It also republishes batches that are 'queued' with every receipt in --
        the counter proves nothing is outstanding, so a publish that was lost
        between its commit and its deferred run is simply run again.
        """
        cutoff = time.time() - max(0.0, float(idle_seconds))
        with self.db() as db:
            reaped = self._reap_stale_batch_members(db, STALE_BUILD_SECONDS)
            # A member whose receipts stopped arriving blocks every sibling in its
            # batch until STALE_BUILD_SECONDS (2h) expires. One member missing a
            # `semantic:*` receipt was measured holding back the other 15
            # documents of its batch for the full two hours, even though all 15
            # were fully receipted and ready.
            #
            # Reap on a much shorter window here, but only for members that have
            # made *no* progress at all: they hold at least one receipt (so the
            # producer did start) and have not gained another for this long. A
            # member still receiving receipts is progressing, and one with none is
            # covered by the building-state reap above.
            reaped += self._reap_abandoned_members(db, STRANDED_MEMBER_SECONDS)
            if reaped:
                logger.warning(
                    "reaped %d stale batch member(s) whose producer exited before "
                    "the batch could settle",
                    reaped,
                )
            stranded = [
                row[0]
                for row in db.execute(
                    """
                    SELECT b.id FROM batches b
                    WHERE b.state='building' AND b.remaining=0
                      AND EXISTS (
                          SELECT 1 FROM batch_members m JOIN builds x
                            ON x.generation = m.generation
                          WHERE m.batch = b.id AND x.state = 'queued')
                      AND NOT EXISTS (
                          SELECT 1 FROM batch_members m JOIN builds x
                            ON x.generation = m.generation
                          WHERE m.batch = b.id AND x.state = 'building')
                      AND COALESCE((
                          SELECT MAX(x.created) FROM batch_members m JOIN builds x
                            ON x.generation = m.generation
                          WHERE m.batch = b.id), 0) <= ?
                    LIMIT ?
                    """,
                    (cutoff, limit),
                )
            ]
            # A batch with no members can never publish: `_collect_publication`
            # returns None for it (`if not rows: return None`), so deferring it
            # again just retries forever. Observed live as "Retried 3 queued
            # publication batch(es) whose publish never landed" repeating every
            # 15s while a semantic message waited on the generation those empty
            # batches were supposed to publish. Settle them as published-empty so
            # the recovery loop stops chasing them.
            empty = [
                row[0]
                for row in db.execute(
                    "SELECT b.id FROM batches b WHERE b.state='queued' "
                    "AND b.remaining=0 "
                    "AND NOT EXISTS (SELECT 1 FROM batch_members m WHERE m.batch=b.id) "
                    "LIMIT ?",
                    (limit,),
                )
            ]
            if empty:
                marks = ",".join("?" * len(empty))
                db.execute(
                    f"UPDATE batches SET state='published' WHERE id IN ({marks})", empty
                )
                logger.info(
                    "Settled %d empty publication batch(es) that could never publish",
                    len(empty),
                )
            unapplied = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM batches WHERE state='queued' AND remaining=0 LIMIT ?",
                    (limit,),
                )
            ]
            sealed_count = 0
            for batch in stranded:
                sealed = db.execute(
                    "UPDATE batches SET state='queued' WHERE id=? AND state='building'",
                    (batch,),
                ).rowcount
                if sealed:
                    # The publish itself runs after this transaction commits.
                    self._defer_publication(batch=batch)
                    sealed_count += 1
            for batch in unapplied:
                self._defer_publication(batch=batch)
        if sealed_count:
            logger.info(
                "Sealed %d stranded publication batch(es) whose producer never returned",
                sealed_count,
            )
        if unapplied:
            logger.info(
                "Retried %d queued publication batch(es) whose publish never landed",
                len(unapplied),
            )
        return sealed_count

    def fail(self, generation, reason):
        self._retire(generation, "failed", reason)

    def supersede(self, generation):
        """Drop a replaced build and let the batch it was queued in finish without it.

        Only the ``desired`` row tells the writer that its chunk is obsolete, and
        nothing else re-checks the batch that chunk belongs to: leaving the member
        'queued' kept the batch waiting for a receipt that could never arrive, so
        the healthy chapters indexed beside it stayed invisible to retrieval.
        """
        self._retire(generation, "superseded", "Newer index attempt superseded batch member")

    def _retire(self, generation, state, reason):
        with self.db() as db:
            self._build(db, generation)
            batch = self._member_batch(db, generation)
            # Readiness is judged before the state change: afterwards the member
            # never looks ready, which would double-count its decrement.
            ready = batch is not None and self._member_ready(db, generation)
            changed = db.execute(
                "UPDATE builds SET state=?,error=? WHERE generation=? AND state IN ('building','queued')",
                (state, reason, generation),
            ).rowcount
            if batch is None:
                return
            if changed and not ready:
                self._decrement_batch(db, batch)
            # Retire only this member; the rest of the batch keeps publishing,
            # and the batch fails as a whole when no live member is left.
            self._settle_batch(db, batch, reason)

    def state(self, generation):
        with self.db(readonly=True) as db:
            return dict(self._build(db, generation))

    def is_superseded(self, generation):
        """True when a newer index attempt for this source owns publication now.

        A queued chunk only learns it is obsolete from the ``desired`` row: the
        publication gate marks the build it condemns, but a re-index can hand the
        chunk to the writer before that ever runs. Reading the row state alone
        therefore turned replaced work into a failed request -- an ingestion that
        reported failure while the newer attempt published the same chapter.
        """
        with self.db(readonly=True) as db:
            row = self._build(db, generation)
            if row["state"] in {"superseded", "published", "failed"}:
                return row["state"] == "superseded"
            desired = db.execute(
                "SELECT generation FROM desired WHERE account=? AND namespace=? AND uri=?",
                (row["account"], row["namespace"], row["uri"]),
            ).fetchone()
            return bool(desired) and desired[0] != generation

    def declared_image_plan(self, account, uri):
        """True when the current desired generation for this URI was built by the
        joint image producer (an ``image:`` member was declared at begin time).

        A failed joint build never reaches a snapshot manifest, so callers that
        only read the manifest would mistake a jointly-intended image for one
        that was never published as joint.
        """
        with self.db(readonly=True) as db:
            row = db.execute(
                "SELECT b.expected FROM desired d JOIN builds b ON b.generation=d.generation "
                "WHERE d.account=? AND d.uri=?",
                (account, uri),
            ).fetchone()
        return bool(row) and any(child.startswith("image:") for child in json.loads(row[0]))

    @property
    def _query_leases(self) -> QueryLeaseStore:
        """Query-view lease store, deliberately not publication.db.

        Pin/heartbeat/release used to be three writes into this store's
        single-writer queue on every find; under any background publisher they
        queued behind ``BEGIN IMMEDIATE`` (measured: finds stretching to 18s
        and leases lapsing into ``QueryLeaseExpired`` behind a 16s maintenance
        claim). The rows only need to outlive their query and stay visible to
        the retention/GC planners, so they live in a dedicated tiny WAL
        database next to this store's file.
        """
        store = self._query_leases_store
        if store is None:
            with _QUERY_LEASE_STORE_LOCK:
                store = self._query_leases_store
                if store is None:
                    store = QueryLeaseStore(self.path + ".query_leases.sqlite3")
                    self._query_leases_store = store
        return store

    def capture(self, account, ttl=120, busy_timeout_ms: int | None = None):
        """Pin this query's view: a lease over the current heads + their manifest.

        The manifest read is a deferred read transaction (never queues on the
        single writer) and the lease row lands in the dedicated lease store, so
        a query pays zero publication write transactions. The lease pins
        exactly the snapshot ids the manifest was built from; ``prune_snapshots``
        and the GC planners merge the lease store's live rows, and the
        re-verify below restores the pin/read atomicity the single-writer
        design used to guarantee for free.
        """
        for _attempt in range(_QUERY_CAPTURE_ATTEMPTS):
            with self.db(readonly=True, busy_timeout_ms=busy_timeout_ms) as db:
                manifest = self._head_manifest(db, account)
                snapshot_ids = sorted({row[0] for row in db.execute(
                    "SELECT s.id FROM heads h JOIN snapshots s ON s.id=h.snapshot WHERE h.account=?",
                    (account,),
                )})
            lease = self._query_leases.insert(snapshot_ids, ttl, busy_timeout_ms=busy_timeout_ms)
            if not snapshot_ids:
                return lease, manifest
            with self.db(readonly=True, busy_timeout_ms=busy_timeout_ms) as db:
                marks = ",".join("?" * len(snapshot_ids))
                kept = {row[0] for row in db.execute(
                    f"SELECT id FROM snapshots WHERE id IN ({marks})", snapshot_ids
                )}
            if set(snapshot_ids) <= kept:
                return lease, manifest
            # A retention sweep retired a head snapshot between the read and the
            # pin; serving this view would silently miss published content, so
            # drop the lease and retry on a fresh read.
            self._query_leases.release(lease)
        raise QueryLeaseExpired("Publication snapshots retired while pinning the query view")

    def _head_manifest(self, db, account):
        """Materialise the published manifest of every namespace of ``account``.

        Read-only on purpose: callers on the event loop (a file read projecting
        its published bytes) must not open a write transaction, because the
        single writer turns that into a busy-timeout wait -- measured as 12-33s
        event-loop stalls in ``source_reference``.
        """
        manifest = {}
        for row in db.execute(
            "SELECT s.id FROM heads h JOIN snapshots s ON s.id=h.snapshot WHERE h.account=?",
            (account,),
        ):
            # decode once per snapshot per process: re-decoding an ~11MB gzip
            # blob inside a write transaction held the single writer lock for
            # ~116ms per call (capture runs once per file in the vector phase,
            # which is what stretched lock waits to seconds).
            for uri, value in _snapshot_manifest(db, row["id"]).items():
                if uri in manifest:
                    raise RuntimeError("Overlapping publication namespaces")
                manifest[uri] = value
        return manifest

    def release(self, lease):
        self._query_leases.release(lease)

    def renew(self, lease, ttl=120, busy_timeout_ms: int | None = None):
        if not self._query_leases.renew(lease, ttl, busy_timeout_ms=busy_timeout_ms):
            raise QueryLeaseExpired("Publication query lease expired; retry query")

    # ── Tombstones ──

    def tombstone(self, account, uri):
        """Write a deletion tombstone; recall and read-back must refuse at once."""
        if not isinstance(uri, str) or not uri.startswith("viking://"):
            raise ValueError("Tombstone requires a canonical URI")
        with self.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO tombstones VALUES (?,?,?)", (account, uri, time.time())
            )

    def tombstone_many(self, account, uris):
        """Write many deletion tombstones in one transaction.

        Account deletion marks every published source of a scope and used to
        open one tiny BEGIN IMMEDIATE per source, queueing thousands of writers
        behind the single writer. Validation mirrors :meth:`tombstone` and runs
        before the transaction opens, so a bad URI writes nothing.
        """
        wanted = list(dict.fromkeys(uris))
        for uri in wanted:
            if not isinstance(uri, str) or not uri.startswith("viking://"):
                raise ValueError("Tombstone requires a canonical URI")
        if not wanted:
            return 0
        created = time.time()
        with self.db() as db:
            db.executemany(
                "INSERT OR REPLACE INTO tombstones VALUES (?,?,?)",
                [(account, uri, created) for uri in wanted],
            )
        return len(wanted)

    def clear_tombstone(self, account, uri):
        """Drop a tombstone only when the source is re-created as current content."""
        with self.db() as db:
            db.execute("DELETE FROM tombstones WHERE account=? AND uri=?", (account, uri))

    def clear_tombstones(self, account, uris):
        """Drop the tombstones of many URIs in one transaction.

        Re-creating a source clears the tombstone of the URI itself and of every
        ancestor directory; the per-level walk opened one BEGIN IMMEDIATE per
        level, so a bulk ingest issued tens of thousands of tiny write
        transactions and convoyed every other writer behind the single writer.
        The list is chunked inside the one transaction: SQLite bounds the host
        parameters of a statement, while the writer lock is taken once.
        """
        wanted = list(dict.fromkeys(uris))
        if not wanted:
            return 0
        removed = 0
        with self.db() as db:
            for start in range(0, len(wanted), TOMBSTONE_BATCH):
                chunk = wanted[start : start + TOMBSTONE_BATCH]
                marks = ",".join("?" * len(chunk))
                removed += db.execute(
                    f"DELETE FROM tombstones WHERE account=? AND uri IN ({marks})",
                    (account, *chunk),
                ).rowcount
        return removed

    def clear_tombstone_subtree(self, account, uri):
        """Drop the tombstones of *uri* and everything beneath it.

        A re-created directory can arrive through one whole-tree copy instead of
        per-file writes, so no individual write exists that would clear the
        descendants' tombstones. Matching uses the same ancestor rule as
        ``is_tombstoned`` so a cleared subtree cannot still be refused by a row
        this call did not consider. One prefix statement replaces one DELETE per
        row, which keeps the writer lock hold time flat in the subtree size.
        """
        prefix = uri.rstrip("/") + "/"
        with self.db() as db:
            return db.execute(
                "DELETE FROM tombstones WHERE account=? AND (uri=? OR substr(uri,1,?)=?)",
                (account, uri, len(prefix), prefix),
            ).rowcount

    def tombstones(self, account):
        with self.db(readonly=True) as db:
            return [row[0] for row in db.execute("SELECT uri FROM tombstones WHERE account=?", (account,))]

    def tombstone_uris(self, account) -> set:
        """Tombstoned URIs as a set, for post-ranking rechecks.

        Reading the same rows as :meth:`tombstones` but as a set lets the search
        path test ancestors in O(depth) without handing the engine one clause per
        tombstone.
        """
        return set(self.tombstones(account))

    def is_tombstoned(self, account, uri):
        """True when uri or any ancestor directory has an active tombstone.

        Pure read: this is called on every source read (including from the event
        loop via ``_OpsMixin._ensure_not_tombstoned``), so taking the writer lock
        here serialized all readers behind bulk publications and showed up as
        multi-second lock waits inside ``read_file_bytes``. WAL readers do not
        block writers, so the readonly transaction removes that convoy.
        """
        with self.db(readonly=True) as db:
            return _tombstone_covers(db, account, uri)

    def sources(self, account):
        """Published source URIs for an account, across all namespaces."""
        with self.db(readonly=True) as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT uri FROM desired WHERE account=?", (account,)
                )
            ]

    def visibility(self, manifest, account=None):
        # Filter before ranking, never over-fetch then discard staged candidates.
        legacy = [Eq("index_generation", "")]
        if manifest:
            legacy.append(
                RawDSL({"op": "must_not", "field": "uri", "conds": list(manifest), "para": "-d=0"})
            )
        base = (
            Or(
                [
                    And(legacy),
                    *[
                        And([Eq("uri", uri), Eq("index_generation", value["generation"])])
                        for uri, value in manifest.items()
                    ],
                ]
            )
            if manifest
            else And(legacy)
        )
        # Deletion tombstones take priority over every snapshot so a stale query
        # view cannot return removed content.
        dead = self.tombstones(account) if account is not None else []
        if dead:
            return And(
                [base, RawDSL({"op": "must_not", "field": "uri", "conds": dead, "para": "-d=-1"})]
            )
        return base

    @contextmanager
    def source_reference(self, account, uri):
        view = _view.get()
        owned = view is None
        if owned:
            lease = None
            # Read-only: this runs on the event loop while projecting a file read,
            # and taking a lease here opened a write transaction that waited out
            # the busy timeout behind the single writer (12-33s loop stalls).
            # The manifest is materialised below, so the result is just as stable.
            with self.db(readonly=True) as db:
                manifest = self._head_manifest(db, account)
        else:
            lease = None
            # read() already renewed the shared lease for this query.
            _, manifest = view.read(account, self)
        try:
            item = manifest.get(uri)
            reference = item.get("content") if item else None
            if item and reference is None:
                raise RuntimeError(
                    "Published source has no immutable content; rebuild before enabling content projection"
                )
            for suffix, level in (("/.abstract.md", "0"), ("/.overview.md", "1")):
                if uri.endswith(suffix):
                    directory = manifest.get(uri[: -len(suffix)])
                    if directory:
                        if "semantic_contents" not in directory:
                            raise RuntimeError(
                                "Published directory has no immutable sidecars; rebuild before enabling content projection"
                            )
                        reference = directory["semantic_contents"].get(level)
                        if reference is None:
                            from openviking_cli.exceptions import NotFoundError

                            raise NotFoundError(uri, "published semantic source")
                    break
            yield reference
            if owned and lease is not None:
                self.renew(lease)
        finally:
            if owned and lease is not None:
                self.release(lease)
    def protected_content_revisions(self, db=None):
        """Derived-body roots protected by active/read/in-flight generations.

        This is a read-only reachability plan, not permission to delete blobs;
        Wiki/image/retry lifecycle roots still need the unified registry.
        """
        if db is not None:
            return self._protected_content_revisions(db)
        with self.db() as connection:
            return self._protected_content_revisions(connection)

    def _protected_content_revisions(self, db):
        snapshots = {r[0] for r in db.execute("SELECT snapshot FROM heads")}
        for row in db.execute("SELECT snapshots FROM leases WHERE expires>?", (time.time(),)):
            snapshots.update(json.loads(row[0]))
        snapshots.update(self._query_leases.live_snapshot_ids())
        protected = {
            (row["account"], json.loads(row["reference"])["sha256"])
            for row in db.execute("SELECT account,reference FROM wiki_schemas")
        }
        # Prepared Wiki tasks pin parsed Markdown, never original uploads.
        # Retirement/physical GC is not enabled yet; retain every saved plan.
        for row in db.execute("SELECT account,manifest FROM wiki_build_plans"):
            for source in json.loads(row["manifest"])["sources"]:
                protected.add((row["account"], source["sha256"]))
        # Private extraction outputs remain readable for saved/failed jobs. A
        # future explicit job retirement policy must release these references.
        for row in db.execute("SELECT account,checkpoint FROM kb_jobs WHERE kind IN ('W1','W2')"):
            checkpoint = json.loads(row['checkpoint'])
            for name in ('draft_receipt', 'extraction_progress'):
                for receipt in checkpoint.get(name, {}).get('receipts', []):
                    reference = receipt.get('reference', {})
                    if reference.get('sha256'):
                        protected.add((row['account'], reference['sha256']))
        for snapshot in snapshots:
            row = db.execute(
                "SELECT account FROM snapshots WHERE id=?", (snapshot,)
            ).fetchone()
            if row is None:
                continue
            from openviking.indexing.registry_gc import load_snapshot_manifest

            for item in load_snapshot_manifest(db, snapshot).values():
                if item.get("content"):
                    protected.add((row["account"], item["content"]["sha256"]))
                    cache = item["content"].get("image_semantics")
                    if cache:
                        protected.add((row["account"], cache["sha256"]))
                for reference in item.get("semantic_contents", {}).values():
                    protected.add((row["account"], reference["sha256"]))
        for row in db.execute(
            "SELECT b.account,s.reference FROM builds b JOIN sources s ON s.generation=b.generation WHERE b.state IN ('building','queued')"
        ):
            reference = json.loads(row["reference"])
            protected.add((row["account"], reference["sha256"]))
            if reference.get("image_semantics"):
                protected.add((row["account"], reference["image_semantics"]["sha256"]))
        for row in db.execute(
            "SELECT b.account,s.reference FROM builds b JOIN semantic_sources s ON s.generation=b.generation WHERE b.state IN ('building','queued')"
        ):
            for reference in json.loads(row["reference"]).values():
                protected.add((row["account"], reference["sha256"]))
        return protected

    def dependents(self, account, uri):
        """Enumerate dependents of a URI: generations and derived content shas.

        Covers child vectors (content), image semantics caches, L0/L1 semantic
        sidecars and publication receipts, so a delete can mark every asset it
        must later CAS-clean rather than relying on TTL alone.
        """
        with self.db() as db:
            generations = []
            shas = []
            for row in db.execute(
                "SELECT b.generation,s.reference FROM builds b JOIN sources s "
                "ON s.generation=b.generation WHERE b.account=? AND b.uri=?",
                (account, uri),
            ):
                generations.append(row["generation"])
                reference = json.loads(row["reference"])
                shas.append(reference["sha256"])
                if reference.get("image_semantics"):
                    shas.append(reference["image_semantics"]["sha256"])
            for row in db.execute(
                "SELECT b.generation,s.reference FROM builds b JOIN semantic_sources s "
                "ON s.generation=b.generation WHERE b.account=? AND b.uri=?",
                (account, uri),
            ):
                generations.append(row["generation"])
                shas.extend(
                    reference["sha256"] for reference in json.loads(row["reference"]).values()
                )
            children = [
                child[0]
                for generation in generations
                for child in db.execute(
                    "SELECT child FROM receipts WHERE generation=?", (generation,)
                )
            ]
            return {
                "generations": sorted(set(generations)),
                "content_shas": sorted(set(shas)),
                "children": sorted(set(children)),
            }

    def retired_candidates(self, grace_seconds=3600):
        """Read-only GC plan; physical deletion must revalidate protections.

        No destructive GC is enabled until content/Wiki references share the
        same protection registry and storage deletion can be fenced.
        """
        with self.db() as db:
            protected = {r[0] for r in db.execute("SELECT snapshot FROM heads")}
            for row in db.execute("SELECT snapshots FROM leases WHERE expires>?", (time.time(),)):
                protected.update(json.loads(row[0]))
            for row in db.execute(
                "SELECT snapshots FROM gc_leases WHERE expires>?", (time.time(),)
            ):
                protected.update(json.loads(row[0]))
            protected.update(self._query_leases.live_snapshot_ids())
            generations = set()
            for snapshot in protected:
                from openviking.indexing.registry_gc import load_snapshot_manifest

                manifest = load_snapshot_manifest(db, snapshot)
                generations.update(v["generation"] for v in manifest.values())
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM builds WHERE created<? AND state IN ('published','failed','superseded')",
                    (time.time() - grace_seconds,),
                )
                if r["generation"] not in generations
            ]

    # ── Physical GC (dependency reachability + retention + CAS) ──

    def gc_lease(self, account, ttl=120):
        """Pin the current reachable snapshots for one reader/task, like a query lease.

        Sweeps expired rows in the same transaction: ``capture`` no longer takes
        a publication write transaction, so the writers of this table own its
        housekeeping (the periodic snapshot prune also sweeps).
        """
        lease = uuid4().hex
        now = time.time()
        with self.db() as db:
            db.execute("DELETE FROM gc_leases WHERE expires<?", (now,))
            snapshots = [
                row[0] for row in db.execute("SELECT snapshot FROM heads WHERE account=?", (account,))
            ]
            db.execute(
                "INSERT INTO gc_leases VALUES (?,?,?,?)",
                (lease, account, json.dumps(snapshots), now + ttl),
            )
        return lease

    def gc_renew(self, lease, ttl=120):
        with self.db() as db:
            changed = db.execute(
                "UPDATE gc_leases SET expires=? WHERE id=? AND expires>?",
                (time.time() + ttl, lease, time.time()),
            ).rowcount
            if not changed:
                raise RuntimeError("GC lease expired; re-capture before continuing")
            return True

    def gc_release(self, lease):
        with self.db() as db:
            db.execute("DELETE FROM gc_leases WHERE id=?", (lease,))

    def _protected_blobs(self, db):
        """Content revisions reachable from every live root, plus tombstoned URIs.

        Live GC leases pin the snapshots an active reader/task captured, so an
        unexpired lease keeps its generation's bodies even after a switch.
        """
        protected = self._protected_content_revisions(db)
        leased = {
            snapshot
            for row in db.execute("SELECT snapshots FROM gc_leases WHERE expires>?", (time.time(),))
            for snapshot in json.loads(row[0])
        }
        from openviking.indexing.registry_gc import load_snapshot_manifest

        for snapshot in leased:
            row = db.execute("SELECT account FROM snapshots WHERE id=?", (snapshot,)).fetchone()
            if row is None:
                continue
            for item in load_snapshot_manifest(db, snapshot).values():
                if item.get("content"):
                    protected.add((row["account"], item["content"]["sha256"]))
                    cache = item["content"].get("image_semantics")
                    if cache:
                        protected.add((row["account"], cache["sha256"]))
                for reference in item.get("semantic_contents", {}).values():
                    protected.add((row["account"], reference["sha256"]))
        # Derived bodies of a deleted source stay reachable until its tombstone
        # is cleared, so deletion does not immediately free shared assets.
        dead_uris = [(row[0], row[1]) for row in db.execute("SELECT account,uri FROM tombstones")]
        if dead_uris:
            for row in db.execute("SELECT account,id FROM snapshots"):
                for uri, item in load_snapshot_manifest(db, row["id"]).items():
                    if any(row["account"] == owner and (uri == d or uri.startswith(d.rstrip("/") + "/")) for owner, d in dead_uris):
                        if item.get("content"):
                            protected.add((row["account"], item["content"]["sha256"]))
                            if item["content"].get("image_semantics"):
                                protected.add((row["account"], item["content"]["image_semantics"]["sha256"]))
                        for reference in item.get("semantic_contents", {}).values():
                            protected.add((row["account"], reference["sha256"]))
        return protected

    def plan_gc(self, account=None, *, grace_seconds=3600):
        """Mark reclaimable blobs: retired generations and failed/orphan attempts.

        Marks are revalidated under CAS before deletion; a concurrent
        re-reference (new build/publish/lease) clears the mark.
        """
        _seconds(grace_seconds)
        now = time.time()
        marked = []
        retired = {r["generation"] for r in self.retired_candidates(grace_seconds)}
        with self.db() as db:
            candidates = set()
            for row in db.execute(
                "SELECT b.generation,b.account,s.reference FROM builds b "
                "JOIN sources s ON s.generation=b.generation "
                "WHERE b.state='published'",
            ):
                if account is not None and row["account"] != account:
                    continue
                if row["generation"] not in retired:
                    continue
                reference = json.loads(row["reference"])
                candidates.add((row["account"], reference["sha256"], "content"))
                if reference.get("image_semantics"):
                    candidates.add(
                        (row["account"], reference["image_semantics"]["sha256"], "image_cache")
                    )
            # Failed/unregistered attempts: a producer that began a build but
            # never published leaves an orphan revision.
            for row in db.execute(
                "SELECT b.account,s.reference FROM builds b JOIN sources s "
                "ON s.generation=b.generation WHERE b.state IN ('failed','superseded')",
            ):
                if account is not None and row["account"] != account:
                    continue
                reference = json.loads(row["reference"])
                candidates.add((row["account"], reference["sha256"], "orphan"))
                if reference.get("image_semantics"):
                    candidates.add((row["account"], reference["image_semantics"]["sha256"], "image_cache"))
            for row in db.execute(
                "SELECT b.generation,b.account,b.state,s.reference FROM builds b JOIN semantic_sources s "
                "ON s.generation=b.generation WHERE b.state IN ('published','failed','superseded')"
            ):
                if account is not None and row["account"] != account:
                    continue
                if row["state"] == 'published' and row["generation"] not in retired:
                    continue
                for reference in json.loads(row["reference"]).values():
                    candidates.add((row["account"], reference["sha256"], "semantic"))
            protected = self._protected_blobs(db)
            for owner, sha, kind in sorted(candidates):
                from openviking_cli.exceptions import FailedPreconditionError
                try:
                    self._gc_assert_no_unknown_writer(db, owner)
                except FailedPreconditionError:
                    continue
                if (owner, sha) in protected:
                    continue
                existing = db.execute(
                    "SELECT created FROM gc_marks WHERE account=? AND sha=? AND kind=?",
                    (owner, sha, kind),
                ).fetchone()
                # Preserve the original mark time so retention is measured from
                # the first mark, not from every GC pass.
                created = existing["created"] if existing else now
                db.execute(
                    "INSERT OR REPLACE INTO gc_marks VALUES (?,?,?,0,'marked',?)",
                    (owner, sha, kind, created),
                )
                marked.append({"account": owner, "sha": sha, "kind": kind})
        return marked

    async def reclaim(self, account=None, *, retention_seconds=0, delete=None):
        """Revalidate marks, but refuse unsupported physical deletion.

        Generic callbacks and Boolean results cannot establish permanent writer
        fencing, actual delete visibility or joint vector/lexical/meta cleanup.
        Kept marks may be diagnosed; eligible deletion requires the trusted
        physical-GC protocol, whose production capability is still unavailable.
        """
        from openviking_cli.exceptions import FailedPreconditionError
        _seconds(retention_seconds)
        now = time.time()
        outcomes = []
        with self.db() as db:
            protected = self._protected_blobs(db)
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM gc_marks WHERE state='marked'" + ("" if account is None else " AND account=?"),
                () if account is None else (account,),
            )]
            for row in rows:
                try:
                    self._gc_assert_no_unknown_writer(db, row['account'])
                except FailedPreconditionError:
                    outcomes.append({'sha': row['sha'], 'released': False, 'reason': 'write_recovery_required'})
                    continue
                if (row['account'], row['sha']) in protected:
                    db.execute('DELETE FROM gc_marks WHERE account=? AND sha=? AND kind=?',
                               (row['account'], row['sha'], row['kind']))
                    outcomes.append({'sha': row['sha'], 'released': False, 'reason': 'referenced'})
                elif now - row['created'] < retention_seconds:
                    outcomes.append({'sha': row['sha'], 'released': False, 'reason': 'retention'})
                else:
                    raise FailedPreconditionError(
                        'PHYSICAL_GC_CAPABILITY_REQUIRED: no trusted all-index delete visibility and generation fence; callbacks are not recovery evidence'
                    )
        return outcomes

    def gc_audit(self, account):
        with self.db() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM gc_audit WHERE account=? ORDER BY created", (account,)
                )
            ]


@lru_cache(maxsize=8)
def store(path):
    return PublicationStore(path)


def configured_store(embedding=None):
    if embedding is None:
        from openviking_cli.utils.config.open_viking_config import OpenVikingConfigSingleton

        config = OpenVikingConfigSingleton._instance
        if config is None:
            return None  # Read-only feature probe must not initialize global config.
        embedding = config.embedding
    chunking = getattr(embedding, "chunking", None)
    endpoint = getattr(chunking, "publication_authority_socket", "")
    if endpoint:
        from openviking.indexing.registry_authority import authority_repository

        return authority_repository(endpoint, chunking.publication_authority_token_file)
    path = getattr(chunking, "publication_path", "")
    return store(path) if path else None


def visible_filter(ctx):
    registry = configured_store()
    if registry is None:
        return None
    view = _view.get()
    if view is not None:
        _, manifest = view.read(ctx.account_id, registry)
        return registry.visibility(manifest, ctx.account_id)
    # Non-public low-level readers capture only this operation's view. Bounded:
    # this runs on the event loop when no query view is pinned.
    lease, manifest = registry.capture(
        ctx.account_id, busy_timeout_ms=QUERY_RENEW_BUSY_TIMEOUT_MS
    )
    try:
        return registry.visibility(manifest, ctx.account_id)
    finally:
        registry.release(lease)


def visibility_snapshot(ctx):
    """Return ``(manifest, dead_uris)`` for post-ranking visibility rechecks.

    ``visible_filter`` enumerates every published URI as its own filter clause,
    which is one node per published document (174k for the enterprise corpus).
    Shipping that DSL to the vector engine costs seconds on a narrow scope and
    minutes on a tree-wide one, so the hot search path instead asks the engine
    for the coarse scope only and rechecks publication visibility here. The
    recheck is exact for every row that is returned.
    """
    registry = configured_store()
    if registry is None:
        return None
    view = _view.get()
    if view is not None:
        _, manifest = view.read(ctx.account_id, registry)
        return manifest, registry.tombstone_uris(ctx.account_id)
    lease, manifest = registry.capture(
        ctx.account_id, busy_timeout_ms=QUERY_RENEW_BUSY_TIMEOUT_MS
    )
    try:
        return manifest, registry.tombstone_uris(ctx.account_id)
    finally:
        registry.release(lease)


def tombstoned_uri(uri: str, dead) -> bool:
    """True when ``uri`` or any of its ancestors is tombstoned."""
    if not dead:
        return False
    parts = uri.split("/")
    for end in range(len(parts), 0, -1):
        if "/".join(parts[:end]) in dead:
            return True
    return False


def recheck_visibility(ctx, rows, snapshot=None):
    """Drop rows whose published generation is not the visible one.

    Mirrors ``PublicationStore.visibility``: a row is visible when its account
    manifest holds the same ``index_generation`` for its URI, or when it is a
    legacy row (empty generation) that the manifest does not know about. Rows
    under a tombstone are always dropped.
    """
    if not rows:
        return list(rows or [])
    snap = snapshot if snapshot is not None else visibility_snapshot(ctx)
    if snap is None:
        return list(rows)
    manifest, dead = snap
    kept = []
    for row in rows:
        uri = row.get("uri")
        if isinstance(uri, str) and uri:
            if tombstoned_uri(uri, dead):
                continue
            item = manifest.get(uri)
            generation = row.get("index_generation")
            if item is None:
                if generation not in (None, ""):
                    continue
            elif item.get("generation") != generation:
                continue
        kept.append(row)
    return kept


# A query's publication view lease. Kept in sync with ``capture``/``renew``'s
# own default so the heartbeat renews well inside the window.
_SLOW_TX_MS_ENV = "OPENVIKING_SLOW_TX_MS"
_SLOW_TX_MS_DEFAULT = 500.0


def _slow_tx_threshold_ms() -> float:
    """Transactions slower than this get logged with their caller stack.

    The publication write lock serializes every writer in the process, so one
    transaction holding it for seconds stalls the whole pipeline behind it.
    Set OPENVIKING_SLOW_TX_MS=0 to disable.
    """
    raw = os.environ.get(_SLOW_TX_MS_ENV)
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return _SLOW_TX_MS_DEFAULT


QUERY_LEASE_TTL_SECONDS = 120

# Lease renewals issued from the request path run on the event loop, so they may
# wait only briefly on the publication write lock: an unbounded wait queued
# behind the publisher froze the server for 204-244s (watchdog stacks showed
# db -> renew -> QueryView.read -> visible_filter -> vector search). The
# off-loop heartbeat thread keeps the lease alive across the deferred renewal.
QUERY_RENEW_BUSY_TIMEOUT_MS = 1000
# End-of-query verification may afford a longer bounded wait: refusing a result
# costs the client a full re-run of the query (tens of seconds), so give the
# writer queue five seconds before declaring the lease lapsed.
QUERY_RENEW_VERIFY_BUSY_TIMEOUT_MS = 5000

# Dead unbacked write fences (no source_write_refs row) can never be settled by
# verification, so ones older than this are auto-settled instead of blocking a
# URI's directory generation forever.  See PublicationStore._settle_dead_fences.
AUTO_SETTLE_FENCE_SECONDS = 600.0
# An abandoned ``writing`` row is the normal outcome of a crash: ``source_write``
# (openviking/indexing/content.py:167) only clears the fence on its success path.
# Live evidence: 165 unbacked ``writing`` rows aged ~6h, each of them blocking its
# directory's L0/L1 generation for good (7 failed records on an otherwise complete
# manual).  A genuine long write deserves much more room than an ``uncertain``
# fence, but it must not be immortal.
AUTO_SETTLE_WRITING_SECONDS = 1800.0

# pid + import time: unique per server start, stable for the process lifetime.
_PROCESS_GENERATION = f"{os.getpid()}:{int(time.time() * 1000)}"


def current_process_generation() -> str:
    """Identity of this process generation for write-fence ownership.

    One server writes a given data directory at a time, so a fence stamped with
    a different generation belongs to a process that has already exited: nothing
    can still be running under it, and it can be settled immediately instead of
    waiting out ``AUTO_SETTLE_FENCE_SECONDS`` (live: a restart left 26 fences and
    the 600 s window was far too slow for the run that hit them).
    """
    return _PROCESS_GENERATION
# A batch member left in 'building' by a producer that exited without finishing
# its chunk set keeps ``remaining`` above zero forever, so the batch never
# settles and every sibling member stays unpublished.  Live evidence: 15 batches
# with members 5.7-24.5h old, trapping 3,000+ queued members.  Well above the
# worst legitimate delay (an embedding backlog of ~35k messages at ~9/s is about
# an hour), so a slow-but-live producer is never reaped.
STALE_BUILD_SECONDS = 7200.0
# How long a batch member may sit with a partially-complete receipt set before it
# is failed so its siblings can publish. Long enough that an embedding backlog or
# a slow semantic call is not mistaken for an abandoned member; short enough that
# one stuck member does not hide a whole batch for two hours.
STRANDED_MEMBER_SECONDS = 900.0
AUTO_SETTLE_FENCE_ACTOR = "system:auto-fence-settle"
AUTO_SETTLE_FENCE_REASON = (
    "writer left an unbacked uncertain fence older than 600s; the backend holds no "
    "operation that any verifier could prove terminal"
)


class QueryLeaseExpired(RuntimeError):
    """The query's publication view lease lapsed before the result was returned.

    Under a bulk reindex the publication write lock is held for long stretches:
    the heartbeat's tiny renewal ``UPDATE`` waits out its busy timeout behind
    the publisher, the lease expires, and refusing the result is correct (it
    would be served from a view the store no longer guarantees). The read-only
    query entry points retry on this instead of failing the request.
    """


class QueryView:
    """One query's publication view, opened by ``pin_query_snapshot`` or by the
    first read that needs it.

    ``capture`` writes a lease row under an exclusive transaction, so the gate
    no longer takes one on entry: a request rejected by argument validation
    must not cost the writer path anything.  A query that passes validation
    pins its view immediately, so a generation published while the query runs
    stays invisible to it and its blobs stay GC-protected for its whole read.
    The account is taken from the caller that pins or reads, which keeps the
    gate from reaching into a storage class's private state to guess it.
    """

    def __init__(self, registry, account=None):
        self._registry = registry
        self._account = account
        self._lease = None
        self._manifest = None
        self._heartbeat = None
        self._renewed_at = 0.0

    def _same_store(self, registry):
        return registry is self._registry or registry.path == self._registry.path

    @property
    def registry(self):
        return self._registry

    def read(self, account, registry):
        """Return ``(lease, manifest)`` pinned for this query, capturing once."""
        if not self._same_store(registry):
            raise RuntimeError("Index query view belongs to a different account/store")
        if self._lease is None:
            if self._account is not None and self._account != account:
                raise RuntimeError("Index query view belongs to a different account/store")
            self._account = account
            try:
                self._lease, self._manifest = registry.capture(
                    account,
                    ttl=QUERY_LEASE_TTL_SECONDS,
                    busy_timeout_ms=QUERY_RENEW_BUSY_TIMEOUT_MS,
                )
            except sqlite3.OperationalError as exc:
                # Could not pin a view without waiting out the publisher convoy.
                # The query is read-only, so let the caller retry on a fresh
                # snapshot instead of freezing the event loop (this call runs on
                # it) or serving a result without a view.
                raise QueryLeaseExpired(
                    f"Publication store busy while pinning the query view: {exc}"
                ) from exc
            self._renewed_at = time.monotonic()
            self._start_heartbeat()
        elif self._account != account:
            raise RuntimeError("Index query view belongs to a different account/store")
        # The heartbeat thread renews off-loop. Only top the lease up from the
        # request path once it is getting old, and never wait unboundedly for
        # the writer lock here: this call sits on the event loop (the watchdog
        # caught it parked in ``BEGIN IMMEDIATE`` for 204-283s). A bounded wait
        # that loses the race is retryable, and the heartbeat still renews.
        if time.monotonic() - self._renewed_at > QUERY_LEASE_TTL_SECONDS / 3:
            try:
                registry.renew(
                    self._lease,
                    ttl=QUERY_LEASE_TTL_SECONDS,
                    busy_timeout_ms=QUERY_RENEW_BUSY_TIMEOUT_MS,
                )
                self._renewed_at = time.monotonic()
            except QueryLeaseExpired:
                raise
            except sqlite3.OperationalError:
                # The publisher holds the write lock. Serving the already-pinned
                # snapshot is correct; the heartbeat (or the next read) renews.
                logger.warning(
                    "Query lease renewal deferred: publication store is busy", exc_info=True
                )
        return self._lease, self._manifest

    def _start_heartbeat(self):
        """Renew the lease from a dedicated thread, not an event-loop task.

        The view is captured from a worker thread (``pin_query_snapshot`` runs
        under ``asyncio.to_thread``), where no loop is running to schedule a
        task on, and a legitimate long read (a wide rerank) can also block the
        event loop for minutes. An asyncio-task heartbeat is never scheduled
        then: the lease expires under the query and the end-of-query renew
        refuses the result. A thread renews independently because the renewal
        is a small sqlite write and the GIL is released during I/O and every
        switch interval, so renewal survives loop saturation.
        """
        if self._heartbeat is not None or self._lease is None:
            return
        stop = threading.Event()
        self._heartbeat = stop
        lease = self._lease

        def _run():
            while not stop.wait(QUERY_LEASE_TTL_SECONDS / 3):
                try:
                    self._registry.renew(lease, ttl=QUERY_LEASE_TTL_SECONDS)
                    self._renewed_at = time.monotonic()
                except RuntimeError:
                    # The lease genuinely lapsed (or was released); the
                    # end-of-query renew refuses the result either way.
                    return
                except Exception:
                    # Transient store errors (e.g. sqlite 'database is locked'
                    # while a BEGIN IMMEDIATE waits out its busy timeout) must
                    # not stop renewal; retry on the next tick.
                    logger.warning("Query lease renewal failed transiently; retrying", exc_info=True)

        threading.Thread(target=_run, name="query-lease-heartbeat", daemon=True).start()

    def renew(self):
        if self._lease is not None:
            # Never return a result after its view lease expired. Bounded, so a
            # publisher convoy cannot freeze the loop while this is verified.
            self._registry.renew(
                self._lease,
                ttl=QUERY_LEASE_TTL_SECONDS,
                busy_timeout_ms=QUERY_RENEW_VERIFY_BUSY_TIMEOUT_MS,
            )
            self._renewed_at = time.monotonic()

    def release(self):
        if self._heartbeat is not None:
            self._heartbeat.set()
            self._heartbeat = None
        if self._lease is not None:
            self._registry.release(self._lease)
            self._lease = None


def pin_query_snapshot(ctx):
    """Fix the running query's publication view before it does any work.

    Public query entry points call this right after argument validation: the
    rejected request never reaches it, and the accepted one holds one snapshot
    for its whole read instead of opening one lazily mid-query.
    """
    view = _view.get()
    if view is None:
        return None
    view.read(ctx.account_id, view.registry)
    return view


# Serving a result from a lapsed view is refused, but the query itself is
# read-only: re-running it on a fresh snapshot is better than a 500 while a
# bulk reindex saturates the publication store.
_QUERY_SNAPSHOT_ATTEMPTS = 3


def query_snapshot(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        registry = configured_store()
        if registry is None or _view.get() is not None:
            return await function(*args, **kwargs)
        ctx = inspect.signature(function).bind_partial(*args, **kwargs).arguments.get("ctx")
        for attempt in range(_QUERY_SNAPSHOT_ATTEMPTS):
            view = QueryView(registry, getattr(ctx, "account_id", None))
            token = _view.set(view)
            try:
                result = await function(*args, **kwargs)
                # Off-loop: the validating body has already pinned and read the
                # view (``to_thread(pin_query_snapshot, ...)`` at the public
                # entry points), and this last renewal is a write transaction.
                await asyncio.to_thread(view.renew)
                return result
            except QueryLeaseExpired:
                if attempt + 1 >= _QUERY_SNAPSHOT_ATTEMPTS:
                    raise
                logger.warning(
                    "Query view lease lapsed under publication write load; "
                    "retrying on a fresh snapshot (attempt %d/%d)",
                    attempt + 2,
                    _QUERY_SNAPSHOT_ATTEMPTS,
                )
                await asyncio.sleep(0.5 * (attempt + 1))
            finally:
                _view.reset(token)
                view.release()

    return wrapped
