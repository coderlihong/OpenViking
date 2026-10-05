"""Real source files, authority receipts and pre-recall metadata constraints."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_content_publication import environment as environment, finish
from test_index_plan import embedding as embedding
from test_semantic_publication import directory, ROOT
from test_registry_authority import setup as setup, _start
from test_wiki_namespaces import domains as domains, prepared as prepared, RAW, ONE

from openviking.indexing.business_meta import build_meta
from openviking.indexing.batch import text_publication_batch
from openviking.indexing.metadata_integration import load_business_meta, project_meta, SIDECAR, TOKEN_COLUMN
from openviking.indexing.publication import query_snapshot
from openviking.indexing.registry_authority import AuthorityRepository
from openviking.indexing.image import enqueue_image_plan
from openviking.retrieve.metadata_query import resolve_metadata, token_filter
from openviking.retrieve.meta_dictionary import BusinessDictionary, MetaFilter, resolve_query
from openviking.service.search_service import SearchService
from openviking.storage.queuefs.embedding_msg import EmbeddingMsg
from openviking.storage.vectordb.index.cuvs_index import matches_filter
from openviking.utils.embedding_utils import vectorize_file
from openviking_cli.exceptions import InvalidArgumentError

URI = ROOT + "/chapter.md"


async def sidecar(e, values, root=ROOT):
    await e.fs.write(root + "/" + SIDECAR, json.dumps(values), ctx=e.ctx)


async def text(e, body="pump pressure repair", uri=URI):
    await e.fs.write(uri, body, ctx=e.ctx)
    e.queue.messages.clear()
    await vectorize_file(uri, {"name": "chapter.md"}, uri.rsplit("/", 1)[0], ctx=e.ctx)
    return list(e.queue.messages)


async def test_real_text_semantic_and_image_share_provenance(environment, embedding):
    e = environment
    await sidecar(e, {"modelid": " sy215-c ", "subject_code": "SY021NCB62788"})
    messages = await text(e)
    await finish(e, messages)
    semantic = await directory(e, "pump pressure")
    await finish(e, semantic)
    image_uri = ROOT + "/diagram.png"
    bundle = await load_business_meta(e.fs, image_uri, e.ctx)
    image = EmbeddingMsg([{"type": "text", "text": "pump diagram"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AQ=="}}],
                        {"uri": image_uri, "account_id": "acct", "namespace_id": "resources", "context_type": "resource", "level": 2,
                         "abstract": "pump diagram", "_require_joint_image_embedding": True, "_business_meta_input": bundle})
    old = embedding.chunking.publish_content
    embedding.chunking.publish_content = False
    e.queue.messages.clear()
    try:
        await enqueue_image_plan(image, embedding, e.queue)
    finally:
        embedding.chunking.publish_content = old
    image_fields = e.queue.messages[0].context_data
    all_fields = [message.context_data for message in messages + semantic] + [image_fields]
    assert len({fields["business_meta_source_fingerprint"] for fields in all_fields}) == 1
    assert len({tuple(fields[TOKEN_COLUMN]) for fields in all_fields}) == 1
    assert len({fields["content_revision"] for fields in all_fields}) == 3
    for fields in all_fields:
        payload = json.loads(fields["business_meta_payload"])
        assert payload["index_generation"] == fields["index_generation"]
        assert payload["content_revision"] == fields["content_revision"]
        assert payload["values"][0]["canonical"] == "SY215-C"
        assert "@sha256:" in payload["values"][0]["source"]


async def test_metadata_change_publishes_with_content_and_old_lease_stays_fixed(environment):
    e = environment
    await sidecar(e, {"modelid": "SY215-C"})
    first = await text(e)
    await finish(e, first)
    lease, _ = e.store.capture("acct")
    await sidecar(e, {"modelid": "SY75-C"})
    second = await text(e)
    assert second[0].context_data["index_plan_id"] != first[0].context_data["index_plan_id"]
    assert e.store.metadata_read("acct", lease, [URI])[0]["payload"]["values"][0]["canonical"] == "SY215-C"
    await finish(e, second)
    current = e.store.metadata_latest("acct", URI)
    assert current["payload"]["values"][0]["canonical"] == "SY75-C"
    assert e.store.metadata_read("acct", lease, [URI])[0]["generation"] == first[0].context_data["index_generation"]
    e.store.release(lease)


async def test_source_edit_during_build_blocks_all_pending_metadata(environment):
    e = environment
    await sidecar(e, {"modelid": "SY215-C"})
    messages = await text(e)
    await sidecar(e, {"modelid": "SY75-C"})
    for message in messages:
        await e.handler.on_dequeue({"data": message.to_json()})
    assert e.store.state(messages[0].context_data["index_generation"])["state"] == "failed"
    assert e.store.metadata_latest("acct", URI) is None


async def test_omitted_sidecar_retains_published_values_without_body_guessing(environment):
    e = environment
    await sidecar(e, {"modelid": "SY215-C"})
    await finish(e, await text(e))
    e.fs._async_agfs.path(e.fs._uri_to_path(ROOT + "/" + SIDECAR, ctx=e.ctx)).unlink()
    await finish(e, await text(e, "This body says SY999-C but meta must not infer it"))
    assert e.store.metadata_latest("acct", URI)["payload"]["values"][0]["canonical"] == "SY215-C"


@pytest.mark.parametrize("raw", ['{"Account_ID":"other"}', '{"acl_read":"public"}', '{"modelid":"A","modelid":"B"}', '{"modelid":NaN}', '{"modelid":["A"]}', '{"business_meta_payload":"forged"}'])
async def test_illegal_sidecar_fails_before_queue(environment, raw):
    e = environment
    await e.fs.write(ROOT + "/" + SIDECAR, raw, ctx=e.ctx)
    with pytest.raises(InvalidArgumentError):
        await text(e)
    assert e.queue.messages == []


async def test_directory_inheritance_nearest_sidecar_and_batch_edit_rejection(environment):
    e = environment
    await sidecar(e, {"modelid": "SY215-C"})
    child = ROOT + "/sub"
    inherited = await load_business_meta(e.fs, child + "/part.md", e.ctx)
    assert inherited["values"]["modelid"] == "SY215-C"
    await sidecar(e, {"modelid": "SY75-C"}, root=child)
    selected = await load_business_meta(e.fs, child + "/part.md", e.ctx)
    assert selected["values"]["modelid"] == "SY75-C"
    assert selected["source"].startswith(child + "/" + SIDECAR)


def test_same_field_or_cross_field_and_and_unknown_are_real_list_filters():
    dictionary = BusinessDictionary("manuals")
    dictionary.add_value("modelid", "SY215-C", aliases=["SY215C"])
    dictionary.add_value("modelid", "SY75-C")
    dictionary.add_value("subject_code", "SY021NCB62788")
    resolution = resolve_query(dictionary, meta_filters=[MetaFilter("modelid", ("SY215C", "GHOST")), MetaFilter("subject_code", ("SY021NCB62788",))])
    predicate = token_filter(resolution)
    good = project_meta(build_meta({"modelid": "SY215-C", "subject_code": "SY021NCB62788"}, document_id="d", content_revision="r").to_payload())
    wrong = project_meta(build_meta({"modelid": "SY75-C", "subject_code": "SY021NCB62788"}, document_id="d", content_revision="r").to_payload())
    assert matches_filter(good, predicate, {TOKEN_COLUMN: "list<string>"})
    assert not matches_filter(wrong, predicate, {TOKEN_COLUMN: "list<string>"})
    unknown = resolve_query(dictionary, meta_filters=[MetaFilter("modelid", ("GHOST",), include_unknown=True)])
    assert not unknown.is_empty()
    assert matches_filter({TOKEN_COLUMN: []}, token_filter(unknown), {TOKEN_COLUMN: "list<string>"})
    assert not matches_filter(good, token_filter(unknown), {TOKEN_COLUMN: "list<string>"})


def test_metadata_receipt_survives_real_authority_process(setup):
    process = _start(setup)
    try:
        store = AuthorityRepository(setup[0], setup[2])
        sidecar_uri = ROOT + "/" + SIDECAR
        epoch = store.observe_draft("acct", sidecar_uri)
        generation = store.begin("acct", "manuals", URI, "meta-plan", ["c", "metadata:meta-plan"])
        payload = build_meta({"modelid": "SY215-C"}, document_id="d", content_revision="r", index_generation=generation).to_payload()
        store.metadata_write(generation, payload, [{"uri": sidecar_uri, "draft_epoch": epoch, "sha256": "a" * 64}])
        store.seal(generation)
        assert store.metadata_latest("acct", URI) is None
        store.acknowledge(generation, "c")
        assert store.metadata_latest("acct", URI)["payload"] == payload
        lease, _ = store.capture("acct")
        with pytest.raises(ValueError, match="account mismatch"):
            store.metadata_read("other", lease, [URI])
        assert store.metadata_read("acct", lease, []) == []
        store.release(lease)
    finally:
        process.terminate()
        process.join(15)
