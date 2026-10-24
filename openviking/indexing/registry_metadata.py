"""Generation-bound business metadata commands for the publication authority."""
from __future__ import annotations

import json
import time

from openviking.indexing.business_meta import AuthoritativeMeta, MetaValue, build_meta
from openviking.indexing.registry_gc import (
    _snapshot_generation_for,
    _snapshot_generation_index,
    _tombstone_covers,
    _under_tombstone,
)
from openviking_cli.exceptions import FailedPreconditionError

METADATA_COMMANDS = ("metadata_write", "metadata_read", "metadata_latest")


def validated_payload(payload):
    if not isinstance(payload, dict) or set(payload) - {
        "document_id", "content_revision", "index_generation", "source", "normalizer_version", "values"
    }:
        raise ValueError("Invalid authoritative metadata payload")
    received = AuthoritativeMeta.from_payload(payload)
    entries = payload.get("values", [])
    if not isinstance(entries, list) or len(entries) != len(received.values):
        raise ValueError("Duplicate or invalid authoritative metadata fields")
    normalized = build_meta(
        {entry.field: entry.raw for entry in received.values.values()},
        document_id=received.document_id, content_revision=received.content_revision,
        source=received.source, index_generation=received.index_generation,
    )
    normalized.values = {field: MetaValue(field, value.raw, value.canonical,
                         received.values[field].source, value.normalizer_version)
                         for field, value in normalized.values.items()}
    if normalized.to_payload() != received.to_payload():
        raise ValueError("Metadata canonical values or provenance are not authoritative")
    return normalized.to_payload()


class RegistryMetadataMixin:
    def init_metadata(self):
        with self.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS publication_metadata (
                generation TEXT PRIMARY KEY, payload TEXT NOT NULL, dependencies TEXT NOT NULL)""")

    @staticmethod
    def _metadata_dependencies(dependencies):
        if not isinstance(dependencies, list) or len(dependencies) > 100:
            raise ValueError("Invalid business metadata source dependencies")
        for reference in dependencies:
            if not isinstance(reference, dict) or set(reference) != {"uri", "draft_epoch", "sha256"}:
                raise ValueError("Invalid business metadata source reference")
            if not isinstance(reference["uri"], str) or not reference["uri"].startswith("viking://") or not reference["uri"].endswith("/.business_meta.json"):
                raise ValueError("Business metadata must use an explicit sidecar URI")
            if isinstance(reference["draft_epoch"], bool) or not isinstance(reference["draft_epoch"], int) or reference["draft_epoch"] < 0:
                raise ValueError("Invalid business metadata source epoch")
            sha = reference["sha256"]
            if sha is not None and (not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha)):
                raise ValueError("Invalid business metadata source SHA")
        if len({reference["uri"] for reference in dependencies}) != len(dependencies):
            raise ValueError("Duplicate metadata source dependencies")

    def metadata_write(self, generation, payload, dependencies):
        """Store immutable metadata and its own receipt in one transaction."""
        self._metadata_dependencies(dependencies)
        payload = validated_payload(payload)
        if payload.get("index_generation") != generation:
            raise ValueError("Metadata generation binding mismatch")
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        references = json.dumps(dependencies, sort_keys=True, separators=(",", ":"))
        with self.db() as db:
            build = self._build(db, generation)
            marker = "metadata:" + build["plan"]
            if marker not in json.loads(build["expected"]) or build["state"] not in {"building", "queued"}:
                raise ValueError("Metadata receipt was not declared or generation is sealed")
            for reference in dependencies:
                self._assert_not_writing(db, build["account"], reference["uri"])
                draft = db.execute("SELECT epoch FROM drafts WHERE account=? AND uri=?",
                                   (build["account"], reference["uri"])).fetchone()
                if draft is None or draft[0] != reference["draft_epoch"]:
                    raise RuntimeError("Business metadata changed before index enqueue")
                if reference["sha256"] is not None:
                    db.execute("UPDATE drafts SET sha=? WHERE account=? AND uri=?",
                               (reference["sha256"], build["account"], reference["uri"]))
            existing = db.execute("SELECT payload,dependencies FROM publication_metadata WHERE generation=?", (generation,)).fetchone()
            if existing and (existing["payload"], existing["dependencies"]) != (serialized, references):
                raise ValueError("Immutable metadata generation changed")
            db.execute("INSERT OR IGNORE INTO publication_metadata VALUES (?,?,?)", (generation, serialized, references))
            inserted = db.execute("INSERT OR IGNORE INTO receipts VALUES (?,?)", (generation, marker)).rowcount
            return self._receipt_recorded(db, self._build(db, generation), inserted)

    def metadata_invalid_reason(self, db, build):
        row = db.execute("SELECT dependencies FROM publication_metadata WHERE generation=?", (build["generation"],)).fetchone()
        if row is None:
            return None
        for reference in json.loads(row["dependencies"]):
            try:
                self._assert_not_writing(db, build["account"], reference["uri"])
            except FailedPreconditionError as exc:
                return str(exc)
            draft = db.execute("SELECT epoch,sha FROM drafts WHERE account=? AND uri=?",
                               (build["account"], reference["uri"])).fetchone()
            if draft is None or draft["epoch"] != reference["draft_epoch"] or (
                reference["sha256"] is not None and draft["sha"] != reference["sha256"]
            ):
                return "Business metadata changed during index build"
        return None

    def metadata_read(self, account, lease, authorized_uris):
        """Read only immutable metadata in an authorized, leased snapshot."""
        if not isinstance(authorized_uris, list) or len(authorized_uris) > 50000 or any(not isinstance(uri, str) for uri in authorized_uris):
            raise ValueError("Explicit bounded authorized URI list required")
        # Read-only: taking BEGIN IMMEDIATE here queued this pure reader behind
        # every writer in the process (measured: thousands of >400ms lock waits
        # from metadata_read during the vector phase).
        with self.db(readonly=True) as db:
            # The lease lives in the dedicated query-lease store; the coverage
            # check must never queue on the publication writer.
            held = self._query_leases.held(lease)
            if held is None:
                raise ValueError("Metadata query lease expired")
            selected = set(authorized_uris)
            # Set-based ancestor test: the previous per-URI `any(...)` scan over
            # every tombstone of the account multiplied into 88k x 60k string
            # comparisons per metadata_read.
            denied = {row[0] for row in db.execute("SELECT uri FROM tombstones WHERE account=?", (account,))}
            result = []
            for snapshot_id in json.loads(held["snapshots"]):
                # Only the two identifying columns: reading the manifest column
                # here pulled an ~11MB gzip blob into Python on every call.
                snapshot = db.execute("SELECT account,namespace FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
                if snapshot is None or snapshot["account"] != account:
                    raise ValueError("Metadata snapshot account mismatch")
                # Memoized immutable {uri: generation} view of the snapshot
                # manifest. Decoding it inline cost ~116ms of event-loop CPU per
                # node (metadata_latest runs once per file during reindex and was
                # measured as multi-second readonly transactions with 0ms of lock
                # wait).
                for uri, generation in _snapshot_generation_index(db, snapshot_id).items():
                    if uri not in selected or _under_tombstone(uri, denied):
                        continue
                    record = db.execute("SELECT payload,dependencies FROM publication_metadata WHERE generation=?", (generation,)).fetchone()
                    result.append({"uri": uri, "namespace_id": snapshot["namespace"],
                                   "generation": generation, "payload": json.loads(record["payload"]) if record else None,
                                   "dependencies": json.loads(record["dependencies"]) if record else []})
            return result

    def metadata_latest(self, account, uri):
        """Latest published authoritative metadata for a single URI.

        This runs once per file during reindex, so it must not materialize an
        account manifest. ``metadata_read`` decodes an ~11MB snapshot blob, and
        reading the head snapshot still decoded the *whole* manifest just to look
        one URI up -- 4.9s per call on a 185k-entry head, on the event loop.

        It must still read the *head snapshot*, not ``desired``: ``desired``
        records what a build was asked to produce, while this must report what is
        actually published. They diverge whenever a build fails, and answering
        from ``desired`` there returns metadata for a generation that never
        published (test_source_edit_during_build_blocks_all_pending_metadata).
        So the manifest stays the authority, but only the one entry is extracted.
        """
        with self.db(readonly=True) as db:
            if _tombstone_covers(db, account, uri):
                return None
            for row in db.execute(
                "SELECT s.id, s.namespace FROM heads h JOIN snapshots s ON s.id=h.snapshot WHERE h.account=?",
                (account,),
            ).fetchall():
                generation = _snapshot_generation_for(db, row["id"], uri)
                if generation is None:
                    continue
                record = db.execute(
                    "SELECT payload,dependencies FROM publication_metadata WHERE generation=?", (generation,)
                ).fetchone()
                return {"uri": uri, "namespace_id": row["namespace"], "generation": generation,
                        "payload": json.loads(record["payload"]) if record else None,
                        "dependencies": json.loads(record["dependencies"]) if record else []}
            return None
