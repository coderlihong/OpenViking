# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Caption backfill into figure blocks of citing markdown files."""

import pytest

from openviking.parse.figure_caption import (
    _EMPTY_CAPTION_PLACEHOLDER as _PLACEHOLDER,
    _backfill_content,
    backfill_image_captions,
    fill_figure_caption,
    find_figure_spans,
)
from openviking.parse.image_rewrite import wrap_in_figure


def _figure(vikingsrc: str, caption: str = "") -> str:
    return wrap_in_figure(vikingsrc, vikingsrc, caption=caption)


class TestFindFigureSpans:
    def test_finds_multiple_blocks(self):
        content = "before\n" + _figure("viking://r/a.jpg") + "\nmid\n" + _figure("viking://r/b.jpg") + "\nafter"
        spans = find_figure_spans(content)
        assert len(spans) == 2
        assert spans[0][2].count('vikingsrc="viking://r/a.jpg"') == 1
        assert spans[1][2].count('vikingsrc="viking://r/b.jpg"') == 1

    def test_unclosed_block_ignored(self):
        content = "<figure>\n    <img src=\"x\"/>\n"
        assert find_figure_spans(content) == []


class TestFillFigureCaption:
    def test_replaces_empty_caption(self):
        block = _figure("viking://r/a.jpg")
        filled = fill_figure_caption(block, "电气原理图，展示连接关系。")
        assert "电气原理图，展示连接关系。" in filled
        assert "<figcaption>" in filled and "</figcaption>" in filled

    def test_replaces_existing_caption(self):
        block = _figure("viking://r/a.jpg", caption="旧描述")
        filled = fill_figure_caption(block, "新描述")
        assert "新描述" in filled
        assert "旧描述" not in filled

    def test_empty_caption_gets_placeholder(self):
        block = _figure("viking://r/a.jpg", caption="旧描述")
        filled = fill_figure_caption(block, "   ")
        assert "未生成图片描述" in filled


class TestBackfillContent:
    def test_matches_by_exact_uri(self):
        image_uri = "viking://resources/doc/images/x.jpg"
        content = "# t\n\n" + _figure(image_uri) + "\n\nbody"
        new_content, filled = _backfill_content(content, image_uri, ["描述X"])
        assert filled == 1
        assert "描述X" in new_content

    def test_matches_by_filename_for_relative_occurrences(self):
        image_uri = "viking://resources/doc/images/x.jpg"
        # Occurrence context resolved the reference to the sibling filename;
        # the vikingsrc stored in the md is relative-form, so matching goes by
        # filename.
        content = "# t\n\n" + _figure(image_uri) + "\n\nbody"
        new_content, filled = _backfill_content(content, "other/prefix/images/x.jpg", ["描述Y"])
        assert filled == 1
        assert "描述Y" in new_content

    def test_no_match_leaves_content_unchanged(self):
        content = "# t\n\n" + _figure("viking://resources/doc/images/other.jpg") + "\n"
        new_content, filled = _backfill_content(content, "viking://resources/doc/images/x.jpg", ["d"])
        assert filled == 0
        assert new_content == content

    def test_two_occurrences_fill_two_blocks(self):
        base = "viking://resources/doc"
        content = _figure(f"{base}/a.jpg") + "\n\n" + _figure(f"{base}/b.jpg")
        new_content, filled = _backfill_content(content, f"{base}/a.jpg", ["第一"])
        assert filled == 1
        assert "第一" in new_content

    def test_inline_code_figure_not_touched(self):
        image_uri = "viking://resources/doc/x.jpg"
        content = "```\n" + _figure(image_uri) + "\n```\n"
        new_content, filled = _backfill_content(content, image_uri, ["d"])
        assert filled == 0
        assert new_content == content

    def test_already_filled_caption_wins(self):
        # First-write-wins: a figure whose figcaption already holds real text
        # is not rewritten — rewriting would bump the md draft epoch and
        # re-trip the joint-index publication gate for sibling images.
        image_uri = "viking://resources/doc/x.jpg"
        content = _figure(image_uri, caption="已有描述")
        new_content, filled = _backfill_content(content, image_uri, ["新描述"])
        assert filled == 0
        assert new_content == content

    def test_placeholder_caption_is_refillable(self):
        image_uri = "viking://resources/doc/x.jpg"
        content = _figure(image_uri, caption=_PLACEHOLDER)
        new_content, filled = _backfill_content(content, image_uri, ["真实描述"])
        assert filled == 1
        assert "真实描述" in new_content
        assert _PLACEHOLDER not in new_content


class _FakeVikingFS:
    def __init__(self, files: dict):
        self.files = files

    async def read_file(self, uri, ctx=None):
        if uri not in self.files:
            raise FileNotFoundError(uri)
        data = self.files[uri]
        return data.decode() if isinstance(data, bytes) else data

    async def write_file(self, uri, content, ctx=None, lease_ref=None):
        self.files[uri] = content


class TestBackfillImageCaptions:
    @pytest.mark.asyncio
    async def test_writes_back_to_citing_md(self):
        image_uri = "viking://resources/res/images/x.jpg"
        md_uri = "viking://resources/res/index.md"
        files = {md_uri: ("# t\n\n" + _figure(image_uri)).encode()}
        fs = _FakeVikingFS(files)
        summary = {
            "name": "x.jpg",
            "summary": "联合摘要",
            "image_occurrences": [
                {"context_uri": md_uri, "summary": "图示描述", "ordinal": 0},
            ],
        }
        updated = await backfill_image_captions(fs, image_uri, summary)
        assert updated == 1
        assert "图示描述" in files[md_uri]

    @pytest.mark.asyncio
    async def test_non_md_context_skipped(self):
        image_uri = "viking://resources/res/images/x.jpg"
        files = {}
        fs = _FakeVikingFS(files)
        summary = {
            "name": "x.jpg",
            "summary": "",
            "image_occurrences": [
                {"context_uri": "viking://resources/res/note.txt", "summary": "d"},
            ],
        }
        assert await backfill_image_captions(fs, image_uri, summary) == 0
