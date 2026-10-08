"""Frozen Wiki prompt/policy versions bind submission, execution and commit."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding
from test_wiki_namespaces import ONE, TWO
from test_wiki_build_plan import prepared as prepared
from test_wiki_namespaces import domains as domains
from test_wiki_extraction import execute_setup, candidate, response
from openviking.service.wiki_job_service import WikiJobService
from openviking.service.kb_job_runtime import DocumentJobRuntime
from openviking.wiki.policies import EXTRACTOR_VERSION, POLICY_VERSION
from openviking_cli.exceptions import ConflictError

@pytest.mark.parametrize('field', ['extractor_version','policy_version'])
async def test_worker_refuses_changed_frozen_execution_version_before_model(domains,monkeypatch,field):
    e,p,_,_,_,plan,model,x = await execute_setup(domains)
    job=await WikiJobService(p,extractor=x).create(e.ctx,plan.build_id,
        expected_plan_digest=plan.digest,idempotency_key='version-worker')
    assert job['effective_config']['extractor_version']==EXTRACTOR_VERSION
    assert job['effective_config']['policy_version']==POLICY_VERSION
    job['effective_config'][field]='changed-version'
    monkeypatch.setattr('openviking.wiki.extraction.WikiDraftExtractor',lambda _:x)
    runtime=DocumentJobRuntime(SimpleNamespace(viking_fs=e.fs),p.registry,None,None)
    lease=SimpleNamespace(checkpoint=AsyncMock())
    with pytest.raises(ConflictError,match='configuration changed'):
        await runtime.extract_wiki(job,e.ctx,lease)
    lease.checkpoint.assert_not_awaited()
    model.get_completion_async.assert_not_called()

@pytest.mark.parametrize('field', ['extractor_version','policy_version'])
async def test_authority_refuses_mismatched_receipt_execution_version(domains,field):
    e,p,_,_,_,plan,model,x = await execute_setup(domains)
    model.get_completion_async.return_value=response(candidate())
    job=await WikiJobService(p,extractor=x).create(e.ctx,plan.build_id,
        expected_plan_digest=plan.digest,idempotency_key='version-commit')
    claim=p.registry.job_claim('worker')
    receipt=await x.extract(e.ctx,plan.build_id,expected_plan_digest=plan.digest)
    receipt['configuration'][field]='changed-version'
    p.registry.job_checkpoint(job['job_id'],'worker',claim['fence'],'wiki_extract',{})
    p.registry.job_checkpoint(job['job_id'],'worker',claim['fence'],'wiki_validate',
        {'draft_receipt':receipt,'diagnostic_published':False})
    with pytest.raises(ValueError,match='effective configuration changed'):
        p.registry.job_complete(job['job_id'],'worker',claim['fence'],'draft_ready',receipt)
    assert p.registry.job_get(e.ctx.account_id,job['job_id'])['state']=='running'


@pytest.mark.parametrize('uris', [(ONE,), (ONE,TWO)], ids=['one-receipt','two-receipts'])
async def test_receipt_replay_authorizes_batch_not_per_receipt(domains,monkeypatch,uris):
    """I090: terminal receipt replay must not re-scan the whole plan per receipt.

    The old loop paid two full-plan authorization scans per receipt — quadratic
    at real handbook scale (2k receipts x 2k sources). The bracketed loop adds a
    constant number of scans on top of extract(), whatever the receipt count.
    """
    from openviking.wiki.namespaces import NamespaceCatalog
    e,p,c,admin,backend,plan,model,x = await execute_setup(domains,uris=uris)
    async def _model(prompt, **kw):
        uri = TWO if 'two.md' in prompt else ONE
        quote = '行走跑偏检查。' if uri == TWO else '液压泵故障诊断。'
        return response(candidate(uri=uri,quote=quote,claim=quote))
    model.get_completion_async.side_effect=_model
    scans=[]
    real_authorize=NamespaceCatalog.authorize_sources
    async def counting(self,ctx,**kw):
        scans.append(kw.get('target_uri'))
        return await real_authorize(self,ctx,**kw)
    monkeypatch.setattr(NamespaceCatalog,'authorize_sources',counting)
    job=await WikiJobService(p,extractor=x).create(e.ctx,plan.build_id,
        expected_plan_digest=plan.digest,idempotency_key='replay-once')
    monkeypatch.setattr('openviking.wiki.extraction.WikiDraftExtractor',lambda _:x)
    runtime=DocumentJobRuntime(SimpleNamespace(viking_fs=e.fs),p.registry,None,None)
    runtime.resolve_context=AsyncMock(return_value=e.ctx)
    runtime._current_identity=AsyncMock(return_value=(e.ctx.role,e.ctx.group_ids))
    runtime.executor.context_resolver=runtime.resolve_context
    lease_checkpoints=[]
    class Lease:
        async def checkpoint(self,stage,checkpoint):
            lease_checkpoints.append((stage,checkpoint))
    total_receipts=len(plan.sources)
    finished=await runtime.extract_wiki(job,e.ctx,Lease())
    assert finished['state']=='draft_ready'
    # Same scan total whatever the receipt count proves the replay loop adds a
    # constant, not 2*R; the old code would scale the delta with receipts.
    REPLAY_CONSTANT_SCANS.append(len(scans))
    progress=[cp for stage,cp in lease_checkpoints if stage=='wiki_extract' and 'receipt_replay_progress' in cp]
    assert progress and progress[-1]['receipt_replay_progress']=={'done':total_receipts,'total':total_receipts}
    assert lease_checkpoints[-1][0]=='wiki_validate'


def test_receipt_replay_scan_count_independent_of_receipts():
    """I090 pairing assertion: both parametrized runs scanned the same count."""
    recorded=[n for n in REPLAY_CONSTANT_SCANS if n is not None]
    assert len(recorded)==2 and recorded[0]==recorded[1], recorded

REPLAY_CONSTANT_SCANS=[]
