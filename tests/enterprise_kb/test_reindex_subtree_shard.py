# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Subtree sharding for a recursive resource reindex.

A serial recursive walk finishes the whole subtree's semantic (VLM) phase
before its vector phase starts, which leaves the remote VLM idle for the vector
phase. Sharding splits one recursive directory into per-child units so the two
phases of different children overlap. These tests pin the decision logic and
the per-unit ordering without touching a real VikingFS.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from openviking.service import reindex_executor as module
from openviking.service.reindex_executor import (
    ReindexExecutor,
    _ReindexCounters,
    _ReindexRunContext,
)

ROOT = "viking://resources/raw/menus/797284230104246"
CTX = SimpleNamespace(
    account_id="acct",
    user=SimpleNamespace(user_id="u"),
    role="admin",
    peer_id="u",
)


def _entry(uri: str, *, directory: bool) -> dict:
    return {"uri": uri, "isDir": directory}


def _run() -> _ReindexRunContext:
    return _ReindexRunContext(ctx=CTX, counters=_ReindexCounters(), lock=None)


def _install(monkeypatch, entries, *, concurrency=4, fail_on=None):
    """Patch every collaborator the sharded path touches; return call logs."""
    log = {"children": [], "semantic": [], "vectors": [], "stages": []}
    executor = ReindexExecutor()
    fake_fs = SimpleNamespace()

    async def _stat(uri, ctx=None, skip_count=False):
        return {"isDir": True, "is_dir": True}

    fake_fs.stat = _stat
    monkeypatch.setattr(module, "get_viking_fs", lambda: fake_fs)
    monkeypatch.setattr(
        ReindexExecutor,
        "_effective_subtree_shard_concurrency",
        staticmethod(lambda: concurrency),
    )

    async def _tree_all(self, fs, uri, show_all_hidden=True, ctx=None):
        return entries

    async def _update_stage(self, stage):
        log["stages"].append(stage)

    async def _run_semantic_processor(self, **kwargs):
        log["semantic"].append(kwargs)
        return None

    async def _reindex_resource(
        self,
        *,
        uri,
        mode,
        run,
        recursive=True,
        shard=True,
        propagate_to_parent=True,
    ):
        log["children"].append((uri, recursive, shard, propagate_to_parent))
        # The real child unit merges into its own counters; the sharded parent
        # must fold those back into the run's counters.
        run.counters.rebuilt_records += 1
        if fail_on is not None and uri == fail_on:
            raise RuntimeError(f"child unit failed: {uri}")

    async def _reindex_resource_vectors(self, **kwargs):
        log["vectors"].append(kwargs)

    monkeypatch.setattr(ReindexExecutor, "_tree_all", _tree_all)
    monkeypatch.setattr(ReindexExecutor, "_update_stage", _update_stage)
    monkeypatch.setattr(ReindexExecutor, "_run_semantic_processor", _run_semantic_processor)
    monkeypatch.setattr(ReindexExecutor, "_reindex_resource", _reindex_resource)
    monkeypatch.setattr(
        ReindexExecutor, "_reindex_resource_vectors", _reindex_resource_vectors
    )
    return executor, log


def test_children_run_as_units_and_the_directory_is_refreshed_last(monkeypatch):
    entries = [
        _entry(f"{ROOT}/a", directory=True),
        _entry(f"{ROOT}/b", directory=True),
        _entry(f"{ROOT}/c", directory=True),
        _entry(f"{ROOT}/chapter.md", directory=False),
        _entry(f"{ROOT}/chapter.md/.abstract.md", directory=False),
        _entry(f"{ROOT}/a/nested", directory=True),
        _entry(f"{ROOT}/a/nested.md", directory=False),
    ]
    executor, log = _install(monkeypatch, entries)
    run = _run()

    assert asyncio.run(executor._reindex_resource_sharded(uri=ROOT, mode="semantic_and_vectors", run=run))

    children = {uri: (recursive, shard, propagate) for uri, recursive, shard, propagate in log["children"]}
    assert set(children) == {
        f"{ROOT}/a",
        f"{ROOT}/b",
        f"{ROOT}/c",
        f"{ROOT}/chapter.md",
    }, "only direct children are units; nested entries stay inside their parent unit"
    assert children[f"{ROOT}/a"] == (True, False, False), "child units never shard again"
    assert children[f"{ROOT}/chapter.md"] == (False, False, False)

    # The directory's own L0/L1 is rebuilt once, after the children, and its
    # vector record is the last thing written for this directory.
    assert len(log["semantic"]) == 1
    assert log["semantic"][0]["uri"] == ROOT
    assert log["semantic"][0]["recursive"] is False
    assert log["semantic"][0]["propagate_to_parent"] is False
    assert len(log["vectors"]) == 1
    assert log["vectors"][0]["uri"] == ROOT
    assert log["vectors"][0]["recursive"] is False
    assert run.counters.rebuilt_records == 4, "child counters are folded back"


def test_disabled_concurrency_falls_back_to_the_serial_walk(monkeypatch):
    entries = [_entry(f"{ROOT}/a", directory=True), _entry(f"{ROOT}/b", directory=True)]
    executor, log = _install(monkeypatch, entries, concurrency=1)

    assert asyncio.run(executor._reindex_resource_sharded(uri=ROOT, mode="semantic_and_vectors", run=_run())) is False
    assert log["children"] == []
    assert log["semantic"] == []


def test_single_child_falls_back_to_the_serial_walk(monkeypatch):
    entries = [_entry(f"{ROOT}/only", directory=True), _entry(f"{ROOT}/only/deep.md", directory=False)]
    executor, log = _install(monkeypatch, entries)

    assert asyncio.run(executor._reindex_resource_sharded(uri=ROOT, mode="semantic_and_vectors", run=_run())) is False
    assert log["children"] == []


def test_child_failure_still_runs_siblings_then_propagates(monkeypatch):
    entries = [
        _entry(f"{ROOT}/a", directory=True),
        _entry(f"{ROOT}/b", directory=True),
        _entry(f"{ROOT}/c", directory=True),
    ]
    executor, log = _install(monkeypatch, entries, fail_on=f"{ROOT}/b")
    run = _run()

    with pytest.raises(RuntimeError, match="child unit failed"):
        asyncio.run(executor._reindex_resource_sharded(uri=ROOT, mode="semantic_and_vectors", run=run))

    assert len(log["children"]) == 3
    assert run.counters.rebuilt_records == 3, "a failed sibling must not discard the others' counters"
    assert len(log["vectors"]) == 1, "the directory refresh still runs before the failure is raised"


def test_hidden_meta_and_nested_entries_are_not_units(monkeypatch):
    entries = [
        _entry(f"{ROOT}/.abstract.md", directory=False),
        _entry(f"{ROOT}/.overview.md", directory=False),
        _entry(f"{ROOT}/toc.md", directory=False),
        _entry(f"{ROOT}/sub", directory=True),
        _entry(f"{ROOT}/sub/leaf.md", directory=False),
        _entry(f"{ROOT}/other", directory=True),
        _entry(f"{ROOT}/chapter.md", directory=False),
    ]
    executor, log = _install(monkeypatch, entries)

    asyncio.run(executor._reindex_resource_sharded(uri=ROOT, mode="semantic_and_vectors", run=_run()))

    units = [uri for uri, _r, _s, _p in log["children"]]
    # Navigation metadata (.abstract.md/.overview.md/toc.md) is never a unit of
    # its own, and nested entries belong to their parent unit.
    assert units == [f"{ROOT}/sub", f"{ROOT}/other", f"{ROOT}/chapter.md"]


def test_non_recursive_resources_never_shard(monkeypatch):
    entries = [_entry(f"{ROOT}/a", directory=True), _entry(f"{ROOT}/b", directory=True)]
    executor, log = _install(monkeypatch, entries)
    shard_calls = []

    async def _sharded(self, *, uri, mode, run):
        shard_calls.append(uri)
        return True

    monkeypatch.setattr(ReindexExecutor, "_reindex_resource_sharded", _sharded)

    asyncio.run(
        executor._reindex_resource(
            uri=f"{ROOT}/a.md", mode="semantic_and_vectors", run=_run(), recursive=False
        )
    )

    assert shard_calls == [], "a leaf rebuild keeps its single sequential unit"


# ---------------------------------------------------------------------------
# A shard must not reach outside the subtree it owns.
#
# Sharding splits one directory into per-child units and runs them under a single
# tree lease held by the outer reindex. Each unit's semantic work has two upward
# hooks: aggregating the parent directory (``aggregate_directory``) and the
# explicit ancestor refresh (``_refresh_ancestor_semantics``). Both write sidecars
# whose exact leases live *outside* the shard's tree lease, so a unit that takes
# either one queues behind the run that owns it.
#
# Live, before the fix, a 74-unit shard of one directory produced 111
# "Semantic dispatch ... is lock-blocked" warnings, re-processed its own parent 38
# times, and gave up on 291 dispatches; ``semantic_and_vectors`` took 104.7s with
# the parent directory's sidecars never written. With both hooks disabled inside a
# shard: 0 blocked, 0 abandoned, 20.5s.
# ---------------------------------------------------------------------------


def test_a_sharded_child_does_not_aggregate_its_parent(monkeypatch):
    entries = [_entry(f"{ROOT}/a", directory=True), _entry(f"{ROOT}/b", directory=True)]
    executor, log = _install(monkeypatch, entries)

    assert asyncio.run(
        executor._reindex_resource_sharded(uri=ROOT, mode="semantic_and_vectors", run=_run())
    )

    assert log["children"], "the directory must still shard into child units"
    for uri, _recursive, _shard, propagate in log["children"]:
        assert propagate is False, (
            f"child unit {uri} asked to propagate to its parent; the shard does not "
            "own the parent's lease and will block on it"
        )


def test_a_sharded_child_does_not_refresh_ancestors(monkeypatch):
    """The leaf branch must skip the ancestor refresh when it is a shard."""
    executor, log = _install(monkeypatch, [_entry(f"{ROOT}/a", directory=True)])
    refreshed = []

    async def _refresh(self, *, uri, run):
        refreshed.append(uri)

    async def _semantic(self, **kwargs):
        log["semantic"].append(kwargs)
        return None

    monkeypatch.setattr(ReindexExecutor, "_refresh_ancestor_semantics", _refresh)
    monkeypatch.setattr(ReindexExecutor, "_run_semantic_processor", _semantic)

    asyncio.run(
        executor._reindex_resource(
            uri=f"{ROOT}/chapter.md",
            mode="semantic_and_vectors",
            run=_run(),
            recursive=False,
            shard=False,
            propagate_to_parent=False,
        )
    )

    assert refreshed == [], (
        "a shard refreshed its ancestors, whose leases the outer reindex already "
        "holds; every shard then blocks on the same path"
    )


def test_a_standalone_leaf_still_refreshes_its_ancestors(monkeypatch):
    """The guard must not disable ancestor refresh for ordinary leaf rebuilds."""
    executor = ReindexExecutor()
    refreshed = []

    async def _refresh(self, *, uri, run):
        refreshed.append(uri)

    async def _semantic(self, **kwargs):
        return None

    async def _vectors(self, **kwargs):
        return None

    async def _sweep(self, uri, ctx=None):
        return None

    monkeypatch.setattr(ReindexExecutor, "_refresh_ancestor_semantics", _refresh)
    monkeypatch.setattr(ReindexExecutor, "_run_semantic_processor", _semantic)
    monkeypatch.setattr(ReindexExecutor, "_reindex_resource_vectors", _vectors)
    monkeypatch.setattr(ReindexExecutor, "_sweep_stale_placeholder_sidecars", _sweep)

    asyncio.run(
        executor._reindex_resource(
            uri=f"{ROOT}/chapter.md",
            mode="semantic_and_vectors",
            run=_run(),
            recursive=False,
            shard=False,
            propagate_to_parent=True,
        )
    )

    assert refreshed == [f"{ROOT}/chapter.md"], (
        "a non-sharded leaf rebuild must still bubble its L0/L1 into its ancestors"
    )


def test_a_sharded_child_still_writes_its_own_sidecars(monkeypatch):
    """Disabling upward propagation must not disable the child's own L0/L1.

    ``SemanticDagExecutor._overview_task`` returns early when
    ``aggregate_directory`` is False, skipping the write of the directory's own
    ``.abstract.md``/``.overview.md``. That flag is therefore *not* the knob for
    "don't bubble into the parent" -- ``propagate_to_parent`` is, and it gates
    ``_enqueue_parent_refresh`` on its own.

    Setting the wrong one produced a fully vectorized tree with no directory
    sidecars at all: 388 chapters rebuilt, 1042 vectors written, and zero
    ``.abstract.md`` files. These assertions pin the correct knob.
    """
    entries = [_entry(f"{ROOT}/a", directory=True), _entry(f"{ROOT}/b", directory=True)]
    executor, log = _install(monkeypatch, entries)

    assert asyncio.run(
        executor._reindex_resource_sharded(uri=ROOT, mode="semantic_and_vectors", run=_run())
    )

    # The directory's own refresh runs once, after the children.
    assert len(log["semantic"]) == 1
    own = log["semantic"][0]
    assert own["uri"] == ROOT
    assert own["propagate_to_parent"] is False, (
        "the shard's own refresh must not bubble into ancestors it does not own"
    )
    assert own.get("aggregate_directory", True) is True, (
        "aggregate_directory=False also skips writing this directory's own "
        ".abstract.md/.overview.md, leaving the tree vectorized but undocumented"
    )
