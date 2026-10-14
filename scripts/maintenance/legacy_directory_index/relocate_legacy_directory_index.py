# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Move chapter bodies that occupy ``{dir}/index.md`` to ``{dir}/{dir}.md``.

A directory's ``index.md`` is the generated navigation catalogue: the file an agent
greps to list the directory, kept out of the vector and lexical chains.  Corpora
that arrived file-by-file from upstream documentation use the same name for a
section's own prose, and then the catalogue has nowhere to go -- the indexer refuses
to overwrite prose, so every one of those directories is left without a listing.

The pass relocates the prose to the name the parser itself produces for a branch
chapter (``{dir}/{dir}.md``), then removes the old file so its vectors are retired
and the directory's own reindex can publish the catalogue.  It talks to a running
server over HTTP only: a second process opening the same account registry or vector
store as the server is what produced the historical lock collisions.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from openviking.core.directory_index import (  # noqa: E402
    DIRECTORY_INDEX_FILENAME,
    authored_index_collision,
    is_generated_directory_index,
)

DEFAULT_PAGE_SIZE = 500


@dataclass(frozen=True)
class Collision:
    """One directory whose ``index.md`` holds authored prose."""

    directory: str
    index_uri: str
    canonical_uri: str
    body: str
    canonical_exists: bool


@dataclass
class ScanReport:
    """What a read-only pass saw, so a fleet plan can quote its own cost."""

    directories: int = 0
    catalogued: int = 0
    collisions: list[Collision] = field(default_factory=list)


@dataclass(frozen=True)
class RelocationResult:
    directory: str
    action: str
    index_uri: str = ""
    canonical_uri: str = ""
    detail: str = ""


class Client:
    """Minimal content-API client; every mutation goes through the running server."""

    def __init__(self, base_url: str, api_key: str, timeout: float) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    def _ok(self, response: httpx.Response) -> Any:
        if response.status_code >= 400:
            raise RuntimeError(f"{response.request.method} {response.request.url} -> {response.text[:200]}")
        return response.json().get("result")

    def ls(self, uri: str) -> list[dict[str, Any]]:
        """Return every direct child with its ``isDir`` flag, paging past ``node_limit``."""

        entries: list[dict[str, Any]] = []
        offset = 0
        while True:
            result = self._ok(
                self._http.get(
                    "/api/v1/fs/ls",
                    params={
                        "uri": uri,
                        "simple": "false",
                        "output": "original",
                        "node_limit": DEFAULT_PAGE_SIZE,
                        "offset": offset,
                    },
                )
            )
            page = result or []
            entries.extend(page)
            if len(page) < DEFAULT_PAGE_SIZE:
                return entries
            offset += len(page)

    def read_body(self, uri: str) -> str | None:
        """Return the stored text, or ``None`` when the file is not readable."""

        response = self._http.get("/api/v1/content/read", params={"uri": uri, "raw": "true"})
        if response.status_code >= 400:
            return None
        result = response.json().get("result")
        return result if isinstance(result, str) else None

    def write(self, uri: str, body: str) -> None:
        self._ok(
            self._http.post(
                "/api/v1/content/write",
                json={
                    "uri": uri,
                    "content": body,
                    "mode": "create",
                    # Landing only: the tree gets one reindex afterwards, which is
                    # what rebuilds vectors and publishes the navigation catalogue.
                    "processing_mode": "none",
                },
            )
        )

    def rm(self, uri: str) -> None:
        self._ok(self._http.request("DELETE", "/api/v1/fs", params={"uri": uri, "recursive": "false"}))


def scan(client: Client, root_uri: str, limit: int | None) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """Yield ``(directory, children)`` for the root and every directory beneath it.

    One listing serves both jobs -- finding the ``index.md`` collision and discovering
    child directories -- because a full-tree pass over a live server is bound by request
    count, and a directory's children are exactly what its collision check needs.
    """

    visited = 0
    queue = [root_uri.rstrip("/")]
    while queue:
        directory = queue.pop(0)
        entries = client.ls(directory)
        yield directory, entries
        visited += 1
        if limit is not None and visited >= limit:
            return
        queue.extend(sorted(str(e.get("uri") or "").rstrip("/") for e in entries if e.get("isDir")))


def find_collisions(client: Client, root_uri: str, limit: int | None) -> ScanReport:
    report = ScanReport()
    for directory, entries in scan(client, root_uri, limit):
        report.directories += 1
        names = {str(entry.get("uri") or "").rstrip("/").rsplit("/", 1)[-1] for entry in entries}
        if DIRECTORY_INDEX_FILENAME not in names:
            continue
        index_uri = f"{directory}/{DIRECTORY_INDEX_FILENAME}"
        body = client.read_body(index_uri)
        if body is None:
            report.collisions.append(
                Collision(
                    directory=directory,
                    index_uri=index_uri,
                    canonical_uri="",
                    body="",
                    canonical_exists=False,
                )
            )
            continue
        if is_generated_directory_index(body):
            report.catalogued += 1
            continue
        canonical = authored_index_collision(index_uri, body)
        if canonical is None:
            continue
        report.collisions.append(
            Collision(
                directory=directory,
                index_uri=index_uri,
                canonical_uri=canonical,
                body=body,
                canonical_exists=canonical.rsplit("/", 1)[-1] in names,
            )
        )
    return report


def relocate(client: Client, collision: Collision) -> RelocationResult:
    if not collision.canonical_uri:
        return RelocationResult(
            directory=collision.directory, action="unreadable", index_uri=collision.index_uri
        )
    if collision.canonical_exists:
        return RelocationResult(
            directory=collision.directory,
            action="blocked_canonical_exists",
            index_uri=collision.index_uri,
            canonical_uri=collision.canonical_uri,
            detail="a file already owns the chapter name; nothing was overwritten",
        )
    client.write(collision.canonical_uri, collision.body)
    client.rm(collision.index_uri)
    return RelocationResult(
        directory=collision.directory,
        action="relocated",
        index_uri=collision.index_uri,
        canonical_uri=collision.canonical_uri,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base-url", required=True, help="running server, e.g. http://127.0.0.1:1933")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("VIKING_API_KEY"),
        help="user/admin key for the target tenant; VIKING_API_KEY keeps it out of the process list",
    )
    parser.add_argument("--tree-uri", required=True, help="resource tree to scan, e.g. viking://resources/raw/menus")
    parser.add_argument("--apply", action="store_true", help="perform the moves (default: plan only)")
    parser.add_argument("--limit", type=int, help="stop after this many directories (planning sanity check)")
    parser.add_argument("--report", type=Path, help="write one JSON line per directory decision")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    if not args.api_key:
        parser.error("--api-key is required (or set VIKING_API_KEY)")

    started = time.monotonic()
    client = Client(args.base_url, args.api_key, args.timeout)
    try:
        report = find_collisions(client, args.tree_uri, args.limit)
        print(
            f"scanned tree={args.tree_uri} limit={args.limit or 'none'} "
            f"directories={report.directories} catalogued={report.catalogued} "
            f"authored_index_md={len(report.collisions)} elapsed={time.monotonic() - started:.1f}s",
            flush=True,
        )
        counts: dict[str, int] = {}
        with (args.report.open("w", encoding="utf-8") if args.report else _NullHandle()) as sink:
            for collision in report.collisions:
                result = relocate(client, collision) if args.apply else _plan(collision)
                counts[result.action] = counts.get(result.action, 0) + 1
                sink.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")
    finally:
        client.close()

    print(json.dumps(counts, ensure_ascii=False), flush=True)
    if args.apply and counts.get("relocated"):
        print(
            "Run one serial reindex over the tree (mode=semantic_and_vectors) to rebuild the "
            "relocated chapters' vectors and publish each directory's catalogue.",
            flush=True,
        )
    return 0


def _plan(collision: Collision) -> RelocationResult:
    if not collision.canonical_uri:
        return RelocationResult(
            directory=collision.directory, action="would-be-unreadable", index_uri=collision.index_uri
        )
    if collision.canonical_exists:
        return RelocationResult(
            directory=collision.directory,
            action="would-be-blocked",
            index_uri=collision.index_uri,
            canonical_uri=collision.canonical_uri,
        )
    return RelocationResult(
        directory=collision.directory,
        action="would-relocate",
        index_uri=collision.index_uri,
        canonical_uri=collision.canonical_uri,
    )


class _NullHandle:
    def __enter__(self):
        class _Sink:
            @staticmethod
            def write(_text: str) -> None:
                return None

        return _Sink()

    def __exit__(self, *_exc):
        return False


if __name__ == "__main__":
    raise SystemExit(main())
