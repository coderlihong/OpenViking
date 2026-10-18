# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Env-gated SQLite slow-operation tracer (debug aid, off by default).

Set ``OPENVIKING_SQL_TRACE_MS=<ms>`` to make every SQLite operation whose
execute / fetch / iteration time exceeds the threshold write one line to
``OPENVIKING_SQL_TRACE_LOG`` (default ``/tmp/ov_sql_trace.log``), including the
SQL text, the row count and — for operations slower than
``OPENVIKING_SQL_TRACE_STACK_MS`` (default 200 ms) — the Python stack that
issued it.

This module only uses the standard library and must be imported *before* the
``openviking`` package so that it wraps ``sqlite3.connect`` for every store.
When the environment variable is unset, ``install_from_env`` is a no-op and
costs nothing.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import traceback

__all__ = ["install_from_env", "is_installed"]

_SKIP_FRAMES = ("sql_trace.py", "sqlite3/__init__.py", "<string>")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class _Sink:
    """Serialised, line-buffered writer shared by every traced connection."""

    def __init__(self) -> None:
        self.threshold_ms = _env_float("OPENVIKING_SQL_TRACE_MS", 0.0)
        self.stack_ms = _env_float("OPENVIKING_SQL_TRACE_STACK_MS", 200.0)
        self.path = os.environ.get("OPENVIKING_SQL_TRACE_LOG") or "/tmp/ov_sql_trace.log"
        self._lock = threading.Lock()
        self._fh = None
        self._pending = 0

    def _handle(self):
        if self._fh is None:
            self._fh = open(self.path, "a", encoding="utf-8", buffering=1)
        return self._fh

    def write(self, line: str) -> None:
        with self._lock:
            try:
                handle = self._handle()
                handle.write(line)
                self._pending += 1
            except Exception:  # pragma: no cover - tracing must never break the server
                pass

    def emit(self, kind: str, sql: str, ms: float, rows: int | None = None) -> None:
        if ms < self.threshold_ms:
            return
        stamp = time.strftime("%H:%M:%S")
        thread = threading.current_thread().name
        head = f"[{stamp}] pid={os.getpid()} {thread} {kind} {ms:9.1f}ms rows={rows if rows is not None else '-'} sql={_compact(sql)}"
        if ms >= self.stack_ms:
            frames = traceback.extract_stack()[:-2]
            frames = [f for f in frames if not any(s in f.filename for s in _SKIP_FRAMES)]
            rendered = "".join(traceback.format_list(frames[-14:]))
            head = f"{head}\n{rendered.rstrip()}"
        self.write(head + "\n")


_SINK: _Sink | None = None


def _compact(sql: str, limit: int = 400) -> str:
    text = " ".join(str(sql).split())
    if len(text) > limit:
        text = text[:limit] + f"...<+{len(text) - limit} chars>"
    return text


def _maybe(kind: str, sql: str, ms: float, rows: int | None = None) -> None:
    sink = _SINK
    if sink is not None:
        sink.emit(kind, sql, ms, rows)


class _TracingCursor:
    __slots__ = ("_cur", "_sql", "_iter_ms", "_iter_rows")

    def __init__(self, cur, sql: str = "") -> None:
        object.__setattr__(self, "_cur", cur)
        object.__setattr__(self, "_sql", sql)
        object.__setattr__(self, "_iter_ms", 0.0)
        object.__setattr__(self, "_iter_rows", 0)

    # -- writes ---------------------------------------------------------
    def execute(self, sql, parameters=()):
        object.__setattr__(self, "_sql", str(sql))
        object.__setattr__(self, "_iter_ms", 0.0)
        object.__setattr__(self, "_iter_rows", 0)
        start = time.perf_counter()
        try:
            self._cur.execute(sql, parameters)
            return self
        finally:
            _maybe("execute", self._sql, (time.perf_counter() - start) * 1000.0)

    def executemany(self, sql, seq_of_parameters):
        object.__setattr__(self, "_sql", str(sql))
        start = time.perf_counter()
        try:
            self._cur.executemany(sql, seq_of_parameters)
            return self
        finally:
            _maybe("executemany", self._sql, (time.perf_counter() - start) * 1000.0)

    def executescript(self, sql_script):
        object.__setattr__(self, "_sql", str(sql_script))
        start = time.perf_counter()
        try:
            self._cur.executescript(sql_script)
            return self
        finally:
            _maybe("executescript", self._sql, (time.perf_counter() - start) * 1000.0)

    # -- reads ----------------------------------------------------------
    def fetchone(self):
        start = time.perf_counter()
        try:
            return self._cur.fetchone()
        finally:
            _maybe("fetchone", self._sql, (time.perf_counter() - start) * 1000.0)

    def fetchmany(self, size=None):
        start = time.perf_counter()
        try:
            if size is None:
                return self._cur.fetchmany()
            return self._cur.fetchmany(size)
        finally:
            _maybe("fetchmany", self._sql, (time.perf_counter() - start) * 1000.0)

    def fetchall(self):
        start = time.perf_counter()
        try:
            rows = self._cur.fetchall()
            return rows
        finally:
            ms = (time.perf_counter() - start) * 1000.0
            count = None
            try:
                count = len(rows)  # type: ignore[possibly-undefined]
            except Exception:
                count = None
            _maybe("fetchall", self._sql, ms, count)

    def __iter__(self):
        object.__setattr__(self, "_iter_ms", 0.0)
        object.__setattr__(self, "_iter_rows", 0)
        return self

    def __next__(self):
        start = time.perf_counter()
        try:
            row = next(self._cur)
        except StopIteration:
            _maybe("iterate", self._sql, self._iter_ms, self._iter_rows)
            raise
        object.__setattr__(self, "_iter_ms", self._iter_ms + (time.perf_counter() - start) * 1000.0)
        object.__setattr__(self, "_iter_rows", self._iter_rows + 1)
        return row

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_cur"), name)

    def __setattr__(self, name, value):
        if name in _TracingCursor.__slots__:
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_cur"), name, value)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._cur.__exit__(*exc)


class _TracingConnection:
    def __init__(self, conn) -> None:
        object.__setattr__(self, "_conn", conn)

    def execute(self, sql, parameters=()):
        start = time.perf_counter()
        cur = self._conn.execute(sql, parameters)
        _maybe("execute", str(sql), (time.perf_counter() - start) * 1000.0)
        return _TracingCursor(cur, str(sql))

    def executemany(self, sql, seq_of_parameters):
        start = time.perf_counter()
        cur = self._conn.executemany(sql, seq_of_parameters)
        _maybe("executemany", str(sql), (time.perf_counter() - start) * 1000.0)
        return _TracingCursor(cur, str(sql))

    def executescript(self, sql_script):
        start = time.perf_counter()
        try:
            return self._conn.executescript(sql_script)
        finally:
            _maybe("executescript", str(sql_script), (time.perf_counter() - start) * 1000.0)

    def cursor(self, *args, **kwargs):
        return _TracingCursor(self._conn.cursor(*args, **kwargs))

    def commit(self):
        start = time.perf_counter()
        try:
            return self._conn.commit()
        finally:
            _maybe("commit", "<commit>", (time.perf_counter() - start) * 1000.0)

    def rollback(self):
        start = time.perf_counter()
        try:
            return self._conn.rollback()
        finally:
            _maybe("rollback", "<rollback>", (time.perf_counter() - start) * 1000.0)

    def close(self):
        return self._conn.close()

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name, value):
        if name == "_conn":
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_conn"), name, value)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)


def is_installed() -> bool:
    return bool(getattr(sqlite3, "_ov_sql_traced", False))


def install_from_env() -> bool:
    """Wrap ``sqlite3.connect`` when ``OPENVIKING_SQL_TRACE_MS`` is set."""
    global _SINK
    if is_installed():
        return True
    if not os.environ.get("OPENVIKING_SQL_TRACE_MS"):
        return False

    _SINK = _Sink()
    real_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        return _TracingConnection(real_connect(*args, **kwargs))

    sqlite3.connect = traced_connect  # type: ignore[assignment]
    sqlite3._ov_sql_traced = True  # type: ignore[attr-defined]
    _SINK.write(
        f"# sql_trace installed pid={os.getpid()} threshold_ms={_SINK.threshold_ms} "
        f"stack_ms={_SINK.stack_ms} log={_SINK.path}\n"
    )
    return True
