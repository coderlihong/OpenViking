"""Actual coordinator SQLite/FTS and producer receipts, with no dense scorer."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_registry_authority import setup as setup, _start
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding, Database

from openviking.indexing.publication import PublicationStore
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
