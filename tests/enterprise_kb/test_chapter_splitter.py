# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Behavioral coverage for lossless, non-blocking chapter planning."""

import pytest

from openviking.parse.chapter_splitter import split_chapters


def bm(title, level=1, page=None):
    return {"title": title, "level": level, "page_num": page}


def assert_lossless(plan, original):
    plan.validate_coverage(original)
    assert (
        "".join(
            c.content
            for c in sorted(
                (c for c in plan.chapters if c.start is not None), key=lambda c: c.start
            )
        )
        == original
    )


def test_bookmarks_align_by_content_with_wrong_overlapping_and_absent_pages():
    text = "封面\n\n# 1 前言\n说明\n\n## 1.1 阅读方法\n方法\n\n# 2 排故\n结尾\n"
    plan = split_chapters(
        text, [bm("1 前言", page=113), bm("1.1 阅读方法", 2), bm("2 排故", page=1)]
    )
    assert plan.structure_source == "pdf_bookmarks"
    assert plan.alignment_status == "matched"
    assert plan.chapters[1].page_start == 113
    assert plan.chapters[2].parent_id == plan.chapters[1].chapter_id
    assert "方法" not in plan.chapters[1].content
    assert_lossless(plan, text)


def test_missing_branch_uses_structural_container_and_plain_ocr_titles():
    text = "1.1 阅读方法\n方法正文\n\n1.2 保养标准\n末尾正文"
    plan = split_chapters(text, [bm("1 前言"), bm("1.1 阅读方法", 2), bm("1.2 保养标准", 2)])
    assert plan.structure_source == "pdf_bookmarks"
    assert plan.chapters[0].match_status == "structural_only"
    assert plan.chapters[0].content == ""
    assert_lossless(plan, text)


@pytest.mark.parametrize(
    "bookmarks",
    [
        [bm("找不到")],
        [bm("B"), bm("A")],
    ],
)
def test_missing_or_wrong_order_auto_falls_back_with_info(bookmarks):
    text = "前文\n\n# A\n甲\n\n### B\n乙\n"
    plan = split_chapters(text, bookmarks)
    assert plan.alignment_status == "failed"
    assert plan.structure_source == "markdown_headings"
    assert plan.info[0]["code"] == "BOOKMARK_ALIGNMENT_FAILED"
    assert plan.chapters[2].parent_id == plan.chapters[1].chapter_id
    assert_lossless(plan, text)


def test_ambiguous_titles_do_not_greedily_take_first_candidate():
    text = "# A\n甲\n\n# A\n乙\n"
    plan = split_chapters(text, [bm("A")])
    assert plan.info[0]["reason"] == "ambiguous_heading_matches"
    assert plan.structure_source == "markdown_headings"
    assert len(plan.chapters) == 2
    assert_lossless(plan, text)


def test_monotonic_global_alignment_disambiguates_title_from_neighbor_anchors():
    text = "# A\n前\n# B\n中\n# A\n后\n"
    plan = split_chapters(text, [bm("B"), bm("A")])
    assert plan.structure_source == "pdf_bookmarks"
    assert plan.chapters[-1].content == "# A\n后\n"
    assert_lossless(plan, text)


def test_fallback_supports_setext_and_ignores_fence_table_and_quote_titles():
    text = "前置\n\nTitle\n=====\n正文\n\n~~~md\n# fake\n~~~\n\n| x |\n|---|\n| # fake2 |\n\n> # fake3\n\n## End\n尾"
    plan = split_chapters(text, [bm("missing")])
    assert [c.title for c in plan.chapters] == ["前置内容", "Title", "End"]
    assert_lossless(plan, text)


def test_rule_blocks_keep_whole_table_code_and_document_tail():
    text = "intro\n\n| h |\n|---|\n" + "| x |\n" * 40 + "\n```py\na\n\nb\n```\n\ntail"
    plan = split_chapters(text, [], section_target_tokens=8)
    assert plan.structure_source == "rule_blocks"
    assert plan.alignment_status == "not_available"
    assert plan.info[0]["code"] == "BOOKMARKS_UNAVAILABLE"
    assert any("| h |" in c.content and c.content.count("| x |") == 40 for c in plan.chapters)
    assert any("```py\na\n\nb\n```" in c.content for c in plan.chapters)
    assert plan.chapters[-1].content.endswith("tail")
    assert_lossless(plan, text)


def test_short_no_heading_document_is_valid_single_chapter():
    plan = split_chapters("一句话", [])
    assert len(plan.chapters) == 1
    assert_lossless(plan, "一句话")


def test_alignment_candidate_limit_falls_back_without_dropping_content():
    text = "# A\n内容\n# B\n尾部"
    plan = split_chapters(text, [bm("A"), bm("B")], max_alignment_candidates=1)
    assert plan.info[0]["reason"] == "alignment_candidate_limit"
    assert_lossless(plan, text)


@pytest.mark.parametrize("content", ["", " \n\t"])
def test_empty_output_is_real_failure(content):
    with pytest.raises(ValueError, match="empty"):
        split_chapters(content, [])


def test_repeated_bookmark_title_with_two_content_anchors_keeps_both_sections():
    text = "# A\nfirst\n# A\nsecond\n"
    plan = split_chapters(text, [bm("A", page=1), bm("A", page=1)])
    assert plan.structure_source == "pdf_bookmarks"
    assert len(plan.chapters) == 2
    assert all(c.alias_of is None for c in plan.chapters)
    assert plan.chapters[0].content.endswith("first\n")
    assert plan.chapters[1].content.endswith("second\n")
    assert_lossless(plan, text)


def test_identical_bookmark_target_with_single_anchor_is_validated_alias():
    text = "# A\n正文"
    plan = split_chapters(text, [bm("A", page=1), bm("A", page=1)])
    assert plan.structure_source == "pdf_bookmarks"
    assert plan.chapters[1].match_status == "validated_alias"
    assert plan.chapters[1].alias_of == plan.chapters[0].chapter_id
    assert_lossless(plan, text)
