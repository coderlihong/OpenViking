# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Per-directory navigation index: generation, marker identity, and exclusion.

The index exists so an agent can locate a chapter without a vector round trip:
list the directory, grep its ``index.md``, and hand the URI to content/read.  It
must therefore (a) carry literal ``viking://`` URIs, (b) never be vectorized,
and (c) never overwrite a legacy chapter body that happens to be named
``index.md``.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest

from openviking.core.directory_index import (
    DIRECTORY_INDEX_FILENAME,
    DirectoryIndexEntry,
    directory_index_entry_uris,
    directory_index_uri,
    is_directory_index_uri,
    is_generated_directory_index,
    is_generated_index_at,
    navigation_description,
    one_line,
    render_directory_index,
    write_directory_index,
)


class FakeVikingFS:
    """Minimal VikingFS double: an in-memory map plus not-found semantics."""

    def __init__(self, files: Optional[dict[str, str]] = None) -> None:
        self.files = dict(files or {})
        self.writes: list[str] = []

    async def read_file(self, uri: str, *, ctx: Any = None, **_kwargs: Any) -> str:
        if uri not in self.files:
            from openviking_cli.exceptions import NotFoundError

            raise NotFoundError(uri, "file")
        return self.files[uri]

    async def write_file(
        self, uri: str, content: str, *, ctx: Any = None, lease_ref: Any = None
    ) -> None:
        self.files[uri] = content
        self.writes.append(uri)


ROOT = "viking://resource/menus/797105495448853"


def _generated(dir_uri: str) -> str:
    return render_directory_index(
        dir_uri,
        [DirectoryIndexEntry(name="10-2-1", uri=f"{dir_uri}/10-2-1", description="拆卸步骤")],
        [
            DirectoryIndexEntry(
                name="10-2-2.md",
                uri=f"{dir_uri}/10-2-2.md",
                description="# 10.2.2 安装步骤\n\n**SY195/205/215/225C9液压挖掘机**\n",
            )
        ],
    )


def test_directory_index_uri_and_filename_tests():
    assert directory_index_uri(f"{ROOT}/10-2/") == f"{ROOT}/10-2/index.md"
    assert DIRECTORY_INDEX_FILENAME == "index.md"
    assert is_directory_index_uri(f"{ROOT}/10-2/index.md")
    assert not is_directory_index_uri(f"{ROOT}/10-2/overview.md")


def test_rendered_index_carries_literal_uris_for_grep():
    body = _generated(f"{ROOT}/10-2")
    uris = directory_index_entry_uris(body)
    assert f"{ROOT}/10-2/10-2-1" in uris
    assert f"{ROOT}/10-2/10-2-2.md" in uris
    # The catalogue states what it is, so an agent does not mistake it for a chapter.
    assert "不建向量" in body and "BM25" in body
    # It also states how to use it: the locate loop is grep-or-read, never recall.
    assert "grep" in body and "content/read" in body
    assert "## 子目录" in body and "## 文件" in body
    # A multi-line summary collapses into one greppable line.
    assert "SY195/205/215/225C9液压挖掘机" in body
    entry_lines = [line for line in body.splitlines() if line.startswith("- [")]
    assert len(entry_lines) == 2
    assert all("viking://" in line for line in entry_lines)


def test_marker_is_the_authority_not_the_filename():
    generated = _generated(f"{ROOT}/10-2")
    assert is_generated_directory_index(generated)
    assert is_generated_directory_index(generated.encode("utf-8"))

    # A branch chapter body published before this feature also lives at index.md.
    authored = "# 10.2 进行操作时的注意事项\n\n进行装置的拆卸或安装时必须遵循下列注意事项。\n"
    assert not is_generated_directory_index(authored)
    assert not is_generated_directory_index(None)
    assert not is_generated_directory_index(b"\xff\xfe not utf8")
    # Frontmatter that is not ours does not opt a file out of indexing.
    assert not is_generated_directory_index("---\ndirectory: viking://resource/x\n---\nbody\n")


def test_one_line_collapses_markdown_noise():
    assert one_line("# 标题\n\n- 项目符号\n  续行\n") == "标题 项目符号 续行"
    assert one_line(None) == ""
    assert len(one_line("长" * 500)) <= 220


def test_navigation_description_drops_meta_and_html():
    chapter = (
        "# 3.1  Dimensions\n"
        "\n"
        "- document_id: 820676206347751\n"
        "- chapter_uri: kb://chapters/844164375775040\n"
        "- page: p.87-88\n"
        "\n"
        '<table border="1"><tr><td>D<br>日</td></tr></table>\n'
        "\n"
        "Machine model specification table for the SY195 series."
    )
    line = navigation_description(chapter)
    assert "document_id:" not in line
    assert "chapter_uri:" not in line
    assert "<table" not in line and "<br>" not in line
    assert "Machine model specification table" in line

    # no meta/HTML at all -> behaves like one_line on prose
    assert navigation_description("# 标题\n\n- 项目符号\n") == one_line("# 标题\n\n- 项目符号\n")
    # provenance-only body carries nothing presentable
    assert navigation_description("- document_id: 1\n- page: p.2") == ""
    assert navigation_description(None) == ""


@pytest.mark.asyncio
async def test_write_creates_index_and_stays_idempotent():
    fs = FakeVikingFS()
    target = directory_index_uri(f"{ROOT}/10-2")
    entries = [
        DirectoryIndexEntry(
            name="10-2-1",
            uri=f"{ROOT}/10-2/10-2-1",
            description="拆卸步骤",
            kind="directory",
        )
    ]
    first = await write_directory_index(
        viking_fs=fs,
        dir_uri=f"{ROOT}/10-2",
        directories=entries,
        files=[],
    )
    assert first.wrote and first.uri == target and first.entries == 1
    second = await write_directory_index(
        viking_fs=fs,
        dir_uri=f"{ROOT}/10-2",
        directories=entries,
        files=[],
    )
    assert not second.wrote and second.reason == "unchanged"
    assert fs.writes == [target]


@pytest.mark.asyncio
async def test_write_never_overwrites_a_legacy_chapter_body():
    body = "# 10.2 进行操作时的注意事项\n\n进行装置的拆卸或安装时必须遵循下列注意事项。\n"
    fs = FakeVikingFS({f"{ROOT}/10-2/index.md": body})
    result = await write_directory_index(
        viking_fs=fs,
        dir_uri=f"{ROOT}/10-2",
        directories=[],
        files=[
            DirectoryIndexEntry(name="a.md", uri=f"{ROOT}/10-2/a.md"),
        ],
    )
    assert not result.wrote and result.reason == "foreign_body"
    assert fs.files[f"{ROOT}/10-2/index.md"] == body
    assert fs.writes == []


@pytest.mark.asyncio
async def test_is_generated_index_at_short_circuits_on_filename():
    fs = FakeVikingFS({f"{ROOT}/10-2/chapter.md": "---\nkind: directory_index\n---\n\nx\n"})
    assert not await is_generated_index_at(fs, f"{ROOT}/10-2/chapter.md")
    assert not await is_generated_index_at(fs, f"{ROOT}/10-2/index.md")
    assert await is_generated_index_at(fs, f"{ROOT}/10-2", cache={}) is False
    fs.files[f"{ROOT}/10-2/index.md"] = _generated(f"{ROOT}/10-2")
    assert await is_generated_index_at(fs, f"{ROOT}/10-2/index.md")
    cache: dict[str, bool] = {}
    assert await is_generated_index_at(fs, f"{ROOT}/10-2/index.md", cache=cache)
    assert cache == {f"{ROOT}/10-2/index.md": True}


# --------------------------------------------------------------------------
# Semantic pipeline hook: the DAG publishes the catalogue beside its sidecars.
# --------------------------------------------------------------------------


def _dag_shim(fs: FakeVikingFS, root_uri: str = ROOT):
    from openviking.storage.queuefs.semantic_dag import SemanticDagExecutor

    class Shim:
        _chapter_manifest = SemanticDagExecutor._chapter_manifest
        _manifest_hint = SemanticDagExecutor._manifest_hint
        _is_generated_navigation_index = SemanticDagExecutor._is_generated_navigation_index
        _media_listing_for = SemanticDagExecutor._media_listing_for
        _write_navigation_index = SemanticDagExecutor._write_navigation_index

    shim = Shim()
    shim._viking_fs = fs
    shim._ctx = None
    shim._root_uri = root_uri
    shim._generation_trigger = "reindex"
    shim._lock = None
    return shim


def _dir_node(children_dirs, file_paths, file_summaries, children_abstracts):
    from openviking.storage.queuefs.semantic_dag import DirNode

    return DirNode(
        uri=f"{ROOT}/10-2",
        children_dirs=children_dirs,
        file_paths=file_paths,
        file_index={path.rsplit("/", 1)[-1]: i for i, path in enumerate(file_paths)},
        child_index={},
        file_summaries=file_summaries,
        children_abstracts=children_abstracts,
        pending=0,
    )


@pytest.mark.asyncio
async def test_navigation_index_lists_every_direct_child_with_provenance():
    fs = FakeVikingFS(
        {
            f"{ROOT}/_manifest.json": json.dumps(
                {
                    "chapters": [
                        {
                            "title": "10.2.1 拆卸步骤",
                            "path": "10-2/10-2-1.md",
                            "page_start": 638,
                        },
                        {"title": "10.2.2 安装步骤", "path": "10-2/10-2-2.md", "page_start": 641},
                    ]
                },
                ensure_ascii=False,
            )
        }
    )
    shim = _dag_shim(fs)
    node = _dir_node(
        children_dirs=[f"{ROOT}/10-2/10-3"],
        file_paths=[f"{ROOT}/10-2/10-2-1.md", f"{ROOT}/10-2/10-2-2.md"],
        file_summaries=[
            {"name": "10-2-1.md", "summary": "拆卸"},
            {"name": "10-2-2.md", "summary": "安装"},
        ],
        children_abstracts=[{"name": "10-3", "abstract": "注意事项"}],
    )
    await shim._write_navigation_index(f"{ROOT}/10-2", node)

    index = fs.files[f"{ROOT}/10-2/index.md"]
    assert is_generated_directory_index(index)
    for uri in (
        f"{ROOT}/10-2/10-3",
        f"{ROOT}/10-2/10-2-1.md",
        f"{ROOT}/10-2/10-2-2.md",
    ):
        assert uri in index
    # Chapter provenance from the parser manifest beats the sanitized directory name.
    assert "10.2.1 拆卸步骤" in index and "p.638" in index
    # The catalogue never lists itself.
    assert DIRECTORY_INDEX_FILENAME not in directory_index_entry_uris(index)


@pytest.mark.asyncio
async def test_navigation_index_write_failure_does_not_raise():
    class BrokenFS(FakeVikingFS):
        async def read_file(self, uri: str, *, ctx: Any = None, **kwargs: Any) -> str:
            raise RuntimeError("storage unavailable")

    shim = _dag_shim(BrokenFS())
    node = _dir_node([], [f"{ROOT}/10-2/a.md"], [{"name": "a.md", "summary": "s"}], [])
    await shim._write_navigation_index(f"{ROOT}/10-2", node)  # must not raise


@pytest.mark.asyncio
async def test_dag_detection_of_generated_vs_authored_index():
    generated = _generated(f"{ROOT}/10-2")
    fs = FakeVikingFS(
        {
            f"{ROOT}/10-2/index.md": generated,
            f"{ROOT}/10-3/index.md": "# 10.3 注意事项\n\n正文。\n",
        }
    )
    shim = _dag_shim(fs)
    assert await shim._is_generated_navigation_index(f"{ROOT}/10-2/index.md")
    # A legacy chapter body stays source knowledge and keeps its vectors.
    assert not await shim._is_generated_navigation_index(f"{ROOT}/10-3/index.md")
    assert not await shim._is_generated_navigation_index(f"{ROOT}/10-2/a.md")


# --------------------------------------------------------------------------
# Chapter layout: the reserved name must stay free for the catalogue.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chapter_layout_leaves_index_md_free(tmp_path):
    from openviking.parse.parsers.markdown import MarkdownParser

    markdown = "\n".join(
        [
            "# 10.2 进行操作时的注意事项",
            "",
            "进行装置的拆卸或安装时，必须遵行下列注意事项。",
            "",
            "## 10.2.1 拆卸",
            "",
            "拆开软管或钢管后，须进行遮盖或装上盲堵。",
            "",
            "## 10.2.2 安装",
            "",
            "重新装配时按配合标记安装。",
            "",
        ]
    )
    parser = MarkdownParser()
    plan = {
        "chapters": [
            {
                "chapter_id": "c1",
                "title": "10.2 进行操作时的注意事项",
                "level": 1,
                "content": markdown.split("## 10.2.1")[0],
                "start": 0,
                "end": None,
                "parent_id": None,
                "page_start": 638,
                "match_status": "exact",
            },
            {
                "chapter_id": "c2",
                "title": "10.2.1 拆卸",
                "level": 2,
                "content": "拆开软管或钢管后，须进行遮盖或装上盲堵。",
                "start": None,
                "end": None,
                "parent_id": "c1",
                "page_start": 639,
                "match_status": "exact",
            },
            {
                "chapter_id": "c3",
                "title": "10.2.2 安装",
                "level": 2,
                "content": "重新装配时按配合标记安装。",
                "start": None,
                "end": None,
                "parent_id": "c1",
                "page_start": 641,
                "match_status": "exact",
            },
        ],
        "structure_source": "markdown_headings",
        "alignment_status": "aligned",
        "bookmarks": [],
        "info": {},
    }
    layout = parser._compute_chapter_layout(
        _ChapterPlan(plan), "viking://temp/1", f"{ROOT}/797105495448853", "维修手册"
    )
    written = {op.uri for op in layout.ops if op.kind == "write"}
    branch_paths = [uri for uri in written if uri.endswith(f"/{DIRECTORY_INDEX_FILENAME}")]
    assert not branch_paths, f"chapter body must not occupy the catalogue name: {branch_paths}"
    # The branch chapter keeps its prose inside its own directory, which also
    # preserves the historical image location "{stem}/images/".
    assert f"{ROOT}/797105495448853/10.2_进行操作时的注意事项/10.2_进行操作时的注意事项.md" in written
    assert f"{ROOT}/797105495448853/10.2_进行操作时的注意事项/10.2.1_拆卸.md" in written


class _ChapterPlan:
    """Duck-typed stand-in for ChapterPlan with the fields the layout reads."""

    def __init__(self, payload: dict[str, Any]) -> None:
        from openviking.parse.chapter_splitter import Chapter

        self.chapters = [Chapter(**record) for record in payload["chapters"]]
        self.structure_source = payload["structure_source"]
        self.alignment_status = payload["alignment_status"]
        self.bookmarks = payload["bookmarks"]
        self.info = payload["info"]

    def validate_coverage(self, _original: str) -> None:
        return None


def test_authored_index_collision_points_at_the_canonical_chapter_name():
    """The reservation names the file the caller should have used."""
    from openviking.core.directory_index import authored_index_collision

    body = "# 6.1 控制功能\n\n概述正文。"
    assert authored_index_collision(f"{ROOT}/6_电气系统/index.md", body) == (
        f"{ROOT}/6_电气系统/6_电气系统.md"
    )
    # A byte payload is the same decision; the boundary sees base64-decoded writes.
    assert authored_index_collision(f"{ROOT}/6_电气系统/index.md", body.encode()) == (
        f"{ROOT}/6_电气系统/6_电气系统.md"
    )


def test_authored_index_collision_leaves_the_catalogue_and_other_trees_alone():
    from openviking.core.directory_index import authored_index_collision

    assert authored_index_collision(f"{ROOT}/10-2/index.md", _generated(f"{ROOT}/10-2")) is None
    assert authored_index_collision(f"{ROOT}/10-2/overview.md", "# prose") is None
    # Memory and skill trees have their own discovery contracts; only resources reserve the name.
    assert authored_index_collision("viking://user/u1/memories/x/index.md", "# prose") is None


# --------------------------------------------------------------------------
# Maintenance pass: legacy prose at ``{dir}/index.md`` is relocated so the
# catalogue has a free name to land on.  The pass is what actually converts a
# pre-feature corpus into one where every directory carries a listing.
# --------------------------------------------------------------------------


def _maintenance():
    from scripts.maintenance.legacy_directory_index import (
        relocate_legacy_directory_index as m,
    )

    return m


class FakeContentClient:
    """In-memory content API double: only the four calls the pass makes."""

    def __init__(self, tree: dict[str, list[dict[str, Any]]], bodies: dict[str, str]) -> None:
        self.tree = tree
        self.files = dict(bodies)
        self.writes: list[str] = []
        self.removed: list[str] = []

    def ls(self, uri: str) -> list[dict[str, Any]]:
        return self.tree.get(uri.rstrip("/"), [])

    def read_body(self, uri: str) -> Optional[str]:
        return self.files.get(uri.rstrip("/"))

    def write(self, uri: str, body: str) -> None:
        self.files[uri] = body
        self.writes.append(uri)

    def rm(self, uri: str) -> None:
        self.files.pop(uri, None)
        self.removed.append(uri)


LEGACY_ROOT = "viking://resources/raw/legacy"
_LEGACY_PROSE = "# 6.1 控制功能\n\n概述正文。\n"


def _legacy_collision(*, canonical_exists: bool = False):
    return _maintenance().Collision(
        directory=f"{LEGACY_ROOT}/6_电气系统",
        index_uri=f"{LEGACY_ROOT}/6_电气系统/index.md",
        canonical_uri=f"{LEGACY_ROOT}/6_电气系统/6_电气系统.md",
        body=_LEGACY_PROSE,
        canonical_exists=canonical_exists,
    )


def test_maintenance_scan_separates_collisions_from_catalogues():
    m = _maintenance()
    tree = {
        LEGACY_ROOT: [
            {"uri": f"{LEGACY_ROOT}/6_电气系统", "isDir": True},
            {"uri": f"{LEGACY_ROOT}/10-2", "isDir": True},
        ],
        f"{LEGACY_ROOT}/6_电气系统": [
            {"uri": f"{LEGACY_ROOT}/6_电气系统/index.md", "isDir": False}
        ],
        f"{LEGACY_ROOT}/10-2": [
            {"uri": f"{LEGACY_ROOT}/10-2/index.md", "isDir": False},
            {"uri": f"{LEGACY_ROOT}/10-2/10-2-1.md", "isDir": False},
        ],
    }
    bodies = {
        f"{LEGACY_ROOT}/6_电气系统/index.md": _LEGACY_PROSE,
        f"{LEGACY_ROOT}/10-2/index.md": _generated(f"{LEGACY_ROOT}/10-2"),
    }
    report = m.find_collisions(FakeContentClient(tree, bodies), LEGACY_ROOT, None)

    assert report.directories == 3
    assert report.catalogued == 1
    assert len(report.collisions) == 1
    collision = report.collisions[0]
    assert collision.directory == f"{LEGACY_ROOT}/6_电气系统"
    assert collision.canonical_uri == f"{LEGACY_ROOT}/6_电气系统/6_电气系统.md"
    assert collision.canonical_exists is False


def test_maintenance_relocation_frees_the_name_and_lands_the_prose():
    m = _maintenance()
    collision = _legacy_collision()
    client = FakeContentClient({}, {collision.index_uri: _LEGACY_PROSE})

    result = m.relocate(client, collision)

    assert result.action == "relocated"
    assert client.writes == [collision.canonical_uri]
    assert client.removed == [collision.index_uri]
    assert client.files[collision.canonical_uri] == _LEGACY_PROSE
    assert collision.index_uri not in client.files


def test_maintenance_relocation_never_clobbers_an_existing_chapter():
    m = _maintenance()
    collision = _legacy_collision(canonical_exists=True)
    client = FakeContentClient({}, {collision.index_uri: _LEGACY_PROSE})

    result = m.relocate(client, collision)

    assert result.action == "blocked_canonical_exists"
    assert client.writes == []
    assert client.removed == []


def test_maintenance_dry_run_plan_mutates_nothing():
    m = _maintenance()
    plan = m._plan(_legacy_collision())
    assert plan.action == "would-relocate"
    assert plan.canonical_uri == f"{LEGACY_ROOT}/6_电气系统/6_电气系统.md"
