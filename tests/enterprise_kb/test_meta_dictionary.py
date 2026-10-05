# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""KB-019: fuzzy dictionary, alias resolution, filter/hint separation, reporting."""

import pytest

from openviking.indexing.business_meta import (
    MISSING_META_SENTINEL,
    build_meta,
)
from openviking.retrieve.meta_dictionary import (
    CONTAINS,
    EXACT,
    FUZZY,
    PREFIX,
    BusinessDictionary,
    MetaDictionaryError,
    MetaFilter,
    MetaHint,
    MetaOpNotAllowedError,
    ProtectedFieldFuzzyError,
    bounded_edit_distance,
    parse_model_expressions,
    resolve_model_query,
    resolve_query,
)
from openviking.storage.expr import And, Eq, Or


def _dictionary() -> BusinessDictionary:
    dictionary = BusinessDictionary("excavator-manuals")
    dictionary.add_value("modelid", "SY215-C", aliases=["SY215C", "215C", "SY215"])
    dictionary.add_value("modelid", "SY75-C", aliases=["SY75C"])
    dictionary.add_value("subject_code", "SY021NCB62788")
    dictionary.add_value("subject_code", "SY021NCB62789")
    dictionary.add_value("bom", "BOM-1001")
    dictionary.add_value("dtc", "P0301")
    dictionary.rebuild_indexes()
    return dictionary


# -- dictionary basics -----------------------------------------------------


def test_exact_lookup_normalizes_case_and_whitespace():
    dictionary = _dictionary()
    result = dictionary.lookup("modelid", "  sy215-c ", EXACT)
    assert result.canonical == ("SY215-C",)
    assert result.matched_via == EXACT


def test_alias_lookup_maps_to_canonical_id():
    dictionary = _dictionary()
    result = dictionary.lookup("modelid", "215C", EXACT)
    assert result.canonical == ("SY215-C",)


def test_prefix_lookup_is_bounded_and_canonical():
    dictionary = _dictionary()
    result = dictionary.lookup("modelid", "SY", PREFIX)
    assert set(result.canonical) == {"SY215-C", "SY75-C"}
    assert result.scan_volume > 0


def test_contains_and_trigram_lookup():
    dictionary = _dictionary()
    result = dictionary.lookup("modelid", "215", CONTAINS)
    assert result.canonical == ("SY215-C",)


def test_fuzzy_lookup_uses_bounded_edit_distance():
    dictionary = _dictionary()
    # "SY215-C" with a typo; distance 1 should still resolve.
    result = dictionary.lookup("modelid", "SY251-C", FUZZY)
    assert "SY215-C" in result.canonical


def test_bounded_edit_distance_caps_result():
    assert bounded_edit_distance("SY215-C", "SY215-C", 2) == 0
    assert bounded_edit_distance("SY215-C", "SY215C", 2) == 1
    assert bounded_edit_distance("abc", "xyz", 1) == 2  # max+1 sentinel


def test_ambiguous_alias_registration_is_rejected():
    dictionary = BusinessDictionary("ns")
    dictionary.add_value("modelid", "SY215-C", aliases=["215"])
    with pytest.raises(MetaDictionaryError):
        dictionary.add_value("modelid", "SY75-C", aliases=["215"])


def test_dictionary_seeded_from_authoritative_meta():
    meta = build_meta(
        {"modelid": "SY215-C", "subject_code": "SY021NCB62788"},
        document_id="doc-1",
        content_revision="rev-1",
    )
    dictionary = BusinessDictionary("ns")
    dictionary.add_meta(meta)
    dictionary.rebuild_indexes()
    assert dictionary.total_values() == 2
    assert dictionary.known_documents() == ("doc-1",)
    assert dictionary.lookup("modelid", "SY215-C", EXACT).canonical == ("SY215-C",)


# -- strict fields: single-character difference is a DIFFERENT configuration --


def test_subject_code_one_char_difference_does_not_collapse_two_configs():
    dictionary = _dictionary()
    result = dictionary.lookup("subject_code", "SY021NCB62789", EXACT)
    assert result.canonical == ("SY021NCB62789",)
    # Even fuzzy must not merge them: strict fields refuse fuzzing entirely.
    with pytest.raises(MetaOpNotAllowedError):
        dictionary.lookup("subject_code", "SY021NCB62789", FUZZY)
    with pytest.raises(MetaOpNotAllowedError):
        dictionary.lookup("bom", "BOM-1002", CONTAINS)


def test_strict_field_typo_returns_no_candidates_not_a_neighbour():
    dictionary = _dictionary()
    result = dictionary.lookup("subject_code", "SY021NCB62790", EXACT)
    assert result.canonical == ()


def test_acl_namespace_applicability_are_never_fuzzy_interpreted():
    dictionary = _dictionary()
    for field in ("namespace_id", "account_id", "applicability"):
        with pytest.raises(ProtectedFieldFuzzyError):
            dictionary.lookup(field, "anything", FUZZY)


# -- meta_filters: hard constraints ----------------------------------------


def test_same_field_candidates_or_cross_field_and():
    dictionary = _dictionary()
    resolution = resolve_query(
        dictionary,
        meta_filters=[
            MetaFilter("modelid", ("SY215-C", "SY75-C"), EXACT),
            MetaFilter("subject_code", ("SY021NCB62788",), EXACT),
        ],
    )
    assert not resolution.is_empty()
    assert isinstance(resolution.hard_filter, And)
    assert resolution.hard_fields["modelid"] == ("SY215-C", "SY75-C")
    assert resolution.hard_fields["subject_code"] == ("SY021NCB62788",)


def test_single_field_filter_is_or_not_and():
    dictionary = _dictionary()
    resolution = resolve_query(
        dictionary, meta_filters=[MetaFilter("modelid", ("SY215-C", "SY75-C"), EXACT)]
    )
    assert isinstance(resolution.hard_filter, Or)
    assert {c.value for c in resolution.hard_filter.conds} == {"SY215-C", "SY75-C"}


def test_explicit_filter_with_no_match_does_not_silently_relax():
    dictionary = _dictionary()
    resolution = resolve_query(
        dictionary, meta_filters=[MetaFilter("modelid", ("NO-SUCH-MODEL",), EXACT)]
    )
    assert resolution.is_empty()
    assert resolution.hard_filter is None
    assert resolution.unsatisfied == ("modelid=NO-SUCH-MODEL",)
    assert resolution.explanations


def test_same_field_unknown_alternative_preserves_or_semantics():
    dictionary = _dictionary()
    resolution = resolve_query(
        dictionary,
        meta_filters=[MetaFilter("modelid", ("SY215-C", "GHOST-MODEL"), EXACT)],
    )
    # Same-field alternatives are OR. An unknown alternative is false while
    # the known alternative remains a valid explicit constraint.
    assert not resolution.is_empty()
    assert resolution.hard_fields == {"modelid": ("SY215-C",)}
    assert resolution.explanations


def test_include_unknown_adds_the_missing_sentinel():
    dictionary = _dictionary()
    resolution = resolve_query(
        dictionary,
        meta_filters=[MetaFilter("modelid", ("SY215-C",), EXACT, include_unknown=True)],
    )
    assert "modelid" in resolution.include_unknown_fields
    assert isinstance(resolution.hard_filter, Or)
    assert {c.value for c in resolution.hard_filter.conds} == {
        "SY215-C",
        MISSING_META_SENTINEL,
    }


def test_unknown_meta_not_matched_without_include_unknown():
    dictionary = _dictionary()
    resolution = resolve_query(
        dictionary, meta_filters=[MetaFilter("modelid", ("SY215-C",), EXACT)]
    )
    assert resolution.include_unknown_fields == ()
    assert isinstance(resolution.hard_filter, Eq)
    assert resolution.hard_filter.value == "SY215-C"
    assert resolution.hard_filter.value != MISSING_META_SENTINEL


def test_filter_operator_not_allowed_on_strict_field():
    dictionary = _dictionary()
    with pytest.raises(MetaOpNotAllowedError):
        resolve_query(
            dictionary, meta_filters=[MetaFilter("bom", ("BOM-100",), CONTAINS)]
        )


# -- meta_hints: boost by default ------------------------------------------


def test_unique_hint_is_boost_not_tighten_by_default():
    dictionary = _dictionary()
    resolution = resolve_query(
        dictionary, meta_hints=[MetaHint("modelid", "215C", FUZZY)]
    )
    assert resolution.boosts["modelid"] == ("SY215-C",)
    assert resolution.tightened == {}
    assert resolution.hard_filter is None
    assert any("boost" in note for note in resolution.explanations)


def test_low_confidence_hint_returns_candidates_and_explanation():
    dictionary = _dictionary()
    resolution = resolve_query(
        dictionary, meta_hints=[MetaHint("modelid", "SY", PREFIX)]
    )
    assert resolution.tightened == {}
    assert set(resolution.boosts["modelid"]) == {"SY215-C", "SY75-C"}
    assert resolution.ambiguous is True
    assert any("ambiguous" in note for note in resolution.explanations)


def test_high_confidence_unique_mapping_may_tighten_when_enabled():
    dictionary = _dictionary()
    resolution = resolve_query(
        dictionary,
        meta_hints=[MetaHint("modelid", "215C", FUZZY)],
        tighten_hints=True,
    )
    assert resolution.tightened["modelid"] == ("SY215-C",)
    assert resolution.boosts == {}


def test_ambiguous_hint_never_tightens_even_when_enabled():
    dictionary = _dictionary()
    resolution = resolve_query(
        dictionary,
        meta_hints=[MetaHint("modelid", "SY", PREFIX)],
        tighten_hints=True,
    )
    assert resolution.tightened == {}
    assert resolution.ambiguous is True


def test_hint_that_matches_nothing_is_ignored_with_explanation():
    dictionary = _dictionary()
    resolution = resolve_query(
        dictionary, meta_hints=[MetaHint("modelid", "ZZ999", EXACT)]
    )
    assert resolution.boosts == {}
    assert resolution.is_empty() is False
    assert any("matched no dictionary value" in note for note in resolution.explanations)


# -- model expression parsing ----------------------------------------------


def test_parse_model_expressions_extracts_model_tokens_only():
    assert parse_model_expressions("How to service SY215-C excavator?") == ("SY215-C",)
    # A subject code is longer than the model shape and is not a model token.
    assert parse_model_expressions("subject SY021NCB62788") == ()
    assert parse_model_expressions("") == ()


def test_resolve_model_query_parses_and_boosts():
    dictionary = _dictionary()
    resolution = resolve_model_query(dictionary, "repair manual for SY215C")
    assert resolution.boosts.get("modelid") == ("SY215-C",)


# -- performance reporting (TC-7.7 hook; NOT a benchmark) -------------------


def test_report_exposes_cardinality_inflation_and_scan_volume():
    dictionary = _dictionary()
    report = dictionary.report(
        documents=3, chunks=120, candidates=40, matched=10, scan_volume=55, accuracy=0.9
    )
    payload = report.to_dict()
    assert payload["namespace_id"] == "excavator-manuals"
    assert payload["dictionary_cardinality"]["modelid"] == 2
    assert payload["total_values"] == 6
    assert payload["documents"] == 3
    assert payload["chunks"] == 120
    assert payload["candidate_inflation"] == pytest.approx(4.0)
    assert payload["scan_volume"] == 55
    assert payload["accuracy"] == 0.9


def test_resolution_reports_scan_volume():
    dictionary = _dictionary()
    resolution = resolve_query(
        dictionary, meta_filters=[MetaFilter("modelid", ("SY215-C",), EXACT)]
    )
    assert resolution.scan_volume >= 1
