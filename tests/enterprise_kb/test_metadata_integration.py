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
from openviking.indexing.metadata_integration import (
    load_business_meta,
    project_meta,
    SIDECAR,
    TOKEN_COLUMN,
    field_token,
    value_token,
)
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
    text_fields_list = [message.context_data for message in messages + semantic]
    all_fields = text_fields_list + [image_fields]
    # The corpus sidecar is inherited verbatim by every producer, but each record
    # additionally states its own modality: joint image embeddings are the
    # ``images`` category, text chunks and directory summaries are ``text``.
    sidecar_tokens = {
        field_token("modelid"), value_token("modelid", "SY215-C"),
        field_token("subject_code"), value_token("subject_code", "SY021NCB62788"),
    }
    for fields in all_fields:
        assert sidecar_tokens <= set(fields[TOKEN_COLUMN])
    assert value_token("category", "IMAGES") in set(image_fields[TOKEN_COLUMN])
    for fields in text_fields_list:
        assert value_token("category", "TEXT") in set(fields[TOKEN_COLUMN])
    assert value_token("category", "TEXT") not in set(image_fields[TOKEN_COLUMN])
    assert len({fields["content_revision"] for fields in all_fields}) == 3
    for fields in all_fields:
        payload = json.loads(fields["business_meta_payload"])
        assert payload["index_generation"] == fields["index_generation"]
        assert payload["content_revision"] == fields["content_revision"]
        model = next(value for value in payload["values"] if value["field"] == "modelid")
        assert model["canonical"] == "SY215-C"
        assert "@sha256:" in model["source"]
        category = next(value for value in payload["values"] if value["field"] == "category")
        # A derived key states that the file's own block produced it, even when
        # the same record also inherits a corpus sidecar.
        assert "inline:" in category["source"]
        source = next(value for value in payload["values"] if value["field"] == "source_uri")
        assert source["canonical"] == fields["uri"].upper()


async def test_a_chapter_narrowed_by_model_still_answers_with_its_figure(environment, embedding):
    """``机型`` + ``images`` is one query, not two disjoint result sets.

    The joint image row stated its own modality and pointed at the chapter, but it
    carried none of the chapter's authored fields: an occurrence is a VLM summary
    plus a token-budgeted context window, and the ``- 机型:`` block is a property
    of the source document's opening, not of the window. So filtering a machine
    model and asking for its figures answered empty over a manual that has both,
    while the same filter over text hit the chapter.
    """
    e = environment
    body = (
        "# 10.2 进行操作时的注意事项\n\n"
        "- 机型: SY215-C\n"
        "- page: p.638-641\n\n"
        "进行装置的拆卸或安装（分解或装配）时，必须遵行下列注意事项。\n"
    )
    messages = await text(e, body)
    image_uri = ROOT + "/diagram.png"
    occurrence = {
        "summary": "pump diagram",
        # The shape the occurrence builder really produces: a decorated, budgeted
        # window the leading-block scan cannot read a provenance bullet out of.
        "context": f"Source URI: {URI}\nChapter: 拆装\nNeighboring source text:\n起吊时选用足够强度的设备。\n",
        "context_uri": URI,
        "context_hash": "h" * 64,
        "source_meta": {"machine_model": "SY215-C", "page": "p.638-641"},
    }
    image = EmbeddingMsg(
        [
            {"type": "text", "text": "pump diagram"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AQ=="}},
        ],
        {
            "uri": image_uri,
            "account_id": "acct",
            "namespace_id": "resources",
            "context_type": "resource",
            "level": 2,
            "abstract": "pump diagram",
            "_require_joint_image_embedding": True,
        },
    )
    old = embedding.chunking.publish_content
    embedding.chunking.publish_content = False
    e.queue.messages.clear()
    try:
        await enqueue_image_plan(image, embedding, e.queue, occurrences=[occurrence])
    finally:
        embedding.chunking.publish_content = old

    image_fields = e.queue.messages[0].context_data
    text_fields = [message.context_data for message in messages]

    def predicate(*pairs):
        dictionary = BusinessDictionary("manuals")
        for field, value in pairs:
            dictionary.add_value(field, value)
        return token_filter(
            resolve_query(dictionary, meta_filters=[MetaFilter(field, (value,)) for field, value in pairs])
        )

    schema = {TOKEN_COLUMN: "list<string>"}
    figures = predicate(("category", "images"), ("machine_model", "SY215-C"))
    prose = predicate(("category", "text"), ("machine_model", "SY215-C"))
    assert matches_filter(image_fields, figures, schema)
    assert not any(matches_filter(fields, figures, schema) for fields in text_fields)
    assert any(matches_filter(fields, prose, schema) for fields in text_fields)
    assert not matches_filter(image_fields, prose, schema)
    # The chapter stays the record's citation target, so the hit is traceable.
    payload = json.loads(image_fields["business_meta_payload"])
    source = next(value for value in payload["values"] if value["field"] == "source_uri")
    assert source["canonical"] == URI.upper()
    page = next(value for value in payload["values"] if value["field"] == "page")
    assert page["raw"] == "p.638-641"
    assert "inline:" in page["source"]


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


async def test_user_sidecar_walk_stops_at_the_user_space(environment):
    """The shared ``viking://user`` container is not a directory: probing
    ``viking://user/.business_meta.json`` names another user and is denied, so
    the walk must stop at the caller's own user space instead."""
    e = environment
    probed: list[str] = []
    original = e.fs.read_file_bytes

    async def recording_read(uri, *args, **kwargs):
        probed.append(uri)
        return await original(uri, *args, **kwargs)

    e.fs.read_file_bytes = recording_read
    uri = "viking://user/default/resources/manual/chapter.md"
    bundle = await load_business_meta(e.fs, uri, e.ctx)
    assert bundle["values"] is None
    assert probed[0] == "viking://user/default/resources/manual/" + SIDECAR
    assert probed[-1] == "viking://user/default/" + SIDECAR
    assert "viking://user/" + SIDECAR not in probed


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
