# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""The lexical write path must not full-scan ``publication_lexical``.

``lexical_write`` runs inside ``BEGIN IMMEDIATE`` -- the single publication
writer. When the account's mirror marker is still missing it seeds
``publication_lexical_index`` with a row count, and that count used to be

    SELECT count(*) FROM publication_lexical
     WHERE json_extract(payload,'$.account_id') = ?

``json_extract`` over a column with no index cannot be planned into anything but
``SCAN publication_lexical``: on the live store that is 258k rows and 1.66GiB of
JSON payload, measured **3.64s per call while holding the write lock**. Every
other writer queued behind it, which is the convoy that was repeatedly
mis-attributed to "the SQLite single writer being slow".

``publication_lexical_scope`` carries the same rows keyed by account and is
written in the same transaction, so counting it is an index range scan
(measured 21.3ms, identical answer).

These tests pin the query plan, not a wall-clock number.
"""

from __future__ import annotations

import pytest

from openviking.indexing.publication import PublicationStore

ACCOUNT = "acct-lex"
NAMESPACE = "ns-lex"


@pytest.fixture()
def store(tmp_path):
    return PublicationStore(str(tmp_path / "registry.db"))


def _write_one(store: PublicationStore, uri: str, chunk_id: str, text: str) -> str:
    generation = store.begin(ACCOUNT, NAMESPACE, uri, "plan", [chunk_id, "lexical:plan"])
    store.lexical_write(
        generation,
        [{"uri": uri, "chunk_id": chunk_id, "chunk_text": text, "content_revision": "rev"}],
    )
    store.acknowledge(generation, chunk_id)
    return generation


def test_the_seeded_row_count_does_not_scan_the_postings_table(store):
    """Seeding the marker must count through the scope mirror, not a JSON scan."""
    _write_one(store, "viking://resources/a.md", "c1", "hydraulic pump pressure")

    with store.db(readonly=True) as db:
        plan = " | ".join(
            str(row[3])
            for row in db.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT count(*) FROM publication_lexical_scope WHERE account=?",
                (ACCOUNT,),
            )
        )
        # The statement the code actually runs must be this shape.
        assert "SCAN publication_lexical" not in plan
        assert "publication_lexical_scope" in plan

        # And the old shape really is the full scan this fix removes -- that is
        # why the query had to change rather than gain an index.
        old = " | ".join(
            str(row[3])
            for row in db.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT count(*) FROM publication_lexical "
                "WHERE json_extract(payload,'$.account_id')=?",
                (ACCOUNT,),
            )
        )
        assert "SCAN publication_lexical" in old


def test_scope_count_matches_the_postings_count(store):
    """The replacement must return exactly what the JSON scan returned."""
    _write_one(store, "viking://resources/a.md", "c1", "first chapter about valves")
    _write_one(store, "viking://resources/b.md", "c2", "second chapter about pumps")

    with store.db(readonly=True) as db:
        scoped = db.execute(
            "SELECT count(*) FROM publication_lexical_scope WHERE account=?", (ACCOUNT,)
        ).fetchone()[0]
        postings = db.execute(
            "SELECT count(*) FROM publication_lexical "
            "WHERE json_extract(payload,'$.account_id')=?",
            (ACCOUNT,),
        ).fetchone()[0]
    assert scoped == postings
    assert scoped == 2


def test_marker_is_seeded_with_the_scope_count(store):
    """After the first lexical write the marker records the mirrored row count."""
    _write_one(store, "viking://resources/a.md", "c1", "alpha beta gamma")

    with store.db(readonly=True) as db:
        row = db.execute(
            "SELECT rows FROM publication_lexical_index WHERE account=?", (ACCOUNT,)
        ).fetchone()
    assert row is not None
    assert row[0] == 1


def test_a_second_write_does_not_recount(store):
    """The marker exists after the first write, so later writes only increment."""
    _write_one(store, "viking://resources/a.md", "c1", "alpha")
    _write_one(store, "viking://resources/b.md", "c2", "beta")

    with store.db(readonly=True) as db:
        row = db.execute(
            "SELECT rows FROM publication_lexical_index WHERE account=?", (ACCOUNT,)
        ).fetchone()
    assert row[0] == 2
