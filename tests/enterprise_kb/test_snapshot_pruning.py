"""Unreachable manifest snapshots must not accumulate forever.

Every publication writes a complete gzipped manifest (~11MB for the enterprise
corpus) while ``heads`` only ever points at the newest one per account, so the
live store reached 1,497 snapshots / 30GB of manifest text of which 1,494 were
unreachable history.  ``prune_snapshots`` drops exactly those: no head, no
unexpired lease, older than the grace period.
"""
from openviking.indexing.publication import PublicationStore


def publish(store, uri, plan):
    generation = store.begin('acct', 'manuals', uri, plan, ['c', 'lexical:' + plan])
    store.acknowledge(generation, 'c')
    store.seal(generation)
    store.lexical_write(generation, [{'uri': uri, 'chunk_id': 'c', 'chunk_text': 'pump'}])
    assert store.state(generation)['state'] == 'published'
    return generation


def snapshots(store):
    with store.db(readonly=True) as db:
        return [row['id'] for row in db.execute('SELECT id FROM snapshots')]


def head(store):
    with store.db(readonly=True) as db:
        return db.execute('SELECT snapshot FROM heads').fetchone()['snapshot']


def test_only_unreachable_snapshots_are_pruned(tmp_path):
    store = PublicationStore(str(tmp_path / 'registry.db'))
    publish(store, 'viking://resources/a.md', 'a')
    publish(store, 'viking://resources/b.md', 'b')
    publish(store, 'viking://resources/c.md', 'c')
    assert len(snapshots(store)) == 3
    # Nothing is pinned and only the head is readable, so the two older
    # snapshots are history no reader can reach.
    assert store.prune_snapshots(0.0) == 2
    assert snapshots(store) == [head(store)]
    # A second call has nothing left to drop: pruning is idempotent.
    assert store.prune_snapshots(0.0) == 0


def test_the_grace_period_protects_fresh_snapshots(tmp_path):
    store = PublicationStore(str(tmp_path / 'registry.db'))
    publish(store, 'viking://resources/a.md', 'a')
    publish(store, 'viking://resources/b.md', 'b')
    assert store.prune_snapshots(3600.0) == 0
    assert len(snapshots(store)) == 2


def test_pruning_is_bounded_per_call(tmp_path):
    store = PublicationStore(str(tmp_path / 'registry.db'))
    for index in range(5):
        publish(store, f'viking://resources/{index}.md', f'p{index}')
    assert store.prune_snapshots(0.0, limit=2) == 2
    assert len(snapshots(store)) == 3
    assert store.prune_snapshots(0.0, limit=2) == 2
    assert snapshots(store) == [head(store)]


def test_an_unexpired_lease_pins_the_snapshot_it_captured(tmp_path):
    store = PublicationStore(str(tmp_path / 'registry.db'))
    publish(store, 'viking://resources/a.md', 'a')
    first = head(store)
    lease, _ = store.capture('acct')
    publish(store, 'viking://resources/b.md', 'b')
    # The first snapshot stopped being the head, but the reader holding the
    # lease is still allowed to finish against it.
    assert first in snapshots(store)
    assert store.prune_snapshots(0.0) == 0
    store.release(lease)
    # With the lease gone the stale snapshot is unreachable and goes.
    assert store.prune_snapshots(0.0) == 1
    assert snapshots(store) == [head(store)]



def test_settled_build_rows_are_pruned_but_the_newest_failure_is_kept(tmp_path):
    """``builds`` is append-only history; only its newest row per URI is read.

    Live: 653,922 rows / 337MB, and every pass over a tree adds a superseded row
    per record. The acceptance gate, the interrupted-generation scan and the
    startup resume all read the newest row per URI, so an older settled row is
    droppable -- while a newest ``failed`` row must survive, because that is
    exactly what the resume pass retries.
    """
    import time

    store = PublicationStore(str(tmp_path / 'registry.db'))
    now = time.time()
    with store.db() as db:
        rows = [
            # superseded by a newer published row: droppable
            ('g1', 'viking://resources/x.md', 'failed', now - 10 * 86400.0),
            ('g2', 'viking://resources/x.md', 'published', now - 9 * 86400.0),
            # newest for its URI: the resume pass still needs it
            ('g3', 'viking://resources/y.md', 'failed', now - 10 * 86400.0),
            # settled, but inside the retention window
            ('g4', 'viking://resources/z.md', 'superseded', now - 60.0),
            ('g5', 'viking://resources/z.md', 'published', now - 50.0),
        ]
        for generation, uri, state, created in rows:
            db.execute(
                'INSERT INTO builds(generation,account,namespace,uri,plan,expected,state,created,error)'
                " VALUES (?,'acct','manuals',?,'p','[]',?,?,NULL)",
                (generation, uri, state, created),
            )

    assert store.prune_settled_builds(7 * 86400.0, 100) == 1
    with store.db(readonly=True) as db:
        left = [row[0] for row in db.execute('SELECT generation FROM builds ORDER BY generation')]
    assert left == ['g2', 'g3', 'g4', 'g5']


def test_newest_generation_lookup_has_an_index(tmp_path):
    """The gate and the retention prune ask "newest row per URI" on every call."""
    store = PublicationStore(str(tmp_path / 'registry.db'))
    with store.db(readonly=True) as db:
        names = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='builds'")}
    assert 'builds_uri_created' in names


def test_retired_generations_exclude_the_live_one(tmp_path):
    """Orphan-vector reclamation must never touch a generation still in use.

    Live: 530,826 of 714,532 generations were retired and their 957,757 vectors
    were 73.5% of a 78GB store -- and, since retrieval never filters by
    generation, they were still searchable next to the current record of the
    same URI.
    """
    import time

    store = PublicationStore(str(tmp_path / 'registry.db'))
    now = time.time()
    with store.db() as db:
        rows = [
            ('live', 'viking://resources/a.md', 'published', 10 * 86400.0),
            ('retired', 'viking://resources/b.md', 'published', 10 * 86400.0),
            ('recent', 'viking://resources/c.md', 'superseded', 60.0),
            ('inflight', 'viking://resources/d.md', 'queued', 10 * 86400.0),
        ]
        for generation, uri, state, age in rows:
            db.execute(
                'INSERT INTO builds(generation,account,namespace,uri,plan,expected,state,created,error)'
                " VALUES (?,'acct','manuals',?,'p','[]',?,?,NULL)",
                (generation, uri, state, now - age),
            )
        db.execute("INSERT INTO desired VALUES ('acct','manuals','viking://resources/a.md','live')")

    # The live generation, a generation inside the grace period and an in-flight
    # build are all left alone; only the settled, undemanded one is reclaimable.
    assert [row['generation'] for row in store.retired_generations(86400.0, 100)] == ['retired']
    assert [row['uri'] for row in store.retired_generations(86400.0, 100)] == [
        'viking://resources/b.md'
    ]

    # Nothing is reclaimable while the whole scan is inside the grace period.
    assert store.retired_generations(30 * 86400.0, 100) == []


def test_published_chunks_expose_the_revision_the_reuse_path_compares(tmp_path):
    """The vector stage skips a file whose revision the live generation already has.

    ``content_revision`` is the sha256 of the source text, so this lookup is what
    lets an unchanged file avoid a fresh embedding call -- live, that took a
    no-change reindex of one tree from 1,117 embeddings to 8.
    """
    store = PublicationStore(str(tmp_path / 'registry.db'))
    uri = 'viking://resources/a.md'
    generation = store.begin('acct', 'manuals', uri, 'p', ['c', 'lexical:p'])
    store.acknowledge(generation, 'c')
    store.seal(generation)
    store.lexical_write(generation, [{
        'uri': uri, 'chunk_id': 'c', 'chunk_text': 'pump', 'content_revision': 'rev-1',
    }])

    assert store.published_chunks('acct', uri) == (generation, [('c', 'rev-1')])
    # No live generation and no published chunks both mean "cannot reuse".
    assert store.published_chunks('acct', 'viking://resources/missing.md') is None
