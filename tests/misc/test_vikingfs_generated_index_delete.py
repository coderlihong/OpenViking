# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Deleting a generated directory catalogue must fail loudly, not no-op.

``{dir}/index.md`` is rebuilt from the directory's children after any change.
Deleting it used to answer ok with ``estimated_deleted_count=0`` while the file
was still listed (the refresh recreated it inside the same request), which
looks like a silent data loss bug. Authored bodies that merely share the
filename stay deletable; the frontmatter marker decides.
"""

from __future__ import annotations

import contextvars
from unittest.mock import AsyncMock

import pytest

from openviking.indexing import publication
from openviking.server.identity import RequestContext, Role
from openviking.storage.viking_fs import VikingFS
from openviking_cli.exceptions import FailedPreconditionError
from openviking_cli.session.user_id import UserIdentifier

GENERATED = (
    b"---\n"
    b"directory: viking://resources/\n"
    b"entry_count: 1\n"
    b"generated_by:\n"
    b"  component: SemanticDagExecutor\n"
    b"  trigger: content_delete\n"
    b"kind: directory_index\n"
    b"---\n\n# resources\n"
)


def _make_viking_fs() -> VikingFS:
    fs = VikingFS.__new__(VikingFS)
    fs.agfs = AsyncMock()
    fs._async_agfs = fs.agfs
    fs.query_embedder = None
    fs.rerank_config = None
    fs.grep_config = None
    fs.vector_store = None
    fs.acl_manager = None
    fs._encryptor = None
    fs._bound_ctx = contextvars.ContextVar("vikingfs_bound_ctx_generated_index", default=None)
    return fs


def _user_ctx() -> RequestContext:
    return RequestContext(
        user=UserIdentifier(account_id="acct1", user_id="alice"),
        role=Role.USER,
    )


@pytest.mark.asyncio
async def test_rm_refuses_generated_directory_index(monkeypatch):
    monkeypatch.setattr(publication, "configured_store", lambda: None)
    fs = _make_viking_fs()
    fs.agfs.stat = AsyncMock(return_value={"isDir": False})
    fs.agfs.read = AsyncMock(return_value=GENERATED)
    tombstones: list[str] = []
    monkeypatch.setattr(
        VikingFS, "_tombstone_uri", lambda self, uri, ctx: tombstones.append(uri)
    )

    with pytest.raises(FailedPreconditionError, match="generated directory catalogue"):
        await fs.rm("viking://resources/index.md", ctx=_user_ctx())

    # A refused removal must not leave a deletion fence behind: the tombstone
    # would make reads answer NOT_FOUND while the file is still listed.
    assert tombstones == []
    fs.agfs.rm.assert_not_called()


@pytest.mark.asyncio
async def test_rm_still_allows_an_authored_body_named_index(monkeypatch):
    monkeypatch.setattr(publication, "configured_store", lambda: None)
    fs = _make_viking_fs()
    fs.agfs.stat = AsyncMock(return_value={"isDir": False})
    fs.agfs.read = AsyncMock(return_value=b"# legacy chapter prose\n\nNo frontmatter here.\n")
    fs.agfs.pathlock_acquire_exact = AsyncMock(return_value={"lease_ref": "lease-1"})
    fs.agfs.pathlock_release = AsyncMock()
    fs.agfs.rm = AsyncMock(return_value=True)

    result = await fs.rm("viking://resources/index.md", ctx=_user_ctx())

    fs.agfs.rm.assert_awaited_once()
    assert result["estimated_deleted_count"] == 0
