"""HTTP strict_diagnostic submissions read real registry receipts.

WikiJobService.create wires the G3 adjudicator over the planner registry: an
empty catalog names trusted_entity_catalog, a registered entity matching the
pinned schema satisfies identity, and the refusal message is the receipt's own
reason — never a caller-supplied boolean.
"""
from dataclasses import replace
import pytest

from test_wiki_catalog import environment as environment, register_domains, setup
from test_index_plan import embedding as embedding

from openviking.indexing.publication import PublicationStore
from openviking.server.identity import Role
from openviking.service.wiki_job_service import WikiJobService
from openviking.wiki.catalog import WikiCatalog
from openviking.wiki.schema import SchemaError
from test_wiki_schema import freeze, package


def entry(**changes):
    value = {'canonical_id': 'candidate-' + 'a' * 64, 'entity_type': 'Thing', 'schema_id': 'business',
             'schema_version': '1', 'schema_digest': None, 'aliases': ['挖机'],
             'source_uri': 'viking://resources/wiki/Thing.md'}
    value.update(changes)
    return value


async def strict_service(environment):
    e = environment
    e.fs._async_agfs.root.mkdir(parents=True, exist_ok=True)
    catalog, ctx, bundle = await setup(e)
    store = PublicationStore(e.store.path)
    await e.fs.write('viking://resources/raw/manual/one.md', '# 一章\n\n液压泵故障诊断。', ctx=e.ctx)
    from openviking.wiki.build_plan import WikiBuildPlanner
    return e, WikiBuildPlanner(e.fs, registry=store), ctx, bundle


async def test_strict_submission_names_empty_catalog_gate(environment):
    e, p, _, bundle = await strict_service(environment)
    plan = await p.prepare_sources(e.ctx, 'manual-wiki', ['viking://resources/raw/manual/one.md'],
                                   input_kind='uri', idempotency_key='strict-empty')
    service = WikiJobService(p)
    with pytest.raises(SchemaError) as err:
        await service.create(e.ctx, plan.build_id, expected_plan_digest=plan.digest,
                             idempotency_key='strict-exec', profile='strict_diagnostic')
    text = str(err.value)
    assert 'trusted_entity_catalog' in text and 'WIKI_PUBLICATION_GATE_PENDING' in text


async def test_registered_catalog_identity_advances_to_next_missing_gate(environment):
    e, p, _, bundle = await strict_service(environment)
    catalog = WikiCatalog(e.fs, registry=p.registry)
    admin = replace(e.ctx, role=Role.ADMIN)
    await catalog.register_entities(admin, 'wiki', [entry(schema_digest=bundle.digest)])
    plan = await p.prepare_sources(e.ctx, 'manual-wiki', ['viking://resources/raw/manual/one.md'],
                                   input_kind='uri', idempotency_key='strict-entry')
    service = WikiJobService(p)
    with pytest.raises(SchemaError) as err:
        await service.create(e.ctx, plan.build_id, expected_plan_digest=plan.digest,
                             idempotency_key='strict-exec', profile='strict_diagnostic')
    text = str(err.value)
    assert 'trusted_entity_catalog' not in text
    assert 'schema_identity_resolution' not in text  # catalog entry matches the pin
    assert 'closed_relationships' in text            # no admitted merge graph under the root yet


async def test_unregistered_collection_binding_is_an_unreadable_gate(environment):
    e, p, _, bundle = await strict_service(environment)
    service = WikiJobService(p)
    plan = await p.prepare_sources(e.ctx, 'manual-wiki', ['viking://resources/raw/manual/one.md'],
                                   input_kind='uri', idempotency_key='strict-lost')
    with p.registry.db() as db:
        db.execute("DELETE FROM wiki_entity_catalog WHERE account='acct'")
        db.execute("DROP TABLE wiki_entity_catalog")
    with pytest.raises(SchemaError) as err:
        await service.create(e.ctx, plan.build_id, expected_plan_digest=plan.digest,
                             idempotency_key='strict-exec', profile='strict_diagnostic')
    assert 'unreadable' in str(err.value)


async def test_draft_profile_still_skips_receipt_reading(environment):
    e, p, _, bundle = await strict_service(environment)
    from unittest.mock import AsyncMock
    from openviking.wiki.extraction import WikiDraftExtractor
    from openviking.wiki.namespaces import NamespaceCatalog
    admin = replace(e.ctx, role=Role.ADMIN)
    await NamespaceCatalog(e.fs, registry=p.registry).set_export(
        admin, source_namespace='manuals', target_namespace='wiki', principal='user:user', allowed=True)
    model = AsyncMock()
    x = WikiDraftExtractor(p, model=model, model_profile={'model': 'fixture-draft', 'provider': 'fixture'})
    plan = await p.prepare_sources(e.ctx, 'manual-wiki', ['viking://resources/raw/manual/one.md'],
                                   input_kind='uri', idempotency_key='draft-plain')
    job = await WikiJobService(p, extractor=x).create(e.ctx, plan.build_id, expected_plan_digest=plan.digest,
                                                      idempotency_key='draft-exec')
    assert job['effective_config']['extraction_profile'] == 'draft'
