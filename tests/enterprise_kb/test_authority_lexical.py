"""Actual coordinator SQLite/FTS and producer receipts, with no dense scorer."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_registry_authority import setup as setup, _start
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding, Database

from openviking.indexing.publication import PublicationStore
from openviking.indexing.registry_lexical import lexical_vocab_table
from openviking.indexing.registry_authority import AuthorityRepository
from test_wiki_build_plan import prepared as prepared
from test_wiki_namespaces import domains as domains, ONE, RAW, WIKI

URI = 'viking://resources/manual.md'
DERIVED = WIKI + '/images/pump.png'


def test_lexical_config_requires_verified_publication(embedding):
    from pydantic import ValidationError
    from openviking_cli.utils.config.embedding_config import ResourceChunkingConfig
    config = embedding.chunking.model_dump()
    config.update(lexical_enabled=True, publish_content=False)
    with pytest.raises(ValidationError, match='verified content publication'):
        ResourceChunkingConfig(**config)
    config.update(publish_content=True, publication_path='')
    with pytest.raises(ValidationError, match='publish_content requires publication_path'):
        ResourceChunkingConfig(**config)


def publish(repository, uri=URI, text='pump pump pressure', account='acct', plan='p'):
    generation = repository.begin(account, 'manuals', uri, plan, ['c', 'lexical:' + plan])
    repository.acknowledge(generation, 'c')
    repository.seal(generation)
    assert repository.state(generation)['state'] == 'queued'
    repository.lexical_write(generation, [{'uri': uri, 'chunk_id': 'c', 'chunk_text': text}])
    assert repository.state(generation)['state'] == 'published'
    return generation


def query(repository, lease, uris=(URI,), account='acct'):
    return repository.lexical_search(account, lease, list(uris), 'pump', 10)


def test_receipt_blocks_publication_then_real_authority_bm25(setup):
    process = _start(setup)
    try:
        repository = AuthorityRepository(setup[0], setup[2])
        generation = publish(repository)
        lease, _ = repository.capture('acct')
        rows = query(repository, lease)
        assert len(rows) == 1
        assert rows[0]['uri'] == URI and rows[0]['index_generation'] == generation
        assert rows[0]['lexical_provider'] == 'authority_fts5'
        assert rows[0]['chunk_text'] == 'pump pump pressure'
        repository.release(lease)
        with pytest.raises(ValueError, match='lease expired'):
            query(repository, lease)
    finally:
        process.terminate()
        process.join(15)


def test_snapshot_and_acl_exclude_hidden_content_from_bm25_statistics(tmp_path):
    repository = PublicationStore(str(tmp_path/'registry.db'))
    old = publish(repository)
    lease, _ = repository.capture('acct')
    first = query(repository, lease)
    publish(repository, uri='viking://resources/private.md', text='pump '*200, plan='private')
    publish(repository, account='other', text='pump '*300, plan='other')
    replacement = publish(repository, text='pump changed', plan='replacement')
    current, _ = repository.capture('acct')
    assert query(repository, lease) == first
    assert query(repository, lease)[0]['index_generation'] == old
    assert query(repository, current)[0]['index_generation'] == replacement
    assert query(repository, current, uris=()) == []
    with pytest.raises(ValueError, match='account mismatch'):
        query(repository, lease, account='other')
    repository.tombstone('acct', URI)
    assert query(repository, lease) == []


def test_incomplete_or_mutating_lexical_receipt_rolls_back(tmp_path):
    repository = PublicationStore(str(tmp_path/'registry.db'))
    generation = repository.begin('acct','manuals',URI,'p',['a','b','lexical:p'])
    entry = {'uri':URI,'chunk_id':'a','chunk_text':'pump'}
    with pytest.raises(ValueError,match='exactly match'):
        repository.lexical_write(generation,[entry])
    with repository.db() as db:
        assert db.execute('SELECT count(*) FROM publication_lexical').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM receipts').fetchone()[0] == 0
    entries=[entry,dict(entry,chunk_id='b')]
    repository.lexical_write(generation,entries)
    with pytest.raises(ValueError,match='Immutable'):
        repository.lexical_write(generation,[dict(entry,chunk_text='changed'),entries[1]])


def test_lexical_filter_is_applied_before_topk_and_statistics(tmp_path):
    repository = PublicationStore(str(tmp_path/'registry.db'))
    publish(repository)
    other = 'viking://resources/other.md'
    publish(repository, uri=other, text='pump '*100, plan='other')
    lease, _ = repository.capture('acct')
    only_manual = {'op':'must','field':'uri','conds':[URI],'para':'-d=0'}
    selected = repository.lexical_search('acct', lease, [URI,other], 'pump', 1, metadata_filter=only_manual)
    baseline = repository.lexical_search('acct', lease, [URI], 'pump', 1)
    assert selected == baseline and selected[0]['uri']==URI
    for bad in ({'category':'pump'}, {'op':'must','field':'unknown','conds':['pump']},
                {'op':'range','field':'updated_at','gte':'2026-01-01'}):
        with pytest.raises(ValueError, match='Unsupported|native time conversion'):
            repository.lexical_search('acct', lease, [URI,other], 'pump', 1, metadata_filter=bad)
    assert repository.lexical_search('acct', lease, [URI,other], 'pump', 1,
        metadata_filter={'op':'must','field':'uri','conds':['viking://resources/missing.md']}) == []


def test_mirror_rebuild_fills_a_pre_migration_account_and_flips_the_marker(tmp_path):
    from openviking.indexing.registry_lexical import lexical_fts_table
    repository = PublicationStore(str(tmp_path/'registry.db'))
    other = 'viking://resources/other.md'
    publish(repository, plan='p1')
    publish(repository, uri=other, text='pump pump other', plan='p2')
    # A database written before the mirror existed: rows are there, index is not.
    with repository.db() as db:
        db.execute('DELETE FROM publication_lexical_index')
        db.execute(f'DROP TABLE IF EXISTS {lexical_fts_table("acct")}')
    assert repository.lexical_index_status() == []
    built = repository.rebuild_lexical_index()
    # count(*) on an external-content FTS5 table reports the content table, so a
    # complete mirror used to be unprovable and the marker stayed incomplete.
    assert built['acct'] == {'account': 'acct', 'rows': 2, 'expected': 2, 'complete': True}
    assert [row['rows'] for row in repository.lexical_index_status()] == [2]
    lease, _ = repository.capture('acct')
    hits = query(repository, lease, uris=(URI, other))
    assert {row['uri'] for row in hits} == {URI, other}
    assert all(row['lexical_provider'] == 'authority_fts5' for row in hits)


def test_a_dropped_mirror_is_detected_and_rebuilt(tmp_path):
    from openviking.indexing.registry_lexical import lexical_fts_table
    repository = PublicationStore(str(tmp_path/'registry.db'))
    publish(repository)
    assert [row['rows'] for row in repository.lexical_index_status()] == [1]
    with repository.db() as db:
        db.execute(f'DROP TABLE {lexical_fts_table("acct")}')
    # The marker claims a mirror that is gone; the sweep must notice.
    assert repository._lexical_accounts_due(None) == ['acct']
    assert repository.rebuild_lexical_index()['acct']['complete'] is True


async def test_existing_text_producer_writes_lexical_before_vector_publication(environment, embedding):
    from openviking.storage.collection_schemas import TextEmbeddingHandler
    from openviking.utils.embedding_utils import vectorize_file
    from openviking.indexing.publication import configured_store
    e=environment
    embedding.chunking.lexical_enabled=True
    await e.fs.write_file(URI,'pump pressure at the tail',ctx=e.ctx)
    await vectorize_file(URI,{"name":"manual.md","summary":"summary"},"viking://resources",ctx=e.ctx)
    repository=configured_store()
    lease, manifest=repository.capture('acct')
    assert manifest == {}
    repository.release(lease)
    for message in e.queue.messages:
        outcome=await TextEmbeddingHandler(Database()).on_dequeue({'data':message.to_json()})
        assert outcome.outcome.value == 'success', outcome.error
    lease, manifest=repository.capture('acct')
    rows=query(repository,lease)
    assert rows and rows[0]['uri']==URI
    assert rows[0]['index_generation']==manifest[URI]['generation']


async def test_recall_rechecks_current_access_before_rerank(environment, embedding):
    from openviking.retrieve.lexical_recall import configured_lexical_recall
    from openviking.indexing.publication import configured_store
    e=environment
    embedding.chunking.lexical_enabled=True
    publish(configured_store())
    e.fs._can_access_many=AsyncMock(side_effect=[{URI:True},{URI:False}])
    recall=configured_lexical_recall(e.fs)
    assert await recall(query=SimpleNamespace(query='pump'),ctx=e.ctx,context_type='resource',target_dirs=[],scope_dsl=None,limit=10)==[]
    assert e.fs._can_access_many.await_count == 2


async def publish_derived_lexical(e):
    from openviking.indexing.content import archive_source
    await e.fs.write_file_bytes(DERIVED, b'fixture image asset bytes', ctx=e.ctx)
    reference, _ = await archive_source(e.fs, DERIVED, e.ctx)
    reference['image_context_epochs'] = {ONE: e.store.observe_draft(e.ctx.account_id, ONE)}
    generation = e.store.begin(e.ctx.account_id, 'wiki', DERIVED, 'image-plan',
                               ['image:one', 'lexical:image-plan'], source_ref=reference)
    e.store.acknowledge(generation, 'image:one')
    e.store.seal(generation)
    assert e.store.state(generation)['state'] == 'queued'
    e.store.lexical_write(generation, [{'uri': DERIVED, 'chunk_id': 'image:one',
                                        'chunk_text': 'pump assembly exploded view'}])
    assert e.store.state(generation)['state'] == 'published'


async def drain(env):
    from openviking.storage.collection_schemas import TextEmbeddingHandler
    from test_index_plan import Database
    for message in env.queue.messages:
        outcome = await TextEmbeddingHandler(Database()).on_dequeue({'data': message.to_json()})
        assert outcome.outcome.value == 'success', outcome.error


async def index_manual(e, env, embedding, body='pump pressure at the tail'):
    from openviking.utils.embedding_utils import vectorize_file
    embedding.chunking.lexical_enabled = True
    await e.fs.write(URI, body, ctx=e.ctx)
    env.queue.messages.clear()
    assert await vectorize_file(URI, {"name": "manual.md", "summary": "summary"}, "viking://resources", ctx=e.ctx)
    await drain(env)


async def test_bm25_excludes_derived_record_with_denied_contributor(domains, environment, embedding):
    from openviking.retrieve.lexical_recall import configured_lexical_recall
    e, planner, policy, admin, backend = domains
    env = environment
    await index_manual(e, env, embedding)
    await publish_derived_lexical(e)
    recall = configured_lexical_recall(e.fs)
    rows = await recall(query=SimpleNamespace(query='pump'), ctx=e.ctx,
                        context_type='resource', target_dirs=[], scope_dsl=None, limit=10)
    assert {row['uri'] for row in rows} == {URI, DERIVED}
    backend.acl(RAW, [{'principal': 'user:other', 'level': 'read'}])
    rows = await recall(query=SimpleNamespace(query='pump'), ctx=e.ctx,
                        context_type='resource', target_dirs=[], scope_dsl=None, limit=10)
    assert [row['uri'] for row in rows] == [URI]


async def test_bm25_revocation_between_authority_and_rerank_drops_cached_text(domains, environment, embedding):
    from openviking.retrieve.lexical_recall import configured_lexical_recall
    e, planner, policy, admin, backend = domains
    env = environment
    await index_manual(e, env, embedding, body='unrelated manual body text')
    await publish_derived_lexical(e)
    state = {'calls': 0}
    original = type(e.fs)._can_access_many.__get__(e.fs)

    async def access(uris, ctx, **kwargs):
        state['calls'] += 1
        if state['calls'] >= 2:
            backend.acl(RAW, [{'principal': 'user:other', 'level': 'read'}])
        return await original(uris, ctx, **kwargs)

    e.fs._can_access_many = access
    recall = configured_lexical_recall(e.fs)
    rows = await recall(query=SimpleNamespace(query='pump'), ctx=e.ctx,
                        context_type='resource', target_dirs=[], scope_dsl=None, limit=10)
    assert rows == []


async def test_real_authority_bm25_reaches_existing_retrieval_without_dense(environment, embedding):
    from test_hybrid_profile import HybridStorage
    from openviking.retrieve.hierarchical_retriever import HierarchicalRetriever, RetrieverMode
    from openviking.retrieve.lexical_recall import configured_lexical_recall
    from openviking.indexing.publication import configured_store
    from openviking_cli.retrieve.types import TypedQuery, ContextType
    e=environment
    embedding.chunking.lexical_enabled=True
    generation=publish(configured_store())
    e.fs._can_access_many=AsyncMock(return_value={URI:True})
    storage=HybridStorage([])
    storage.collection_exists_bound=AsyncMock(return_value=False)
    retriever=HierarchicalRetriever(storage=storage, embedder=None,
                                   lexical_search=configured_lexical_recall(e.fs))
    result=await retriever.retrieve(TypedQuery(query='pump',context_type=ContextType.RESOURCE,intent=''),
                                    ctx=e.ctx, mode=RetrieverMode.HYBRID)
    assert storage.search_calls == []
    assert len(result.matched_contexts)==1
    hit=result.matched_contexts[0]
    assert hit.uri == URI and hit.abstract == 'pump pump pressure'
    assert hit.matches[0]['chunk_id']=='c' and hit.matches[0]['text']=='pump pump pressure'
    assert hit.score > 0


def test_scope_index_resolves_the_authorized_scope_by_primary_key(tmp_path):
    repository = PublicationStore(str(tmp_path/'registry.db'))
    nested = 'viking://resources/manual/1_chapter.md'
    first = publish(repository, plan='p1')
    second = publish(repository, uri=nested, text='pump pump pump nested', plan='p2')
    with repository.db(readonly=True) as db:
        scope = {(row['uri'], row['generation']): row['doc_len'] for row in db.execute(
            'SELECT uri, generation, doc_len FROM publication_lexical_scope')}
        lengths = dict(db.execute('SELECT uri, length(tokens) FROM publication_lexical'))
    assert set(scope) == {(URI, first), (nested, second)}
    # The write path and the rebuild must agree on the ranking inputs, otherwise a
    # score would depend on which one filled the scope index.
    assert scope == {key: lengths[key[0]] for key in scope}
    lease, _ = repository.capture('acct')
    assert {row['uri'] for row in query(repository, lease, uris=(URI, nested))} == {URI, nested}
    # A scope index that has not been rebuilt yet must never answer with an empty
    # result: searching falls back to the whole-scope scan instead.
    with repository.db() as db:
        db.execute('DELETE FROM publication_lexical_scope')
    hits = query(repository, lease, uris=(URI, nested))
    assert {row['uri'] for row in hits} == {URI, nested}
    # The fallback is correct but takes minutes on a tree-scoped corpus, so it
    # announces itself: this result must not be mistaken for the indexed path.
    assert all(row['lexical_provider'] == 'authority_fts5_scan' for row in hits)


def test_scope_index_keeps_every_chunk_of_one_uri(tmp_path):
    """One URI owns one scope row per published chunk, not one row total.

    Keying the scope index by (account, uri, generation) alone collapses a
    document's chunks into a single row (``INSERT OR REPLACE``), so a scope join
    would hand the ranker only the last chunk of every file -- silently
    truncated results. Measured on the live corpus: 318,573 lexical rows became
    76,094 scope rows.
    """
    repository = PublicationStore(str(tmp_path/'registry.db'))
    generation = repository.begin('acct', 'manuals', URI, 'p1', ['c1', 'c2', 'c3', 'lexical:p1'])
    for child in ('c1', 'c2', 'c3'):
        repository.acknowledge(generation, child)
    repository.seal(generation)
    repository.lexical_write(generation, [
        {'uri': URI, 'chunk_id': 'c1', 'chunk_text': 'pump inlet pressure'},
        {'uri': URI, 'chunk_id': 'c2', 'chunk_text': 'pump outlet pressure'},
        {'uri': URI, 'chunk_id': 'c3', 'chunk_text': 'pump relief valve'},
    ])
    with repository.db(readonly=True) as db:
        scope = [row[0] for row in db.execute(
            'SELECT rowid FROM publication_lexical_scope WHERE account=? AND uri=?', ('acct', URI))]
        lexical = db.execute(
            "SELECT COUNT(*) FROM publication_lexical WHERE json_extract(payload,'$.uri')=?",
            (URI,)).fetchone()[0]
    assert lexical == 3
    assert len(scope) == 3 and len(set(scope)) == 3
    lease, _ = repository.capture('acct')
    for term in ('inlet', 'outlet', 'relief'):
        hits = repository.lexical_search('acct', lease, [URI], term, 10)
        assert hits, f'the {term} chunk is missing from the scope index'


def test_scoped_search_uses_the_mirror_not_the_fallback_scan(tmp_path):
    """A rebuilt account must rank through the persistent index, not the scan.

    The whole-scope scan materialises and re-tokenizes the corpus on every
    query (minutes for a tree scope), so a silent downgrade is a production
    outage rather than a slow path: the vocabulary view has to exist next to the
    mirror and be readable without a write transaction. Observed as a 627s
    tree-scoped ``find`` while narrow queries stayed fast.
    """
    repository = PublicationStore(str(tmp_path/'registry.db'))
    publish(repository, plan='p1')
    vocab = lexical_vocab_table('acct')
    with repository.db(readonly=True) as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                          (vocab,)).fetchone() is not None
    lease, _ = repository.capture('acct')
    assert [row['lexical_provider'] for row in query(repository, lease)] == ['authority_fts5']
    # A database upgraded from before the view existed has the mirror only: the
    # query must still answer, and must say that it answered the slow way.
    with repository.db() as db:
        db.execute(f'DROP TABLE {vocab}')
    assert [row['lexical_provider'] for row in query(repository, lease)] == ['authority_fts5_scan']
    # init_lexical recreates the view for an existing mirror, restoring the fast
    # path without a full rebuild.
    repository.init_lexical()
    assert [row['lexical_provider'] for row in query(repository, lease)] == ['authority_fts5']


def test_scope_filter_treats_a_denied_root_as_covering_its_subtree():
    """A tombstoned directory hides its subtree, not its name-siblings."""
    from openviking.indexing.registry_lexical import _under_denied_root

    denied = {"viking://resources/raw/menus/dead"}
    assert _under_denied_root("viking://resources/raw/menus/dead", denied)
    assert _under_denied_root("viking://resources/raw/menus/dead/a/b.md", denied)
    assert not _under_denied_root("viking://resources/raw/menus/deadline/a.md", denied)
    assert not _under_denied_root("viking://resources/raw/menus", denied)
    assert not _under_denied_root("viking://resources/raw/menus/a.md", set())


def test_scope_filter_stays_linear_in_the_number_of_roots():
    """131k authorized URIs against 59k tombstones must not cost minutes.

    The previous nested scan was O(authorized x denied): 8e9 string compares on
    the enterprise corpus, i.e. the 318-second read transaction measured on a
    tree-scoped search while every SQL statement inside it ran in under a second.
    """
    import time

    from openviking.indexing.registry_lexical import _under_denied_root

    denied = {f"viking://resources/raw/g{i}" for i in range(10_000)}
    authorized = [f"viking://resources/raw/g{i % 20_000}/a/b/c/doc{i}.md" for i in range(20_000)]
    started = time.perf_counter()
    hit = [uri for uri in authorized if _under_denied_root(uri, denied)]
    elapsed = time.perf_counter() - started
    assert len(hit) == 10_000
    assert elapsed < 5.0, f"scope filter took {elapsed:.1f}s; the ancestor-set path regressed"


def test_superseded_lexical_rows_are_pruned_with_their_index_entries(tmp_path):
    """A reindex writes a whole new lexical copy; the old copy must not be forever.

    Live: ``publication_lexical`` reached 717,810 rows / 9.1 GB inside a 13.6 GB
    store because nothing ever removed the copy belonging to a superseded
    generation, so every pass over a tree grew the store again.
    """
    import time

    from openviking.indexing.registry_lexical import lexical_fts_table

    repository = PublicationStore(str(tmp_path / 'registry.db'))
    first = publish(repository, plan='p1', text='pump pump pressure')
    second = publish(repository, plan='p2', text='valve valve torque')

    with repository.db() as db:
        # The newer publish moved ``desired`` on; age the old build past the floor.
        db.execute('UPDATE builds SET created=? WHERE generation=?',
                   (time.time() - 2 * 86400.0, first))
        assert db.execute('SELECT generation FROM desired').fetchone()[0] == second
        assert db.execute('SELECT COUNT(*) FROM publication_lexical').fetchone()[0] == 2

    assert repository.prune_superseded_lexical(86400.0, 100) == 1

    mirror = lexical_fts_table('acct')
    with repository.db(readonly=True) as db:
        survivors = [row[0] for row in db.execute('SELECT generation FROM publication_lexical')]
        scoped = db.execute('SELECT COUNT(*) FROM publication_lexical_scope').fetchone()[0]
        marked = db.execute(
            "SELECT rows FROM publication_lexical_index WHERE account='acct'").fetchone()[0]
        postings = db.execute(f'SELECT COUNT(*) FROM {mirror}').fetchone()[0]
    assert survivors == [second]
    assert (scoped, marked) == (1, 1)
    # The mirror is external-content and has no bulk delete, so its posting for
    # the pruned row survives until the next rebuild -- inert, because search
    # resolves candidates from the scope index first.
    assert postings >= 1

    lease, _ = repository.capture('acct')
    rows = repository.lexical_search('acct', lease, [URI], 'valve', 10)
    assert [row['index_generation'] for row in rows] == [second]
    # The pruned generation is unreachable even by its own term.
    assert repository.lexical_search('acct', lease, [URI], 'pump', 10) == []
    repository.release(lease)


def test_lexical_prune_keeps_a_generation_an_active_lease_can_still_read(tmp_path):
    """A captured snapshot pins its lexical rows until the lease expires."""
    import time

    repository = PublicationStore(str(tmp_path / 'registry.db'))
    first = publish(repository, plan='p1')
    publish(repository, plan='p2')
    lease, _ = repository.capture('acct')  # a reader is mid-query on the old head
    with repository.db() as db:
        db.execute('UPDATE builds SET created=? WHERE generation=?',
                   (time.time() - 2 * 86400.0, first))
    assert repository.prune_superseded_lexical(86400.0, 100) == 0
    with repository.db(readonly=True) as db:
        assert db.execute('SELECT COUNT(*) FROM publication_lexical').fetchone()[0] == 2
    repository.release(lease)
