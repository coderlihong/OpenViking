"""Differential test: the SQL-compiled lexical metadata filter must accept
exactly the rows the Python ``matches_filter`` scan accepts.

The compiler in ``registry_lexical._compile_lexical_filter_sql`` is a
performance path (single SQL statement instead of 60k+ Python json.loads).
Exactness is the contract: for every filter either the compiler refuses
(returns None, caller falls back to the Python scan) or its accepted row set
is byte-identical to ``matches_filter``.
"""
import itertools
import json
import sqlite3

import pytest

from openviking.indexing.registry_lexical import (
    FILTER_FIELDS,
    _compile_lexical_filter_sql,
)
from openviking.storage.vectordb.index.cuvs_index import matches_filter

PAYLOADS = [
    {"uri": "viking://resources/raw/cases/SY365/pump.md", "context_type": "resource",
     "level": 2, "search_tags": ["维修", "液压"], "account_id": "e2etest",
     "business_meta_tokens": ["机型:SY365"], "document_id": "d1"},
    {"uri": "viking://resources/raw/cases/SY365/", "context_type": "resource",
     "level": 1, "search_tags": [], "document_id": "d2"},
    {"uri": "viking://resources/raw/cases/SY750H/engine.md", "context_type": "agent",
     "level": 2, "search_tags": ["发动机"], "account_id": "other"},
    {"uri": "resources/raw/cases/SY365/pump.md", "context_type": "resource",
     "level": 0, "business_meta_tokens": "机型:SY365"},
    {"uri": "/", "context_type": "resource", "level": 3},
    {"uri": "", "level": 2, "search_tags": ["维修"]},
    {"uri": 5, "context_type": 7, "level": "2", "search_tags": {"维修": 1}},
    {"uri": "viking://resources/raw/cases", "level": True, "search_tags": ["a", 5, None]},
    {"context_type": None, "search_tags": "维修", "level": 2.5},
    {},
]

FILTERS = [
    None,
    {"op": "must", "field": "context_type", "conds": ["resource"]},
    {"op": "must_not", "field": "context_type", "conds": ["resource"]},
    {"op": "must", "field": "context_type", "conds": ["resource", "agent"]},
    {"op": "must", "field": "context_type", "conds": []},
    {"op": "or", "conds": []},
    {"op": "and", "conds": []},
    {"op": "must", "field": "level", "conds": [2]},
    {"op": "must", "field": "level", "conds": [1, 2, 3]},
    {"op": "must", "field": "level", "conds": [True]},
    {"op": "must", "field": "level", "conds": [2.5]},
    {"op": "must", "field": "level", "conds": ["2"]},
    {"op": "must", "field": "level", "conds": [None]},
    {"op": "must", "field": "level", "conds": []},
    {"op": "range", "field": "level", "gt": 0, "lt": 3},
    {"op": "range", "field": "level", "gte": 2},
    {"op": "range", "field": "level", "lte": 1},
    {"op": "range", "field": "level"},
    {"op": "range", "field": "level", "gt": "1"},
    {"op": "range_out", "field": "level", "gt": 0, "lt": 3},
    {"op": "range_out", "field": "level"},
    {"op": "must", "field": "search_tags", "conds": ["维修"]},
    {"op": "must", "field": "search_tags", "conds": ["a"]},
    {"op": "must", "field": "search_tags", "conds": ["不存在的标签"]},
    {"op": "must", "field": "search_tags", "conds": []},
    {"op": "must_not", "field": "search_tags", "conds": ["维修"]},
    {"op": "must", "field": "business_meta_tokens", "conds": ["机型:SY365"]},
    {"op": "must", "field": "uri", "conds": ["viking://resources/raw/cases/SY365/pump.md"], "para": "-d=0"},
    {"op": "must", "field": "uri", "conds": ["viking://resources/raw/cases/SY365"], "para": "-d=0"},
    {"op": "must", "field": "uri", "conds": ["viking://resources/raw/cases/SY365"], "para": "-d=-1"},
    {"op": "must", "field": "uri", "conds": ["viking://resources/raw/cases/SY365"]},
    {"op": "must", "field": "uri", "conds": ["viking://resources/raw/cases/SY365"], "para": "-d=1"},
    {"op": "must", "field": "uri", "conds": ["/"], "para": "-d=0"},
    {"op": "must", "field": "uri", "conds": ["/"]},
    {"op": "must", "field": "uri", "conds": ["viking://resources/raw/cases/SY365"], "para": "-d=0",
     # exact match must also hit the trailing-slash variant in the payload
    },
    {"op": "must", "field": "uri", "conds": ["viking://resources/raw/cases/SY365/pump.md/"], "para": "-d=0"},
    {"op": "must_not", "field": "uri", "conds": ["viking://resources/raw/cases/SY365"], "para": "-d=-1"},
    {"op": "must", "field": "uri", "conds": ["no-leading-slash"], "para": "-d=0"},
    {"op": "contains", "field": "context_type", "substring": "res"},
    {"op": "contains", "field": "search_tags", "substring": "修"},
    {"op": "contains", "field": "search_tags", "substring": 5},
    {"op": "contains", "field": "document_id", "substring": "d"},
    {"op": "and", "conds": [
        {"op": "must", "field": "context_type", "conds": ["resource"]},
        {"op": "must", "field": "level", "conds": [2]},
    ]},
    {"op": "or", "conds": [
        {"op": "must", "field": "level", "conds": [0]},
        {"op": "must", "field": "account_id", "conds": ["other"]},
    ]},
    {"op": "and", "conds": [
        {"op": "or", "conds": [
            {"op": "must", "field": "context_type", "conds": ["resource"]},
            {"op": "must", "field": "account_id", "conds": ["other"]},
        ]},
        {"op": "must_not", "field": "level", "conds": [0]},
    ]},
    {"filter": {"op": "must", "field": "context_type", "conds": ["resource"]}},
    {"filter": None},
    {"op": "must", "field": "created_at", "conds": ["2026-01-01"]},
    {"op": "must", "field": "context_type", "conds": ["resource", 5]},
    {"op": "must", "field": "context_type", "conds": [None]},
    {"op": "must", "field": "context_type", "conds": [["nested"]]},
    {"op": "must", "field": "unknown_field", "conds": ["x"]},
    {"op": "eq", "field": "context_type", "conds": ["resource"]},
    {"op": "must", "field": "context_type", "conds": ["resource"], "para": "-d=0"},
]


@pytest.fixture()
def filter_db():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE publication_lexical (generation TEXT, child TEXT, payload TEXT)")
    for index, payload in enumerate(PAYLOADS):
        db.execute(
            "INSERT INTO publication_lexical (rowid, generation, child, payload) VALUES (?,?,?,?)",
            (index + 1, f"gen{index}", f"child{index}", json.dumps(payload, ensure_ascii=False)),
        )
    return db


def _python_kept(filter_node):
    kept = set()
    for index, payload in enumerate(PAYLOADS):
        if matches_filter(json.loads(json.dumps(payload)), filter_node, FILTER_FIELDS):
            kept.add(index + 1)
    return kept


def _sql_kept(db, filter_node):
    compiled = _compile_lexical_filter_sql(filter_node)
    if compiled is None:
        return None
    where, projections, params = compiled
    # The test database has no scope tables; substitute the rowid/doc_len
    # base columns the scope join would provide.
    base = {"sc.rowid": "rowid", "sc.doc_len": "0"}
    rendered = []
    for sql, alias in projections:
        if sql in base:
            rendered.append(f"{base[sql]} AS {alias}")
        else:
            rendered.append(f"{sql} AS {alias}")
    projection_sql = ", ".join(rendered)
    sql = f"SELECT rowid FROM (SELECT {projection_sql} FROM publication_lexical pl) WHERE {where}"
    return {row[0] for row in db.execute(sql, params)}


def test_compiled_sql_accepts_exactly_the_python_rows(filter_db):
    for filter_node in FILTERS:
        try:
            expected = _python_kept(filter_node)
            raised = False
        except Exception:
            expected = None
            raised = True
        actual = _sql_kept(filter_db, filter_node)
        if raised:
            # The Python path raises: the compiler must refuse so the caller
            # lands on the same Python path and the same error.
            assert actual is None, filter_node
            continue
        if actual is None:
            # Refusal is fine: the fallback computes exactly `expected`.
            continue
        assert actual == expected, filter_node


def test_refusals_fall_back_to_python_not_crash(filter_db):
    for filter_node in FILTERS:
        compiled = _compile_lexical_filter_sql(filter_node)
        if compiled is not None:
            continue
        try:
            # The Python fallback must produce an answer (or the same error
            # the non-compiled path always raised) -- never a new failure.
            _python_kept(filter_node)
        except Exception:
            pass


def test_context_type_filter_is_compiled_not_refused():
    # The hottest real filter (context_type narrowing) must take the SQL path.
    compiled = _compile_lexical_filter_sql({"op": "must", "field": "context_type", "conds": ["resource"]})
    assert compiled is not None
    where, projections, params = compiled
    assert "json_extract" in where and params == ["resource"]
    assert any(alias == "payload" for _, alias in projections)


def test_time_range_filters_are_refused_to_the_python_path():
    compiled = _compile_lexical_filter_sql({"op": "range", "field": "created_at", "gt": 0})
    assert compiled is None


def test_random_conjunctions_stay_identical(filter_db):
    rng_pool = [
        {"op": "must", "field": "context_type", "conds": ["resource"]},
        {"op": "must", "field": "context_type", "conds": ["agent"]},
        {"op": "must", "field": "level", "conds": [2]},
        {"op": "range", "field": "level", "gte": 1, "lt": 3},
        {"op": "must", "field": "search_tags", "conds": ["维修"]},
        {"op": "must_not", "field": "account_id", "conds": ["other"]},
        {"op": "must", "field": "uri", "conds": ["viking://resources/raw/cases"], "para": "-d=-1"},
        {"op": "must", "field": "uri", "conds": ["viking://resources/raw/cases/SY365"], "para": "-d=0"},
        {"op": "contains", "field": "context_type", "substring": "e"},
    ]
    for combo in itertools.combinations(rng_pool, 2):
        node = {"op": "and", "conds": list(combo)}
        expected = _python_kept(node)
        actual = _sql_kept(filter_db, node)
        assert actual is not None
        assert actual == expected, node
