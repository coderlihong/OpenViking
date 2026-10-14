# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""A branch chapter's own prose lives in *its* directory, not in a re-opened parent.

The published chapter tree is what the agent navigates, so the layout rule is
load-bearing: ``章/节/节.md``.  Repeating the whole nested stem
(``章/节/章/节.md``) made the writer create a directory that no bookmark asked for
and gave the vector store a row whose URI contradicted the table of contents.
"""

from openviking.parse.chapter_splitter import Chapter, ChapterPlan
from openviking.parse.parsers.markdown import MarkdownParser

BODIES = [
    ("c1", "第 1 章 拆装", 1, None, "# 第 1 章 拆装\n本章父级正文。\n"),
    ("c2", "1.1 注意事项", 2, "c1", "## 1.1 注意事项\n节级正文。\n"),
    ("c3", "1.1.1 盲堵", 3, "c2", "### 1.1.1 盲堵\n叶子正文。\n"),
    ("c4", "第 2 章 检查", 1, None, "# 第 2 章 检查\n另一章正文。\n"),
]


def _plan() -> ChapterPlan:
    original = "".join(body for *_, body in BODIES)
    cursor = 0
    chapters = []
    for index, (chapter_id, title, level, parent_id, body) in enumerate(BODIES, start=1):
        chapters.append(
            Chapter(
                chapter_id=chapter_id,
                title=title,
                level=level,
                content=body,
                start=cursor,
                end=cursor + len(body),
                parent_id=parent_id,
                page_start=index,
            )
        )
        cursor += len(body)
    return ChapterPlan(chapters, "pdf_bookmarks", "matched", [])


def _layout():
    return MarkdownParser()._compute_chapter_layout(
        _plan(), "viking://temp/parse", "viking://temp/parse/manual", "manual"
    )


def test_nested_branch_self_file_repeats_only_its_own_segment():
    layout = _layout()
    paths = {record["chapter_id"]: record["path"] for record in layout.meta["chapters"]}
    assert paths == {
        "c1": "第_1_章_拆装/第_1_章_拆装.md",
        "c2": "第_1_章_拆装/1.1_注意事项/1.1_注意事项.md",
        "c3": "第_1_章_拆装/1.1_注意事项/1.1.1_盲堵.md",
        "c4": "第_2_章_检查.md",
    }


def test_no_write_path_reopens_an_ancestor_segment():
    """``章/节/章/...`` would mean a directory the outline never declared."""
    for op in _layout().ops:
        if op.kind != "write":
            continue
        segments = [s for s in op.uri.split("/") if s and s != "."]
        assert len(segments) == len(set(segments)), f"duplicated layer in {op.uri}"


def test_branch_directories_are_created_before_their_files():
    ops = _layout().ops
    made = {op.uri for op in ops if op.kind == "mkdir"}
    assert "viking://temp/parse/manual/第_1_章_拆装" in made
    assert "viking://temp/parse/manual/第_1_章_拆装/1.1_注意事项" in made


def test_index_md_stays_reserved_for_the_generated_catalogue():
    layout = _layout()
    written = [op.uri for op in layout.ops if op.kind == "write"]
    assert not [uri for uri in written if uri.endswith("/index.md")]
    assert written.count("viking://temp/parse/manual/toc.md") == 1
