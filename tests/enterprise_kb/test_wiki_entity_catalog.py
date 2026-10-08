"""Authoritative wiki entity catalog: persistence, namespace isolation, alias ownership."""
import pytest
from test_index_plan import embedding as embedding
from test_wiki_catalog import environment as environment, register_domains

from openviking.indexing.publication import PublicationStore
from openviking.server.identity import Role
from openviking.wiki.catalog import WikiCatalog
from openviking_cli.exceptions import ConflictError, NotFoundError


def entry(canonical_id='candidate-' + 'a' * 64, entity_type='Thing', **changes):
    value = {'canonical_id': canonical_id, 'entity_type': entity_type, 'schema_id': 'business',
             'schema_version': '1', 'schema_digest': 'b' * 64, 'aliases': ['挖机'], 'source_uri': 'viking://resources/wiki/Thing.md'}
    value.update(changes)
    return value


async def catalog(environment):
    from dataclasses import replace
    e = environment
    await register_domains(e)
    store = PublicationStore(e.store.path)
    return WikiCatalog(e.fs, registry=store), replace(e.ctx, role=Role.ADMIN), store


async def test_entities_round_trip_and_reopen(environment):
    e = environment
    c, ctx, _ = await catalog(e)
    registered = await c.register_entities(ctx, 'manuals', [entry()])
    assert registered == {'registered': 1, 'aliases': {'挖机': entry()['canonical_id']}}
    again = WikiCatalog(e.fs, registry=PublicationStore(e.store.path))
    row = await again.get_entity(ctx, 'manuals', entry()['canonical_id'])
    assert row['entity_type'] == 'Thing' and row['aliases'] == ['挖机']
    rows = await again.list_entities(ctx, 'manuals')
    assert [r['canonical_id'] for r in rows] == [entry()['canonical_id']]


async def test_alias_owner_is_immutable_and_idempotent(environment):
    e = environment
    c, ctx, _ = await catalog(e)
    first = entry()
    await c.register_entities(ctx, 'manuals', [first])
    with pytest.raises(ConflictError):
        await c.register_entities(ctx, 'manuals', [entry(canonical_id='candidate-' + 'c' * 64)])
    idempotent = c.registry.wiki_entity_register(e.ctx.account_id, 'manuals', [first])
    assert idempotent['registered'] == 1


async def test_batch_self_collision_rejected_before_any_write(environment):
    e = environment
    c, ctx, _ = await catalog(e)
    twin = entry(canonical_id='candidate-' + 'd' * 64)
    with pytest.raises(ConflictError):
        await c.register_entities(ctx, 'manuals', [entry(), twin])
    with pytest.raises(NotFoundError):
        await c.get_entity(ctx, 'manuals', entry()['canonical_id'])


async def test_entries_require_registered_namespace(environment):
    e = environment
    c, ctx, _ = await catalog(e)
    with pytest.raises(NotFoundError):
        await c.register_entities(ctx, 'nowhere', [entry()])


async def test_malformed_entries_are_value_errors(environment):
    e = environment
    c, ctx, _ = await catalog(e)
    with pytest.raises(ValueError):
        await c.register_entities(ctx, 'manuals', [{'canonical_id': 'x'}])
    with pytest.raises(ValueError):
        await c.register_entities(ctx, 'manuals', [entry(schema_digest='nothex')])
    with pytest.raises(ValueError):
        await c.register_entities(ctx, 'manuals', [entry(aliases=[' a '])])
