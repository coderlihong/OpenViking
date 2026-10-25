# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""The ``(account, uri)`` lookups must not scan an account's whole ``desired`` set.

``desired`` is keyed ``(account, namespace, uri)``. Three callers legitimately do
not know the namespace and ask by ``(account, uri)`` only:

* ``declared_image_plan``  -- once per image on the reindex vector path,
* ``published_chunks``     -- once per file on the same path,
* the cross-namespace overlap guard in ``begin``.

With only the primary key those queries use the ``account`` prefix and then
compare ``uri`` row by row. On the live e2etest account that is 198,179 index
entries per call, measured at 11.9-16.0ms. ``declared_image_plan`` alone ran
102,828 times for the menus corpus -- roughly twenty minutes of synchronous
SQLite on the asyncio event loop, which is what froze the server: ``sample``
showed the main thread inside ``sqlite3BtreeNext`` and the write lock was never
held (an outside ``BEGIN IMMEDIATE`` probe recorded ``blocked=0`` for 30
minutes).

These tests pin the index and the query plans, not the timings, so they stay
fast and deterministic.
"""

from __future__ import annotations

import sqlite3

import pytest

from openviking.indexing.publication import PublicationStore


@pytest.fixture()
def store(tmp_path):
    return PublicationStore(str(tmp_path / "registry.db"))


def _plan(store: PublicationStore, sql: str, params=()) -> str:
    """Flatten EXPLAIN QUERY PLAN output into one searchable string."""
    with store.db(readonly=True) as db:
        rows = db.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return " | ".join(str(row[3]) for row in rows)


def test_the_account_uri_index_exists(store):
    """A fresh store creates the index that answers namespace-less lookups."""
    with store.db(readonly=True) as db:
        names = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    assert "desired_account_uri" in names


def test_declared_image_plan_uses_the_index(store):
    """The per-image lookup searches the index instead of scanning the account."""
    plan = _plan(
        store,
        "SELECT b.expected FROM desired d JOIN builds b ON b.generation=d.generation "
        "WHERE d.account=? AND d.uri=?",
        ("acct", "viking://resources/a.jpg"),
    )
    assert "desired_account_uri" in plan, plan
    # A SCAN of ``desired`` is the regression this index exists to prevent.
    assert "SCAN d" not in plan and "SCAN desired" not in plan, plan


def test_published_chunks_uses_the_index(store):
    plan = _plan(
        store,
        "SELECT generation FROM desired WHERE account=? AND uri=? LIMIT 1",
        ("acct", "viking://resources/a.md"),
    )
    assert "desired_account_uri" in plan, plan
    assert "SCAN desired" not in plan, plan


def test_overlap_guard_uses_the_index(store):
    """``begin``'s cross-namespace guard asks the same namespace-less question."""
    plan = _plan(
        store,
        "SELECT 1 FROM desired WHERE account=? AND uri=? AND namespace!=?",
        ("acct", "viking://resources/a.md", "manuals"),
    )
    assert "desired_account_uri" in plan, plan
    assert "SCAN desired" not in plan, plan


def test_namespaced_lookups_still_use_the_primary_key(store):
    """The index must not displace the primary key on queries that know the namespace."""
    plan = _plan(
        store,
        "SELECT generation FROM desired WHERE account=? AND namespace=? AND uri=?",
        ("acct", "manuals", "viking://resources/a.md"),
    )
    assert "SCAN desired" not in plan, plan


def test_existing_databases_get_the_index_on_startup(tmp_path):
    """``CREATE TABLE IF NOT EXISTS`` never retrofits, so a legacy store needs the DDL.

    The live 1935 database was built before this index existed; it must gain it
    on the next start rather than needing a manual migration.
    """
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE desired (
          account TEXT, namespace TEXT, uri TEXT, generation TEXT,
          PRIMARY KEY(account,namespace,uri));
        CREATE INDEX desired_generation ON desired(generation);
        INSERT INTO desired VALUES ('acct','manuals','viking://resources/a.md','g1');
        """
    )
    legacy.commit()
    legacy.close()

    # Opening the store runs the schema block, which must add the missing index.
    store = PublicationStore(str(path))
    with store.db(readonly=True) as db:
        names = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        kept = db.execute("SELECT generation FROM desired").fetchone()[0]
    assert "desired_account_uri" in names
    assert kept == "g1"  # the retrofit must not touch data


def test_declared_image_plan_still_answers_correctly(store):
    """The index is an optimisation: the method must keep returning the same answer."""
    uri = "viking://resources/raw/m/images/a.jpg"
    generation = store.begin(
        "acct", "resources", uri, "p", ["image:asset", "image:caption"]
    )
    store.acknowledge(generation, "image:asset")
    store.acknowledge(generation, "image:caption")
    store.seal(generation)

    assert store.declared_image_plan("acct", uri) is True
    assert store.declared_image_plan("acct", "viking://resources/raw/m/images/b.jpg") is False


def test_declared_image_plan_is_false_for_a_plain_text_plan(store):
    uri = "viking://resources/raw/m/chapter.md"
    generation = store.begin("acct", "resources", uri, "p", ["c1"])
    store.acknowledge(generation, "c1")
    store.seal(generation)

    assert store.declared_image_plan("acct", uri) is False
