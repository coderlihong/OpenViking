import sys
from pathlib import Path

SDK_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]

if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _isolated_import_test(test):
    """Import checks need a fresh interpreter without mutating the suite modules."""
    import functools
    import inspect
    import subprocess
    import textwrap

    @functools.wraps(test)
    def run():
        source = textwrap.dedent(inspect.getsource(test))
        source = source[source.index("def "):]
        setup = ("import sys\nfrom pathlib import Path\n"
                 f"SDK_ROOT = Path({str(SDK_ROOT)!r})\n"
                 f"REPO_ROOT = Path({str(REPO_ROOT)!r})\n"
                 "sys.path.insert(0, str(REPO_ROOT))\n"
                 "sys.path.insert(0, str(SDK_ROOT))\n")
        result = subprocess.run([sys.executable, "-c", setup + source + f"\n{test.__name__}()"],
                                capture_output=True, text=True, cwd=REPO_ROOT)
        assert result.returncode == 0, result.stdout + result.stderr
    return run


@_isolated_import_test
def test_openviking_top_level_exports_http_clients():
    import openviking
    from openviking_cli.client.http import AsyncHTTPClient as LegacyAsyncHTTPClient
    from openviking_cli.client.sync_http import SyncHTTPClient as LegacySyncHTTPClient

    assert openviking.AsyncHTTPClient is LegacyAsyncHTTPClient
    assert openviking.SyncHTTPClient is LegacySyncHTTPClient
    assert not hasattr(openviking, "AsyncOpenViking")
    assert not hasattr(openviking, "SyncOpenViking")
    assert not hasattr(openviking, "OpenViking")


@_isolated_import_test
def test_openviking_client_module_exports_http_clients():
    import os

    from openviking_cli.client.http import AsyncHTTPClient as LegacyAsyncHTTPClient
    from openviking_cli.client.sync_http import SyncHTTPClient as LegacySyncHTTPClient

    os.environ["OPENVIKING_LOG_ROUTING"] = "stdout"
    import openviking.client as client_module

    assert client_module.AsyncHTTPClient is LegacyAsyncHTTPClient
    assert client_module.SyncHTTPClient is LegacySyncHTTPClient


@_isolated_import_test
def test_openviking_client_module_can_fallback_to_repo_local_sdk():
    original_sys_path = list(sys.path)
    try:
        sys.path[:] = [p for p in sys.path if p != str(SDK_ROOT)]

        import openviking.client as client_module

        assert client_module.AsyncHTTPClient.__module__ == "openviking_cli.client._http_compat"
        assert client_module.SyncHTTPClient.__module__ == "openviking_cli.client._http_compat"
    finally:
        sys.path[:] = original_sys_path


@_isolated_import_test
def test_openviking_client_module_does_not_import_service_stack():

    import openviking.client as client_module

    assert "openviking.service" not in sys.modules
    assert client_module.AsyncHTTPClient.__module__ == "openviking_cli.client._http_compat"


@_isolated_import_test
def test_openviking_http_client_preserves_legacy_exception_types():
    import openviking
    from openviking_cli.exceptions import ConflictError

    client = openviking.AsyncHTTPClient(url="http://127.0.0.1:1933")

    try:
        client._raise_exception({"code": "CONFLICT", "message": "mapped"})
    except Exception as exc:
        assert isinstance(exc, ConflictError)
        assert exc.code == "CONFLICT"
    else:
        raise AssertionError("expected ConflictError")
