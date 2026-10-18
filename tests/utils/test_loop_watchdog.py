"""Tests for the event-loop stall watchdog (diagnostics only)."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

from openviking.utils import loop_watchdog


def test_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("OPENVIKING_LOOP_WATCHDOG", raising=False)
    assert loop_watchdog.watchdog_enabled() is False
    assert loop_watchdog.start_loop_watchdog() is False


def test_enabled_flag_parsing(monkeypatch) -> None:
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv("OPENVIKING_LOOP_WATCHDOG", value)
        assert loop_watchdog.watchdog_enabled() is True
    monkeypatch.setenv("OPENVIKING_LOOP_WATCHDOG", "0")
    assert loop_watchdog.watchdog_enabled() is False
    monkeypatch.setenv("OPENVIKING_LOOP_WATCHDOG", "off")
    assert loop_watchdog.watchdog_enabled() is False


def test_env_floats_fall_back_on_bad_values(monkeypatch) -> None:
    monkeypatch.setenv("OPENVIKING_LOOP_WATCHDOG_THRESHOLD", "abc")
    assert loop_watchdog._env_positive_float(
        "OPENVIKING_LOOP_WATCHDOG_THRESHOLD", 2.0
    ) == 2.0
    monkeypatch.setenv("OPENVIKING_LOOP_WATCHDOG_THRESHOLD", "-5")
    assert loop_watchdog._env_positive_float(
        "OPENVIKING_LOOP_WATCHDOG_THRESHOLD", 2.0
    ) == 2.0
    monkeypatch.setenv("OPENVIKING_LOOP_WATCHDOG_THRESHOLD", "0.5")
    assert loop_watchdog._env_positive_float(
        "OPENVIKING_LOOP_WATCHDOG_THRESHOLD", 2.0
    ) == 0.5


def test_stall_dump_names_the_blocking_frame(monkeypatch, tmp_path: Path) -> None:
    log_path = tmp_path / "watchdog.log"
    monkeypatch.setenv("OPENVIKING_LOOP_WATCHDOG", "1")
    monkeypatch.setenv("OPENVIKING_LOOP_WATCHDOG_PATH", str(log_path))
    monkeypatch.setenv("OPENVIKING_LOOP_WATCHDOG_INTERVAL", "0.02")
    monkeypatch.setenv("OPENVIKING_LOOP_WATCHDOG_THRESHOLD", "0.1")
    monkeypatch.setenv("OPENVIKING_LOOP_WATCHDOG_MIN_DUMP_GAP", "0.05")
    monkeypatch.setattr(loop_watchdog, "_started", False)

    started: dict[str, bool] = {}

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _main() -> None:
            started["ok"] = loop_watchdog.start_loop_watchdog()
            # Block the loop on purpose so the watchdog has something to report.
            time.sleep(0.5)

        try:
            loop.run_until_complete(_main())
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    thread = threading.Thread(target=_run, name="watchdog-test-loop")
    thread.start()
    thread.join(timeout=15)
    assert not thread.is_alive()
    assert started.get("ok") is True
    assert log_path.exists()
    text = log_path.read_text(encoding="utf-8")
    assert "event loop stalled" in text
    # The blocking call site is in the loop thread's Python stack.
    assert "_main" in text
    assert "watchdog-test-loop" in text


def test_dump_tasks_writes_the_await_chain() -> None:
    """A hung-but-idle loop is only diagnosable through task stacks."""
    import asyncio
    import io

    from openviking.utils.loop_watchdog import dump_tasks

    async def scenario() -> str:
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = asyncio.Event()

        async def waiter() -> None:
            started.set()
            await release.wait()

        task = asyncio.create_task(waiter(), name="ov-test-waiter")
        await started.wait()
        await asyncio.sleep(0)
        buffer = io.StringIO()
        dump_tasks(buffer, loop)
        release.set()
        await task
        return buffer.getvalue()

    text = asyncio.run(scenario())
    assert "ov-test-waiter" in text
    assert "waiter" in text
    assert "asyncio tasks" in text
