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

import inspect
import os

if os.name == "posix":
    import fcntl
import json
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache, wraps
from pathlib import Path
from uuid import uuid4

from openviking.indexing.registry_lexical import RegistryLexicalMixin
from openviking.indexing.registry_metadata import RegistryMetadataMixin
from openviking.indexing.registry_gc import RegistryGCMixin, _seconds
from openviking.indexing.registry_domains import RegistryDomains
from openviking.indexing.jobs import DurableJobsMixin
from openviking.indexing.write_recovery import WriteRecoveryMixin
from openviking.storage.expr import And, Eq, Or, RawDSL
from openviking.storage.vector_ids import publication_member_id

_view = ContextVar("text_index_query_view", default=None)


class PublicationStore(RegistryDomains, DurableJobsMixin, WriteRecoveryMixin, RegistryLexicalMixin, RegistryMetadataMixin, RegistryGCMixin):
    def __init__(self, path: str, *, authority_owned=False):
        self.path = str(Path(path).expanduser().resolve())
        if (Path(self.path).parent / 'RESTORE_PENDING.json').exists():
            from openviking_cli.exceptions import FailedPreconditionError
            raise FailedPreconditionError('RESTORE_PENDING: joint restore validation must finish before registry startup')
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._authority_owned = authority_owned
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
                CREATE TABLE IF NOT EXISTS desired (
                  account TEXT, namespace TEXT, uri TEXT, generation TEXT,
                  PRIMARY KEY(account,namespace,uri));
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
                  id TEXT PRIMARY KEY, account TEXT, namespace TEXT, state TEXT, error TEXT);
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

        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO wiki_collection_domains(account,collection_id,state) SELECT account,collection_id,'legacy_domain_pending' FROM wiki_collections")
        self.init_lexical()
        self.init_metadata()
        self.init_jobs()
        self.init_write_recovery()
        self.init_physical_gc()

    @contextmanager
    def db(self):
        lock = open(self.path + ".authority.lock", "a+b")
        if os.name == "posix" and not self._authority_owned:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close()
                raise RuntimeError("Registry is owned by an authority; use its socket repository")
        try:
            db = sqlite3.connect(self.path, timeout=30)
        except BaseException:
            lock.close()
            raise
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
            lock.close()

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
                    "SELECT 1 FROM batch_members m JOIN builds b ON b.generation=m.generation "
                    "WHERE m.batch=? AND b.uri=?",
                    (batch, uri),
                ).fetchone()
                if duplicate:
                    raise ValueError("Duplicate source URI in publication batch")
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
        if db.execute("SELECT 1 FROM source_writes WHERE account=? AND uri=?", (account, uri)).fetchone():
            raise RuntimeError("Source has an in-flight or uncertain write; retry after verified completion")

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
            db.execute("INSERT INTO source_writes VALUES (?,?,?,'writing',?)", (token, account, uri, time.time()))
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
                "INSERT INTO batches VALUES (?,?,?,'building',NULL)", (batch, account, namespace)
            )
        return batch

    def _fail_batch(self, db, batch, reason):
        db.execute(
            "UPDATE batches SET state='failed',error=? WHERE id=? AND state!='published'",
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
        with self.db() as db:
            db.execute(
                "UPDATE batches SET state='queued' WHERE id=? AND state='building'", (batch,)
            )
            return self._publish_batch_if_complete(db, batch)

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

    def _invalid_reason(self, db, row):
        try:
            self._assert_not_writing(db, row["account"], row["uri"])
        except RuntimeError as exc:
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
                except RuntimeError as exc:
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
                except RuntimeError as exc:
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

    def _publish_rows(self, db, rows):
        first = rows[0]
        head = db.execute(
            "SELECT snapshot FROM heads WHERE account=? AND namespace=?",
            (first["account"], first["namespace"]),
        ).fetchone()
        manifest = (
            json.loads(
                db.execute("SELECT manifest FROM snapshots WHERE id=?", (head[0],)).fetchone()[0]
            )
            if head
            else {}
        )
        for row in rows:
            item = {"generation": row["generation"], "plan": row["plan"]}
            source = db.execute(
                "SELECT reference FROM sources WHERE generation=?", (row["generation"],)
            ).fetchone()
            if source:
                item["content"] = json.loads(source[0])
            semantic = db.execute(
                "SELECT reference FROM semantic_sources WHERE generation=?", (row["generation"],)
            ).fetchone()
            if semantic:
                item["semantic_contents"] = json.loads(semantic[0])
            manifest[row["uri"]] = item
            db.execute(
                "UPDATE builds SET state='published' WHERE generation=?", (row["generation"],)
            )
        snapshot = uuid4().hex
        db.execute(
            "INSERT INTO snapshots VALUES (?,?,?,?,?)",
            (snapshot, first["account"], first["namespace"], json.dumps(manifest), time.time()),
        )
        db.execute(
            "INSERT OR REPLACE INTO heads VALUES (?,?,?)",
            (first["account"], first["namespace"], snapshot),
        )

    def _publish_batch_if_complete(self, db, batch):
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
        for row in rows:
            reason = self._invalid_reason(db, row)
            if row["state"] in {"failed", "superseded"} or reason:
                self._fail_batch(db, batch, reason or row["error"] or "Batch member failed")
                return False
        if not rows or not all(self._has_all_receipts(db, row) for row in rows):
            return False
        self._publish_rows(db, rows)
        db.execute("UPDATE batches SET state='published' WHERE id=?", (batch,))
        return True

    def _publish_if_complete(self, db, row):
        member = db.execute(
            "SELECT batch FROM batch_members WHERE generation=?", (row["generation"],)
        ).fetchone()
        if member:
            if not self._has_all_receipts(db, row):
                return False
            return self._publish_batch_if_complete(db, member[0])
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
        self._publish_rows(db, [row])
        return True

    def can_write(self, generation, fields):
        with self.db() as db:
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
        with self.db() as db:
            row = self._build(db, generation)
            if row["state"] == "building":
                db.execute("UPDATE builds SET state='queued' WHERE generation=?", (generation,))
            return self._publish_if_complete(db, self._build(db, generation))

    def acknowledge(self, generation, child):
        with self.db() as db:
            row = self._build(db, generation)
            if child not in json.loads(row["expected"]):
                raise ValueError("Unexpected child cannot satisfy publication")
            if row["state"] not in {"building", "queued"}:
                return False
            db.execute("INSERT OR IGNORE INTO receipts VALUES (?,?)", (generation, child))
            return self._publish_if_complete(db, row)

    def fail(self, generation, reason):
        with self.db() as db:
            self._build(db, generation)
            member = db.execute(
                "SELECT batch FROM batch_members WHERE generation=?", (generation,)
            ).fetchone()
            if member:
                self._fail_batch(db, member[0], reason)
            db.execute(
                "UPDATE builds SET state='failed',error=? WHERE generation=? AND state IN ('building','queued')",
                (reason, generation),
            )

    def state(self, generation):
        with self.db() as db:
            return dict(self._build(db, generation))

    def capture(self, account, ttl=120):
        with self.db() as db:
            rows = db.execute(
                "SELECT s.id,s.manifest FROM heads h JOIN snapshots s ON s.id=h.snapshot WHERE h.account=?",
                (account,),
            ).fetchall()
            manifest = {}
            for row in rows:
                for uri, value in json.loads(row["manifest"]).items():
                    if uri in manifest:
                        raise RuntimeError("Overlapping publication namespaces")
                    manifest[uri] = value
            lease = uuid4().hex
            db.execute(
                "INSERT INTO leases VALUES (?,?,?)",
                (lease, json.dumps([r["id"] for r in rows]), time.time() + ttl),
            )
        return lease, manifest

    def release(self, lease):
        with self.db() as db:
            db.execute("DELETE FROM leases WHERE id=?", (lease,))

    def renew(self, lease, ttl=120):
        with self.db() as db:
            changed = db.execute(
                "UPDATE leases SET expires=? WHERE id=? AND expires>?",
                (time.time() + ttl, lease, time.time()),
            ).rowcount
            if not changed:
                raise RuntimeError("Publication query lease expired; retry query")

    # ── Tombstones ──

    def tombstone(self, account, uri):
        """Write a deletion tombstone; recall and read-back must refuse at once."""
        if not isinstance(uri, str) or not uri.startswith("viking://"):
            raise ValueError("Tombstone requires a canonical URI")
        with self.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO tombstones VALUES (?,?,?)", (account, uri, time.time())
            )

    def clear_tombstone(self, account, uri):
        """Drop a tombstone only when the source is re-created as current content."""
        with self.db() as db:
            db.execute("DELETE FROM tombstones WHERE account=? AND uri=?", (account, uri))

    def tombstones(self, account):
        with self.db() as db:
            return [row[0] for row in db.execute("SELECT uri FROM tombstones WHERE account=?", (account,))]

    def is_tombstoned(self, account, uri):
        """True when uri or any ancestor directory has an active tombstone."""
        with self.db() as db:
            rows = [row[0] for row in db.execute("SELECT uri FROM tombstones WHERE account=?", (account,))]
        return any(uri == dead or uri.startswith(dead.rstrip("/") + "/") for dead in rows)

    def sources(self, account):
        """Published source URIs for an account, across all namespaces."""
        with self.db() as db:
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
            lease, manifest = self.capture(account)
        else:
            if view[0] != account or view[1].path != self.path:
                raise RuntimeError("Source query view belongs to another account/store")
            lease, manifest = view[2], view[3]
        try:
            self.renew(lease)
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
            self.renew(lease)
        finally:
            if owned:
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
                "SELECT account,manifest FROM snapshots WHERE id=?", (snapshot,)
            ).fetchone()
            for item in json.loads(row["manifest"]).values():
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
            generations = set()
            for snapshot in protected:
                manifest = json.loads(
                    db.execute("SELECT manifest FROM snapshots WHERE id=?", (snapshot,)).fetchone()[
                        0
                    ]
                )
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
        """Pin the current reachable snapshots for one reader/task, like a query lease."""
        lease = uuid4().hex
        with self.db() as db:
            snapshots = [
                row[0] for row in db.execute("SELECT snapshot FROM heads WHERE account=?", (account,))
            ]
            db.execute(
                "INSERT INTO gc_leases VALUES (?,?,?,?)",
                (lease, account, json.dumps(snapshots), time.time() + ttl),
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
        for snapshot in leased:
            row = db.execute("SELECT account,manifest FROM snapshots WHERE id=?", (snapshot,)).fetchone()
            if row is None:
                continue
            for item in json.loads(row["manifest"]).values():
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
            for row in db.execute("SELECT account,manifest FROM snapshots"):
                for uri, item in json.loads(row["manifest"]).items():
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
        if view[0] != ctx.account_id or view[1].path != registry.path:
            raise RuntimeError("Index query view belongs to a different account/store")
        registry.renew(view[2])
        return registry.visibility(view[3], ctx.account_id)
    # Non-public low-level readers capture only this operation's view.
    lease, manifest = registry.capture(ctx.account_id)
    try:
        return registry.visibility(manifest, ctx.account_id)
    finally:
        registry.release(lease)


def query_snapshot(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        registry = configured_store()
        if registry is None or _view.get() is not None:
            return await function(*args, **kwargs)
        ctx = inspect.signature(function).bind_partial(*args, **kwargs).arguments.get("ctx")
        if ctx is None:
            ctx = args[0]._ctx_or_default(None)
        lease, manifest = registry.capture(ctx.account_id)
        token = _view.set((ctx.account_id, registry, lease, manifest))
        try:
            result = await function(*args, **kwargs)
            registry.renew(lease)  # Never return a result after its view lease expired.
            return result
        finally:
            _view.reset(token)
            registry.release(lease)

    return wrapped
