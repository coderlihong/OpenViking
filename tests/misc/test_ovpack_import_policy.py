# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Security regression tests for ovpack import target-policy enforcement."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from openviking.server.identity import RequestContext, Role
from openviking.service.pack_service import PackService
from openviking.storage.index_consistency import IndexConsistencyReport, IndexExpectation
from openviking.storage.ovpack.operations import (
    _enqueue_direct_vectorization,
    _image_occurrence_records,
    backup_ovpack,
    export_ovpack,
    import_ovpack,
    restore_ovpack,
)
from openviking.storage.vector_ids import chunk_vector_record_id, vector_record_id
from openviking_cli.exceptions import (
    FailedPreconditionError,
    InvalidArgumentError,
    NotFoundError,
    PermissionDeniedError,
)
from openviking_cli.session.user_id import UserIdentifier


class FakeVikingFS:
    def __init__(self, existing_roots: set[str] | None = None) -> None:
        self.written_files: list[str] = []
        self.created_dirs: list[str] = []
        self.tree_calls: list[str] = []
        self.write_contexts: list[RequestContext] = []
        self.existing_roots = existing_roots or set()
        self.removed_roots: list[str] = []

    async def stat(self, uri: str, ctx=None, skip_count=False):
        assert skip_count is True
        return {"uri": uri, "isDir": True}

    async def mkdir(self, uri: str, exist_ok: bool = False, ctx=None):
        self.created_dirs.append(uri)

    async def ls(self, uri: str, ctx=None):
        if uri in self.existing_roots:
            return []
        raise NotFoundError(uri, "file")

    async def rm(self, uri: str, recursive: bool = False, ctx=None):
        assert recursive is True
        self.removed_roots.append(uri)

    async def write_file_bytes(self, uri: str, data: bytes, ctx=None):
        self.written_files.append(uri)
        self.write_contexts.append(ctx)

    async def tree(self, uri: str, node_limit=None, level_limit=None, ctx=None):
        self.tree_calls.append(uri)
        return []

    async def exists(self, uri: str, ctx=None):
        return False

    async def read_file(self, uri: str, ctx=None):
        raise FileNotFoundError(uri)


class FakeExportVikingFS:
    def __init__(self) -> None:
        self.binary_files = {
            "viking://resources/demo/notes.txt": b"hello",
        }
        self.text_files = {
            "viking://resources/demo/.abstract.md": "root abstract",
            "viking://resources/demo/.overview.md": "root overview",
        }

    async def tree(
        self,
        uri: str,
        show_all_hidden: bool = False,
        node_limit=None,
        level_limit=None,
        ctx=None,
    ):
        assert uri == "viking://resources/demo"
        assert show_all_hidden is True
        assert node_limit is None
        assert level_limit is None
        return [
            {
                "rel_path": ".overview.md",
                "uri": "viking://resources/demo/.overview.md",
                "isDir": False,
                "size": 13,
            },
            {
                "rel_path": "notes.txt",
                "uri": "viking://resources/demo/notes.txt",
                "isDir": False,
                "size": 5,
            },
        ]

    async def exists(self, uri: str, ctx=None):
        return uri in self.text_files

    async def read_file(self, uri: str, ctx=None):
        return self.text_files[uri]

    async def read_file_bytes(self, uri: str, ctx=None):
        if uri in self.text_files:
            return self.text_files[uri].encode("utf-8")
        return self.binary_files[uri]


class OverviewOnlyExportVikingFS(FakeExportVikingFS):
    def __init__(self) -> None:
        super().__init__()
        self.text_files = {
            "viking://resources/demo/.overview.md": "root overview",
        }


class MissingSidecarExportVikingFS(FakeExportVikingFS):
    def __init__(self) -> None:
        super().__init__()
        self.text_files = {}


class ReservedPathExportVikingFS(FakeExportVikingFS):
    def __init__(self) -> None:
        super().__init__()
        self.binary_files.update(
            {
                "viking://resources/demo/.ovpack/foo.txt": b"hello",
                "viking://resources/demo/.notes.txt": b"dot",
                "viking://resources/demo/_._notes.txt": b"escaped-looking",
            }
        )

    async def tree(
        self,
        uri: str,
        show_all_hidden: bool = False,
        node_limit=None,
        level_limit=None,
        ctx=None,
    ):
        assert uri == "viking://resources/demo"
        return [
            {
                "rel_path": ".ovpack",
                "uri": "viking://resources/demo/.ovpack",
                "isDir": True,
                "size": 0,
            },
            {
                "rel_path": ".ovpack/foo.txt",
                "uri": "viking://resources/demo/.ovpack/foo.txt",
                "isDir": False,
                "size": 5,
            },
            {
                "rel_path": ".notes.txt",
                "uri": "viking://resources/demo/.notes.txt",
                "isDir": False,
                "size": 3,
            },
            {
                "rel_path": "_._notes.txt",
                "uri": "viking://resources/demo/_._notes.txt",
                "isDir": False,
                "size": 15,
            },
        ]


class FakeBackupVikingFS:
    def __init__(self) -> None:
        self.binary_files = {
            "viking://resources/README.md": b"hello",
            "viking://resources/.watch_tasks.json": b"{}",
            "viking://resources/.watch_tasks.json.bak": b"{}",
            "viking://resources/.watch_tasks.json.tmp": b"{}",
            "viking://user/resources/sessions/sess_1/.meta.json": b'{"session_id":"sess_1"}',
        }
        self.tree_contexts: list[tuple[str, RequestContext]] = []

    async def tree(
        self,
        uri: str,
        show_all_hidden: bool = False,
        node_limit=None,
        level_limit=None,
        ctx=None,
    ):
        assert show_all_hidden is True
        assert node_limit is None
        assert level_limit is None
        self.tree_contexts.append((uri, ctx))
        if uri == "viking://resources":
            return [
                {
                    "rel_path": "README.md",
                    "uri": "viking://resources/README.md",
                    "isDir": False,
                    "size": 5,
                },
                {
                    "rel_path": ".watch_tasks.json",
                    "uri": "viking://resources/.watch_tasks.json",
                    "isDir": False,
                    "size": 2,
                },
                {
                    "rel_path": ".watch_tasks.json.bak",
                    "uri": "viking://resources/.watch_tasks.json.bak",
                    "isDir": False,
                    "size": 2,
                },
                {
                    "rel_path": ".watch_tasks.json.tmp",
                    "uri": "viking://resources/.watch_tasks.json.tmp",
                    "isDir": False,
                    "size": 2,
                },
            ]
        if uri == "viking://user":
            return [
                {
                    "rel_path": "resources",
                    "uri": "viking://user/resources",
                    "isDir": True,
                    "size": 0,
                },
                {
                    "rel_path": "resources/sessions",
                    "uri": "viking://user/resources/sessions",
                    "isDir": True,
                    "size": 0,
                },
                {
                    "rel_path": "resources/sessions/sess_1",
                    "uri": "viking://user/resources/sessions/sess_1",
                    "isDir": True,
                    "size": 0,
                },
                {
                    "rel_path": "resources/sessions/sess_1/.meta.json",
                    "uri": "viking://user/resources/sessions/sess_1/.meta.json",
                    "isDir": False,
                    "size": 23,
                },
            ]
        return []

    async def exists(self, uri: str, ctx=None):
        return False

    async def read_file(self, uri: str, ctx=None):
        raise FileNotFoundError(uri)

    async def read_file_bytes(self, uri: str, ctx=None):
        return self.binary_files[uri]


class MissingSidecarBackupVikingFS(FakeBackupVikingFS):
    async def tree(
        self,
        uri: str,
        show_all_hidden: bool = False,
        node_limit=None,
        level_limit=None,
        ctx=None,
    ):
        if uri == "viking://user":
            return [
                {
                    "rel_path": ".overview.md",
                    "uri": "viking://user/.overview.md",
                    "isDir": False,
                    "size": 0,
                }
            ]
        return await super().tree(
            uri,
            show_all_hidden=show_all_hidden,
            node_limit=node_limit,
            level_limit=level_limit,
            ctx=ctx,
        )


class FakeRestoreVectorVikingFS(FakeVikingFS):
    async def tree(self, uri: str, node_limit=None, level_limit=None, ctx=None):
        self.tree_calls.append(uri)
        if uri == "viking://resources":
            return [
                {
                    "rel_path": "README.md",
                    "uri": "viking://resources/README.md",
                    "isDir": False,
                    "name": "README.md",
                }
            ]
        return []


class FakeVectorStore:
    def __init__(self) -> None:
        self.upserts: list[dict[str, object]] = []

    async def filter(self, **kwargs):
        uri = kwargs["filter"].value
        if uri == "viking://resources/demo":
            return [
                {
                    "uri": uri,
                    "context_type": "resource",
                    "level": 0,
                    "abstract": "root abstract",
                    "vector": [0.4, 0.5, 0.6],
                },
                {
                    "uri": uri,
                    "context_type": "resource",
                    "level": 1,
                    "abstract": "root overview",
                    "vector": [0.7, 0.8, 0.9],
                },
            ]
        if uri == "viking://resources/demo/notes.txt":
            return [
                {
                    "uri": uri,
                    "context_type": "resource",
                    "level": 2,
                    "abstract": "note summary",
                    "tags": ["snapshot"],
                    "vector": [0.1, 0.2, 0.3],
                }
            ]
        return []

    async def upsert(self, data, ctx=None):
        self.upserts.append(dict(data))
        return data.get("id", "")


class IncompleteVectorStore(FakeVectorStore):
    async def filter(self, **kwargs):
        return []


class OverviewOnlyVectorStore(FakeVectorStore):
    async def filter(self, **kwargs):
        uri = kwargs["filter"].value
        if uri == "viking://resources/demo":
            return [
                {
                    "uri": uri,
                    "context_type": "resource",
                    "level": 1,
                    "abstract": "root overview",
                    "vector": [0.7, 0.8, 0.9],
                }
            ]
        if uri == "viking://resources/demo/notes.txt":
            return await super().filter(**kwargs)
        return []


class HybridIndexVectorStore(FakeVectorStore):
    _index_name = "context_idx"

    def _get_default_backend(self):
        return self

    def _get_collection(self):
        return self

    def get_index_meta_data(self, index_name):
        assert index_name == self._index_name
        return {"VectorIndex": {"IndexType": "flat_hybrid"}}


@pytest.fixture
def request_ctx() -> RequestContext:
    return RequestContext(user=UserIdentifier("acct", "alice"), role=Role.USER)


@pytest.fixture
def temp_ovpack_path() -> Path:
    fd, path = tempfile.mkstemp(suffix=".ovpack")
    os.close(fd)
    ovpack_path = Path(path)
    try:
        yield ovpack_path
    finally:
        ovpack_path.unlink(missing_ok=True)


def _write_ovpack(path: Path, entries: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)


def _content_sha256(entries: list[dict[str, object]]) -> str:
    payload = json.dumps(
        entries,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _index_records_bytes(records: list[dict[str, object]]) -> bytes:
    if not records:
        return b""
    lines = [
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for record in records
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _set_manifest_index(
    manifest: dict[str, object], records: list[dict[str, object]] | None = None
) -> bytes:
    records = records or []
    data = _index_records_bytes(records)
    manifest["index"] = {
        "records": {
            "path": "_ovpack/index_records.jsonl",
            "count": len(records),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    }
    return data


def _manifest_for_files(root_name: str, files: dict[str, str]) -> dict[str, object]:
    entries: list[dict[str, object]] = [{"path": "", "kind": "directory"}]
    content_entries: list[dict[str, object]] = []
    for rel_path, content in sorted(files.items()):
        data = content.encode("utf-8")
        file_entry = {
            "path": rel_path,
            "kind": "file",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        entries.append(file_entry)
        content_entries.append(
            {
                "path": rel_path,
                "size": file_entry["size"],
                "sha256": file_entry["sha256"],
            }
        )

    manifest: dict[str, object] = {
        "kind": "openviking.ovpack",
        "format_version": 3,
        "root": {
            "name": root_name,
            "uri": f"viking://resources/{root_name}",
            "scope": "resources",
        },
        "entries": entries,
        "content_sha256": _content_sha256(content_entries),
    }
    _set_manifest_index(manifest)
    return manifest


def _write_ovpack_with_manifest(
    path: Path,
    root_name: str,
    files: dict[str, str],
    *,
    manifest: dict[str, object] | None = None,
    index_records: list[dict[str, object]] | None = None,
) -> None:
    manifest = manifest or _manifest_for_files(root_name, files)
    index_data = _set_manifest_index(manifest, index_records)
    entries = {
        f"{root_name}/": "",
        f"{root_name}/files/": "",
        f"{root_name}/_ovpack/": "",
        f"{root_name}/_ovpack/index_records.jsonl": index_data.decode("utf-8"),
        f"{root_name}/_ovpack/manifest.json": json.dumps(manifest),
    }
    entries.update(
        {f"{root_name}/files/{rel_path}": content for rel_path, content in files.items()}
    )
    _write_ovpack(path, entries)


def _rewrite_ovpack_index(
    path: Path,
    root_name: str,
    index_records: list[dict[str, object]],
) -> None:
    with zipfile.ZipFile(path, "r") as zf:
        members = {info.filename: zf.read(info.filename) for info in zf.infolist()}

    manifest_path = f"{root_name}/_ovpack/manifest.json"
    index_path = f"{root_name}/_ovpack/index_records.jsonl"
    manifest = json.loads(members[manifest_path].decode("utf-8"))
    index_data = _set_manifest_index(manifest, index_records)
    members[manifest_path] = json.dumps(manifest).encode("utf-8")
    members[index_path] = index_data

    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def _rewrite_ovpack_manifest(path: Path, root_name: str, mutate) -> None:
    with zipfile.ZipFile(path, "r") as zf:
        members = {info.filename: zf.read(info.filename) for info in zf.infolist()}

    manifest_path = f"{root_name}/_ovpack/manifest.json"
    manifest = json.loads(members[manifest_path].decode("utf-8"))
    mutate(manifest)
    members[manifest_path] = json.dumps(manifest).encode("utf-8")

    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def test_index_consistency_report_limits_public_and_error_records():
    missing = tuple(
        IndexExpectation(
            uri=f"viking://resources/demo/file_{index}.md",
            rel_path=f"file_{index}.md",
            level=2,
        )
        for index in range(25)
    )
    report = IndexConsistencyReport(expected=missing, missing_records=missing)

    public = report.to_dict()
    assert "expected" not in public
    assert public["expected_count"] == 25
    assert public["missing_record_count"] == 25
    assert len(public["missing_records"]) == 20
    assert public["missing_records_truncated"] is True

    details = report.details()
    assert details["missing_record_count"] == 25
    assert details["missing_records"] == ["file_0.md#level=2"]
    assert details["missing_records_truncated"] is True


@pytest.mark.asyncio
async def test_export_ovpack_writes_v3_manifest_with_abstract_overviews(
    temp_ovpack_path: Path, request_ctx: RequestContext
):
    await export_ovpack(
        FakeExportVikingFS(),
        "viking://resources/demo",
        str(temp_ovpack_path),
        ctx=request_ctx,
    )

    with zipfile.ZipFile(temp_ovpack_path, "r") as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("demo/_ovpack/manifest.json").decode("utf-8"))
        index_records = [
            json.loads(line)
            for line in zf.read("demo/_ovpack/index_records.jsonl").decode("utf-8").splitlines()
        ]

    assert "demo/files/notes.txt" in names
    assert "demo/files/.overview.md" in names
    assert manifest["format_version"] == 3
    assert manifest["kind"] == "openviking.ovpack"
    note_entry = next(entry for entry in manifest["entries"] if entry["path"] == "notes.txt")
    note_sha256 = hashlib.sha256(b"hello").hexdigest()
    assert note_entry["size"] == 5
    assert note_entry["sha256"] == note_sha256
    overview_entry = next(entry for entry in manifest["entries"] if entry["path"] == ".overview.md")
    overview_sha256 = hashlib.sha256(b"root overview").hexdigest()
    assert overview_entry["sha256"] == overview_sha256
    assert manifest["content_sha256"] == _content_sha256(
        [
            {"path": ".overview.md", "size": 13, "sha256": overview_sha256},
            {"path": "notes.txt", "size": 5, "sha256": note_sha256},
        ]
    )
    assert manifest["index"]["records"]["count"] == len(index_records)
    assert index_records[0]["path"] == ""
    assert index_records[0]["text"] == "root abstract"


@pytest.mark.asyncio
async def test_export_ovpack_skips_missing_abstract_overviews(
    temp_ovpack_path: Path,
    request_ctx: RequestContext,
):
    await export_ovpack(
        MissingSidecarExportVikingFS(),
        "viking://resources/demo",
        str(temp_ovpack_path),
        ctx=request_ctx,
    )

    with zipfile.ZipFile(temp_ovpack_path, "r") as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("demo/_ovpack/manifest.json").decode("utf-8"))

    manifest_paths = {entry["path"] for entry in manifest["entries"]}
    assert "demo/files/notes.txt" in names
    assert "demo/files/.overview.md" not in names
    assert ".overview.md" not in manifest_paths


@pytest.mark.asyncio
async def test_backup_restore_contract(
    temp_ovpack_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    admin_ctx = RequestContext(user=UserIdentifier("acct", "admin"), role=Role.ADMIN)
    backup_fs = FakeBackupVikingFS()
    await PackService(backup_fs).backup_ovpack(str(temp_ovpack_path), ctx=admin_ctx)

    with zipfile.ZipFile(temp_ovpack_path, "r") as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("openviking-backup/_ovpack/manifest.json").decode("utf-8"))

    manifest_paths = {entry["path"] for entry in manifest["entries"]}
    assert "openviking-backup/files/resources/README.md" in names
    assert "openviking-backup/files/user/resources/sessions/sess_1/.meta.json" in names
    assert "openviking-backup/files/resources/.watch_tasks.json" not in names
    assert "openviking-backup/files/resources/.watch_tasks.json.bak" not in names
    assert "openviking-backup/files/resources/.watch_tasks.json.tmp" not in names
    assert "resources/.watch_tasks.json" not in manifest_paths
    assert "resources/.watch_tasks.json.bak" not in manifest_paths
    assert "resources/.watch_tasks.json.tmp" not in manifest_paths
    assert all(ctx.role == Role.ROOT for _, ctx in backup_fs.tree_contexts)
    assert manifest["root"] == {
        "name": "openviking-backup",
        "uri": "viking://",
        "scope": "root",
        "package_type": "backup",
    }
    assert manifest["scopes"] == ["resources", "user"]
    user_entries = {
        entry["path"]: entry
        for entry in manifest["entries"]
        if entry["path"].startswith("user/resources")
    }
    assert user_entries
    assert {entry["user_id"] for entry in user_entries.values()} == {"resources"}

    with pytest.raises(InvalidArgumentError, match=r"must be restored"):
        await import_ovpack(FakeVikingFS(), str(temp_ovpack_path), "viking://", admin_ctx)

    vectorization_calls: list[tuple[str, RequestContext]] = []

    async def capture_vectorization(viking_fs, uri, ctx, **kwargs):
        vectorization_calls.append((uri, ctx))

    monkeypatch.setattr(
        "openviking.storage.ovpack.operations._enqueue_direct_vectorization",
        capture_vectorization,
    )

    fake_fs = FakeVikingFS()
    fake_fs.ls = AsyncMock(return_value=[])
    fake_fs.stat = AsyncMock(side_effect=FileNotFoundError())
    fake_fs.rm = AsyncMock()
    assert (
        await PackService(fake_fs).restore_ovpack(
            str(temp_ovpack_path), ctx=admin_ctx, on_conflict="overwrite"
        )
        == "viking://"
    )
    assert fake_fs.written_files == [
        "viking://resources/README.md",
        "viking://user/resources/sessions/sess_1/.meta.json",
    ]
    fake_fs.rm.assert_not_awaited()
    assert all(ctx.role == Role.ROOT for ctx in fake_fs.write_contexts)
    assert [uri for uri, _ in vectorization_calls] == [
        "viking://resources",
        "viking://user/resources",
    ]
    user_vector_ctx = vectorization_calls[1][1]
    assert user_vector_ctx.user.user_id == "resources"
    assert user_vector_ctx.role == Role.ROOT
    assert "viking://user" not in fake_fs.created_dirs


@pytest.mark.asyncio
async def test_backup_skips_missing_abstract_overviews(
    temp_ovpack_path: Path,
    request_ctx: RequestContext,
):
    await backup_ovpack(
        MissingSidecarBackupVikingFS(),
        str(temp_ovpack_path),
        ctx=request_ctx,
    )

    with zipfile.ZipFile(temp_ovpack_path, "r") as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("openviking-backup/_ovpack/manifest.json").decode("utf-8"))

    manifest_paths = {entry["path"] for entry in manifest["entries"]}
    assert "openviking-backup/files/user/.overview.md" not in names
    assert "user/.overview.md" not in manifest_paths
    assert "user" in manifest_paths


@pytest.mark.asyncio
async def test_restore_ovpack_applies_backup_manifest_scalar_metadata(
    temp_ovpack_path: Path, request_ctx: RequestContext, monkeypatch: pytest.MonkeyPatch
):
    await backup_ovpack(
        FakeBackupVikingFS(),
        str(temp_ovpack_path),
        ctx=request_ctx,
    )

    _rewrite_ovpack_index(
        temp_ovpack_path,
        "openviking-backup",
        [
            {
                "record_id": "r000001",
                "path": "resources/README.md",
                "kind": "file",
                "level": 2,
                "text": "portable summary",
                "scalars": {
                    "abstract": "portable summary",
                    "description": "portable description",
                    "tags": ["portable"],
                },
            }
        ],
    )

    vectorized_files: list[dict[str, object]] = []

    async def capture_vectorize_file(**kwargs):
        vectorized_files.append(kwargs)

    monkeypatch.setattr(
        "openviking.storage.ovpack.operations.vectorize_file", capture_vectorize_file
    )

    await restore_ovpack(FakeRestoreVectorVikingFS(), str(temp_ovpack_path), request_ctx)

    assert len(vectorized_files) == 1
    assert vectorized_files[0]["file_path"] == "viking://resources/README.md"
    assert vectorized_files[0]["summary_dict"] == {
        "name": "README.md",
        "summary": "portable summary",
    }
    assert vectorized_files[0]["scalar_override"]["tags"] == ["portable"]


@pytest.mark.asyncio
async def test_export_include_vectors_rejects_missing_index_records(
    temp_ovpack_path: Path, request_ctx: RequestContext
):
    with pytest.raises(
        InvalidArgumentError,
        match=r"incomplete OpenViking vector index snapshot",
    ) as exc_info:
        await export_ovpack(
            FakeExportVikingFS(),
            "viking://resources/demo",
            str(temp_ovpack_path),
            ctx=request_ctx,
            vector_store=IncompleteVectorStore(),
            include_vectors=True,
        )

    assert exc_info.value.details["missing_record_count"] == 3
    assert exc_info.value.details["missing_records"] == [".#level=0"]
    assert exc_info.value.details["missing_records_truncated"] is True
    assert not temp_ovpack_path.read_bytes()


@pytest.mark.asyncio
async def test_export_include_vectors_allows_overview_without_abstract(
    temp_ovpack_path: Path, request_ctx: RequestContext
):
    await export_ovpack(
        OverviewOnlyExportVikingFS(),
        "viking://resources/demo",
        str(temp_ovpack_path),
        ctx=request_ctx,
        vector_store=OverviewOnlyVectorStore(),
        include_vectors=True,
    )

    with zipfile.ZipFile(temp_ovpack_path, "r") as zf:
        index_records = [
            json.loads(line)
            for line in zf.read("demo/_ovpack/index_records.jsonl").decode("utf-8").splitlines()
        ]

    root_levels = {record["level"] for record in index_records if record["path"] == ""}
    assert root_levels == {1}


@pytest.mark.asyncio
async def test_ovpack_roundtrips_dot_and_escaped_looking_user_paths(
    temp_ovpack_path: Path,
    request_ctx: RequestContext,
    monkeypatch: pytest.MonkeyPatch,
):
    async def noop_vectorization(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "openviking.storage.ovpack.operations._enqueue_direct_vectorization",
        noop_vectorization,
    )

    await export_ovpack(
        ReservedPathExportVikingFS(),
        "viking://resources/demo",
        str(temp_ovpack_path),
        ctx=request_ctx,
    )

    with zipfile.ZipFile(temp_ovpack_path, "r") as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("demo/_ovpack/manifest.json").decode("utf-8"))

    assert "demo/files/.ovpack/foo.txt" in names
    assert "demo/files/.notes.txt" in names
    assert "demo/files/_._notes.txt" in names
    manifest_paths = {entry["path"] for entry in manifest["entries"]}
    assert {".ovpack", ".ovpack/foo.txt", ".notes.txt", "_._notes.txt"} <= manifest_paths

    fake_fs = FakeVikingFS()
    await import_ovpack(fake_fs, str(temp_ovpack_path), "viking://resources/imported", request_ctx)

    assert fake_fs.written_files == [
        "viking://resources/imported/demo/.ovpack/foo.txt",
        "viking://resources/imported/demo/.notes.txt",
        "viking://resources/imported/demo/_._notes.txt",
    ]


@pytest.mark.asyncio
async def test_export_include_vectors_rejects_hybrid_index_snapshot(
    temp_ovpack_path: Path, request_ctx: RequestContext
):
    with pytest.raises(
        InvalidArgumentError,
        match=r"only support pure dense",
    ) as exc_info:
        await export_ovpack(
            FakeExportVikingFS(),
            "viking://resources/demo",
            str(temp_ovpack_path),
            ctx=request_ctx,
            vector_store=HybridIndexVectorStore(),
            include_vectors=True,
        )

    assert exc_info.value.details["reason"] == "current vector index type is hybrid"
    assert not temp_ovpack_path.read_bytes()


@pytest.mark.asyncio
async def test_import_ovpack_restores_required_dense_vector_snapshot(
    temp_ovpack_path: Path, request_ctx: RequestContext, monkeypatch: pytest.MonkeyPatch
):
    embedding_metadata = {
        "provider": "test",
        "model": "demo",
        "input": "text",
        "dimensions": 3,
    }
    monkeypatch.setattr(
        "openviking.storage.ovpack.vectors.embedding_snapshot_metadata",
        lambda dimensions: {**embedding_metadata, "dimensions": dimensions},
    )
    monkeypatch.setattr(
        "openviking.storage.ovpack.vectors.current_embedding_metadata",
        lambda: embedding_metadata,
    )

    await export_ovpack(
        FakeExportVikingFS(),
        "viking://resources/demo",
        str(temp_ovpack_path),
        ctx=request_ctx,
        vector_store=FakeVectorStore(),
        include_vectors=True,
    )

    with zipfile.ZipFile(temp_ovpack_path, "r") as zf:
        assert "demo/_ovpack/dense.f32" in set(zf.namelist())

    fake_fs = FakeVikingFS()
    vector_store = FakeVectorStore()
    result = await import_ovpack(
        fake_fs,
        str(temp_ovpack_path),
        "viking://resources/imported",
        request_ctx,
        vector_mode="require",
        vector_store=vector_store,
    )

    assert result == "viking://resources/imported/demo"
    assert fake_fs.tree_calls == []
    assert len(vector_store.upserts) == 3
    note_record = next(
        record
        for record in vector_store.upserts
        if record["uri"] == "viking://resources/imported/demo/notes.txt"
    )
    assert note_record["vector"] == pytest.approx([0.1, 0.2, 0.3])
    assert note_record["tags"] == ["snapshot"]
    assert note_record["created_at"]
    assert note_record["updated_at"]
    assert note_record["active_count"] == 0


@pytest.mark.asyncio
async def test_import_legacy_ovpack_without_manifest_is_rejected(
    temp_ovpack_path: Path, request_ctx: RequestContext
):
    _write_ovpack(
        temp_ovpack_path,
        {
            "demo/files/.overview.md": "ATTACKER_OVERVIEW",
            "demo/files/notes.txt": "hello",
        },
    )
    fake_fs = FakeVikingFS()

    with pytest.raises(InvalidArgumentError, match=r"Missing ovpack manifest"):
        await import_ovpack(fake_fs, str(temp_ovpack_path), "viking://resources", request_ctx)

    assert fake_fs.written_files == []


@pytest.mark.asyncio
async def test_import_ovpack_rejects_manifest_file_hash_mismatch(
    temp_ovpack_path: Path, request_ctx: RequestContext
):
    manifest = _manifest_for_files("demo", {"notes.txt": "hello"})
    _write_ovpack_with_manifest(
        temp_ovpack_path,
        "demo",
        {"notes.txt": "jello"},
        manifest=manifest,
    )
    fake_fs = FakeVikingFS()
    fake_fs.ls = AsyncMock(return_value=[])

    with pytest.raises(InvalidArgumentError, match=r"sha256 does not match manifest"):
        await import_ovpack(
            fake_fs,
            str(temp_ovpack_path),
            "viking://resources",
            request_ctx,
            on_conflict="skip",
        )

    assert fake_fs.written_files == []


@pytest.mark.asyncio
async def test_import_ovpack_rejects_previous_manifest_version(
    temp_ovpack_path: Path, request_ctx: RequestContext
):
    manifest = _manifest_for_files("demo", {"notes.txt": "hello"})
    manifest["format_version"] = 2
    _write_ovpack_with_manifest(temp_ovpack_path, "demo", {"notes.txt": "hello"}, manifest=manifest)
    fake_fs = FakeVikingFS()

    with pytest.raises(InvalidArgumentError, match=r"Unsupported ovpack format_version 2"):
        await import_ovpack(fake_fs, str(temp_ovpack_path), "viking://resources", request_ctx)

    assert fake_fs.written_files == []


@pytest.mark.asyncio
async def test_import_ovpack_rejects_manifest_unexpected_directory(
    temp_ovpack_path: Path, request_ctx: RequestContext
):
    manifest = _manifest_for_files("demo", {"notes.txt": "hello"})
    _write_ovpack(
        temp_ovpack_path,
        {
            "demo/": "",
            "demo/files/": "",
            "demo/_ovpack/": "",
            "demo/_ovpack/index_records.jsonl": "",
            "demo/_ovpack/manifest.json": json.dumps(manifest),
            "demo/files/notes.txt": "hello",
            "demo/files/empty/": "",
        },
    )
    fake_fs = FakeVikingFS()

    with pytest.raises(InvalidArgumentError, match=r"entries do not match manifest") as exc_info:
        await import_ovpack(fake_fs, str(temp_ovpack_path), "viking://resources", request_ctx)

    assert exc_info.value.details["unexpected_directories"] == ["empty"]
    assert fake_fs.written_files == []


@pytest.mark.asyncio
async def test_import_ovpack_restores_user_session_without_vectorization(
    temp_ovpack_path: Path, request_ctx: RequestContext
):
    files = {
        ".meta.json": json.dumps({"session_id": "victim"}),
        "messages.jsonl": '{"id":"msg_1","role":"user","parts":[{"type":"text","text":"hi"}]}\n',
    }
    manifest = _manifest_for_files("victim", files)
    manifest["root"] = {
        "name": "victim",
        "uri": "viking://user/alice/sessions/victim",
        "scope": "user",
    }
    _write_ovpack_with_manifest(
        temp_ovpack_path,
        "victim",
        files,
        manifest=manifest,
    )
    fake_fs = FakeVikingFS()

    result = await import_ovpack(
        fake_fs,
        str(temp_ovpack_path),
        "viking://user/alice/sessions",
        request_ctx,
    )

    assert result == "viking://user/alice/sessions/victim"
    assert fake_fs.written_files == [
        "viking://user/alice/sessions/victim/.meta.json",
        "viking://user/alice/sessions/victim/messages.jsonl",
    ]
    assert fake_fs.tree_calls == []

    invalid_fs = FakeVikingFS()
    with pytest.raises(InvalidArgumentError, match=r"source scope does not match target scope"):
        await import_ovpack(invalid_fs, str(temp_ovpack_path), "viking://resources", request_ctx)
    with pytest.raises(InvalidArgumentError, match=r"source path is incompatible"):
        await import_ovpack(
            invalid_fs,
            str(temp_ovpack_path),
            "viking://user/alice/sessions/victim",
            request_ctx,
        )
    assert invalid_fs.written_files == []


@pytest.mark.asyncio
async def test_import_ovpack_rejects_legacy_session_scope(
    temp_ovpack_path: Path, request_ctx: RequestContext
):
    manifest = _manifest_for_files("demo", {"notes.txt": "hello"})
    _write_ovpack_with_manifest(temp_ovpack_path, "demo", {"notes.txt": "hello"}, manifest=manifest)
    fake_fs = FakeVikingFS()

    with pytest.raises(InvalidArgumentError, match=r"scope: session"):
        await import_ovpack(fake_fs, str(temp_ovpack_path), "viking://session", request_ctx)

    assert fake_fs.written_files == []


@pytest.mark.asyncio
async def test_import_top_level_scope_package_requires_root_target(
    temp_ovpack_path: Path, request_ctx: RequestContext
):
    manifest = _manifest_for_files("resources", {"README.md": "hello"})
    manifest["root"] = {
        "name": "resources",
        "uri": "viking://resources",
        "scope": "resources",
    }
    _write_ovpack_with_manifest(
        temp_ovpack_path,
        "resources",
        {"README.md": "hello"},
        manifest=manifest,
    )
    fake_fs = FakeVikingFS()

    with pytest.raises(InvalidArgumentError, match=r"must be imported to viking://"):
        await import_ovpack(fake_fs, str(temp_ovpack_path), "viking://resources", request_ctx)

    assert await import_ovpack(fake_fs, str(temp_ovpack_path), "viking://", request_ctx) == (
        "viking://resources"
    )

    _write_ovpack_with_manifest(
        temp_ovpack_path,
        "renamed",
        {"README.md": "hello"},
        manifest=manifest,
    )
    with pytest.raises(InvalidArgumentError, match=r"root name does not match zip root"):
        await import_ovpack(
            FakeVikingFS(), str(temp_ovpack_path), "viking://resources", request_ctx
        )


HIERARCHICAL_ROOT = "viking://resources/big"
HIERARCHICAL_FILE = f"{HIERARCHICAL_ROOT}/chapter.md"


class HierarchicalExportVikingFS(FakeExportVikingFS):
    """One directory with L0/L1 sidecars plus one chunked markdown file."""

    def __init__(self) -> None:
        super().__init__()
        self.text_files = {
            f"{HIERARCHICAL_ROOT}/.abstract.md": "big root abstract",
            f"{HIERARCHICAL_ROOT}/.overview.md": "big root overview",
        }
        self.binary_files = {HIERARCHICAL_FILE: b"# chapter\nbody body body\n"}

    async def tree(
        self,
        uri: str,
        show_all_hidden: bool = False,
        node_limit=None,
        level_limit=None,
        ctx=None,
    ):
        assert uri == HIERARCHICAL_ROOT
        return [
            {
                "rel_path": ".abstract.md",
                "uri": f"{HIERARCHICAL_ROOT}/.abstract.md",
                "isDir": False,
                "size": 17,
            },
            {
                "rel_path": ".overview.md",
                "uri": f"{HIERARCHICAL_ROOT}/.overview.md",
                "isDir": False,
                "size": 17,
            },
            {
                "rel_path": "chapter.md",
                "uri": HIERARCHICAL_FILE,
                "isDir": False,
                "size": 25,
            },
        ]


def hierarchical_vector_rows() -> list[dict[str, object]]:
    """Directory L0/L1 rows plus one file summary row and twelve chunk rows."""
    rows: list[dict[str, object]] = [
        {
            "uri": HIERARCHICAL_ROOT,
            "type": "text",
            "context_type": "resource",
            "level": 0,
            "abstract": "big root abstract",
            "vector": [10.0, 10.5, 10.25],
        },
        {
            "uri": HIERARCHICAL_ROOT,
            "type": "text",
            "context_type": "resource",
            "level": 1,
            "abstract": "big root overview",
            "vector": [11.0, 11.5, 11.25],
        },
        {
            "uri": HIERARCHICAL_FILE,
            "type": "text",
            "context_type": "resource",
            "level": 2,
            "name": "chapter.md",
            "abstract": "chapter summary",
            "vector": [12.0, 12.5, 12.25],
        },
    ]
    for ordinal in range(12):
        rows.append(
            {
                "uri": HIERARCHICAL_FILE,
                "type": "text",
                "context_type": "resource",
                "level": 2,
                "chunk_id": f"chunk-{ordinal:02d}",
                "parent_chunk_id": "parent-01",
                "chunk_ordinal": ordinal,
                "chunk_text": f"chunk body {ordinal:02d}",
                "parent_text": "parent body",
                "content_revision": "rev-1",
                "chunker_version": "test-chunker",
                "index_plan_id": "plan-1",
                "index_generation": "gen-1",
                "image_context_uri": HIERARCHICAL_FILE,
                "business_meta_payload": json.dumps(
                    {"category": "text", "machine_model": "TEST10"}, ensure_ascii=False
                ),
                "search_tags": ["TEST10"],
                "abstract": f"chunk body {ordinal:02d}",
                "vector": [float(ordinal), float(ordinal) + 0.5, float(ordinal) + 0.25],
            }
        )
    return rows


class RowTableVectorStore(FakeVectorStore):
    """Honours the query limit, like engines that cap how many rows they return."""

    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        super().__init__()
        self.rows = rows or []

    async def filter(self, **kwargs):
        uri = kwargs["filter"].value
        limit = kwargs.get("limit", 10)
        matched = [dict(row) for row in self.rows if row.get("uri") == uri]
        return matched[:limit]


def _exported_index_records(path: Path, root_name: str) -> list[dict[str, object]]:
    with zipfile.ZipFile(path, "r") as zf:
        return [
            json.loads(line)
            for line in zf.read(f"{root_name}/_ovpack/index_records.jsonl")
            .decode("utf-8")
            .splitlines()
        ]


@pytest.mark.asyncio
async def test_export_ovpack_carries_every_row_and_chunk_linkage(
    temp_ovpack_path: Path, request_ctx: RequestContext
):
    await export_ovpack(
        HierarchicalExportVikingFS(),
        HIERARCHICAL_ROOT,
        str(temp_ovpack_path),
        ctx=request_ctx,
        vector_store=RowTableVectorStore(hierarchical_vector_rows()),
        include_vectors=True,
    )

    index_records = _exported_index_records(temp_ovpack_path, "big")
    assert {record["level"] for record in index_records if record["path"] == ""} == {0, 1}

    chapter_records = [record for record in index_records if record["path"] == "chapter.md"]
    assert len(chapter_records) == 13
    summary = [record for record in chapter_records if not record["scalars"].get("chunk_id")]
    assert len(summary) == 1
    assert summary[0]["scalars"]["abstract"] == "chapter summary"

    chunk_records = sorted(
        (record for record in chapter_records if record["scalars"].get("chunk_id")),
        key=lambda record: record["scalars"]["chunk_ordinal"],
    )
    assert [record["scalars"]["chunk_id"] for record in chunk_records] == [
        f"chunk-{ordinal:02d}" for ordinal in range(12)
    ]
    linked = next(
        record for record in chunk_records if record["scalars"]["chunk_id"] == "chunk-03"
    )
    assert linked["scalars"]["parent_chunk_id"] == "parent-01"
    assert linked["scalars"]["parent_text"] == "parent body"
    assert linked["scalars"]["chunk_text"] == "chunk body 03"
    assert linked["scalars"]["chunk_ordinal"] == 3
    assert linked["scalars"]["content_revision"] == "rev-1"
    assert linked["scalars"]["image_context_uri"] == HIERARCHICAL_FILE
    assert json.loads(linked["scalars"]["business_meta_payload"]) == {
        "category": "text",
        "machine_model": "TEST10",
    }
    assert linked["scalars"]["search_tags"] == ["TEST10"]


@pytest.mark.asyncio
async def test_import_ovpack_restores_hierarchical_vectors_with_canonical_ids(
    temp_ovpack_path: Path, request_ctx: RequestContext, monkeypatch: pytest.MonkeyPatch
):
    embedding_metadata = {
        "provider": "test",
        "model": "demo",
        "input": "text",
        "dimensions": 3,
    }
    monkeypatch.setattr(
        "openviking.storage.ovpack.vectors.embedding_snapshot_metadata",
        lambda dimensions: {**embedding_metadata, "dimensions": dimensions},
    )
    monkeypatch.setattr(
        "openviking.storage.ovpack.vectors.current_embedding_metadata",
        lambda: embedding_metadata,
    )

    await export_ovpack(
        HierarchicalExportVikingFS(),
        HIERARCHICAL_ROOT,
        str(temp_ovpack_path),
        ctx=request_ctx,
        vector_store=RowTableVectorStore(hierarchical_vector_rows()),
        include_vectors=True,
    )

    vector_store = RowTableVectorStore()
    result = await import_ovpack(
        FakeVikingFS(),
        str(temp_ovpack_path),
        "viking://resources/imported",
        request_ctx,
        vector_mode="require",
        vector_store=vector_store,
    )

    assert result == "viking://resources/imported/big"
    imported_root = result
    imported_file = f"{imported_root}/chapter.md"
    account_id = request_ctx.account_id
    assert len(vector_store.upserts) == 15
    by_id = {str(row["id"]): row for row in vector_store.upserts}
    assert len(by_id) == 15

    abstract_row = by_id[vector_record_id(account_id, imported_root, 0)]
    assert abstract_row["abstract"] == "big root abstract"
    assert abstract_row["vector"] == pytest.approx([10.0, 10.5, 10.25])
    overview_row = by_id[vector_record_id(account_id, imported_root, 1)]
    assert overview_row["abstract"] == "big root overview"
    assert overview_row["vector"] == pytest.approx([11.0, 11.5, 11.25])

    summary_row = by_id[vector_record_id(account_id, imported_file, 2)]
    assert summary_row["abstract"] == "chapter summary"
    assert summary_row["vector"] == pytest.approx([12.0, 12.5, 12.25])

    for ordinal in range(12):
        chunk = by_id[
            chunk_vector_record_id(account_id, imported_file, f"chunk-{ordinal:02d}")
        ]
        assert chunk["uri"] == imported_file
        assert chunk["level"] == 2
        assert chunk["parent_chunk_id"] == "parent-01"
        assert chunk["parent_text"] == "parent body"
        assert chunk["chunk_text"] == f"chunk body {ordinal:02d}"
        assert chunk["content"] == f"chunk body {ordinal:02d}"
        assert chunk["chunk_ordinal"] == ordinal
        assert chunk["content_revision"] == "rev-1"
        assert chunk["chunker_version"] == "test-chunker"
        assert chunk["image_context_uri"] == HIERARCHICAL_FILE
        assert json.loads(chunk["business_meta_payload"]) == {
            "category": "text",
            "machine_model": "TEST10",
        }
        assert chunk["search_tags"] == ["TEST10"]
        assert chunk["vector"] == pytest.approx(
            [float(ordinal), float(ordinal) + 0.5, float(ordinal) + 0.25]
        )
        assert "index_generation" not in chunk
        assert "index_plan_id" not in chunk
    assert {row["uri"] for row in vector_store.upserts} == {imported_root, imported_file}


def stale_generation_rows() -> list[dict[str, object]]:
    """One directory L0 record left live in three generations, out of order.

    An interrupted publisher leaves older generations untombstoned; each carries
    its own abstract and vector, and only the newest is current.
    """
    rows = hierarchical_vector_rows()
    for row in rows:
        if row["uri"] == HIERARCHICAL_ROOT and row["level"] == 0:
            row["abstract"] = "abstract v1"
            row["vector"] = [1.0, 1.0, 1.0]
            row["created_at"] = "2026-10-10T17:27:02.000Z"
            row["updated_at"] = "2026-10-10T17:27:02.000Z"
    rows.append(
        {
            "uri": HIERARCHICAL_ROOT,
            "context_type": "resource",
            "level": 0,
            "abstract": "abstract v3",
            "vector": [3.0, 3.0, 3.0],
            "created_at": "2026-10-10T17:43:23.000Z",
            "updated_at": "2026-10-10T17:43:23.000Z",
        }
    )
    rows.append(
        {
            "uri": HIERARCHICAL_ROOT,
            "context_type": "resource",
            "level": 0,
            "abstract": "abstract v2",
            "vector": [2.0, 2.0, 2.0],
            "created_at": "2026-10-10T18:15:51.000Z",
            "updated_at": "2026-10-10T18:15:51.000Z",
        }
    )
    return rows


def _level0_abstracts(path: Path) -> list[str]:
    return [
        record["scalars"]["abstract"]
        for record in _exported_index_records(path, "big")
        if record["path"] == "" and record["level"] == 0
    ]


@pytest.mark.asyncio
async def test_export_orders_duplicate_generations_oldest_first(
    temp_ovpack_path: Path, request_ctx: RequestContext
):
    await export_ovpack(
        HierarchicalExportVikingFS(),
        HIERARCHICAL_ROOT,
        str(temp_ovpack_path),
        ctx=request_ctx,
        vector_store=RowTableVectorStore(stale_generation_rows()),
        include_vectors=True,
    )

    assert _level0_abstracts(temp_ovpack_path) == ["abstract v1", "abstract v3", "abstract v2"]


@pytest.mark.asyncio
async def test_import_keeps_newest_generation_of_duplicate_rows(
    temp_ovpack_path: Path, request_ctx: RequestContext, monkeypatch: pytest.MonkeyPatch
):
    embedding_metadata = {
        "provider": "test",
        "model": "demo",
        "input": "text",
        "dimensions": 3,
    }
    monkeypatch.setattr(
        "openviking.storage.ovpack.vectors.embedding_snapshot_metadata",
        lambda dimensions: {**embedding_metadata, "dimensions": dimensions},
    )
    monkeypatch.setattr(
        "openviking.storage.ovpack.vectors.current_embedding_metadata",
        lambda: embedding_metadata,
    )

    await export_ovpack(
        HierarchicalExportVikingFS(),
        HIERARCHICAL_ROOT,
        str(temp_ovpack_path),
        ctx=request_ctx,
        vector_store=RowTableVectorStore(stale_generation_rows()),
        include_vectors=True,
    )

    vector_store = RowTableVectorStore()
    result = await import_ovpack(
        FakeVikingFS(),
        str(temp_ovpack_path),
        "viking://resources/imported",
        request_ctx,
        vector_mode="require",
        vector_store=vector_store,
    )

    level0_id = vector_record_id(request_ctx.account_id, result, 0)
    writes = [row for row in vector_store.upserts if str(row["id"]) == level0_id]
    assert [row["abstract"] for row in writes] == ["abstract v1", "abstract v3", "abstract v2"]
    assert writes[-1]["vector"] == pytest.approx([2.0, 2.0, 2.0])
    assert "index_generation" not in writes[-1]


def test_image_occurrence_records_replays_rows_in_ordinal_order():
    records = [
        {
            "path": "dir/images/a.jpg",
            "level": 2,
            "scalars": {
                "chunk_id": "image:second",
                "chunk_ordinal": 1,
                "chunk_text": "cap-b",
                "image_context_uri": "viking://resources/raw/dir/ch.md",
                "image_context_hash": "hash-b",
                "parent_text": "ctx-b",
            },
        },
        {
            "path": "dir/images/a.jpg",
            "level": 2,
            "scalars": {
                "chunk_id": "image:first",
                "chunk_ordinal": 0,
                "chunk_text": "cap-a",
                "image_context_uri": "",
                "image_context_hash": "",
                "parent_text": "",
            },
        },
        {
            "path": "dir/ch.md",
            "level": 2,
            "scalars": {"chunk_id": "text:1", "chunk_text": "chapter", "abstract": "chapter"},
        },
    ]
    replayed = _image_occurrence_records(records, "dir/images/a.jpg", "viking://resources/raw")
    assert [record["ordinal"] for record in replayed] == [0, 1]
    assert replayed[0]["context_uri"] == ""
    assert replayed[1]["summary"] == "cap-b"
    assert replayed[1]["context_hash"] == "hash-b"
    assert replayed[1]["context"] == "ctx-b"

    assert _image_occurrence_records(records, "dir/ch.md", "viking://resources/raw") is None
    out_of_root = _image_occurrence_records(
        records, "dir/images/a.jpg", "viking://resources/other"
    )
    assert out_of_root == [replayed[0]]


def test_image_occurrence_records_collapses_duplicate_generations():
    def row(caption: str, generation: str) -> dict:
        return {
            "path": "dir/images/a.jpg",
            "level": 2,
            "scalars": {
                "chunk_id": "image:dup",
                "chunk_ordinal": 0,
                "chunk_text": caption,
                "image_context_uri": "viking://resources/raw/dir/ch.md",
                "image_context_hash": "hash",
                "parent_text": "ctx",
                "index_generation": generation,
            },
        }

    records = [row("old caption", "gen-1"), row("new caption", "gen-2")]
    replayed = _image_occurrence_records(records, "dir/images/a.jpg", "viking://resources/raw")
    assert len(replayed) == 1
    assert replayed[0]["summary"] == "new caption"


@pytest.mark.asyncio
async def test_recompute_import_replays_image_occurrences_with_source_meta(
    request_ctx: RequestContext, monkeypatch: pytest.MonkeyPatch
):
    chapter_uri = "viking://resources/raw/dir/ch.md"
    image_uri = "viking://resources/raw/dir/images/a.jpg"

    class ReplayVikingFS(FakeVikingFS):
        async def tree(self, uri, node_limit=None, level_limit=None, ctx=None):
            self.tree_calls.append(uri)
            return [
                {"uri": "viking://resources/raw/dir", "rel_path": "dir", "isDir": True},
                {
                    "uri": chapter_uri,
                    "rel_path": "dir/ch.md",
                    "isDir": False,
                    "name": "ch.md",
                },
                {
                    "uri": image_uri,
                    "rel_path": "dir/images/a.jpg",
                    "isDir": False,
                    "name": "a.jpg",
                },
            ]

        async def read_file(self, uri, ctx=None):
            if uri == chapter_uri:
                return b"- document_id: 820676206347751\n- page: p.23-32\n"
            raise FileNotFoundError(uri)

    captured: dict[str, dict] = {}

    async def fake_vectorize_file(**kwargs):
        captured[kwargs["file_path"]] = kwargs

    monkeypatch.setattr(
        "openviking.storage.ovpack.operations.vectorize_file", fake_vectorize_file
    )

    index_records = [
        {
            "path": "dir/images/a.jpg",
            "level": 2,
            "scalars": {
                "chunk_id": "image:abc",
                "chunk_ordinal": 0,
                "chunk_text": "该图为品牌标识。",
                "abstract": "该图为品牌标识。",
                "image_context_uri": chapter_uri,
                "image_context_hash": "hash-1",
                "parent_text": "Source URI: viking://resources/raw/dir/ch.md",
            },
        },
        {"path": "dir/ch.md", "level": 2, "scalars": {"chunk_id": "text:1", "abstract": "chapter"}},
    ]

    await _enqueue_direct_vectorization(
        ReplayVikingFS(),
        "viking://resources/raw",
        ctx=request_ctx,
        index_records=index_records,
    )

    image_call = captured[image_uri]
    occurrences = image_call["summary_dict"]["image_occurrences"]
    assert len(occurrences) == 1
    assert occurrences[0]["context_uri"] == chapter_uri
    assert occurrences[0]["context_hash"] == "hash-1"
    assert occurrences[0]["context"] == "Source URI: viking://resources/raw/dir/ch.md"
    assert occurrences[0]["source_meta"] == {
        "source_document_id": "820676206347751",
        "page": "p.23-32",
    }
    assert image_call["summary_dict"]["summary"] == "该图为品牌标识。"
    assert "image_occurrences" not in captured[chapter_uri]["summary_dict"]


class NamespaceRootVikingFS(FakeVikingFS):
    """FakeVikingFS that models the namespace-root delete guard of the real FS.

    ``rm`` on a scope root (viking://resources, viking://user/<id>) raises
    PermissionDeniedError exactly like the production ACL guard; concrete child
    paths delete normally.
    """

    def __init__(self, existing_roots: set[str] | None = None) -> None:
        super().__init__(existing_roots)

    async def ls(self, uri: str, output: str = "original", ctx=None):
        if uri in self.existing_roots:
            if uri == "viking://resources":
                return [
                    {"uri": "viking://resources/stale.txt", "isDir": False},
                ]
            return []
        raise NotFoundError(uri, "file")

    async def rm(self, uri: str, recursive: bool = False, ctx=None):
        assert recursive is True
        canonical = uri.rstrip("/")
        parts = [p for p in canonical.split("/") if p]
        if canonical == "viking://resources" or (
            parts[:1] == ["user"] and len(parts) == 2
        ):
            raise PermissionDeniedError(
                "Deleting a namespace root requires root access; use a concrete content "
                "path instead.",
                resource=uri,
            )
        self.removed_roots.append(uri)


@pytest.mark.asyncio
async def test_import_top_level_scope_overwrite_clears_children_without_deleting_root(
    temp_ovpack_path: Path, request_ctx: RequestContext
):
    manifest = _manifest_for_files("resources", {"README.md": "hello"})
    manifest["root"] = {
        "name": "resources",
        "uri": "viking://resources",
        "scope": "resources",
    }
    _write_ovpack_with_manifest(
        temp_ovpack_path,
        "resources",
        {"README.md": "hello"},
        manifest=manifest,
    )
    fake_fs = NamespaceRootVikingFS(existing_roots={"viking://resources"})

    result = await import_ovpack(
        fake_fs,
        str(temp_ovpack_path),
        "viking://",
        request_ctx,
        on_conflict="overwrite",
    )

    assert result == "viking://resources"
    # The stale pre-existing child is cleared; the root itself is never removed.
    assert "viking://resources/stale.txt" in fake_fs.removed_roots
    assert all(uri != "viking://resources" for uri in fake_fs.removed_roots)
    assert "viking://resources/README.md" in fake_fs.written_files


class RegeneratedCatalogueVikingFS(NamespaceRootVikingFS):
    """NamespaceRootVikingFS whose root index.md is a generated catalogue."""

    async def rm(self, uri: str, recursive: bool = False, ctx=None):
        canonical = uri.rstrip("/")
        if canonical == "viking://resources/index.md":
            raise FailedPreconditionError(
                f"Cannot delete the generated directory catalogue: {uri}. "
                "It is regenerated from the directory's children after any "
                "change; delete the directory or its authored content instead.",
                details={"resource": uri, "expected": "authored content"},
            )
        await super().rm(uri, recursive=recursive, ctx=ctx)


@pytest.mark.asyncio
async def test_import_top_level_overwrite_keeps_regenerated_catalogue(
    temp_ovpack_path: Path, request_ctx: RequestContext
):
    manifest = _manifest_for_files("resources", {"README.md": "hello"})
    manifest["root"] = {
        "name": "resources",
        "uri": "viking://resources",
        "scope": "resources",
    }
    _write_ovpack_with_manifest(
        temp_ovpack_path,
        "resources",
        {"README.md": "hello"},
        manifest=manifest,
    )
    fake_fs = RegeneratedCatalogueVikingFS(existing_roots={"viking://resources"})

    result = await import_ovpack(
        fake_fs,
        str(temp_ovpack_path),
        "viking://",
        request_ctx,
        on_conflict="overwrite",
    )

    assert result == "viking://resources"
    assert "viking://resources/README.md" in fake_fs.written_files
    # The generated catalogue survived clearing; the pack member overwrote it.
    assert "viking://resources/index.md" not in fake_fs.removed_roots
