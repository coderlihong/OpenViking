# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""HTTP/SDK, durable options and task-info contracts for chapter imports."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from openviking.parse.parser_router import ParserRouter
from openviking.server.identity import RequestContext, Role
from openviking.server.routers import resources
from openviking.server.routers.resources import AddResourceRequest
from openviking.service.resource_service import ResourceService
from openviking.service.task_store import PersistentTaskStore
from openviking.service.task_tracker import TaskTracker
from openviking.storage.queuefs.add_resource_msg import AddResourceMsg
from openviking_cli.exceptions import InvalidArgumentError
from openviking_cli.session.user_id import UserIdentifier
from tests.test_task_tracker import _FakeAgfs


async def test_http_chapter_options_pass_through_without_mutating_args(monkeypatch):
    add = AsyncMock(return_value={"status": "success", "task_id": "t1"})
    monkeypatch.setattr(
        resources,
        "get_service",
        lambda: SimpleNamespace(resources=SimpleNamespace(add_resource=add)),
    )
    ctx = RequestContext(user=UserIdentifier("account", "user"), role=Role.USER)
    request = AddResourceRequest(
        path="https://example.com/manual.pdf",
        structure_policy="bookmarks_preferred",
        parse_backend="local",
        section_target_tokens=2048,
        args={"site": False},
    )
    await resources.add_resource(SimpleNamespace(headers={}), request, ctx)
    assert add.await_args.kwargs["args"] == {
        "structure_policy": "bookmarks_preferred",
        "parse_backend": "local",
        "section_target_tokens": 2048,
        "site": False,
    }
    assert request.args == {"site": False}


@pytest.mark.parametrize(
    "args",
    [
        {"structure_policy": "manual_review"},
        {"parse_backend": "unknown-model"},
        {"section_target_tokens": 0, "structure_policy": "bookmarks_preferred"},
        {"section_target_tokens": True, "structure_policy": "bookmarks_preferred"},
        {"structure_policy": "bookmarks_preferred", "parse_mode": "no_split"},
    ],
)
def test_unavailable_or_invalid_options_are_rejected_before_queue(args):
    with pytest.raises(InvalidArgumentError):
        ResourceService()._normalize_add_resource_args(args, watch_interval=0)


def test_http_duplicate_option_sources_rejected():
    with pytest.raises(ValidationError, match="both"):
        AddResourceRequest(
            path="https://example.com/manual.pdf",
            structure_policy="bookmarks_preferred",
            args={"structure_policy": "bookmarks_preferred"},
        )


async def test_sdk_posts_typed_chapter_options(monkeypatch):
    from openviking_sdk import AsyncHTTPClient

    client = AsyncHTTPClient(url="http://localhost:1933")
    request = AsyncMock(return_value=object())
    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr(client, "_handle_response_data", lambda _: {"result": {"task_id": "t1"}})
    options = {
        "structure_policy": "bookmarks_preferred",
        "parse_backend": "local",
        "section_target_tokens": 1024,
    }
    assert await client.add_resource(
        "https://example.com/manual.pdf", parent="viking://resources/raw", options=options
    ) == {"task_id": "t1"}
    body = request.await_args.kwargs["json"]
    assert {k: body[k] for k in options} == options
    assert body["parent"] == "viking://resources/raw"
    await client.close()


def test_chapter_options_survive_queue_roundtrip():
    msg = AddResourceMsg(
        task_id="t1",
        account_id="a",
        user_id="u",
        role="user",
        root_uri="viking://resources/manual",
        path="/tmp/manual.pdf",
        args={
            "structure_policy": "bookmarks_preferred",
            "parse_backend": "local",
            "section_target_tokens": 4096,
        },
    )
    restored = AddResourceMsg.from_dict(msg.to_dict())
    assert restored.args == msg.args


async def test_info_survives_restart_and_appears_in_task_list_and_detail():
    store = PersistentTaskStore(_FakeAgfs())
    tracker = TaskTracker(store)
    task = await tracker.create("add_resource", account_id="a", user_id="u")
    progress = {
        "info": [
            {
                "code": "BOOKMARK_ALIGNMENT_FAILED",
                "message": "书签对齐未成功",
                "fallback_strategy": "markdown_headings",
            }
        ],
        "alignment_status": "failed",
        "structure_source": "markdown_headings",
    }
    await tracker.start(task.task_id, "a", "u", stage="parsing")
    await tracker.update_stage(task.task_id, "persisting", "a", "u", meta=progress)
    await tracker.complete(task.task_id, {"root_uri": "viking://resources/manual"}, "a", "u")
    restored = TaskTracker(store)
    detail = await restored.get(task.task_id, "a", "u")
    assert detail.to_dict()["info"] == progress["info"]
    assert detail.status.value == "completed"
    assert detail.error is None
    tasks = await restored.list_tasks(account_id="a", user_id="u")
    assert tasks[0].to_dict()["alignment_status"] == "failed"


async def test_chapter_router_bypasses_understanding(monkeypatch):
    registry = SimpleNamespace(parse=AsyncMock(return_value="parsed"))
    router = ParserRouter(registry)
    monkeypatch.setattr(router, "should_use_understanding_api", lambda *_a, **_k: True)
    assert await router.parse("manual.pdf", structure_policy="bookmarks_preferred") == "parsed"
    assert registry.parse.await_args.kwargs["structure_policy"] == "bookmarks_preferred"
    # An authored markdown asked for a chapter tree, so it still stays away from
    # the external parser; the PDF-only policy just has nothing to act on here.
    assert await router.parse("manual.md", structure_policy="bookmarks_preferred") == "parsed"
    assert "structure_policy" not in registry.parse.await_args.kwargs


@pytest.mark.parametrize("size", [True, "1024", 0, 100001])
def test_http_section_budget_rejects_coercion_and_out_of_range(size):
    with pytest.raises(ValidationError):
        AddResourceRequest(
            path="manual.pdf",
            structure_policy="bookmarks_preferred",
            section_target_tokens=size,
        )


async def test_sdk_local_upload_preserves_original_filename_and_chapter_options(
    monkeypatch, tmp_path
):
    from openviking_sdk import AsyncHTTPClient

    source = tmp_path / "维修手册.pdf"
    source.write_bytes(b"%PDF-test")
    client = AsyncHTTPClient(url="http://localhost:1933")
    request = AsyncMock(return_value=object())
    upload = AsyncMock(return_value="upload-id")
    monkeypatch.setattr(client, "_upload_temp_file", upload)
    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr(client, "_handle_response_data", lambda _: {"result": {"task_id": "t1"}})
    await client.add_resource(
        str(source),
        parent="viking://resources/raw",
        options={
            "structure_policy": "bookmarks_preferred",
            "parse_backend": "local",
        },
    )
    upload.assert_awaited_once_with(str(source))
    body = request.await_args.kwargs["json"]
    assert body["source_name"] == "维修手册.pdf"
    assert body["temp_file_id"] == "upload-id"
    assert "path" not in body
    assert body["structure_policy"] == "bookmarks_preferred"
    await client.close()


def test_sync_sdk_forwards_chapter_options(monkeypatch):
    from openviking_sdk import SyncHTTPClient

    client = SyncHTTPClient(url="http://localhost:1933")
    request = AsyncMock(return_value=object())
    monkeypatch.setattr(client._async_client, "_request", request)
    monkeypatch.setattr(
        client._async_client, "_handle_response_data", lambda _: {"result": {"task_id": "t1"}}
    )
    assert client.add_resource(
        "https://example.com/manual.pdf",
        options={
            "structure_policy": "bookmarks_preferred",
            "parse_backend": "local",
        },
    ) == {"task_id": "t1"}
    assert request.await_args.kwargs["json"]["structure_policy"] == "bookmarks_preferred"
    client.close()
