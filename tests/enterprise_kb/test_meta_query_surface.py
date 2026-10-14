"""The meta surface an agent actually types, and the anchors a PDF outline gives.

Covers three defects found by running the static-KB loop end to end:

* a corpus writes ``- 机型: …`` / ``- 关键词: a, b, c`` in Chinese, and the query
  side refused the same Chinese field name it accepted on the write side;
* ``keywords=盲堵`` resolved to nothing because the whole bullet string was one
  stored value;
* a chapter title rendered with the spaces its typeset form carries
  (``第 10 章 故障诊断与排故``) was not recognised as an outline anchor, so the
  document lost its chapter tree.
"""

from __future__ import annotations

import pytest

from openviking.indexing.business_meta import AuthoritativeMeta, MetaValue
from openviking.indexing.record_meta import canonical_field_name
from openviking.parse.chapter_splitter import _NUMBERED_TITLE, split_chapters
from openviking.retrieve.meta_dictionary import BusinessDictionary
from openviking.retrieve.metadata_query import parse_constraints


# ------------------------------------------------------------------ field names
@pytest.mark.parametrize(
    ("authored", "canonical"),
    [
        ("机型", "machine_model"),
        ("关键词", "keywords"),
        ("页码", "page"),
        ("文档编号", "source_document_id"),
        ("document_id", "source_document_id"),
        ("machine_model", "machine_model"),
        ("subject_code", "subject_code"),
    ],
)
def test_authored_field_names_resolve_to_one_surface(authored, canonical):
    assert canonical_field_name(authored) == canonical


def test_unknown_field_name_passes_through():
    assert canonical_field_name("自定义字段") == "自定义字段"


def test_filter_field_alias_is_applied_before_validation():
    (parsed,) = parse_constraints([{"field": "机型", "values": ["SY215C9"]}])
    assert parsed.field == "machine_model"
    # ``document_id`` is a reserved system column; the alias has to land on the
    # filterable field instead of raising on the reserved name.
    (parsed,) = parse_constraints([{"field": "document_id", "values": ["797105495448853"]}])
    assert parsed.field == "source_document_id"


# ---------------------------------------------------------------- list-valued meta
def _meta(**values: str) -> AuthoritativeMeta:
    return AuthoritativeMeta(
        document_id="doc-1",
        content_revision="rev-1",
        source="inline:viking://resources/manual/chapter.md",
        normalizer_version="business-meta-v1",
        values={
            key: MetaValue(
                field=key,
                raw=value,
                canonical=value.upper(),
                source="inline",
                normalizer_version="business-meta-v1",
            )
            for key, value in values.items()
        },
    )


def test_a_single_keyword_resolves_to_the_row_that_lists_it():
    dictionary = BusinessDictionary("ns")
    dictionary.add_meta(_meta(keywords="拆装, 液压管路, 盲堵, 防冻剂"))
    resolved = dictionary.lookup_exact("keywords", "盲堵")
    assert resolved.canonical == ("拆装, 液压管路, 盲堵, 防冻剂",)
    assert resolved.matched_via == "exact"


def test_a_shared_keyword_widens_across_rows_instead_of_colliding():
    dictionary = BusinessDictionary("ns")
    dictionary.add_meta(_meta(keywords="拆装, 盲堵"))
    dictionary.add_meta(_document_meta("doc-2", keywords="液压回路, 盲堵"))
    resolved = dictionary.lookup_exact("keywords", "盲堵")
    assert set(resolved.canonical) == {"拆装, 盲堵", "液压回路, 盲堵"}


def test_whole_list_still_matches_exactly():
    dictionary = BusinessDictionary("ns")
    dictionary.add_meta(_meta(keywords="拆装, 液压管路, 盲堵, 防冻剂"))
    (constraint,) = parse_constraints(
        [{"field": "关键词", "values": ["拆装, 液压管路, 盲堵, 防冻剂"]}]
    )
    resolved = dictionary.lookup_exact(constraint.field, constraint.values[0])
    assert resolved.canonical == ("拆装, 液压管路, 盲堵, 防冻剂",)


def test_an_authored_field_name_filters_a_list_member_end_to_end():
    dictionary = BusinessDictionary("ns")
    dictionary.add_meta(_meta(keywords="拆装, 液压管路, 盲堵, 防冻剂"))
    (constraint,) = parse_constraints([{"field": "关键词", "values": ["盲堵"]}])
    resolved = dictionary.lookup_exact(constraint.field, constraint.values[0])
    assert resolved.canonical == ("拆装, 液压管路, 盲堵, 防冻剂",)


def _document_meta(document_id: str, **values: str) -> AuthoritativeMeta:
    meta = _meta(**values)
    return AuthoritativeMeta(
        document_id=document_id,
        content_revision=meta.content_revision,
        source=meta.source,
        normalizer_version=meta.normalizer_version,
        values=meta.values,
    )


# ------------------------------------------------------------------- PDF anchors
@pytest.mark.parametrize(
    "title",
    [
        "第 1 章 安全须知",
        "第10章 故障诊断与排故",
        "第三篇 液压系统",
        "10.2 进行操作时的注意事项",
        "1．进行拆卸工作时的注意事项",
    ],
)
def test_typeset_chapter_titles_are_anchors(title):
    assert _NUMBERED_TITLE.match(title)


@pytest.mark.parametrize("line", ["第三章", "这是一句普通正文"])
def test_bare_marker_and_prose_are_not_anchors(line):
    assert not _NUMBERED_TITLE.match(line)


def test_a_spaced_outline_builds_the_chapter_tree():
    content = "\n\n".join(
        f"第 {n} 章 标题{n}\n本节正文说明拆装与压力测试的要求，用于对齐书签锚点。" for n in (1, 2, 3)
    )
    bookmarks = [
        {"level": 1, "title": f"第 {n} 章 标题{n}", "page_num": n} for n in (1, 2, 3)
    ]
    plan = split_chapters(content, bookmarks, section_target_tokens=4096)
    assert plan.structure_source == "pdf_bookmarks"
    assert [chapter.title for chapter in plan.chapters] == [
        "第 1 章 标题1",
        "第 2 章 标题2",
        "第 3 章 标题3",
    ]
    assert all(chapter.page_start for chapter in plan.chapters)
