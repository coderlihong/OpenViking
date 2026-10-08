"""HTTP -> durable Wiki job -> real private blobs, with a declared model fixture."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from test_wiki_extraction import execute_setup, candidate, response, ONE, TWO
from test_wiki_namespaces import domains as domains, grant, RAW, WIKI
from test_wiki_build_plan import prepared as prepared
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding

from openviking.server.auth import get_request_context
from openviking.server.routers import wiki
from openviking.service.kb_job_runtime import DocumentJobRuntime
from openviking.service.wiki_job_service import WikiJobService
from openviking_cli.exceptions import PermissionDeniedError


@pytest.mark.parametrize('input_kind', ['uri', 'upload'])
async def test_execute_separate_from_prepare_http_job_and_private_read(domains, monkeypatch, input_kind):
    e, planner, policy, admin, backend, plan, model, extractor = await execute_setup(domains)
    model.get_completion_async.return_value = response(candidate())
    if input_kind == 'upload':
        backend.acl(RAW, [{'principal': 'user:user', 'level': 'write'}])
        plan = await planner.prepare_sources(e.ctx, 'manual-wiki', [plan.sources[0].uri],
            input_kind='upload', idempotency_key='materialized-upload')
    jobs = WikiJobService(planner, extractor=extractor)
    app = FastAPI()
    app.include_router(wiki.router)
    app.dependency_overrides[get_request_context] = lambda: e.ctx
    app.dependency_overrides[wiki.get_wiki_jobs] = lambda: jobs
    app.dependency_overrides[wiki.get_planner] = lambda: planner
    # The real runtime and repository execute the captured source; only the
    # model completion and identity registry adapter are explicit substitutes.
    monkeypatch.setattr('openviking.wiki.extraction.WikiDraftExtractor', lambda _: extractor)
    runtime = DocumentJobRuntime(SimpleNamespace(viking_fs=e.fs), planner.registry, None, None)
    runtime.resolve_context = AsyncMock(return_value=e.ctx)
    runtime._current_identity = AsyncMock(return_value=(e.ctx.role, e.ctx.group_ids))
    runtime.executor.context_resolver = runtime.resolve_context
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        prepared_response = await client.get('/api/v1/kb/wiki/builds/' + plan.build_id)
        assert prepared_response.json()['result']['state'] == 'prepared'
        url = '/api/v1/kb/wiki/builds/' + plan.build_id + '/execute'
        body = {'expected_plan_digest': plan.digest, 'idempotency_key': 'execute-one'}
        submitted = await client.post(url, json=body)
        assert submitted.status_code == 202, submitted.text
        job = submitted.json()['result']
        replay = await client.post(url, json=body)
        assert replay.json()['result']['job_id'] == job['job_id']
        assert job['state'] == 'queued' and job['kind'] == ('W1' if input_kind == 'upload' else 'W2')
        finished = await runtime.executor.run_once()
        assert finished['state'] == 'draft_ready', finished
        assert finished['checkpoint']['diagnostic_published'] is False
        receipt = finished['checkpoint']['draft_receipt']
        for draft in receipt['receipts']:
            assert (e.ctx.account_id, draft['reference']['sha256']) in planner.registry.protected_content_revisions()
        read = await client.get('/api/v1/kb/wiki/jobs/' + job['job_id'] + '/drafts/0')
        assert read.status_code == 200
        assert read.json()['result']['candidates'][0]['uri'] == WIKI + '/Thing/pump.md'
        assert planner.registry.job_get(e.ctx.account_id, job['job_id'])['state'] != 'ready'
        backend.acl(RAW, [{'principal': 'user:other', 'level': 'read'}])
        with pytest.raises(PermissionDeniedError):
            await jobs.read_draft(e.ctx, job['job_id'], 0)


async def test_draft_receipt_cannot_be_self_declared_or_cross_plan(domains):
    e, planner, policy, admin, backend, plan, model, extractor = await execute_setup(domains)
    model.get_completion_async.return_value = response(candidate())
    jobs = WikiJobService(planner, extractor=extractor)
    job = await jobs.create(e.ctx, plan.build_id, expected_plan_digest=plan.digest, idempotency_key='guard')
    claim = planner.registry.job_claim('worker')
    receipt = await extractor.extract(e.ctx, plan.build_id, expected_plan_digest=plan.digest)
    planner.registry.job_checkpoint(job['job_id'], 'worker', claim['fence'], 'wiki_extract', {})
    planner.registry.job_checkpoint(job['job_id'], 'worker', claim['fence'], 'wiki_validate',
                                   {'draft_receipt': receipt, 'diagnostic_published': False})
    for bad in (None, dict(receipt, plan_digest='0' * 64), dict(receipt, diagnostic_publishable=True)):
        with pytest.raises(ValueError, match='private candidate receipt'):
            planner.registry.job_complete(job['job_id'], 'worker', claim['fence'], 'draft_ready', bad)
    assert planner.registry.job_complete(job['job_id'], 'worker', claim['fence'], 'draft_ready', receipt)


async def test_terminal_gate_reconciles_coverage_with_failed_sources(domains):
    """I095: a section that exhausts its retries records a failure instead of a
    receipt, so the terminal gate must treat fixed-plan coverage as
    receipts ∪ failed_sources. An unaccounted gap still rejects; the same gap
    recorded in failed_sources completes as a private draft."""
    e, planner, policy, admin, backend, plan, model, extractor = await execute_setup(domains, uris=(ONE, TWO))
    model.get_completion_async.return_value = response(candidate())
    jobs = WikiJobService(planner, extractor=extractor)
    job = await jobs.create(e.ctx, plan.build_id, expected_plan_digest=plan.digest, idempotency_key='coverage')
    claim = planner.registry.job_claim('worker')
    receipt = await extractor.extract(e.ctx, plan.build_id, expected_plan_digest=plan.digest)
    # two.md's default candidate does not match its text, so it fails extraction
    # and is recorded in failed_sources with no receipt: a genuine accounted gap.
    sources = {s.uri for s in plan.sources}
    covered = {r['source_uri'] for r in receipt['receipts']}
    assert covered != sources and receipt['failed_sources']
    assert covered | set(receipt['failed_sources']) == sources
    planner.registry.job_checkpoint(job['job_id'], 'worker', claim['fence'], 'wiki_extract', {})
    unaccounted = dict(receipt, failed_sources=[])
    planner.registry.job_checkpoint(job['job_id'], 'worker', claim['fence'], 'wiki_validate',
                                    {'draft_receipt': unaccounted, 'diagnostic_published': False})
    with pytest.raises(ValueError, match='must cover every fixed source'):
        planner.registry.job_complete(job['job_id'], 'worker', claim['fence'], 'draft_ready', unaccounted)
    planner.registry.job_checkpoint(job['job_id'], 'worker', claim['fence'], 'wiki_validate',
                                    {'draft_receipt': receipt, 'diagnostic_published': False})
    assert planner.registry.job_complete(job['job_id'], 'worker', claim['fence'], 'draft_ready', receipt)
    assert planner.registry.job_get(e.ctx.account_id, job['job_id'])['state'] == 'draft_ready'


async def test_terminal_gate_reconciles_legacy_receipt_by_failed_section_count(domains):
    """I095 legacy branch: a receipt written before failed_sources existed carries
    only per-error failed_sections counts. The gate reconciles the uncovered
    sources against that declared count, so a count that does not account for the
    gap rejects while a matching count completes as a private draft."""
    e, planner, policy, admin, backend, plan, model, extractor = await execute_setup(domains, uris=(ONE, TWO))
    model.get_completion_async.return_value = response(candidate())
    jobs = WikiJobService(planner, extractor=extractor)
    job = await jobs.create(e.ctx, plan.build_id, expected_plan_digest=plan.digest, idempotency_key='legacy-count')
    claim = planner.registry.job_claim('worker')
    receipt = await extractor.extract(e.ctx, plan.build_id, expected_plan_digest=plan.digest)
    sources = {s.uri for s in plan.sources}
    covered = {r['source_uri'] for r in receipt['receipts']}
    gap = len(sources) - len(covered)
    assert gap and receipt['failed_sources']  # two.md failed extraction, no receipt
    legacy = {k: v for k, v in receipt.items() if k != 'failed_sources'}
    planner.registry.job_checkpoint(job['job_id'], 'worker', claim['fence'], 'wiki_extract', {})
    bad = dict(legacy, failed_sections={'SCHEMA_VIOLATION': gap + 5})
    planner.registry.job_checkpoint(job['job_id'], 'worker', claim['fence'], 'wiki_validate',
                                    {'draft_receipt': bad, 'diagnostic_published': False})
    with pytest.raises(ValueError, match='must cover every fixed source'):
        planner.registry.job_complete(job['job_id'], 'worker', claim['fence'], 'draft_ready', bad)
    legacy['failed_sections'] = {'SCHEMA_VIOLATION': gap}
    planner.registry.job_checkpoint(job['job_id'], 'worker', claim['fence'], 'wiki_validate',
                                    {'draft_receipt': legacy, 'diagnostic_published': False})
    assert planner.registry.job_complete(job['job_id'], 'worker', claim['fence'], 'draft_ready', legacy)
    assert planner.registry.job_get(e.ctx.account_id, job['job_id'])['state'] == 'draft_ready'


@pytest.mark.parametrize('change', ['export', 'source'])
async def test_changed_inputs_between_validation_and_terminal_commit_are_rejected(domains, change):
    e, planner, policy, admin, backend, plan, model, extractor = await execute_setup(domains)
    backend.acl(RAW, [{'principal': 'user:user', 'level': 'write'}])
    model.get_completion_async.return_value = response(candidate())
    jobs = WikiJobService(planner, extractor=extractor)
    job = await jobs.create(e.ctx, plan.build_id, expected_plan_digest=plan.digest, idempotency_key='window')
    claim = planner.registry.job_claim('worker')
    receipt = await extractor.extract(e.ctx, plan.build_id, expected_plan_digest=plan.digest)
    planner.registry.job_checkpoint(job['job_id'], 'worker', claim['fence'], 'wiki_extract', {})
    planner.registry.job_checkpoint(job['job_id'], 'worker', claim['fence'], 'wiki_validate',
                                   {'draft_receipt': receipt, 'diagnostic_published': False})
    if change == 'export':
        await grant(policy, admin, allowed=False)
        error, reason = ValueError, 'export policy changed'
    else:
        from openviking_cli.exceptions import ConflictError
        await e.fs.write_file(plan.sources[0].uri, 'changed source', ctx=e.ctx)
        error, reason = ConflictError, 'Wiki input changed'
    with pytest.raises(error, match=reason):
        planner.registry.job_complete(job['job_id'], 'worker', claim['fence'], 'draft_ready', receipt)
    assert planner.registry.job_get(e.ctx.account_id, job['job_id'])['state'] == 'running'
