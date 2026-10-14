# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for content write endpoint."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from openviking.server.routers import filesystem
from tests.server.authored_content_helpers import first_authored_file_uri


async def test_write_endpoint_registered(client):
    resp = await client.get("/api/v1/content/write")
    assert resp.status_code == 405


async def test_set_tags_endpoint_registered(client):
    resp = await client.get("/api/v1/fs/attrs/set_tags")
    assert resp.status_code == 405


async def test_fs_attrs_filters_legacy_non_kv_search_tags(monkeypatch):
    class FakeVikingDBManagerProxy:
        def __init__(self, *_args):
            pass

        async def filter(self, **_kwargs):
            return [
                {
                    "level": 2,
                    "search_tags": ["default", "team=search", "bad=", "channel=app"],
                }
            ]

    monkeypatch.setattr(filesystem, "VikingDBManagerProxy", FakeVikingDBManagerProxy)

    tags = await filesystem._tags_attr(
        SimpleNamespace(vikingdb_manager=object()),
        "viking://user/default/memories/events/example.md",
        SimpleNamespace(),
        is_dir=False,
    )

    assert tags == ["team=search", "channel=app"]


async def test_write_rejects_directory_uri(client_with_resource):
    client, uri = client_with_resource
    resp = await client.post(
        "/api/v1/content/write",
        json={"uri": uri, "content": "new content"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "INVALID_ARGUMENT"


async def test_write_allows_existing_semantic_sidecar_but_rejects_relations(client_with_resource):
    client, uri = client_with_resource

    overview_resp = await client.post(
        "/api/v1/content/write",
        json={"uri": f"{uri}/.overview.md", "content": "new content"},
    )
    assert overview_resp.status_code == 200

    malformed_resp = await client.post(
        "/api/v1/content/write",
        json={"uri": f"{uri}/.overview.md", "content": "---\n"},
    )
    assert malformed_resp.status_code == 400
    assert malformed_resp.json()["error"]["code"] == "INVALID_ARGUMENT"

    relations_resp = await client.post(
        "/api/v1/content/write",
        json={"uri": f"{uri}/.relations.json", "content": "new content"},
    )
    assert relations_resp.status_code == 400
    body = relations_resp.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "INVALID_ARGUMENT"


async def test_write_replaces_existing_resource_file(client_with_resource):
    client, uri = client_with_resource
    file_uri = await first_authored_file_uri(client, uri)

    write_resp = await client.post(
        "/api/v1/content/write",
        json={
            "uri": file_uri,
            "content": "# Updated\n\nFresh content.",
            "mode": "replace",
            "wait": True,
        },
    )
    assert write_resp.status_code == 200
    body = write_resp.json()
    assert body["status"] == "ok"
    assert body["result"]["uri"] == file_uri
    assert body["result"]["mode"] == "replace"

    read_resp = await client.get("/api/v1/content/read", params={"uri": file_uri})
    assert read_resp.status_code == 200
    assert read_resp.json()["result"] == "# Updated\n\nFresh content."


async def test_write_appends_existing_resource_file(client_with_resource):
    client, uri = client_with_resource
    file_uri = await first_authored_file_uri(client, uri)
    original = (await client.get("/api/v1/content/read", params={"uri": file_uri})).json()["result"]

    write_resp = await client.post(
        "/api/v1/content/write",
        json={
            "uri": file_uri,
            "content": "\n\nAppended section.",
            "mode": "append",
            "wait": True,
        },
    )
    assert write_resp.status_code == 200

    read_resp = await client.get("/api/v1/content/read", params={"uri": file_uri})
    assert read_resp.status_code == 200
    assert read_resp.json()["result"] == original + "\n\nAppended section."


async def test_write_without_wait_publishes_its_revision(client_with_resource):
    client, uri = client_with_resource
    file_uri = await first_authored_file_uri(client, uri)
    original = (await client.get("/api/v1/content/read", params={"uri": file_uri})).json()["result"]

    write_resp = await client.post(
        "/api/v1/content/write",
        json={
            "uri": file_uri,
            "content": "\nImmediate append.",
            "mode": "append",
            "wait": False,
        },
    )
    assert write_resp.status_code == 200
    body = write_resp.json()
    assert body["result"]["content_updated"] is True
    assert body["result"]["semantic_status"] == "queued"
    assert body["result"]["vector_status"] == "queued"

    # A queued write is served from the generation that published it, so the read
    # view moves to the new body only when that generation commits -- unlike
    # ``wait=true``, which blocks here until it has.
    expected = original + "\nImmediate append."
    deadline = time.monotonic() + 30
    while True:
        read_resp = await client.get("/api/v1/content/read", params={"uri": file_uri})
        assert read_resp.status_code == 200
        if read_resp.json()["result"] == expected:
            break
        if time.monotonic() >= deadline:
            pytest.fail("Queued write never published its revision")
        await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_write_missing_uri_validation(client):
    resp = await client.post("/api/v1/content/write", json={"content": "missing uri"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_write_rejects_removed_semantic_flags(client_with_resource):
    client, uri = client_with_resource
    file_uri = await first_authored_file_uri(client, uri)

    resp = await client.post(
        "/api/v1/content/write",
        json={
            "uri": file_uri,
            "content": "updated",
            "regenerate_semantics": False,
            "revectorize": False,
        },
    )

    assert resp.status_code == 400


async def test_api_create_mode_new_file_success(client):
    """Test create mode with a new file."""
    resp = await client.post(
        "/api/v1/content/write",
        json={
            "uri": "viking://user/default/memories/new_file.md",
            "content": "new content",
            "mode": "create",
            "wait": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["result"]["mode"] == "create"


async def test_api_create_mode_write_then_read(client):
    """Create a new file then read it back — verify content roundtrips."""
    uri = "viking://user/default/memories/create_readback_test.md"

    write_resp = await client.post(
        "/api/v1/content/write",
        json={
            "uri": uri,
            "content": "# Hello\n\nWrite-then-read verification.",
            "mode": "create",
            "wait": True,
        },
    )
    assert write_resp.status_code == 200

    read_resp = await client.get("/api/v1/content/read", params={"uri": uri})
    assert read_resp.status_code == 200
    assert read_resp.json()["result"] == "# Hello\n\nWrite-then-read verification."


async def test_api_create_mode_existing_file_409(client_with_resource):
    """Test create mode on an existing file should return 409."""
    client, uri = client_with_resource
    file_uri = await first_authored_file_uri(client, uri)

    resp = await client.post(
        "/api/v1/content/write",
        json={
            "uri": file_uri,
            "content": "new content",
            "mode": "create",
            "wait": True,
        },
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "ALREADY_EXISTS"


async def test_api_create_mode_invalid_extension_400(client):
    """Test create mode with .exe extension should return 400."""
    resp = await client.post(
        "/api/v1/content/write",
        json={
            "uri": "viking://user/default/memories/test.exe",
            "content": "malicious content",
            "mode": "create",
            "wait": True,
        },
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["status"] == "error"
    assert "extension" in body["error"]["message"].lower()


async def test_api_create_mode_empty_content_success(client):
    """Test create mode with empty content should succeed."""
    resp = await client.post(
        "/api/v1/content/write",
        json={
            "uri": "viking://user/default/memories/empty.md",
            "content": "",
            "mode": "create",
            "wait": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["result"]["mode"] == "create"


async def test_api_create_mode_regression_replace_unchanged(client_with_resource):
    """Test replace mode still works (regression test)."""
    client, uri = client_with_resource
    file_uri = await first_authored_file_uri(client, uri)
    resp = await client.post(
        "/api/v1/content/write",
        json={
            "uri": file_uri,
            "content": "# Updated\n\nFresh content.",
            "mode": "replace",
            "wait": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["result"]["mode"] == "replace"

    read_resp = await client.get("/api/v1/content/read", params={"uri": file_uri})
    assert read_resp.status_code == 200
    assert read_resp.json()["result"] == "# Updated\n\nFresh content."


async def test_set_tags_requires_tags_field(client_with_resource):
    client, uri = client_with_resource
    file_uri = await first_authored_file_uri(client, uri)
    resp = await client.post("/api/v1/fs/attrs/set_tags", json={"uri": file_uri})
    assert resp.status_code == 400


async def test_set_tags_passes_tags_to_service(client, service, monkeypatch):
    captured = {}

    async def fake_set_tags(
        *,
        uri,
        tags,
        ctx,
        mode="replace",
        recursive=False,
    ):
        del ctx
        captured["uri"] = uri
        captured["tags"] = tags
        captured["mode"] = mode
        captured["recursive"] = recursive
        return {"uri": uri, "tags": tags}

    monkeypatch.setattr(service.fs, "set_tags", fake_set_tags)

    resp = await client.post(
        "/api/v1/fs/attrs/set_tags",
        json={
            "uri": "viking://resources/demo/file.md",
            "tags": ["a", "b"],
            "mode": "append",
            "recursive": True,
        },
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert captured == {
        "uri": "viking://resources/demo/file.md",
        "tags": ["a", "b"],
        "mode": "append",
        "recursive": True,
    }


async def test_set_tags_rejects_wait_and_timeout_fields(client_with_resource):
    client, uri = client_with_resource
    file_uri = await first_authored_file_uri(client, uri)
    resp = await client.post(
        "/api/v1/fs/attrs/set_tags",
        json={
            "uri": file_uri,
            "tags": ["team=search"],
            "wait": True,
            "timeout": 3,
        },
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["status"] == "error"


async def test_set_tags_discards_invalid_kv_tag(client_with_resource):
    client, uri = client_with_resource
    file_uri = await first_authored_file_uri(client, uri)
    resp = await client.post(
        "/api/v1/fs/attrs/set_tags",
        json={"uri": file_uri, "tags": ["project-a", "team=search"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["result"]["tags"] == ["team=search"]


async def test_write_reserves_directory_index_for_the_generated_catalogue(client_with_resource):
    """An authored body may not take ``{dir}/index.md``; the name lists the directory.

    The upload that produced the legacy chapter corpora wrote each section's prose to
    ``index.md``, which left those directories with no navigation catalogue at all.
    """
    client, root = client_with_resource
    reserved = f"{root.rstrip('/')}/6_Electrical_System/index.md"

    resp = await client.post(
        "/api/v1/content/write",
        json={"uri": reserved, "content": "# 6 Electrical System\n\nChapter prose.", "mode": "replace"},
    )

    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "INVALID_ARGUMENT"
    assert f"{root.rstrip('/')}/6_Electrical_System/6_Electrical_System.md" in error["message"]
    assert error["details"]["write_to"] == f"{root.rstrip('/')}/6_Electrical_System/6_Electrical_System.md"

    stat = await client.get("/api/v1/fs/stat", params={"uri": reserved})
    assert stat.status_code == 404


async def test_write_accepts_the_catalogue_itself(client_with_resource):
    """The catalogue carries the ``directory_index`` marker, so it owns the name."""
    from openviking.core.directory_index import render_directory_index

    client, root = client_with_resource
    directory = f"{root.rstrip('/')}/6_Electrical_System"
    body = render_directory_index(directory, [], [])

    resp = await client.post(
        "/api/v1/content/write",
        json={"uri": f"{directory}/index.md", "content": body, "mode": "replace", "wait": True},
    )

    assert resp.status_code == 200, resp.json()


async def test_batch_write_reserves_directory_index(client_with_resource):
    client, root = client_with_resource
    directory = f"{root.rstrip('/')}/6_Electrical_System"

    resp = await client.post(
        "/api/v1/content/batch-write",
        json={
            "root_uri": root,
            "operations": [
                {"uri": f"{directory}/6.1_Overview.md", "content": "# Overview\n"},
                {"uri": f"{directory}/index.md", "content": "# 6 Electrical System\n"},
            ],
        },
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["details"]["write_to"] == f"{directory}/{directory.rsplit('/', 1)[-1]}.md"
    written = await client.get("/api/v1/fs/stat", params={"uri": f"{directory}/6.1_Overview.md"})
    assert written.status_code == 404
