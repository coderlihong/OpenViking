"""Fixed binary-upload identity and replay without source mutation."""
import hashlib
import json
from unittest.mock import AsyncMock

import pytest
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding
from test_wiki_build_plan import prepared as prepared
from openviking_cli.exceptions import ConflictError, FailedPreconditionError, PermissionDeniedError
from openviking.wiki.schema import SchemaError

ROOT = 'viking://resources/raw/replay-doc'
SOURCE = ROOT + '/chapter.md'
BINARY = b'%PDF-1.7 immutable upload'

async def prepare(e, planner, materialize, *, key='binary-1', binary=BINARY, policy='fail'):
    return await planner.prepare_materialized_upload(e.ctx, 'manual-wiki', 'replay-doc.pdf',
        materialize=materialize, idempotency_key=key, source_sha256=hashlib.sha256(binary).hexdigest(),
        source_size=len(binary), update_policy=policy)

async def setup(prepared):
    e, p = prepared
    async def materialize(root):
        assert root == ROOT
        await e.fs.write_file_bytes(SOURCE, b'# Chapter\nRepair evidence.', ctx=e.ctx)
    return e, p, AsyncMock(side_effect=materialize)

async def test_same_key_replay_preserves_plan_and_source_epoch_without_reparse(prepared):
    e,p,m = await setup(prepared)
    plan = await prepare(e,p,m)
    epoch = e.store.observe_draft(e.ctx.account_id, SOURCE)
    again = await prepare(e,p,m,policy='replace')
    assert again == plan
    assert m.await_count == 1
    assert e.store.observe_draft(e.ctx.account_id, SOURCE) == epoch
    assert json.loads(plan.manifest)['upload_source']['sha256'] == hashlib.sha256(BINARY).hexdigest()

async def test_same_key_changed_binary_rejected_before_parser_or_write(prepared):
    e,p,m = await setup(prepared)
    await prepare(e,p,m)
    epoch = e.store.observe_draft(e.ctx.account_id, SOURCE)
    with pytest.raises(ConflictError,match='different input'):
        await prepare(e,p,m,binary=b'different bytes')
    assert m.await_count == 1
    assert e.store.observe_draft(e.ctx.account_id, SOURCE) == epoch

async def test_changed_source_cannot_be_restored_by_replay(prepared):
    e,p,m = await setup(prepared)
    await prepare(e,p,m)
    await e.fs.write_file_bytes(SOURCE,b'# Outside edit',ctx=e.ctx)
    with pytest.raises(ConflictError): await prepare(e,p,m)
    assert m.await_count == 1
    assert await e.fs.read_file_bytes(SOURCE,ctx=e.ctx) == b'# Outside edit'


async def test_replay_rechecks_captured_source_permissions_without_reparsing(prepared, monkeypatch):
    e,p,m = await setup(prepared)
    await prepare(e,p,m)
    original = e.fs._ensure_access

    async def revoked(uri, ctx=None, **kwargs):
        if uri == SOURCE:
            raise PermissionDeniedError('Source access revoked')
        return await original(uri, ctx, **kwargs)

    monkeypatch.setattr(e.fs, '_ensure_access', revoked)
    with pytest.raises(PermissionDeniedError, match='revoked'):
        await prepare(e,p,m)
    assert m.await_count == 1

@pytest.mark.parametrize('policy,error', [('fail',ConflictError)])
async def test_existing_tree_never_overlaid_or_deleted_before_safe_replacement(prepared,policy,error):
    e,p,m = await setup(prepared)
    await e.fs.write_file_bytes(SOURCE,b'# Old chapter',ctx=e.ctx)
    with pytest.raises(error): await prepare(e,p,m,policy=policy)
    m.assert_not_awaited()
    assert await e.fs.read_file_bytes(SOURCE,ctx=e.ctx) == b'# Old chapter'

async def test_new_key_cannot_silently_overwrite_fixed_plan(prepared):
    e,p,m = await setup(prepared)
    plan = await prepare(e,p,m)
    with pytest.raises(FailedPreconditionError):
        await prepare(e,p,m,key='binary-2',policy='replace')
    assert m.await_count == 1
    assert await p.check_current_inputs(e.ctx,plan.build_id) == plan

@pytest.mark.parametrize('changes', [{'source_sha256':'invalid'}, {'source_size':True}, {'update_policy':'merge'}])
async def test_invalid_identity_rejected_before_materialization(prepared,changes):
    e,p,m = await setup(prepared)
    args=dict(materialize=m,idempotency_key='bad',source_sha256=hashlib.sha256(BINARY).hexdigest(),
              source_size=len(BINARY),update_policy='fail')
    args.update(changes)
    with pytest.raises(SchemaError):
        await p.prepare_materialized_upload(e.ctx,'manual-wiki','replay-doc.pdf',**args)
    m.assert_not_awaited()
