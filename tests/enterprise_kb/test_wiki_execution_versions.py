"""Frozen Wiki prompt/policy versions bind submission, execution and commit."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding
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
