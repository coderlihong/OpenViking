# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Vector records state their modality category and their source provenance.

An image joint embedding and a text parent/child chunk embedding must be
distinguishable *by meta filter*, and every record must be citable back to the
viking URI it was built from.  These tests pin the derivation (the authored
``- key: value`` block), the write side, and the read side equality token so the
two can never drift apart.
"""

from __future__ import annotations

import json

from openviking.indexing.business_meta import build_meta, canonical_filter
from openviking.indexing.metadata_integration import project_meta, value_token
from openviking.indexing.record_meta import (
    IMAGE_CATEGORY,
    TEXT_CATEGORY,
    extract_inline_metadata,
    stamp_record_meta,
)
from openviking.retrieve.meta_dictionary import (
    EXACT,
    FUZZY,
    MetaFilter,
    BusinessDictionary,
    MetaOpNotAllowedError,
    resolve_query,
    rule_for,
)
from openviking.retrieve.metadata_query import token_filter

CHAPTER = """# 10.2 进行操作时的注意事项

- document_id: 797105495448853
- chapter_uri: kb://chapters/843907453002281
- page: p.638-641

## 10.2进行操作时的注意事项

进行装置的拆卸或安装（分解或装配）时，必须遵行下列注意事项。

<figure>
    <img src="images/844775934052881.jpg"/ vikingsrc="viking://resource/menus/797105495448853/images/844775934052881.jpg">
        <figcaption># 故障诊断与排故\n## 10.2进行操作时的注意事项\n\nSANY</figcaption>
</figure>

**拆装**

**SY195/205/215/225C9液压挖掘机**

1．进行拆卸工作时的注意事项

·若发动机冷却液含有防冻剂，应正确地处理。
"""

ROOT = "viking://resource/menus/797105495448853"


def test_authored_provenance_block_becomes_filterable_values():
    values = extract_inline_metadata(CHAPTER)
    # ``document_id`` is a reserved system column, so the corpus's own document
    # number is stored under a name the meta layer can actually hold.
    assert values == {
        "source_document_id": "797105495448853",
        "chapter_uri": "kb://chapters/843907453002281",
        "page": "p.638-641",
    }


def test_chinese_keys_alias_onto_the_same_fields():
    values = extract_inline_metadata(
        "# 章\n- 机型: SY195/205C9\n- 关键词: 拆装, 液压管路\n- 页码: p.638\n正文\n"
    )
    assert values["machine_model"] == "SY195/205C9"
    assert values["keywords"] == "拆装, 液压管路"
    assert values["page"] == "p.638"


def test_scan_stops_before_ordinary_prose_lists():
    # The bullet list inside the chapter body is instructions, not metadata.
    values = extract_inline_metadata("# 章\n- document_id: 1\n\n·拆卸前应在必要的地方做配合标记\n")
    assert values == {"source_document_id": "1"}
    assert extract_inline_metadata("# 章\n先写正文\n- document_id: 1\n") == {}
    assert extract_inline_metadata("") == {}


def test_trusted_provenance_is_read_off_the_source_document_not_the_window():
    # An image row is built from a VLM summary plus a token-budgeted context
    # window that opens with its own ``Source URI:`` header.  The figure of a long
    # chapter may not even have the provenance block inside its window, so the
    # caller that holds the source passes what the source declared instead of
    # letting the record lose the chapter's ``机型``/``page`` at filter time.
    fields = {"uri": f"{ROOT}/10.2/images/844775934052881.jpg", "account_id": "acct"}
    window = "Source URI: viking://resource/10.2/10.2.md\nChapter: 拆装\nNeighboring source text:\n起吊时选用足够强度的设备。\n"
    derived = stamp_record_meta(
        fields,
        category=IMAGE_CATEGORY,
        contents=(window,),
        source_uri=f"{ROOT}/10.2/10.2.md",
        image_uri=fields["uri"],
        values={"机型": "SY215C9液压挖掘机", "page": "p.638-641"},
    )
    # The authored Chinese key resolves through the same alias table the query side
    # uses, so one field is one filter surface.
    assert derived["machine_model"] == "SY215C9液压挖掘机"
    assert derived["page"] == "p.638-641"
    assert derived["category"] == IMAGE_CATEGORY
    # Without the hand-over, the same window yields nothing -- which is exactly the
    # hole: no value is invented from the window's decoration.
    assert extract_inline_metadata(window) == {}


def test_text_and_image_records_state_different_categories():
    text_fields = {"uri": f"{ROOT}/10.2/10.2.md", "account_id": "acct"}
    derived_text = stamp_record_meta(
        text_fields, category=TEXT_CATEGORY, contents=(CHAPTER,), source_uri=text_fields["uri"]
    )
    assert derived_text["category"] == "text"
    assert derived_text["source_uri"] == text_fields["uri"]
    assert derived_text["page"] == "p.638-641"

    image_fields = {"uri": f"{ROOT}/10.2/images/844775934052881.jpg", "account_id": "acct"}
    derived_image = stamp_record_meta(
        image_fields,
        category=IMAGE_CATEGORY,
        contents=("图注：故障诊断与排故",),
        source_uri=f"{ROOT}/10.2/10.2.md",
        image_uri=image_fields["uri"],
    )
    assert derived_image["category"] == "images"
    # An image points at the chapter that embeds it, and at itself.
    assert derived_image["source_uri"] == f"{ROOT}/10.2/10.2.md"
    assert derived_image["image_uri"] == image_fields["uri"]
    # No provenance block in the VLM summary, so no invented page number.
    assert "page" not in derived_image


def test_sidecar_still_owns_corpus_fields_and_the_file_owns_its_provenance():
    fields = {
        "uri": f"{ROOT}/10.2/10.2.md",
        "account_id": "acct",
        "_business_meta_input": {
            "values": {"modelid": "SY215-C", "subject_code": "SY021NCB62788", "page": "p.1"},
            "source": "viking://resource/menus/.business_meta.json@sha256:abc",
            "dependencies": [{"uri": f"{ROOT}/.business_meta.json", "sha256": "abc"}],
            "fingerprint": "fp",
        },
    }
    stamp_record_meta(fields, category=TEXT_CATEGORY, contents=(CHAPTER,))
    bundle = fields["_business_meta_input"]
    # Declared corpus fields are untouched and keep their own provenance source.
    assert bundle["values"]["modelid"] == "SY215-C"
    assert bundle["values"]["subject_code"] == "SY021NCB62788"
    # A file-scoped key is corrected by the file itself.
    assert bundle["values"]["page"] == "p.638-641"
    assert bundle["values"]["category"] == "text"
    assert bundle["source"].endswith("+inline:" + f"{ROOT}/10.2/10.2.md")
    assert bundle["dependencies"] == [{"uri": f"{ROOT}/.business_meta.json", "sha256": "abc"}]
    assert bundle["fingerprint"] != "fp"


def test_stamping_never_touches_user_search_tags():
    fields = {"uri": f"{ROOT}/a.md", "account_id": "acct", "search_tags": ["owner:液压"]}
    stamp_record_meta(fields, category=TEXT_CATEGORY, contents=("# 章\n- page: p.2\n",))
    assert fields["search_tags"] == ["owner:液压"]


def test_unknown_category_is_rejected():
    try:
        stamp_record_meta({"uri": "viking://resource/a.md"}, category="image")
    except ValueError as exc:
        assert "unsupported vector record category" in str(exc)
    else:  # pragma: no cover - the write side must stay a closed enum
        raise AssertionError("category must be exactly images or text")


def _tokens_for(fields, category_values):
    bundle = fields["_business_meta_input"]
    meta = build_meta(
        bundle["values"],
        document_id="doc",
        content_revision="rev",
        source=bundle["source"],
    )
    assert meta.canonical("category") == category_values
    return project_meta(meta.to_payload())


def test_write_token_and_read_token_agree_for_both_categories():
    text_fields = {"uri": f"{ROOT}/a.md", "account_id": "acct"}
    stamp_record_meta(text_fields, category=TEXT_CATEGORY, contents=("# 章\n- page: p.9\n",))
    text_tokens = set(_tokens_for(text_fields, "TEXT")[token_column()])

    image_fields = {"uri": f"{ROOT}/a/images/x.jpg", "account_id": "acct"}
    stamp_record_meta(
        image_fields,
        category=IMAGE_CATEGORY,
        contents=("图注",),
        source_uri=f"{ROOT}/a.md",
        image_uri=image_fields["uri"],
    )
    image_tokens = set(_tokens_for(image_fields, "IMAGES")[token_column()])

    assert value_token("category", "TEXT") in text_tokens
    assert value_token("category", "IMAGES") in image_tokens
    assert value_token("category", "TEXT") not in image_tokens
    # Provenance rides the same equality channel: the image row points back at the
    # chapter that embeds it, and at itself.
    assert value_token("source_uri", f"{ROOT}/a.md".upper()) in image_tokens
    assert value_token("image_uri", image_fields["uri"].upper()) in image_tokens


def token_column():
    from openviking.indexing.metadata_integration import TOKEN_COLUMN

    return TOKEN_COLUMN


def test_meta_filter_on_category_resolves_to_the_written_token():
    fields = {"uri": f"{ROOT}/a.md", "account_id": "acct"}
    stamp_record_meta(fields, category=IMAGE_CATEGORY, contents=("图注",), source_uri=f"{ROOT}/a.md")
    meta = build_meta(
        fields["_business_meta_input"]["values"],
        document_id="doc",
        content_revision="rev",
        source=fields["_business_meta_input"]["source"],
    )
    dictionary = BusinessDictionary("menus")
    dictionary.add_meta(meta)
    resolution = resolve_query(
        dictionary, meta_filters=[MetaFilter("category", ("images",), EXACT)]
    )
    predicate = token_filter(resolution)
    assert predicate["field"] == token_column()
    assert value_token("category", "IMAGES") in predicate["conds"]
    # The same filter expressed with the singular alias still means images.
    alias = resolve_query(dictionary, meta_filters=[MetaFilter("category", ("IMAGES",), EXACT)])
    assert not alias.is_empty()


def test_category_is_exact_only_and_never_fuzzed():
    assert rule_for("category").allows(EXACT) is True
    for op in (FUZZY, "contains", "prefix"):
        assert rule_for("category").allows(op) is False, op


def test_category_filter_expression_is_a_plain_column_equality():
    expression = canonical_filter("category", ["images"])
    assert expression.field == "meta__category"
    assert expression.value == "IMAGES"


def test_derived_values_survive_json_round_trip():
    fields = {"uri": f"{ROOT}/a.md", "account_id": "acct"}
    stamp_record_meta(fields, category=TEXT_CATEGORY, contents=(CHAPTER,))
    meta = build_meta(
        fields["_business_meta_input"]["values"],
        document_id="doc",
        content_revision="rev",
        source=fields["_business_meta_input"]["source"],
    )
    columns = project_meta(meta.to_payload())
    payload = json.loads(columns["business_meta_payload"])
    values = {entry["field"]: entry["raw"] for entry in payload["values"]}
    assert values["chapter_uri"] == "kb://chapters/843907453002281"
    assert values["page"] == "p.638-641"
    assert values["category"] == "text"


def payload_column():
    from openviking.indexing.metadata_integration import PAYLOAD_COLUMN

    return PAYLOAD_COLUMN


def test_a_stored_row_reads_back_as_meta_and_provenance():
    """The columns a record was written with are what a hit answers with.

    Filtering proves the tokens exist; an agent reading a hit still needs the
    values themselves, and in the spelling the corpus used rather than the
    upper-cased form the equality index stores.
    """

    from openviking.indexing.record_meta import meta_view, provenance_view

    chapter = f"{ROOT}/10.2/10.2.md"
    fields = {"uri": f"{ROOT}/10.2/images/844775934052881.jpg", "account_id": "acct"}
    stamp_record_meta(
        fields,
        category=IMAGE_CATEGORY,
        contents=(CHAPTER,),
        source_uri=chapter,
        image_uri=fields["uri"],
        values={"机型": "SY215-C"},
    )
    stored = _tokens_for(fields, "IMAGES")[payload_column()]

    meta = meta_view(stored)
    assert meta["category"] == IMAGE_CATEGORY
    assert meta["machine_model"] == "SY215-C"
    assert meta["page"] == "p.638-641"
    assert meta["source_uri"] == chapter
    assert meta["image_uri"] == fields["uri"]

    row = {
        "uri": fields["uri"],
        "chunk_id": "image:1",
        "content_revision": "rev",
        "image_context_uri": chapter,
        payload_column(): stored,
    }
    provenance = provenance_view(row)
    # The figure cites the chapter that embeds it, and the revision it was read at.
    assert provenance["chapter_uri"] == chapter
    assert provenance["source_uri"] == chapter
    assert provenance["image_uri"] == fields["uri"]
    assert provenance["content_revision"] == "rev"
    assert provenance["category"] == IMAGE_CATEGORY

    # Nothing is invented for a row that declares nothing.
    assert meta_view(None) == {}
    assert meta_view("{ not json") == {}
    assert meta_view({"values": [{"field": "page"}]}) == {}
    assert provenance_view({"uri": f"{ROOT}/x.md"}) == {"source_uri": f"{ROOT}/x.md"}


def test_a_hit_carries_its_meta_and_provenance_to_the_client():
    from openviking_cli.retrieve.types import ContextType, FindResult, MatchedContext

    chapter = f"{ROOT}/10.2/10.2.md"
    hit = MatchedContext(
        uri=chapter,
        context_type=ContextType.RESOURCE,
        abstract="章",
        type="text",
        meta={"category": TEXT_CATEGORY, "page": "p.638-641"},
        provenance={"source_uri": chapter, "content_revision": "rev"},
    )
    rendered = FindResult(memories=[], resources=[hit], skills=[]).to_dict()["resources"][0]
    assert rendered["meta"] == {"category": TEXT_CATEGORY, "page": "p.638-641"}
    assert rendered["provenance"] == {"source_uri": chapter, "content_revision": "rev"}

    bare = FindResult(
        memories=[],
        resources=[MatchedContext(uri=chapter, context_type=ContextType.RESOURCE)],
        skills=[],
    ).to_dict()["resources"][0]
    assert "meta" not in bare and "provenance" not in bare
