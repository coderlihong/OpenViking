"""The publication write lock serializes every writer; slow holders must be visible.

A transaction that keeps the lock for seconds stalls receipts, leases and
publishes behind it, which is what surfaced as multi-second reindex stalls and
"database is locked" spurious failures.  These tests pin the diagnostic that
names the caller instead of guessing from the outside.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager

from openviking.indexing import publication
from openviking.indexing.publication import PublicationStore


def test_slow_tx_threshold_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("OPENVIKING_SLOW_TX_MS", raising=False)
    assert publication._slow_tx_threshold_ms() == 500.0
    monkeypatch.setenv("OPENVIKING_SLOW_TX_MS", "25")
    assert publication._slow_tx_threshold_ms() == 25.0
    monkeypatch.setenv("OPENVIKING_SLOW_TX_MS", "-4")
    assert publication._slow_tx_threshold_ms() == 0.0
    monkeypatch.setenv("OPENVIKING_SLOW_TX_MS", "nonsense")
    assert publication._slow_tx_threshold_ms() == 500.0


class _Capture(logging.Handler):
    """The project configures its own logging handlers, so capture directly."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())

    def slow(self):
        return [m for m in self.messages if "slow publication transaction" in m]


@contextmanager
def _captured():
    handler = _Capture()
    publication.logger.addHandler(handler)
    try:
        yield handler
    finally:
        publication.logger.removeHandler(handler)


def test_slow_transaction_names_its_caller(tmp_path, monkeypatch):
    store = PublicationStore(str(tmp_path / "db"))
    monkeypatch.setenv("OPENVIKING_SLOW_TX_MS", "1")
    with _captured() as captured:
        with store.db():
            time.sleep(0.05)
    messages = captured.slow()
    assert messages, "a 50ms transaction was not reported"
    assert "test_slow_transaction_names_its_caller" in messages[-1]
    assert "readonly=False" in messages[-1]


def test_readonly_transaction_is_reported_as_readonly(tmp_path, monkeypatch):
    store = PublicationStore(str(tmp_path / "db"))
    monkeypatch.setenv("OPENVIKING_SLOW_TX_MS", "1")
    with _captured() as captured:
        with store.db(readonly=True) as db:
            time.sleep(0.05)
            db.execute("SELECT 1").fetchone()
    messages = captured.slow()
    assert messages and "readonly=True" in messages[-1]


def test_fast_transaction_stays_quiet(tmp_path, monkeypatch):
    store = PublicationStore(str(tmp_path / "db"))
    monkeypatch.setenv("OPENVIKING_SLOW_TX_MS", "60000")
    with _captured() as captured:
        with store.db() as db:
            db.execute("SELECT 1").fetchone()
    assert not captured.slow()


def test_disabled_threshold_silences_the_report(tmp_path, monkeypatch):
    store = PublicationStore(str(tmp_path / "db"))
    monkeypatch.setenv("OPENVIKING_SLOW_TX_MS", "0")
    with _captured() as captured:
        with store.db():
            time.sleep(0.05)
    assert not captured.slow()
