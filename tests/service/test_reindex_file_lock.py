# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Regression tests for choosing an exact lock when reindexing a file URI."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from openviking.server.identity import RequestContext, Role
from openviking.service import reindex_executor as reindex_module
from openviking.service.reindex_executor import ReindexExecutor
from openviking_cli.session.user_id import UserIdentifier


class _FakeVikingFS:
    def __init__(self, uri: str, path: str):
        self._uri = uri
        self._path = path
        self._async_agfs = SimpleNamespace(
            pathlock_acquire_tree=AsyncMock(
                side_effect=AssertionError("Tree lock requested for file")
            ),
            pathlock_acquire_exact=AsyncMock(return_value={"lease": "file-lease"}),
            pathlock_as_borrowed=AsyncMock(return_value={"borrowed": "file-lease"}),
            pathlock_release=AsyncMock(),
        )

    def _uri_to_path(self, uri: str, ctx=None):
        assert uri == self._uri
        return self._path

    async def exists(self, uri: str, ctx=None):
        return uri == self._uri

    async def stat(self, uri: str, ctx=None, skip_count=False):
        assert uri == self._uri
        assert skip_count is True
        return {"isDir": False}

    async def read_file(self, uri: str, ctx=None):
        assert uri == self._uri
        return "# Profile\nSingle file reindex source.\n"


async def test_reindex_single_file_uri_acquires_exact_lock_without_not_a_directory(
    tmp_path, monkeypatch, caplog
):
    uri = "viking://resources/profile.md"
    path = str(tmp_path / "profile.md")
    (tmp_path / "profile.md").write_text("# Profile\nSingle file reindex source.\n")

    viking_fs = _FakeVikingFS(uri, path)
    service = SimpleNamespace(
        viking_fs=viking_fs,
        vikingdb_manager=SimpleNamespace(has_queue_manager=True),
    )
    monkeypatch.setattr(reindex_module, "get_service", lambda: service)
    monkeypatch.setattr(reindex_module, "get_viking_fs", lambda: viking_fs)

    executor = ReindexExecutor()
    executor._fetch_existing_record = AsyncMock(return_value=None)
    executor._upsert_context = AsyncMock()
    executor._effective_file_vectorization_concurrency = lambda: 1
    monkeypatch.setattr(
        reindex_module,
        "get_openviking_config",
        lambda: SimpleNamespace(
            embedding=SimpleNamespace(
                chunking=None, text_source="source_content_only", max_input_tokens=1000
            )
        ),
    )
    ctx = RequestContext(
        user=UserIdentifier("acc1", "test_user"),
        role=Role.ROOT,
    )

    try:
        result = await executor._run(
            uri=uri,
            object_type="resource",
            mode="vectors_only",
            ctx=ctx,
        )
    finally:
        pass

    viking_fs._async_agfs.pathlock_acquire_exact.assert_awaited_once_with(path)
    viking_fs._async_agfs.pathlock_acquire_tree.assert_not_awaited()
    viking_fs._async_agfs.pathlock_release.assert_awaited_once_with({"lease": "file-lease"})
    assert result["status"] == "completed"
    assert result["uri"] == uri
    assert result["scanned_records"] == 1
    assert result["rebuilt_records"] == 1
    assert "Not a directory" not in caplog.text
    assert "Failed to create lock file" not in caplog.text
