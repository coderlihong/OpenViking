"""ACL mutations select current records without weakening generation fences."""
from types import SimpleNamespace

import pytest

from openviking.indexing.publication import PublicationStore
from openviking.server.identity import RequestContext, Role
from openviking.storage.acl import AclManager, AclMode, DirectAcl, EffectiveAcl
from openviking.storage.expr import Or, PathScope
from openviking_cli.exceptions import ConflictError, FailedPreconditionError
from openviking_cli.session.user_id import UserIdentifier

URI = 'viking://resources/wiki/policy-flow'


def publish(registry, uri):
    generation = registry.begin('acct', 'wiki', uri, 'plan', ['c'])
    registry.seal(generation)
    registry.acknowledge(generation, 'c')
    return generation


def record(uri, generation, principal='user:old'):
    return {'id': generation, 'uri': uri, 'account_id': 'acct',
            'index_generation': generation, 'index_plan_id': 'plan',
            'chunk_id': 'c', 'level': 2,
            **EffectiveAcl.from_permissions(
                DirectAcl.from_entries([{'principal': principal, 'level': 'manage'}]),
                DirectAcl(), mode=AclMode.RESTRICTED).context_fields()}


class Contexts:
    def __init__(self, registry, records):
        self.registry, self.records = registry, {r['id']: r for r in records}
        self.written = []

    async def scroll(self, *, filter, output_fields, **kwargs):
        conditions = filter.conds if isinstance(filter, Or) else [filter]
        assert all(isinstance(c, PathScope) for c in conditions)
        rows = [r for r in self.records.values() if any(
            r['uri'] == c.path or (c.depth == -1 and r['uri'].startswith(c.path + '/'))
            for c in conditions)]
        return [{k: r[k] for k in output_fields if k in r} for r in rows], None

    async def get_strict(self, ids, **kwargs):
        return [dict(self.records[id]) for id in ids]

    async def _upsert_many_raw(self, rows, **kwargs):
        # Use the real authority fence: selecting a retired generation would
        # reproduce the production error, not merely fail a mock assertion.
        token = self.registry.generation_write_begin('acct', 'test-contexts', rows)
        self.written.extend(rows)
        self.records.update({r['id']: r for r in rows})
        self.registry.generation_write_confirm('acct', token, 'test-contexts')
        return [r['id'] for r in rows]


@pytest.fixture
def case(tmp_path, monkeypatch):
    registry = PublicationStore(str(tmp_path / 'registry.db'))
    monkeypatch.setattr('openviking.indexing.publication.configured_store', lambda: registry)
    old = publish(registry, URI)
    current = publish(registry, URI)
    child = publish(registry, URI + '/index.md')
    abandoned = registry.begin('acct', 'wiki', URI, 'next', ['c'])
    contexts = Contexts(registry, [record(URI, old), record(URI, current, 'user:current'),
                                   record(URI + '/index.md', child, 'user:current'),
                                   record(URI, abandoned, 'user:staged')])
    ctx = RequestContext(user=UserIdentifier('acct', 'admin'), role=Role.ADMIN)
    return SimpleNamespace(registry=registry, contexts=contexts, manager=AclManager(contexts),
                           ctx=ctx, old=old, current=current, child=child, abandoned=abandoned)


async def test_set_acl_updates_only_current_subtree_and_retired_fence_stays_active(case):
    effective = await case.manager.set_acl(
        URI, [{'principal': 'user:flowadmin', 'level': 'manage'}], case.ctx,
        acl_mode=AclMode.RESTRICTED)
    assert effective.direct.entries[0].principal == 'user:flowadmin'
    assert {r['index_generation'] for r in case.contexts.written} == {case.current, case.child}
    assert case.contexts.records[case.old]['acl_direct_grants'] != \
        case.contexts.records[case.current]['acl_direct_grants']
    with pytest.raises(FailedPreconditionError, match='superseded or retired'):
        case.registry.generation_write_begin('acct', 'late-old-write',
                                             [case.contexts.records[case.old]])
    with case.registry.db() as db:
        assert db.execute('SELECT count(*) FROM leases').fetchone()[0] == 0


async def test_permission_resolution_ignores_inconsistent_retired_and_staged_acl(case):
    direct = await case.manager.get_direct(URI, case.ctx)
    assert direct.entries[0].principal == 'user:current'


async def test_refresh_projects_only_current_generation(case):
    await case.manager.refresh_context_subtree(URI, case.ctx)
    assert {r['index_generation'] for r in case.contexts.written} == {case.current, case.child}


async def test_tombstoned_subtree_is_never_acl_mutated(case):
    case.registry.tombstone('acct', URI + '/index.md')
    await case.manager.set_acl(URI, [{'principal': 'user:new', 'level': 'manage'}], case.ctx)
    assert {r['index_generation'] for r in case.contexts.written} == {case.current}


@pytest.mark.parametrize('changed_uri', [URI, URI + '/index.md', URI + '/new.md'])
async def test_head_switch_during_acl_write_is_conflict_not_false_success(case, changed_uri):
    original = case.contexts._upsert_many_raw
    replacement = None

    async def publish_during_write(rows, **kwargs):
        nonlocal replacement
        ids = await original(rows, **kwargs)
        if replacement is None:
            replacement = publish(case.registry, changed_uri)
            case.contexts.records[replacement] = record(changed_uri, replacement, 'user:new-head')
        return ids

    case.contexts._upsert_many_raw = publish_during_write
    with pytest.raises(ConflictError, match='ACL_PUBLICATION_CHANGED'):
        await case.manager.set_acl(
            URI, [{'principal': 'user:flowadmin', 'level': 'manage'}], case.ctx)
    # The protected old head may legally receive a write, but that does not
    # prove the replacement inherited the requested ACL. Return a conflict.
    assert replacement not in {r['index_generation'] for r in case.contexts.written}
    assert (await case.manager.get_direct(changed_uri, case.ctx)).entries[0].principal == 'user:new-head'
    with case.registry.db() as db:
        assert db.execute('SELECT count(*) FROM leases').fetchone()[0] == 0


async def test_unrelated_head_switch_does_not_conflict_with_acl_update(case):
    original = case.contexts._upsert_many_raw

    async def publish_elsewhere(rows, **kwargs):
        ids = await original(rows, **kwargs)
        publish(case.registry, 'viking://resources/wiki/unrelated')
        return ids

    case.contexts._upsert_many_raw = publish_elsewhere
    effective = await case.manager.set_acl(
        URI, [{'principal': 'user:flowadmin', 'level': 'manage'}], case.ctx)
    assert effective.direct.entries[0].principal == 'user:flowadmin'
