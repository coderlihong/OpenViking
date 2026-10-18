"""Dead unbacked write fences must not block a URI's directory generation forever.

`source_write` (openviking/indexing/content.py:167) leaves an `uncertain` fence
whenever a write fails or is cancelled.  With no `source_write_refs` row nothing
can verify terminality, so before this auto-settle such a fence silently disabled
every later directory generation for that URI (observed: 74 failed records on an
otherwise complete manual, 154 new fences in two minutes of fleet reindex).
"""

import time

import pytest

from openviking.indexing.publication import (
    AUTO_SETTLE_FENCE_ACTOR,
    AUTO_SETTLE_FENCE_SECONDS,
    AUTO_SETTLE_WRITING_SECONDS,
    PublicationStore,
)
from openviking_cli.exceptions import FailedPreconditionError

ACCOUNT = "acct"
URI = "viking://resources/manual/a.md"


def _fence(store, *, age_seconds=0.0, state="uncertain", backed=False):
    token = store.begin_write(ACCOUNT, URI)
    if backed:
        with store.db() as db:
            db.execute("INSERT INTO source_write_refs VALUES (?,?,?,?)", (token, "fs", "op-1", "{}"))
    if state == "uncertain":
        store.uncertain_write(token)
    with store.db() as db:
        db.execute("UPDATE source_writes SET created=? WHERE id=?", (time.time() - age_seconds, token))
    return token


def _state(store):
    with store.db(readonly=True) as db:
        fences = [dict(row) for row in db.execute("SELECT * FROM source_writes")]
        clears = [dict(row) for row in db.execute("SELECT * FROM source_fence_clears")]
        epoch = db.execute("SELECT epoch FROM drafts WHERE account=? AND uri=?", (ACCOUNT, URI)).fetchone()
    return fences, clears, (epoch["epoch"] if epoch else None)


def test_fresh_uncertain_fence_still_blocks(tmp_path):
    store = PublicationStore(str(tmp_path / "db"))
    _fence(store, age_seconds=1.0)

    with pytest.raises(FailedPreconditionError, match="WRITE_RECOVERY_REQUIRED"):
        store.observe_draft(ACCOUNT, URI)

    fences, clears, _ = _state(store)
    assert len(fences) == 1 and not clears


def test_old_unbacked_fence_is_auto_settled(tmp_path):
    store = PublicationStore(str(tmp_path / "db"))
    _fence(store, age_seconds=AUTO_SETTLE_FENCE_SECONDS + 1.0)

    # begin_write bumped the draft epoch to 1; settling bumps it again to 2.
    assert store.observe_draft(ACCOUNT, URI) == 2  # generation may proceed

    fences, clears, epoch = _state(store)
    assert fences == []
    assert len(clears) == 1
    assert clears[0]["actor"] == AUTO_SETTLE_FENCE_ACTOR
    assert "unbacked uncertain fence" in clears[0]["reason"]
    assert clears[0]["confirmed"] is not None
    assert epoch == 2


def test_backed_fence_is_never_auto_settled(tmp_path):
    store = PublicationStore(str(tmp_path / "db"))
    _fence(store, age_seconds=AUTO_SETTLE_FENCE_SECONDS * 10, backed=True)

    with pytest.raises(FailedPreconditionError, match="WRITE_RECOVERY_REQUIRED"):
        store.observe_draft(ACCOUNT, URI)

    fences, clears, _ = _state(store)
    assert len(fences) == 1 and not clears


def test_young_writing_fence_still_blocks(tmp_path):
    store = PublicationStore(str(tmp_path / "db"))
    _fence(store, age_seconds=30.0, state="writing")

    with pytest.raises(FailedPreconditionError, match="WRITE_RECOVERY_REQUIRED"):
        store.observe_draft(ACCOUNT, URI)

    fences, clears, _ = _state(store)
    assert len(fences) == 1 and not clears


def test_abandoned_writing_fence_is_reclaimed(tmp_path):
    """A crashed writer leaves 'writing' behind; it must not be immortal.

    Live evidence: 165 unbacked 'writing' rows aged ~6h blocked every directory
    generation for their URIs (7 failed records on an otherwise complete manual).
    """
    store = PublicationStore(str(tmp_path / "db"))
    _fence(store, age_seconds=AUTO_SETTLE_WRITING_SECONDS + 1.0, state="writing")

    # begin_write bumped the draft epoch to 1; settling bumps it again to 2.
    assert store.observe_draft(ACCOUNT, URI) == 2  # generation may proceed

    fences, clears, epoch = _state(store)
    assert fences == []
    assert len(clears) == 1
    assert clears[0]["actor"] == AUTO_SETTLE_FENCE_ACTOR
    assert epoch == 2


def test_one_live_row_keeps_the_whole_uri_fenced(tmp_path):
    """A live 'writing' row next to a dead one must not be settled away."""
    store = PublicationStore(str(tmp_path / "db"))
    _fence(store, age_seconds=AUTO_SETTLE_FENCE_SECONDS * 10)
    _fence(store, age_seconds=0.5, state="writing")

    with pytest.raises(FailedPreconditionError, match="WRITE_RECOVERY_REQUIRED"):
        store.observe_draft(ACCOUNT, URI)

    fences, clears, _ = _state(store)
    assert len(fences) == 2 and not clears


def _stamp_owner(store, token, owner):
    with store.db() as db:
        db.execute("UPDATE source_writes SET owner=? WHERE id=?", (owner, token))


def test_fence_from_a_dead_process_generation_settles_at_once(tmp_path):
    """A restart must not cost a run its whole time window.

    Live: a restart left 26 fences and the run that hit them was far inside
    ``AUTO_SETTLE_FENCE_SECONDS``, so every directory generation under those URIs
    failed with WRITE_RECOVERY_REQUIRED and 1,068 image summaries were deferred.
    One server writes a data directory at a time, so a fence stamped by another
    generation is provably dead and needs no grace period.
    """
    store = PublicationStore(str(tmp_path / "db"))
    token = _fence(store, age_seconds=0.0)
    _stamp_owner(store, token, "1:1")  # a generation that has already exited

    # begin_write bumped the draft epoch to 1; settling bumps it again to 2.
    assert store.observe_draft(ACCOUNT, URI) == 2  # generation may proceed at once

    fences, clears, epoch = _state(store)
    assert fences == []
    assert len(clears) == 1
    assert clears[0]["actor"] == AUTO_SETTLE_FENCE_ACTOR
    assert epoch == 2


def test_fence_from_this_generation_keeps_the_time_window(tmp_path):
    """The same fresh fence must still block while its own writer may be live."""
    store = PublicationStore(str(tmp_path / "db"))
    _fence(store, age_seconds=0.0)

    with pytest.raises(FailedPreconditionError, match="WRITE_RECOVERY_REQUIRED"):
        store.observe_draft(ACCOUNT, URI)

    fences, clears, _ = _state(store)
    assert len(fences) == 1 and not clears
