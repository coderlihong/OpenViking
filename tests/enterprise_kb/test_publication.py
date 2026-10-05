"""Durable publication, producer/consumer integration, and fixed query views."""

from types import SimpleNamespace

import pytest
from test_index_plan import SOURCE, URI, Database, Embedder, Queue
from test_index_plan import embedding as embedding

from openviking.indexing.enqueue import enqueue_text_plan
from openviking.indexing.publication import (
    PublicationStore,
    configured_store,
    query_snapshot,
    visible_filter,
)
from openviking.server.identity import RequestContext, Role
from openviking.storage.collection_schemas import TextEmbeddingHandler
from openviking.storage.queuefs.embedding_msg import EmbeddingMsg
from openviking.storage.vector_ids import vector_record_id_for_data
from openviking_cli.session.user_id import UserIdentifier


def publish(store, uri, children=("a", "b"), account="acct", namespace="ns"):
    generation = store.begin(account, namespace, uri, "plan", children)
    store.seal(generation)
    for child in children:
        store.acknowledge(generation, child)
    return generation


def view(store, account="acct"):
    lease, manifest = store.capture(account)
    store.release(lease)
    return manifest


def test_no_visibility_before_seal_and_all_unique_receipts(tmp_path):
    store = PublicationStore(str(tmp_path / "publication.db"))
    generation = store.begin("acct", "ns", URI, "plan", ["a", "b"])
    assert not store.acknowledge(generation, "a")
    assert not store.acknowledge(generation, "a")
    assert not view(store)
    assert not store.acknowledge(generation, "b")
    assert not view(store)
    assert store.seal(generation)
    assert view(store)[URI]["generation"] == generation
    assert store.state(generation)["state"] == "published"
    assert not store.acknowledge(generation, "a")
    with pytest.raises(ValueError, match="Unexpected child"):
        store.acknowledge(generation, "unplanned")


def test_latest_attempt_fenced_and_failed_replacement_keeps_old(tmp_path):
    path = str(tmp_path / "publication.db")
    store = PublicationStore(path)
    old = publish(store, URI)
    slow = store.begin("acct", "ns", URI, "slow", ["s"])
    store.seal(slow)
    newest = store.begin("acct", "ns", URI, "newest", ["n"])
    store.seal(newest)
    assert not store.acknowledge(slow, "s")
    assert store.state(slow)["state"] == "superseded"
    store.fail(newest, "model timeout")
    assert not store.acknowledge(newest, "n")
    assert view(PublicationStore(path))[URI]["generation"] == old
    base = {"uri": URI, "account_id": "acct", "chunk_id": "same"}
    assert vector_record_id_for_data(dict(base, index_generation=old)) != vector_record_id_for_data(
        dict(base, index_generation=newest)
    )


def test_concurrent_document_publication_keeps_both_and_account_isolation(tmp_path):
    store = PublicationStore(str(tmp_path / "publication.db"))
    a = store.begin("acct", "ns", "viking://resources/a.md", "planA", ["a"])
    b = store.begin("acct", "ns", "viking://resources/b.md", "planB", ["b"])
    store.seal(a)
    store.seal(b)
    store.acknowledge(a, "a")
    store.acknowledge(b, "b")
    assert len(view(store)) == 2
    publish(store, URI, account="other")
    assert URI not in view(store)
    assert list(view(store, "other")) == [URI]


def test_old_query_lease_protects_retired_generation_and_view(tmp_path):
    store = PublicationStore(str(tmp_path / "publication.db"))
    old = publish(store, URI)
    lease, frozen = store.capture("acct")
    new = publish(store, URI)
    assert frozen[URI]["generation"] == old
    assert view(store)[URI]["generation"] == new
    assert old not in {r["generation"] for r in store.retired_candidates(grace_seconds=0)}
    store.release(lease)
    assert old in {r["generation"] for r in store.retired_candidates(grace_seconds=0)}
    assert new not in {r["generation"] for r in store.retired_candidates(grace_seconds=0)}


@pytest.fixture
def runtime(embedding, tmp_path, monkeypatch):
    embedding.chunking.publication_path = str(tmp_path / "publication.db")
    config = SimpleNamespace(
        embedding=SimpleNamespace(
            dimension=2,
            get_embedder=lambda: Embedder(),
            chunking=embedding.chunking,
            circuit_breaker=SimpleNamespace(
                failure_threshold=5, reset_timeout=60, max_reset_timeout=600
            ),
        ),
        storage=SimpleNamespace(vectordb=SimpleNamespace(name="context")),
    )
    monkeypatch.setattr("openviking_cli.utils.config.get_openviking_config", lambda: config)
    monkeypatch.setattr(
        "openviking_cli.utils.config.open_viking_config.OpenVikingConfigSingleton._instance", config
    )
    return configured_store(embedding)


async def test_real_consumer_ack_publication_and_duplicate_message(runtime, embedding):
    queue = Queue()
    db = Database()
    handler = TextEmbeddingHandler(db)
    base = EmbeddingMsg(
        SOURCE,
        {"uri": URI, "account_id": "acct", "context_type": "resource", "level": 2, "is_leaf": True},
    )
    n = await enqueue_text_plan(base, SOURCE, embedding, queue)
    generation = queue.messages[0].context_data["index_generation"]
    for msg in queue.messages[:-1]:
        result = await handler.on_dequeue({"data": msg.to_json()})
        assert result.outcome.value == "success", result.error
    assert not view(runtime)
    result = await handler.on_dequeue({"data": queue.messages[-1].to_json()})
    assert result.outcome.value == "success", result.error
    assert view(runtime)[URI]["generation"] == generation
    assert len(db.records) == n
    result = await handler.on_dequeue({"data": queue.messages[-1].to_json()})
    assert result.outcome.value == "success" and len(db.records) == n


async def test_cancel_and_false_write_cannot_publish(runtime, embedding):
    class RejectDB(Database):
        async def upsert(self, data, ctx=None, options=None):
            return None

    queue = Queue()
    base = EmbeddingMsg(
        SOURCE, {"uri": URI, "account_id": "acct", "context_type": "resource", "level": 2}
    )
    await enqueue_text_plan(base, SOURCE, embedding, queue)
    generation = queue.messages[0].context_data["index_generation"]
    result = await TextEmbeddingHandler(RejectDB()).on_dequeue(
        {"data": queue.messages[0].to_json()}
    )
    assert result.outcome.value == "failed"
    assert runtime.state(generation)["state"] == "failed"
    assert not view(runtime)
    queue = Queue()
    await enqueue_text_plan(base, SOURCE, embedding, queue)
    generation = queue.messages[0].context_data["index_generation"]
    handler = TextEmbeddingHandler(Database())
    result = await handler.on_cancelled({"data": queue.messages[0].to_json()})
    assert result.outcome.value == "cancelled"
    assert runtime.state(generation)["state"] == "failed"
    for msg in queue.messages:
        await handler.on_dequeue({"data": msg.to_json()})
    assert not view(runtime)


async def test_public_query_pins_view_through_concurrent_publication(runtime):
    old = publish(runtime, URI, namespace="resources")

    class Reader:
        @query_snapshot
        async def read(self, *, ctx):
            before = visible_filter(ctx)
            publish(runtime, URI, namespace="resources")
            after = visible_filter(ctx)
            return before, after

    ctx = SimpleNamespace(account_id="acct")
    before, after = await Reader().read(ctx=ctx)
    assert before == after
    assert old in repr(before)
    assert old not in repr(visible_filter(ctx))


def test_compiled_prefilter_excludes_staged_legacy_and_remapped_rows(tmp_path):
    from openviking.storage.vectordb.index.cuvs_index import matches_filter
    from openviking.storage.vectordb_adapters.local_adapter import LocalCollectionAdapter

    store = PublicationStore(str(tmp_path / "publication.db"))
    old = publish(store, URI)
    new = publish(store, URI)
    adapter = object.__new__(LocalCollectionAdapter)
    compiled = adapter._compile_filter(store.visibility(view(store)))

    def matches(uri, generation):
        fields = {"uri": adapter._encode_uri_field_value(uri), "index_generation": generation}
        return matches_filter(fields, compiled, {"uri": "path", "index_generation": "string"})

    assert matches(URI, new)
    assert not matches(URI, old)
    assert not matches(URI, "")
    assert not matches("viking://resources/copied.md", new)
    assert matches("viking://resources/untouched.md", "")
    assert not matches("viking://resources/untouched.md", "staged")


async def test_enqueue_failure_cannot_publish_successful_early_receipts(runtime, embedding):
    handler = TextEmbeddingHandler(Database())

    class EarlyQueue(Queue):
        async def enqueue(self, msg):
            self.messages.append(msg)
            if len(self.messages) == 2:
                raise RuntimeError("queue storage unavailable")
            result = await handler.on_dequeue({"data": msg.to_json()})
            assert result.outcome.value == "success", result.error
            assert not view(runtime)
            return msg.id

    queue = EarlyQueue()
    base = EmbeddingMsg(
        SOURCE, {"uri": URI, "account_id": "acct", "context_type": "resource", "level": 2}
    )
    with pytest.raises(RuntimeError):
        await enqueue_text_plan(base, SOURCE, embedding, queue)
    generation = queue.messages[0].context_data["index_generation"]
    assert runtime.state(generation)["state"] == "failed"
    assert not view(runtime)


async def test_backend_filters_generation_before_top_k(runtime):
    from openviking.storage.expr import Eq
    from openviking.storage.vectordb.index.cuvs_index import matches_filter
    from openviking.storage.vectordb_adapters.local_adapter import LocalCollectionAdapter
    from openviking.storage.viking_vector_index_backend import VikingVectorIndexBackend

    active = publish(runtime, URI, namespace="resources")
    pending = runtime.begin("acct", "resources", URI, "future", ["x"])
    runtime.seal(pending)
    adapter = object.__new__(LocalCollectionAdapter)
    candidates = [
        {"uri": URI, "index_generation": pending, "account_id": "acct", "score": 1.0},
        {"uri": URI, "index_generation": active, "account_id": "acct", "score": 0.5},
        {"uri": URI, "index_generation": active, "account_id": "other", "score": 0.99},
    ]

    async def query(**kwargs):
        compiled = adapter._compile_filter(kwargs["filter"])
        eligible = [
            row
            for row in candidates
            if matches_filter(
                {**row, "uri": adapter._encode_uri_field_value(row["uri"])},
                compiled,
                {"uri": "path", "index_generation": "string", "account_id": "string"},
            )
        ]
        return sorted(eligible, key=lambda row: row["score"], reverse=True)[: kwargs["limit"]]

    backend = object.__new__(VikingVectorIndexBackend)
    backend.query = query
    ctx = RequestContext(user=UserIdentifier("acct", "user"), role=Role.USER)
    result = await backend.search(
        query_vector=[1.0, 0.0], filter=Eq("account_id", "acct"), limit=1, ctx=ctx
    )
    assert len(result) == 1 and result[0]["index_generation"] == active
