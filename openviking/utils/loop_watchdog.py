"""Event-loop stall watchdog (diagnostics only).

Enabled with ``OPENVIKING_LOOP_WATCHDOG=1``.  A daemon thread pings the running
event loop at a fixed interval; when a ping is not answered within the
threshold the loop is provably blocked, and the watchdog dumps the exact
blocking site:

* the Python stack of every thread (via :func:`sys._current_frames`), which
  names the ``file:line`` that is holding the loop;
* the C-level traceback of all threads (via :func:`faulthandler.dump_traceback`),
  which is what makes a stuck ``sqlite3_step`` visible.

The watchdog never mutates application state, and dumps are rate limited so a
long stall produces a bounded amount of output.  Environment knobs:

``OPENVIKING_LOOP_WATCHDOG``            1/true/yes/on to enable (default off)
``OPENVIKING_LOOP_WATCHDOG_INTERVAL``   ping interval seconds (default 0.25)
``OPENVIKING_LOOP_WATCHDOG_THRESHOLD``  stall threshold seconds (default 2.0)
``OPENVIKING_LOOP_WATCHDOG_MIN_DUMP_GAP`` seconds between dumps (default 15.0)
``OPENVIKING_LOOP_WATCHDOG_PATH``       output path (default tmp/loop_watchdog.log)
``OPENVIKING_LOOP_WATCHDOG_DUMP_TASKS`` seconds between asyncio task snapshots
                                        (default 0 = off); a hung but idle loop
                                        is only visible through task stacks
"""

from __future__ import annotations

import asyncio
import faulthandler
import os
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any

_ENV_ENABLED = "OPENVIKING_LOOP_WATCHDOG"
_ENV_INTERVAL = "OPENVIKING_LOOP_WATCHDOG_INTERVAL"
_ENV_THRESHOLD = "OPENVIKING_LOOP_WATCHDOG_THRESHOLD"
_ENV_MIN_DUMP_GAP = "OPENVIKING_LOOP_WATCHDOG_MIN_DUMP_GAP"
_ENV_PATH = "OPENVIKING_LOOP_WATCHDOG_PATH"
_ENV_TASK_DUMP = "OPENVIKING_LOOP_WATCHDOG_DUMP_TASKS"

_DEFAULT_INTERVAL_SECS = 0.25
_DEFAULT_THRESHOLD_SECS = 2.0
_DEFAULT_MIN_DUMP_GAP_SECS = 15.0
_DEFAULT_PATH = "tmp/loop_watchdog.log"

_started = False
_start_lock = threading.Lock()


def watchdog_enabled() -> bool:
    """True when the watchdog was requested through the environment."""
    return os.environ.get(_ENV_ENABLED, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _open_log() -> IO[str] | None:
    path = Path(os.environ.get(_ENV_PATH, "").strip() or _DEFAULT_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open("a", encoding="utf-8", buffering=1)
    except OSError:
        return None


def _write_python_stacks(out: IO[str]) -> None:
    frames = sys._current_frames()
    for thread in threading.enumerate():
        frame = frames.get(thread.ident)
        if frame is None:
            continue
        out.write(f"--- thread {thread.name} (ident={thread.ident}) ---\n")
        chain = []
        while frame is not None:
            chain.append(frame)
            frame = frame.f_back
        for frame in reversed(chain):
            out.write(
                f'  File "{frame.f_code.co_filename}", line {frame.f_lineno}, '
                f"in {frame.f_code.co_name}\n"
            )
    out.flush()


def dump_stall(out: IO[str], stalled: float, threshold: float) -> None:
    """Write one stall report (Python stacks + C-level tracebacks)."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    out.write(
        f"\n===== {stamp} event loop stalled {stalled:.2f}s "
        f"(threshold {threshold:.2f}s) =====\n"
    )
    out.flush()
    _write_python_stacks(out)
    try:
        faulthandler.dump_traceback(file=out, all_threads=True)
    except Exception:  # pragma: no cover - diagnostics must never raise
        pass
    out.flush()


def dump_tasks(out: IO[str], loop: Any) -> None:
    """Write one snapshot of every live asyncio task and its await chain.

    A loop that is merely *idle* while a task waits forever looks healthy to the
    stall detector, so a hung pipeline needs the task stacks to be visible.
    """
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    out.write(f"\n===== {stamp} asyncio tasks =====\n")
    try:
        tasks = list(asyncio.all_tasks(loop))
    except Exception:  # pragma: no cover - diagnostics must never raise
        tasks = []
    for task in sorted(tasks, key=lambda item: item.get_name()):
        out.write(f"--- task {task.get_name()} state={getattr(task, '_state', '?')} ---\n")
        try:
            stack = task.get_stack(limit=12)
        except Exception:  # pragma: no cover - task already finished
            continue
        for frame in stack:
            out.write(
                f'  File "{frame.f_code.co_filename}", line {frame.f_lineno}, '
                f"in {frame.f_code.co_name}\n"
            )
    out.flush()


def _watch(
    loop: Any,
    interval: float,
    threshold: float,
    min_dump_gap: float,
    out: IO[str],
    task_dump_interval: float = 0.0,
) -> None:
    acked = threading.Event()

    def _ping() -> None:
        acked.set()

    last_dump = 0.0
    next_task_dump = time.monotonic() + task_dump_interval if task_dump_interval > 0 else 0.0
    while True:
        if task_dump_interval > 0 and time.monotonic() >= next_task_dump:
            next_task_dump = time.monotonic() + task_dump_interval
            dump_tasks(out, loop)
        acked.clear()
        try:
            loop.call_soon_threadsafe(_ping)
        except RuntimeError:  # loop closed
            return
        sent_at = time.monotonic()
        if acked.wait(timeout=threshold):
            time.sleep(interval)
            continue
        # The loop did not answer within the threshold: it is blocked right now.
        now = time.monotonic()
        if now - last_dump >= min_dump_gap:
            last_dump = now
            dump_stall(out, now - sent_at, threshold)
        # Keep measuring (and occasionally re-dumping) until it recovers.
        while not acked.wait(timeout=interval):
            now = time.monotonic()
            if now - last_dump >= min_dump_gap:
                last_dump = now
                dump_stall(out, now - sent_at, threshold)
        time.sleep(interval)


def start_loop_watchdog() -> bool:
    """Start the watchdog thread for the running loop.

    Returns True when a watchdog thread was started (or was already running),
    False when it is disabled or no loop is running.
    """
    global _started
    if not watchdog_enabled():
        return False
    with _start_lock:
        if _started:
            return True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        out = _open_log()
        if out is None:
            return False
        thread = threading.Thread(
            target=_watch,
            args=(
                loop,
                _env_positive_float(_ENV_INTERVAL, _DEFAULT_INTERVAL_SECS),
                _env_positive_float(_ENV_THRESHOLD, _DEFAULT_THRESHOLD_SECS),
                _env_positive_float(_ENV_MIN_DUMP_GAP, _DEFAULT_MIN_DUMP_GAP_SECS),
                out,
                _env_positive_float(_ENV_TASK_DUMP, 0.0)
                if os.environ.get(_ENV_TASK_DUMP, "").strip()
                else 0.0,
            ),
            name="ov-loop-watchdog",
            daemon=True,
        )
        thread.start()
        _started = True
        return True
