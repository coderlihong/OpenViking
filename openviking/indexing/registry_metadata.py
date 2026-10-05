"""Generation-bound business metadata commands for the publication authority."""
from __future__ import annotations

import json
import time

from openviking.indexing.business_meta import AuthoritativeMeta, MetaValue, build_meta

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
            db.execute("INSERT OR IGNORE INTO receipts VALUES (?,?)", (generation, marker))
            return self._publish_if_complete(db, build)

    def metadata_invalid_reason(self, db, build):
        row = db.execute("SELECT dependencies FROM publication_metadata WHERE generation=?", (build["generation"],)).fetchone()
        if row is None:
            return None
        for reference in json.loads(row["dependencies"]):
            try:
                self._assert_not_writing(db, build["account"], reference["uri"])
            except RuntimeError as exc:
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
        with self.db() as db:
            held = db.execute("SELECT snapshots FROM leases WHERE id=? AND expires>?", (lease, time.time())).fetchone()
            if held is None:
                raise ValueError("Metadata query lease expired")
            selected = set(authorized_uris)
            denied = [row[0] for row in db.execute("SELECT uri FROM tombstones WHERE account=?", (account,))]
            result = []
            for snapshot_id in json.loads(held["snapshots"]):
                snapshot = db.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
                if snapshot is None or snapshot["account"] != account:
                    raise ValueError("Metadata snapshot account mismatch")
                for uri, item in json.loads(snapshot["manifest"]).items():
                    if uri not in selected or any(uri == root or uri.startswith(root.rstrip("/") + "/") for root in denied):
                        continue
                    record = db.execute("SELECT payload,dependencies FROM publication_metadata WHERE generation=?", (item["generation"],)).fetchone()
                    result.append({"uri": uri, "namespace_id": snapshot["namespace"],
                                   "generation": item["generation"], "payload": json.loads(record["payload"]) if record else None,
                                   "dependencies": json.loads(record["dependencies"]) if record else []})
            return result

    def metadata_latest(self, account, uri):
        lease, _ = self.capture(account)
        try:
            rows = self.metadata_read(account, lease, [uri])
            return rows[0] if rows else None
        finally:
            self.release(lease)
