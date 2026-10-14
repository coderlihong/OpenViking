# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

"""Selection helpers for content-mutation tests.

A recursive listing carries the authored bodies a test wants to mutate alongside
what the pipeline produced: the semantic sidecars and each directory's generated
``index.md`` catalogue, which the content-write boundary refuses to overwrite.
Taking ``[0]`` therefore targets a generated artefact; these helpers name an
authored file instead.
"""

from __future__ import annotations

from typing import Any, Sequence

from openviking.core.directory_index import (
    DIRECTORY_INDEX_FILENAME,
    is_generated_directory_index,
)


async def authored_file_uris(client: Any, root_uri: str) -> list[str]:
    """Return every authored file under ``root_uri``, in listing order."""

    response = await client.get(
        "/api/v1/fs/ls",
        params={"uri": root_uri, "simple": True, "recursive": True, "output": "original"},
    )
    assert response.status_code == 200
    children: Sequence[str] = response.json().get("result") or []
    authored: list[str] = []
    for uri in children:
        if not isinstance(uri, str):
            continue
        name = uri.rstrip("/").rsplit("/", 1)[-1]
        if name.startswith("."):
            continue
        if name == DIRECTORY_INDEX_FILENAME:
            read = await client.get("/api/v1/content/read", params={"uri": uri, "raw": True})
            assert read.status_code == 200
            if is_generated_directory_index(read.json().get("result")):
                continue
        authored.append(uri)
    return authored


async def first_authored_file_uri(client: Any, root_uri: str) -> str:
    """Return the first authored file under ``root_uri``."""

    uris = await authored_file_uris(client, root_uri)
    assert uris, f"no authored file under {root_uri}"
    return uris[0]
