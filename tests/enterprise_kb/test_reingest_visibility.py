# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""A deleted URI has to become readable again when the branch is re-ingested.

Deleting a resource writes a tombstone so no stale vector row or read-back can
still surface it. Ingest, however, publishes the assembled document with one
whole-tree copy that never passes the per-file write which clears a tombstone.
Re-uploading a branch that had been deleted therefore landed real bytes that
every read refused as gone -- and since the caption, summary and embedding steps
of the same ingest read those files, the ingest retried them forever and never
published anything.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.indexing.publication import PublicationStore
from openviking.server.identity import RequestContext, Role
from openviking.storage.viking_fs import VikingFS
from openviking_cli.session.user_id import UserIdentifier

BRANCH = "viking://resources/caption-live"
LEAF = BRANCH + "/note/note.md"
SIBLING = "viking://resources/other-branch"


def _ctx() -> RequestContext:
    return RequestContext(user=UserIdentifier("acct", "alice"), role=Role.ROOT)


@pytest.fixture
def store(tmp_path):
    return PublicationStore(str(tmp_path / "publication.db"))


def test_clearing_a_subtree_leaves_other_branches_tombstoned(store):
    """A re-created branch clears its own rows and its children's, nobody else's."""
    account = _ctx().account_id
    for uri in (BRANCH, LEAF, SIBLING):
        store.tombstone(account, uri)

    cleared = store.clear_tombstone_subtree(account, BRANCH)

    assert cleared == 2
    assert not store.is_tombstoned(account, BRANCH)
    assert not store.is_tombstoned(account, LEAF)
    assert store.is_tombstoned(account, SIBLING)


def _fs_with_registry(monkeypatch, store, copied):
    """A VikingFS whose tombstone registry is the real store and whose cp is recorded."""
    fs = VikingFS.__new__(VikingFS)

    async def _cp(src, dst, **_kwargs):
        copied.append((src, dst))

    fs._async_agfs = SimpleNamespace(cp=_cp)
    monkeypatch.setattr(fs, "_ensure_access", AsyncMock())
    monkeypatch.setattr(fs, "_ensure_parent_dirs", AsyncMock())
    monkeypatch.setattr(fs, "_pathlock_fs_ctx", lambda *_a, **_k: {"account_id": "acct"})
    monkeypatch.setattr(
        fs,
        "_uri_to_path",
        lambda uri, **_kwargs: f"/local/acct/{uri.removeprefix('viking://')}",
    )
    monkeypatch.setattr(VikingFS, "_tombstone_registry", staticmethod(lambda: store))
    return fs


@pytest.mark.asyncio
async def test_persisting_a_re_ingested_branch_makes_it_readable_again(monkeypatch, store):
    """The whole-tree copy is a re-creation, so the destination's tombstones fall first."""
    account = _ctx().account_id
    store.tombstone(account, BRANCH)
    store.tombstone(account, LEAF)
    copied = []
    fs = _fs_with_registry(monkeypatch, store, copied)

    await fs.persist_temp_tree("viking://temp/ingest/run/caption-live", BRANCH, ctx=_ctx())

    assert copied == [
        (
            "/local/acct/temp/ingest/run/caption-live",
            f"/local/acct/{BRANCH.removeprefix('viking://')}",
        )
    ]
    assert not store.is_tombstoned(account, BRANCH)
    assert not store.is_tombstoned(account, LEAF)


@pytest.mark.asyncio
async def test_persisting_under_a_deleted_parent_clears_that_ancestor(monkeypatch, store):
    """A tombstone on an ancestor would hide the new branch just as hard."""
    account = _ctx().account_id
    store.tombstone(account, "viking://resources")
    copied = []
    fs = _fs_with_registry(monkeypatch, store, copied)

    await fs.persist_temp_tree("viking://temp/ingest/run/doc", BRANCH, ctx=_ctx())

    assert copied
    assert not store.is_tombstoned(account, LEAF)
