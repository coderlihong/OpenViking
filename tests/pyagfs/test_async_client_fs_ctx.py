from __future__ import annotations

from typing import Any

import pytest

from openviking.pyagfs.async_client import AsyncAGFSClient
from openviking.pyagfs.helpers import cp


class _RecordingClient:
    """Record ctx values received by the sync AGFS surface."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str] | None]] = []
        self.list_options: dict[str, Any] = {}

    def write(self, path: str, data: bytes, *, ctx: dict[str, str] | None = None) -> str:
        """Record write ctx and return a stable fake backend id."""
        self.calls.append(("write", path, ctx))
        return "written"

    def read(self, path: str, *, ctx: dict[str, str] | None = None, **_: Any) -> bytes:
        """Record read ctx and return a stable fake payload."""
        self.calls.append(("read", path, ctx))
        return b"payload"

    def ls(
        self,
        path: str,
        *,
        ctx: dict[str, str] | None = None,
        **options: Any,
    ) -> list[dict[str, Any]]:
        """Record listing options and return an empty result."""
        self.calls.append(("ls", path, ctx))
        self.list_options = options
        return []


@pytest.mark.asyncio
async def test_async_client_derives_account_ctx_from_local_agfs_path() -> None:
    client = _RecordingClient()
    agfs = AsyncAGFSClient(client)

    await agfs.write("/local/acct-1/data/file.txt", b"x")
    await agfs.ls(
        "/local/acct-1/data",
        offset=2,
        limit=3,
        sort_by="mtime",
        sort_order="desc",
    )

    assert client.calls == [
        ("write", "/local/acct-1/data/file.txt", {"account_id": "acct-1"}),
        ("ls", "/local/acct-1/data", {"account_id": "acct-1"}),
    ]
    assert client.list_options == {
        "offset": 2,
        "limit": 3,
        "sort_by": "mtime",
        "sort_order": "desc",
    }


@pytest.mark.asyncio
async def test_async_client_uses_system_ctx_for_non_local_agfs_path() -> None:
    client = _RecordingClient()
    agfs = AsyncAGFSClient(client)

    await agfs.read("/queue/semantic/dequeue")

    assert client.calls == [("read", "/queue/semantic/dequeue", {"account_id": "_system"})]


@pytest.mark.asyncio
async def test_async_client_preserves_explicit_fs_ctx() -> None:
    client = _RecordingClient()
    agfs = AsyncAGFSClient(client)

    await agfs.write(
        "/local/path-account/data/file.txt", b"x", fs_ctx={"account_id": "ctx-account"}
    )

    assert client.calls == [
        ("write", "/local/path-account/data/file.txt", {"account_id": "ctx-account"})
    ]


@pytest.mark.asyncio
async def test_async_client_rejects_cross_account_mv() -> None:
    client = _RecordingClient()
    agfs = AsyncAGFSClient(client)

    with pytest.raises(ValueError, match="cross-account"):
        await agfs.mv("/local/a/data/file.txt", "/local/b/data/file.txt")


def test_cp_rejects_cross_account_raw_copy() -> None:
    class _CpClient:
        """Minimal cp client; methods must not be called after account guard fails."""

        def stat(self, path: str) -> dict[str, Any]:
            """Fail if cp checks storage before validating account boundaries."""
            raise AssertionError(f"unexpected stat call: {path}")

    with pytest.raises(ValueError, match="cross-account"):
        cp(_CpClient(), "/local/a/data/file.txt", "/local/b/data/file.txt")


def test_cp_rejects_cross_encryption_domain_raw_copy() -> None:
    class _CpClient:
        """Minimal cp client; methods must not be called after account guard fails."""

        def stat(self, path: str) -> dict[str, Any]:
            """Fail if cp checks storage before validating account boundaries."""
            raise AssertionError(f"unexpected stat call: {path}")

    with pytest.raises(ValueError, match="cross-account"):
        cp(_CpClient(), "/local/a/data/file.txt", "/mem/data/file.txt")


@pytest.mark.asyncio
async def test_async_cp_streams_when_same_mount_fast_path_is_unavailable() -> None:
    class _FallbackClient:
        def __init__(self) -> None:
            self.cat_calls: list[tuple[str, bool, dict[str, str] | None]] = []
            self.written_chunks: list[bytes] = []

        def stat(self, path: str, *, ctx: dict[str, str] | None = None) -> dict[str, Any]:
            del ctx
            return {"isDir": path.endswith("/data")}

        def copy_within_mount(
            self, source: str, target: str, *, ctx: dict[str, str] | None = None
        ) -> dict[str, bool]:
            del source, target, ctx
            return {"performed": False}

        def cat(
            self,
            path: str,
            *,
            stream: bool = False,
            ctx: dict[str, str] | None = None,
        ):
            self.cat_calls.append((path, stream, ctx))
            if not stream:
                raise AssertionError("fallback copy loaded the complete file")
            return iter((b"first", b"second"))

        def write(self, path: str, data, *, ctx: dict[str, str] | None = None) -> str:
            del path, ctx
            self.written_chunks = list(data)
            return "written"

    client = _FallbackClient()
    agfs = AsyncAGFSClient(client)

    await agfs.cp(
        "/local/acct/data/source.bin",
        "/local/acct/data/target.bin",
        fs_ctx={"account_id": "acct", "lease_ref": "lease"},
    )

    assert client.cat_calls == [
        (
            "/local/acct/data/source.bin",
            True,
            {"account_id": "acct", "lease_ref": "lease"},
        )
    ]
    assert client.written_chunks == [b"first", b"second"]


@pytest.mark.asyncio
async def test_async_cp_streams_by_default_when_same_mount_fast_path_is_available() -> None:
    class _FastPathClient:
        def __init__(self) -> None:
            self.fast_path_calls: list[tuple[str, str]] = []
            self.cat_calls: list[tuple[str, bool]] = []
            self.written_chunks: list[bytes] = []

        def stat(self, path: str, *, ctx: dict[str, str] | None = None) -> dict[str, Any]:
            del ctx
            return {"isDir": path.endswith("/data")}

        def copy_within_mount(
            self, source: str, target: str, *, ctx: dict[str, str] | None = None
        ) -> dict[str, bool]:
            del ctx
            self.fast_path_calls.append((source, target))
            return {"performed": True}

        def cat(
            self,
            path: str,
            *,
            stream: bool = False,
            ctx: dict[str, str] | None = None,
        ):
            del ctx
            self.cat_calls.append((path, stream))
            return iter((b"first", b"second"))

        def write(self, path: str, data, *, ctx: dict[str, str] | None = None) -> str:
            del path, ctx
            self.written_chunks = list(data)
            return "written"

    client = _FastPathClient()
    agfs = AsyncAGFSClient(client)

    await agfs.cp(
        "/local/acct/data/source.bin",
        "/local/acct/data/target.bin",
    )

    assert client.fast_path_calls == []
    assert client.cat_calls == [("/local/acct/data/source.bin", True)]
    assert client.written_chunks == [b"first", b"second"]


@pytest.mark.asyncio
async def test_async_cp_can_explicitly_use_same_mount_fast_path() -> None:
    class _FastPathClient:
        def __init__(self) -> None:
            self.fast_path_calls: list[tuple[str, str]] = []

        def stat(self, path: str, *, ctx: dict[str, str] | None = None) -> dict[str, Any]:
            del ctx
            return {"isDir": path.endswith("/data")}

        def copy_within_mount(
            self, source: str, target: str, *, ctx: dict[str, str] | None = None
        ) -> dict[str, bool]:
            del ctx
            self.fast_path_calls.append((source, target))
            return {"performed": True}

        def cat(self, *args, **kwargs):
            raise AssertionError("explicit fast path must not stream file bytes")

    client = _FastPathClient()
    agfs = AsyncAGFSClient(client)

    await agfs.cp(
        "/local/acct/data/source.bin",
        "/local/acct/data/target.bin",
        allow_same_mount_fast_path=True,
    )

    assert client.fast_path_calls == [
        ("/local/acct/data/source.bin", "/local/acct/data/target.bin")
    ]


@pytest.mark.asyncio
async def test_async_cp_falls_back_to_complete_write_when_binding_cannot_stream() -> None:
    class _BindingClient:
        def __init__(self) -> None:
            self.fast_path_calls: list[tuple[str, str]] = []
            self.cat_calls: list[tuple[str, bool]] = []
            self.written: bytes | None = None

        def stat(self, path: str, *, ctx: dict[str, str] | None = None) -> dict[str, Any]:
            del ctx
            return {"isDir": path.endswith("/data")}

        def copy_within_mount(
            self, source: str, target: str, *, ctx: dict[str, str] | None = None
        ) -> dict[str, bool]:
            del ctx
            self.fast_path_calls.append((source, target))
            return {"performed": True}

        def cat(
            self,
            path: str,
            *,
            stream: bool = False,
            ctx: dict[str, str] | None = None,
        ) -> bytes:
            del ctx
            self.cat_calls.append((path, stream))
            if stream:
                raise RuntimeError("Streaming not supported in binding mode")
            return b"complete payload"

        def write(self, path: str, data: bytes, *, ctx: dict[str, str] | None = None) -> str:
            del path, ctx
            self.written = data
            return "written"

    client = _BindingClient()
    agfs = AsyncAGFSClient(client)

    await agfs.cp(
        "/local/acct/data/source.bin",
        "/local/acct/data/target.bin",
    )

    assert client.fast_path_calls == []
    assert client.cat_calls == [
        ("/local/acct/data/source.bin", True),
        ("/local/acct/data/source.bin", False),
    ]
    assert client.written == b"complete payload"


class _RecordingLockClient:
    """Record pathlock acquisitions and the wait budget they were armed with."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def pathlock_acquire_exact_batch(
        self,
        ctx: dict[str, str],
        paths: list[str],
        timeout_secs: float = 0.0,
        owner_lease_ref: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "ctx": ctx,
                "paths": list(paths),
                "timeout_secs": timeout_secs,
                "owner_lease_ref": owner_lease_ref,
            }
        )
        return {"lease_ref": "lease"}


def test_pathlock_default_timeout_is_bounded_and_env_overridable(monkeypatch) -> None:
    from openviking.pyagfs import async_client as ac

    monkeypatch.delenv(ac._PATHLOCK_TIMEOUT_ENV, raising=False)
    assert ac._PATHLOCK_DEFAULT_TIMEOUT_SECS > 0
    assert ac.default_pathlock_timeout_secs() == ac._PATHLOCK_DEFAULT_TIMEOUT_SECS

    monkeypatch.setenv(ac._PATHLOCK_TIMEOUT_ENV, "12.5")
    assert ac.default_pathlock_timeout_secs() == 12.5

    monkeypatch.setenv(ac._PATHLOCK_TIMEOUT_ENV, "not-a-number")
    assert ac.default_pathlock_timeout_secs() == ac._PATHLOCK_DEFAULT_TIMEOUT_SECS

    # A negative budget still means "do not wait", never an unbounded wait.
    monkeypatch.setenv(ac._PATHLOCK_TIMEOUT_ENV, "-3")
    assert ac.default_pathlock_timeout_secs() == 0.0


@pytest.mark.asyncio
async def test_pathlock_wrapper_arms_the_default_budget_unless_overridden(monkeypatch) -> None:
    from openviking.pyagfs import async_client as ac

    monkeypatch.delenv(ac._PATHLOCK_TIMEOUT_ENV, raising=False)
    client = _RecordingLockClient()
    agfs = AsyncAGFSClient(client)

    await agfs.pathlock_acquire_exact_batch(["/local/acct/data/.abstract.md"])
    await agfs.pathlock_acquire_exact_batch(["/local/acct/data/.abstract.md"], timeout_secs=0.0)

    assert [call["timeout_secs"] for call in client.calls] == [
        ac._PATHLOCK_DEFAULT_TIMEOUT_SECS,
        0.0,
    ]
