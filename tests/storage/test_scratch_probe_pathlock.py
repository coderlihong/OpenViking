"""Scratch probe: trace pathlock consumption in the retries test (not committed)."""
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from openviking.server.identity import RequestContext, Role
from openviking_cli.session.user_id import UserIdentifier
from openviking.storage.queuefs.semantic_dag import SemanticDagExecutor
from openviking.storage.errors import LockAcquisitionError

from tests.storage.test_semantic_dag_incremental import (
    _FakeVikingFS,
    _FakeProcessor,
)


@pytest.mark.asyncio
async def test_probe_pathlock_sequence(monkeypatch):
    root_uri = "viking://resources/root"
    file_path = f"{root_uri}/a.txt"
    fake_fs = _FakeVikingFS(
        tree={root_uri: [{"name": "a.txt", "isDir": False}]},
        file_contents={file_path: "new content"},
    )
    monkeypatch.setattr("openviking.storage.queuefs.semantic_dag.get_viking_fs", lambda: fake_fs)
    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_dag.get_openviking_config",
        lambda: SimpleNamespace(semantic=SimpleNamespace(overview_sample_limit=32)),
    )

    processor = _FakeProcessor(fake_fs)
    vectorize_directory = AsyncMock(side_effect=[RuntimeError("temporary failure"), None])
    processor._vectorize_directory = vectorize_directory
    ctx = RequestContext(user=UserIdentifier("acc1", "user1"), role=Role.USER)
    executor = SemanticDagExecutor(
        processor=processor, context_type="resource", max_concurrent_llm=2, ctx=ctx,
        incremental_update=True, target_uri=root_uri, recursive=False,
        changes={"modified": [file_path]},
    )

    calls = []

    async def spy_check(*a, **k):
        r = await _orig_check(executor, *a, **k)
        calls.append(("check_children", r))
        return r

    from unittest.mock import patch as _p

    with _p.object(executor, "_check_dir_children_changed",
                   AsyncMock(side_effect=lambda *a, **k: _wrap(executor, "_check_dir_children_changed", a, k, calls))):
        pass

    fake_fs.pathlock_acquire_exact_batch = AsyncMock(
        side_effect=[{"paths": []}, LockAcquisitionError("parent sidecars are busy")]
    )
    await executor.run(root_uri)
    print("PROBE calls:", calls)
    print("PROBE vectorized:", processor.vectorized_files)
    print("PROBE writes:", fake_fs.writes)


async def _orig_check(executor, *a, **k):
    import functools
    real = functools.partial(SemanticDagExecutor._check_dir_children_changed.__wrapped__ if hasattr(SemanticDagExecutor._check_dir_children_changed, "__wrapped__") else SemanticDagExecutor._check_dir_children_changed, executor)
    return await real(*a, **k)


async def _wrap(executor, name, a, k, calls):
    real = getattr(SemanticDagExecutor, name)
    r = await real(executor, *a, **k)
    calls.append((name, r))
    return r
