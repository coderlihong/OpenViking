# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

"""Global test fixtures"""

import asyncio
import shutil
from pathlib import Path
from typing import AsyncGenerator, Generator

import pytest
import pytest_asyncio

from openviking.models.embedder.base import DenseEmbedderBase, EmbedResult
from openviking.server.identity import RequestContext, Role
from openviking.service.core import OpenVikingService
from openviking.service.task_tracker import set_task_tracker
from openviking.storage import viking_fs as viking_fs_module
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils.config.embedding_config import EmbeddingConfig
from openviking_cli.utils.config.open_viking_config import OpenVikingConfigSingleton
from tests.utils.mock_agfs import MockLocalAGFS


# ── Workaround: local .so may lack AGFS_Grep symbol (new in latest source) ──
def _patch_agfs_grep_if_missing():
    """Wrap _setup_functions to catch missing AGFS_Grep and skip its binding."""
    try:
        from openviking.pyagfs.binding_client import BindingLib

        _orig_setup = BindingLib._setup_functions

        def _safe_setup(self):
            try:
                _orig_setup(self)
            except AttributeError as e:
                if "AGFS_Grep" not in str(e):
                    raise
                # Re-implement _setup_functions but skip AGFS_Grep lines.
                # We do this by temporarily removing the Grep lines from the
                # source, but since we can't edit .so, we monkey-patch the lib
                # object's __getattr__ to not fail on AGFS_Grep.
                import ctypes

                class _GrepStub:
                    """Fake ctypes function descriptor for AGFS_Grep."""

                    argtypes = [
                        ctypes.c_int64,
                        ctypes.c_char_p,
                        ctypes.c_char_p,
                        ctypes.c_int,
                        ctypes.c_int,
                        ctypes.c_int,
                        ctypes.c_int,
                    ]
                    restype = ctypes.c_char_p

                    def __call__(self, *args):
                        return b'{"error":"AGFS_Grep not available in this .so version"}'

                # Patch at the CDLL instance level by overriding __getattr__
                orig_class = type(self.lib)
                orig_getattr = orig_class.__getattr__

                def patched_getattr(cdll_self, name):
                    if name == "AGFS_Grep":
                        return _GrepStub()
                    return orig_getattr(cdll_self, name)

                orig_class.__getattr__ = patched_getattr
                try:
                    _orig_setup(self)
                finally:
                    orig_class.__getattr__ = orig_getattr

        BindingLib._setup_functions = _safe_setup
    except Exception:
        pass


_patch_agfs_grep_if_missing()

# Test data root directory
PROJECT_ROOT = Path(__file__).parent.parent
TEST_TMP_DIR = PROJECT_ROOT / "test_data" / "tmp"


@pytest.fixture(scope="session", autouse=True)
def _private_publication_registry(tmp_path_factory):
    """Point the account registry at a file only this test session uses.

    A developer's ``~/.openviking/ov.conf`` sets ``embedding.chunking.publication_path``
    to the registry the *running* instances publish into, and every content write takes
    an exclusive ``BEGIN IMMEDIATE`` on it.  A test session concurrent with a live server
    (or with a second test session) then blocks until sqlite's 30s timeout and the request
    dies with ``database is locked`` -- so pass/fail would depend on what else the machine
    was doing.

    The redirect is a config file, not a mutation of the loaded object: many suites call
    ``OpenVikingConfigSingleton.reset_instance()``, and the next load would silently bring
    the ambient path back.  A machine with no ambient config has no shared registry to
    leak into, so it is left untouched.  Yields the rewritten config path, or ``None``
    when there is nothing to redirect.
    """
    import json
    import os

    from openviking_cli.utils.config.config_loader import resolve_config_path
    from openviking_cli.utils.config.consts import DEFAULT_OV_CONF, OPENVIKING_CONFIG_ENV

    ambient = resolve_config_path(None, OPENVIKING_CONFIG_ENV, DEFAULT_OV_CONF)
    if ambient is None:
        yield None
        return

    document = json.loads(Path(ambient).read_text(encoding="utf-8-sig"))
    chunking = document.setdefault("embedding", {}).setdefault("chunking", {})
    if not (chunking.get("publication_path") or chunking.get("publication_authority_socket")):
        yield None
        return

    registry_dir = tmp_path_factory.mktemp("publication_registry")
    private_conf = registry_dir / "ov.conf"
    chunking["publication_path"] = str(registry_dir / "publication.db")
    # An authority socket would hand the same contention to the live owner process.
    chunking.pop("publication_authority_socket", None)
    chunking.pop("publication_authority_token_file", None)
    private_conf.write_text(json.dumps(document), encoding="utf-8")

    previous = os.environ.get(OPENVIKING_CONFIG_ENV)
    os.environ[OPENVIKING_CONFIG_ENV] = str(private_conf)
    OpenVikingConfigSingleton.reset_instance()
    try:
        yield str(private_conf)
    finally:
        if previous is None:
            os.environ.pop(OPENVIKING_CONFIG_ENV, None)
        else:
            os.environ[OPENVIKING_CONFIG_ENV] = previous
        OpenVikingConfigSingleton.reset_instance()


@pytest.fixture(autouse=True)
def _per_test_publication_registry(_private_publication_registry, tmp_path):
    """Give each test its own account registry.

    The registry keys published revisions by ``(account, uri)`` only, so a session-wide
    file carries one test's revision into the next: a fresh workspace re-ingesting the
    same ``viking://resources/sample/sample.md`` resolves the previous test's digest, and
    a read returns content this test never wrote.  Per-test files also keep the archive
    blobs and their references from outliving each other.
    """
    import json

    private_conf = _private_publication_registry
    if private_conf is None:
        return

    document = json.loads(Path(private_conf).read_text(encoding="utf-8"))
    document["embedding"]["chunking"]["publication_path"] = str(tmp_path / "publication.db")
    Path(private_conf).write_text(json.dumps(document), encoding="utf-8")
    OpenVikingConfigSingleton.reset_instance()


@pytest.fixture(scope="session")
def event_loop():
    """Create session-level event loop"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="function")
def temp_dir() -> Generator[Path, None, None]:
    """Create temp directory, auto-cleanup before and after test"""
    shutil.rmtree(TEST_TMP_DIR, ignore_errors=True)
    TEST_TMP_DIR.mkdir(parents=True, exist_ok=True)
    yield TEST_TMP_DIR


@pytest.fixture(scope="function")
def test_data_dir(temp_dir: Path) -> Path:
    """Create test data directory"""
    data_dir = temp_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


# ============ Service Fixtures ============


@pytest_asyncio.fixture(scope="function")
async def service(
    test_data_dir: Path,
    monkeypatch,
) -> AsyncGenerator[OpenVikingService, None]:
    """Create an initialized service for domain-level tests."""

    previous_viking_fs = viking_fs_module._instance

    class FakeEmbedder(DenseEmbedderBase):
        def __init__(self):
            super().__init__(model_name="test-fake-embedder")

        def embed(self, text: str, is_query: bool = False) -> EmbedResult:
            return EmbedResult(dense_vector=[0.1] * 1024)

        def get_dimension(self) -> int:
            return 1024

    monkeypatch.setattr(EmbeddingConfig, "get_embedder", lambda self: FakeEmbedder())
    mock_agfs = MockLocalAGFS(root_path=test_data_dir / "mock_agfs_root")
    monkeypatch.setattr(
        "openviking.utils.agfs_utils.create_agfs_client",
        lambda *args, **kwargs: mock_agfs,
    )
    OpenVikingConfigSingleton.reset_instance()
    OpenVikingConfigSingleton.initialize(
        config_dict={
            "storage": {
                "workspace": str(test_data_dir),
                "agfs": {"backend": "local"},
                "vectordb": {"backend": "local"},
            },
            "embedding": {
                "dense": {
                    "provider": "openai",
                    "model": "test-embedder",
                    "api_key": "test-key",
                    "dimension": 1024,
                }
            },
        }
    )
    instance = OpenVikingService(
        path=str(test_data_dir),
        user=UserIdentifier.the_default_user(),
    )
    try:
        await instance.initialize()
        yield instance
    finally:
        await instance.close()
        set_task_tracker(None)
        viking_fs_module._instance = previous_viking_fs
        OpenVikingConfigSingleton.reset_instance()


@pytest.fixture(scope="function")
def request_context() -> RequestContext:
    return RequestContext(
        user=UserIdentifier.the_default_user(),
        role=Role.USER,
    )
