"""Deletion propagation: instant tombstones and reference-safe physical GC (KB-007)."""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_content_publication import DiskAGFS, environment as environment  # noqa: F401
from test_image_publication import IMAGE, ROOT, TEXT, consume, image_env, manifest, stage  # noqa: F401
from test_index_plan import Embedder, Queue, embedding as embedding  # noqa: F401

from openviking.indexing.content import blob_path
from openviking.indexing.publication import PublicationStore
from openviking.server.identity import RequestContext, Role
from openviking.storage.vectordb.index.cuvs_index import matches_filter
from openviking.storage.vectordb_adapters.local_adapter import LocalCollectionAdapter
from openviking.storage.viking_fs import VikingFS
from openviking_cli.exceptions import NotFoundError, FailedPreconditionError
from openviking_cli.session.user_id import UserIdentifier


def visible(store, account="acct"):
    lease, current = store.capture(account)
    store.release(lease)
    return current


def backend_matches(store, uri, generation, account="acct"):
    """Evaluate the compiled recall filter the way the local backend does."""
    adapter = object.__new__(LocalCollectionAdapter)
    compiled = adapter._compile_filter(store.visibility(visible(store, account), account))
    fields = {"uri": adapter._encode_uri_field_value(uri), "index_generation": generation}
    return matches_filter(fields, compiled, {"uri": "path", "index_generation": "string"})


class _LockingDiskAGFS(DiskAGFS):
    """DiskAGFS plus the no-op pathlock surface VikingFS.rm expects."""

    async def pathlock_acquire_exact(self, path):
        return {"lease_ref": "test"}

    async def pathlock_acquire_tree(self, path):
        return {"lease_ref": "test"}

    async def pathlock_release(self, lease):
        return None

    async def rm(self, path, recursive=False, **kwargs):
        target = self.path(path)
        if target.is_dir():
            for child in sorted(target.rglob("*"), reverse=True):
                child.rmdir() if child.is_dir() else child.unlink()
            target.rmdir()
        else:
            target.unlink()
        return {"removed": 1}


def fs_with_tombstones(tmp_path, registry):
    """A VikingFS whose AGFS is a real disk fixture and whose registry is real."""
    fs = VikingFS.__new__(VikingFS)
    fs._async_agfs = _LockingDiskAGFS(tmp_path / "agfs")
    fs.acl_manager = None
    fs.vector_store = None
    import contextvars

    fs._bound_ctx = contextvars.ContextVar("deletion_gc_ctx", default=None)
    fs._ensure_access = AsyncMock()
    return fs


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = str(tmp_path / "registry.db")
    monkeypatch.setattr(
        "openviking.indexing.publication.configured_store",
        lambda embedding=None: PublicationStore(path),
    )
    return PublicationStore(path)


def publish(store, uri, children=("a", "b"), account="acct", namespace="resources", ref=None):
    generation = store.begin(account, namespace, uri, "plan", list(children), source_ref=ref)
    store.seal(generation)
    for child in children:
        store.acknowledge(generation, child)
    return generation


def content_ref(body):
    return {"sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}


def test_tombstone_excludes_every_recall_path_at_once(store):
    uri = "viking://resources/manual/a.md"
    other = "viking://resources/manual/b.md"
    generation = publish(store, uri, ref=content_ref(b"deleted body"))
    other_generation = publish(store, other, ref=content_ref(b"kept body"))

    assert backend_matches(store, uri, generation)
    assert backend_matches(store, other, other_generation)

    store.tombstone("acct", uri)

    # Dense/lexical/parent/meta recall share the compiled visibility filter, so a
    # tombstoned URI is refused before ranking while its sibling still matches.
    assert not backend_matches(store, uri, generation)
    assert backend_matches(store, other, other_generation)
    # A child URI under the deleted document is refused too.
    assert not backend_matches(store, uri + "/child.md", generation)
    # The active snapshot still names the generation; only the tombstone hides it.
    assert uri in visible(store)


async def test_read_back_is_refused_after_delete_before_physical_cleanup(store, tmp_path):
    uri = "viking://resources/manual/a.md"
    body = "# manual\n\nold knowledge\n"
    fs = fs_with_tombstones(tmp_path, store)
    ctx = RequestContext(user=UserIdentifier("acct", "user"), role=Role.USER)
    path = fs._uri_to_path(uri, ctx=ctx)
    await fs._async_agfs.write(path, body.encode())
    assert await fs.read_file(uri, ctx=ctx) == body

    # Tombstone without deleting the bytes: read-back must already refuse.
    store.tombstone("acct", uri)
    with pytest.raises(NotFoundError):
        await fs.read_file(uri, ctx=ctx)
    with pytest.raises(NotFoundError):
        await fs.read_file_bytes(uri, ctx=ctx)
    with pytest.raises(NotFoundError):
        await fs.read(uri, ctx=ctx)
    assert fs._async_agfs.path(path).exists()


async def test_rm_writes_tombstone_so_late_reads_fail(store, tmp_path):
    uri = "viking://resources/manual/a.md"
    fs = fs_with_tombstones(tmp_path, store)
    ctx = RequestContext(user=UserIdentifier("acct", "user"), role=Role.USER)
    await fs._async_agfs.write(fs._uri_to_path(uri, ctx=ctx), b"body")
    await fs.rm(uri, ctx=ctx)
    assert store.is_tombstoned("acct", uri)
    with pytest.raises(NotFoundError):
        await fs.read_file(uri, ctx=ctx)


def test_dependents_mark_child_chunks_cache_and_sidecars(store):
    uri = "viking://resources/manual/a.png"
    ref = content_ref(b"asset")
    ref["image_semantics"] = content_ref(b"cache")
    ref["semantic_contents"] = {"0": content_ref(b"l0"), "1": content_ref(b"l1")}
    generation = publish(store, uri, children=("image:0", "lexical:x"), ref=ref)

    deps = store.dependents("acct", uri)
    assert generation in deps["generations"]
    assert set(deps["content_shas"]) == {
        ref["sha256"],
        ref["image_semantics"]["sha256"],
    }
    assert set(deps["children"]) == {"image:0", "lexical:x"}


async def test_cas_protects_asset_re_referenced_after_gc_mark(store):
    uri = "viking://resources/manual/a.md"
    old_ref = content_ref(b"old body")
    store.observe_draft("acct", uri)
    old = publish(store, uri, ref=old_ref)
    # Retire the old generation by publishing a newer one.
    store.observe_draft("acct", uri)
    publish(store, uri, ref=content_ref(b"new body"))

    marked = store.plan_gc("acct", grace_seconds=0)
    assert old_ref["sha256"] in {item["sha"] for item in marked}

    # A concurrent publish re-references the old asset before cleanup runs.
    re_ref = publish(
        store, "viking://resources/manual/copy.md", children=("c",), ref=old_ref
    )
    assert re_ref

    released = []

    async def release(account, sha):
        released.append(sha)
        return True

    outcomes = await store.reclaim("acct", delete=release)
    assert old_ref["sha256"] not in released
    assert any(item["reason"] == "referenced" for item in outcomes)
    assert store.gc_audit("acct") == []


async def test_lease_pending_and_wiki_references_block_release(store):
    uri = "viking://resources/manual/a.md"
    old_ref = content_ref(b"shared asset")
    store.observe_draft("acct", uri)
    old = publish(store, uri, ref=old_ref)

    # A held query/task lease pins the old generation.
    lease = store.gc_lease("acct")
    store.observe_draft("acct", uri)
    publish(store, uri, ref=content_ref(b"replacement"))
    assert old_ref["sha256"] not in {item["sha"] for item in store.plan_gc("acct", grace_seconds=0)}

    released = []

    async def release(account, sha):
        released.append(sha)
        return True

    outcomes = await store.reclaim("acct", delete=release)
    assert old_ref["sha256"] not in released
    assert outcomes == []

    store.gc_release(lease)
    assert old_ref["sha256"] in {item["sha"] for item in store.plan_gc("acct", grace_seconds=0)}


def test_pending_retry_generation_is_protected(store):
    uri = "viking://resources/manual/a.md"
    store.observe_draft("acct", uri)
    publish(store, uri, ref=content_ref(b"live body"))
    retry_ref = content_ref(b"retry body")
    pending = store.begin("acct", "resources", uri, "retry-plan", ["r"], source_ref=retry_ref)
    store.seal(pending)
    # The retry job has not finished; its source revision must stay reachable.
    assert retry_ref["sha256"] in {
        sha for _account, sha in store.protected_content_revisions()
    }
    assert retry_ref["sha256"] not in {item["sha"] for item in store.plan_gc("acct", grace_seconds=0)}


async def test_retention_and_release_audit_after_references_clear(store):
    uri = "viking://resources/manual/a.md"
    old_ref = content_ref(b"old body")
    store.observe_draft("acct", uri)
    old = publish(store, uri, ref=old_ref)
    store.observe_draft("acct", uri)
    publish(store, uri, ref=content_ref(b"new body"))

    assert old_ref["sha256"] in {item["sha"] for item in store.plan_gc("acct", grace_seconds=0)}

    # Retention not yet satisfied: nothing is released and nothing is audited.
    async def keep(account, sha):
        return True

    outcomes = await store.reclaim("acct", retention_seconds=3600, delete=keep)
    assert outcomes[0]["reason"] == "retention"
    assert store.gc_audit("acct") == []

    # Retention alone does not prove an all-index physical release capability.
    released = []

    async def release(account, sha):
        released.append(sha)
        return True

    with pytest.raises(FailedPreconditionError, match='PHYSICAL_GC_CAPABILITY_REQUIRED'):
        await store.reclaim("acct", retention_seconds=0, delete=release)
    assert released == []
    assert store.gc_audit("acct") == []


async def test_failed_unregistered_attempt_is_marked_but_boolean_callback_cannot_release(store):
    uri = "viking://resources/manual/broken.md"
    orphan_ref = content_ref(b"orphan body")
    generation = store.begin("acct", "resources", uri, "plan", ["x"], source_ref=orphan_ref)
    store.fail(generation, "embedding unavailable")

    marked = store.plan_gc("acct", grace_seconds=0)
    assert orphan_ref["sha256"] in {item["sha"] for item in marked}
    assert {item["kind"] for item in marked if item["sha"] == orphan_ref["sha256"]} == {"orphan"}

    released = []

    async def release(account, sha):
        released.append(sha)
        return True

    with pytest.raises(FailedPreconditionError, match='PHYSICAL_GC_CAPABILITY_REQUIRED'):
        await store.reclaim("acct", delete=release)
    assert released == []
    assert store.gc_audit("acct") == []


async def test_deletion_service_tombstones_scope_but_unverified_agfs_gc_stays_pending(store, tmp_path):
    from openviking.indexing.content import blob_path
    from openviking.service.deletion import DeletionService

    uri = "viking://user/user/resources/a.md"
    other = "viking://user/other/resources/b.md"
    body = b"deletable body"
    fs = fs_with_tombstones(tmp_path, store)
    ctx = RequestContext(user=UserIdentifier("acct", "user"), role=Role.USER)
    ref = content_ref(body)
    publish(store, uri, ref=ref)
    publish(store, other, children=("o",), ref=content_ref(b"other body"))
    await fs._async_agfs.write(blob_path(fs, ctx, ref["sha256"]), body)

    service = DeletionService.__new__(DeletionService)
    service._service = SimpleNamespace(viking_fs=fs)
    service._gc_grace_seconds = 0
    service._gc_retention_seconds = 0

    # A user-scoped delete only tombstones that user's sources.
    await service._tombstone_scope("acct", "user", ctx=ctx)
    assert store.is_tombstoned("acct", uri)
    assert not store.is_tombstoned("acct", other)

    # The tombstone alone does not free the derived body.
    assert ref["sha256"] not in {item["sha"] for item in store.plan_gc("acct", grace_seconds=0)}
    assert fs._async_agfs.path(blob_path(fs, ctx, ref["sha256"])).exists()

    # Clearing the deletion root and re-publishing releases the retired body.
    store.clear_tombstone("acct", uri)
    publish(store, uri, ref=content_ref(b"replacement"))
    with pytest.raises(FailedPreconditionError, match='PHYSICAL_GC_CAPABILITY_REQUIRED'):
        await service._collect_garbage("acct", ctx=ctx)
    assert fs._async_agfs.path(blob_path(fs, ctx, ref["sha256"])).exists()
    assert store.gc_audit("acct") == []


async def test_tombstoned_scope_keeps_derived_body_until_cleared(store):
    uri = "viking://resources/manual/a.md"
    ref = content_ref(b"body of deleted doc")
    store.observe_draft("acct", uri)
    publish(store, uri, ref=ref)
    store.tombstone("acct", uri)

    # A deleted source's derived body is not freed while its tombstone exists.
    assert ref["sha256"] not in {item["sha"] for item in store.plan_gc("acct", grace_seconds=0)}
    released = []

    async def release(account, sha):
        released.append(sha)
        return True

    await store.reclaim("acct", delete=release)
    assert ref["sha256"] not in released
