# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Publication visibility is rechecked, not shipped as a filter clause per URI.

``PublicationStore.visibility`` builds one filter clause per published URI. That
DSL has ~175k nodes for the enterprise corpus, and handing it to the vector
engine per query made a tree-wide ``find`` take minutes while a narrow scope
still took seconds. The engine filter now carries only the coarse scope and
``recheck_visibility`` decides what may be returned, which is exact for every
row that leaves the backend.
"""

from types import SimpleNamespace

from openviking.indexing.publication import recheck_visibility, tombstoned_uri


def _ctx(account: str = "acct"):
    return SimpleNamespace(account_id=account)


def test_stale_generation_row_is_dropped():
    snapshot = ({"viking://resources/a.md": {"generation": "g2"}}, set())
    rows = [
        {"uri": "viking://resources/a.md", "index_generation": "g1"},
        {"uri": "viking://resources/a.md", "index_generation": "g2"},
    ]

    kept = recheck_visibility(_ctx(), rows, snapshot)

    assert [row["index_generation"] for row in kept] == ["g2"]


def test_legacy_rows_without_a_generation_are_kept():
    snapshot = ({"viking://resources/a.md": {"generation": "g2"}}, set())
    rows = [
        {"uri": "viking://resources/legacy.md", "index_generation": ""},
        {"uri": "viking://resources/legacy.md"},
        {"uri": "viking://resources/unpublished.md", "index_generation": "gX"},
    ]

    kept = [row["uri"] for row in recheck_visibility(_ctx(), rows, snapshot)]

    assert kept == ["viking://resources/legacy.md", "viking://resources/legacy.md"]


def test_tombstone_hides_a_uri_and_its_descendants():
    dead = {"viking://resources/gone"}

    assert tombstoned_uri("viking://resources/gone", dead)
    assert tombstoned_uri("viking://resources/gone/deep/a.md", dead)
    assert not tombstoned_uri("viking://resources/goneish/a.md", dead)

    snapshot = ({"viking://resources/kept.md": {"generation": "g1"}}, dead)
    rows = [
        {"uri": "viking://resources/gone/deep/a.md", "index_generation": "g1"},
        {"uri": "viking://resources/kept.md", "index_generation": "g1"},
    ]

    kept = [row["uri"] for row in recheck_visibility(_ctx(), rows, snapshot)]

    assert kept == ["viking://resources/kept.md"]


async def test_vector_search_rechecks_without_a_per_uri_filter(monkeypatch):
    import openviking.indexing.derived_access as derived_access
    import openviking.indexing.publication as publication
    import openviking.storage.viking_vector_index_backend as backend_mod
    import openviking.wiki.access as wiki_access

    published = {f"viking://resources/doc{i}.md": {"generation": "g2"} for i in range(500)}
    monkeypatch.setattr(publication, "visibility_snapshot", lambda ctx: (published, set()))

    async def no_filter(manager, ctx):
        return None

    async def identity(manager, ctx, rows):
        return rows

    monkeypatch.setattr(wiki_access, "namespace_candidate_filter", no_filter)
    monkeypatch.setattr(derived_access, "derived_candidate_filter", no_filter)
    monkeypatch.setattr(derived_access, "recheck_derived_candidates", identity)

    backend = object.__new__(backend_mod.VikingVectorIndexBackend)
    backend.acl_manager = None
    seen = []

    async def fake_query(**kwargs):
        seen.append(kwargs)
        return [
            {"uri": "viking://resources/doc0.md", "index_generation": "g1", "_score": 0.9},
            {"uri": "viking://resources/doc0.md", "index_generation": "g2", "_score": 0.8},
            {"uri": "viking://resources/doc1.md", "index_generation": "g2", "_score": 0.7},
        ]

    backend.query = fake_query

    rows = await backend.search(query_vector=[0.0], limit=1, ctx=_ctx())

    assert [row["index_generation"] for row in rows] == ["g2"]
    # 500 URIs are published, yet no publication clause reaches the engine, and
    # the query over-fetches so the recheck has rows to choose from.
    assert seen[0]["filter"] is None
    assert seen[0]["limit"] >= 32


async def test_vector_search_retries_when_the_recheck_drops_everything(monkeypatch):
    import openviking.indexing.derived_access as derived_access
    import openviking.indexing.publication as publication
    import openviking.storage.viking_vector_index_backend as backend_mod
    import openviking.wiki.access as wiki_access

    monkeypatch.setattr(
        publication,
        "visibility_snapshot",
        lambda ctx: ({"viking://resources/doc9.md": {"generation": "g2"}}, set()),
    )

    async def no_filter(manager, ctx):
        return None

    async def identity(manager, ctx, rows):
        return rows

    monkeypatch.setattr(wiki_access, "namespace_candidate_filter", no_filter)
    monkeypatch.setattr(derived_access, "derived_candidate_filter", no_filter)
    monkeypatch.setattr(derived_access, "recheck_derived_candidates", identity)

    backend = object.__new__(backend_mod.VikingVectorIndexBackend)
    backend.acl_manager = None
    seen = []

    async def fake_query(**kwargs):
        seen.append(kwargs)
        if len(seen) == 1:
            # A full page of stale rows: the recheck drops all of them, so the
            # backend must widen the fetch instead of returning nothing.
            return [
                {"uri": f"viking://resources/stale{i}.md", "index_generation": "g1"}
                for i in range(kwargs["limit"])
            ]
        return [{"uri": "viking://resources/doc9.md", "index_generation": "g2"}]

    backend.query = fake_query

    rows = await backend.search(query_vector=[0.0], limit=1, ctx=_ctx())

    assert [row["uri"] for row in rows] == ["viking://resources/doc9.md"]
    assert len(seen) == 2
    assert seen[1]["limit"] > seen[0]["limit"]
