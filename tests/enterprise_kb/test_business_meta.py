# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""KB-018: authoritative business meta storage, inheritance and system protection."""

import pytest

from openviking.indexing.business_meta import (
    CANONICAL_PREFIX,
    FIELDS_COLUMN,
    MISSING_META_SENTINEL,
    NORMALIZER_VERSION,
    RAW_PREFIX,
    SOURCE_PREFIX,
    NORMALIZER_PREFIX,
    AuthoritativeMeta,
    MetaBindingError,
    MetaLimitError,
    MetaLimits,
    MetaStore,
    SystemFieldOverwriteError,
    attach_meta,
    build_meta,
    canonical_filter,
    combined_filter,
    merge_meta,
)
from openviking.storage.expr import And, Eq, Or


def _sample(index_generation=None):
    return build_meta(
        {"modelid": "SY215-C", "subject_code": "SY021NCB62788"},
        document_id="doc-1",
        content_revision="rev-1",
        index_generation=index_generation,
    )


def test_raw_canonical_source_and_normalizer_are_stored_separately():
    meta = build_meta(
        {"modelid": "  sy215-c  "},
        document_id="doc-1",
        content_revision="rev-1",
        source="upload",
    )
    value = meta.values["modelid"]
    assert value.raw == "  sy215-c  "
    assert value.canonical == "SY215-C"
    assert value.source == "upload"
    assert value.normalizer_version == NORMALIZER_VERSION
    assert meta.raw("modelid") == "  sy215-c  "
    assert meta.canonical("modelid") == "SY215-C"
    assert meta.provenance("modelid") == ("upload", NORMALIZER_VERSION)


def test_absent_meta_is_allowed_and_yields_empty_record():
    meta = build_meta(None, document_id="doc-1", content_revision="rev-1")
    assert meta.fields() == ()
    assert meta.canonical("modelid") == MISSING_META_SENTINEL
    assert meta.raw("modelid") is None


@pytest.mark.parametrize(
    "key",
    ["account_id", "Account_ID", "namespace_id", "document_id", "content_revision",
     "index_generation", "uri", "acl_mode", "vector"],
)
def test_system_field_overwrite_is_rejected(key):
    with pytest.raises(SystemFieldOverwriteError):
        build_meta({key: "evil"}, document_id="doc-1", content_revision="rev-1")


def test_reserved_prefixes_are_rejected():
    with pytest.raises(SystemFieldOverwriteError):
        build_meta({"meta_raw__modelid": "x"}, document_id="doc-1", content_revision="rev-1")


def test_extra_key_and_value_limits_are_enforced():
    limits = MetaLimits(max_keys=2, max_key_length=8, max_value_length=4)
    with pytest.raises(MetaLimitError):
        build_meta({"a": "1", "b": "2", "c": "3"}, document_id="d", content_revision="r",
                   limits=limits)
    with pytest.raises(MetaLimitError):
        build_meta({"waytoolongkey": "1"}, document_id="d", content_revision="r", limits=limits)
    with pytest.raises(MetaLimitError):
        build_meta({"modelid": "too-long"}, document_id="d", content_revision="r", limits=limits)


def test_unsupported_value_type_is_rejected():
    with pytest.raises(MetaLimitError):
        build_meta({"modelid": ["SY215-C"]}, document_id="d", content_revision="r")


def test_rebuild_without_meta_does_not_clear_existing_values():
    store = MetaStore()
    store.record(
        {"modelid": "SY215-C", "subject_code": "SY021NCB62788"},
        document_id="doc-1",
        content_revision="rev-1",
        index_generation="g1",
    )
    rebuilt = store.record(
        None,
        document_id="doc-1",
        content_revision="rev-2",
        index_generation="g2",
    )
    assert rebuilt.canonical("modelid") == "SY215-C"
    assert rebuilt.canonical("subject_code") == "SY021NCB62788"
    assert rebuilt.content_revision == "rev-2"
    assert rebuilt.index_generation == "g2"


def test_partial_rebuild_preserves_other_keys():
    existing = _sample()
    merged = merge_meta(
        existing,
        {"modelid": "SY75-C"},
        document_id="doc-1",
        content_revision="rev-2",
    )
    assert merged.canonical("modelid") == "SY75-C"
    assert merged.canonical("subject_code") == "SY021NCB62788"


def test_rebuild_cannot_override_system_fields():
    store = MetaStore()
    store.record({"modelid": "SY215-C"}, document_id="doc-1", content_revision="rev-1")
    with pytest.raises(SystemFieldOverwriteError):
        store.record(
            {"account_id": "other"},
            document_id="doc-1",
            content_revision="rev-2",
        )


def test_chunk_image_and_semantic_records_inherit_authoritative_meta():
    meta = _sample(index_generation="g7")
    columns = meta.to_columns()
    assert columns[CANONICAL_PREFIX + "modelid"] == "SY215-C"
    assert columns[RAW_PREFIX + "modelid"] == "SY215-C"
    assert columns[SOURCE_PREFIX + "modelid"] == "ingest"
    assert columns[NORMALIZER_PREFIX + "modelid"] == NORMALIZER_VERSION
    assert columns[FIELDS_COLUMN] == ["modelid", "subject_code"]
    assert columns["index_generation"] == "g7"

    for record in (
        {"chunk_id": "c1", "document_id": "doc-1", "content_revision": "rev-1"},
        {"image_context_uri": "viking://x.png", "document_id": "doc-1",
         "content_revision": "rev-1"},
        {"level": 0, "document_id": "doc-1", "content_revision": "rev-1"},
    ):
        inherited = attach_meta(record, meta)
        assert inherited[CANONICAL_PREFIX + "modelid"] == "SY215-C"
        assert inherited[CANONICAL_PREFIX + "subject_code"] == "SY021NCB62788"


def test_inheritance_rejects_mismatched_revision():
    meta = _sample()
    with pytest.raises(MetaBindingError):
        attach_meta(
            {"chunk_id": "c1", "document_id": "doc-1", "content_revision": "rev-9"}, meta
        )


def test_meta_change_is_bound_to_the_same_generation_as_content():
    store = MetaStore()
    store.record(
        {"modelid": "SY215-C"},
        document_id="doc-1",
        content_revision="rev-1",
        index_generation="g1",
    )
    updated = store.record(
        {"modelid": "SY75-C"},
        document_id="doc-1",
        content_revision="rev-2",
        index_generation="g2",
    )
    assert updated.index_generation == "g2"
    assert updated.content_revision == "rev-2"
    assert store.get("doc-1", "g1").canonical("modelid") == "SY215-C"
    assert store.get("doc-1", "g2").canonical("modelid") == "SY75-C"


def test_same_field_or_and_cross_field_and():
    same_field = canonical_filter("modelid", ["SY215-C", "SY75-C"])
    assert isinstance(same_field, Or)
    assert {cond.value for cond in same_field.conds} == {"SY215-C", "SY75-C"}

    single = canonical_filter("modelid", ["SY215-C"])
    assert isinstance(single, Eq)

    cross_field = combined_filter(
        {"modelid": ["SY215-C", "SY75-C"], "subject_code": ["SY021NCB62788"]}
    )
    assert isinstance(cross_field, And)
    assert isinstance(cross_field.conds[0], Or)
    assert isinstance(cross_field.conds[1], Eq)

    assert combined_filter({"modelid": []}) is None


def test_no_candidate_filter_is_unsatisfiable_never_a_wildcard():
    expr = canonical_filter("modelid", [])
    assert isinstance(expr, Eq)
    assert expr.value != MISSING_META_SENTINEL


def test_include_unknown_only_adds_the_missing_sentinel_when_requested():
    default = canonical_filter("modelid", ["SY215-C"])
    assert isinstance(default, Eq)

    with_unknown = canonical_filter("modelid", ["SY215-C"], include_unknown=True)
    assert isinstance(with_unknown, Or)
    assert {cond.value for cond in with_unknown.conds} == {"SY215-C", MISSING_META_SENTINEL}


def test_fingerprint_changes_when_raw_or_source_changes():
    base = _sample()
    changed = build_meta(
        {"modelid": "SY215-C", "subject_code": "SY021NCB62788"},
        document_id="doc-1",
        content_revision="rev-1",
        source="manual",
    )
    assert base.fingerprint() != changed.fingerprint()
    assert base.fingerprint() == _sample().fingerprint()


def test_payload_round_trip_preserves_provenance():
    meta = _sample()
    restored = AuthoritativeMeta.from_payload(meta.to_payload())
    assert restored.canonical("modelid") == meta.canonical("modelid")
    assert restored.provenance("modelid") == meta.provenance("modelid")
    assert restored.fingerprint() == meta.fingerprint()