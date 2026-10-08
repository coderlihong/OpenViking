"""W1 staged whole-tree replacement: promote-by-rename commit, old tree never lost."""
import hashlib
import json
from unittest.mock import AsyncMock

import pytest
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding
from test_wiki_build_plan import prepared as prepared
from openviking_cli.exceptions import ConflictError, FailedPreconditionError
from openviking.wiki.schema import SchemaError

ROOT = 'viking://resources/raw/replace-doc'
LIVE = ROOT + '/chapter.md'


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


async def two_revision_setup(prepared):
    e, p = prepared

    async def materialize(root):
        await e.fs.write_file_bytes(root + '/chapter.md', b'# Parsed revision two\n', ctx=e.ctx)

    m = AsyncMock(side_effect=materialize)
    await e.fs.write_file_bytes(LIVE, b'# Old chapter', ctx=e.ctx)
    return e, p, m


def prepare(e, p, m, *, key='rev-2', policy='replace'):
    return p.prepare_materialized_upload(e.ctx, 'manual-wiki', 'replace-doc.pdf',
        materialize=m, idempotency_key=key, source_sha256=_sha(b'binary rev two'),
        source_size=14, update_policy=policy)


async def test_replace_promotes_new_tree_and_old_tree_disappears(prepared):
    e, p, m = await two_revision_setup(prepared)
    plan = await prepare(e, p, m)
    assert {s.uri for s in plan.sources} == {LIVE}
    assert await e.fs.read_file_bytes(LIVE, ctx=e.ctx) == b'# Parsed revision two\n'
    assert not await e.fs.exists(ROOT + '/x.staging-rev-2', ctx=e.ctx)
    assert not await e.fs.exists(ROOT + '/x.retired-rev-2', ctx=e.ctx)


async def test_parser_failure_leaves_live_tree_byte_identical(prepared):
    e, p = prepared
    await e.fs.write_file_bytes(LIVE, b'# Old chapter', ctx=e.ctx)
    boom = AsyncMock(side_effect=RuntimeError('parser died'))

    with pytest.raises(RuntimeError, match='parser died'):
        await prepare(e, p, boom)
    assert await e.fs.read_file_bytes(LIVE, ctx=e.ctx) == b'# Old chapter'
    parent = ROOT.rsplit('/', 1)[0]
    assert not [name for name in ('.staging-', '.retired-')
                if any(n.startswith(name) for n in
                       [child['name'] for child in await e.fs.ls(parent, ctx=e.ctx)])]


async def test_promote_failure_restores_retired_tree(prepared):
    e, p, m = await two_revision_setup(prepared)
    original_mv = e.fs.mv

    async def fail_promote(old_uri, new_uri, ctx=None, **kwargs):
        if old_uri.endswith('.staging-rev-2') and new_uri == ROOT:
            raise RuntimeError('promote lost')
        return await original_mv(old_uri, new_uri, ctx=ctx, **kwargs)

    e.fs.mv = fail_promote
    with pytest.raises(RuntimeError, match='promote lost'):
        await prepare(e, p, m)
    assert await e.fs.read_file_bytes(LIVE, ctx=e.ctx) == b'# Old chapter'


async def test_fail_policy_still_rejects_existing_target(prepared):
    e, p, m = await two_revision_setup(prepared)
    with pytest.raises(ConflictError):
        await prepare(e, p, m, policy='fail')
    m.assert_not_awaited()
    assert await e.fs.read_file_bytes(LIVE, ctx=e.ctx) == b'# Old chapter'


async def test_pinned_plan_blocks_replacement_of_its_tree(prepared):
    e, p, m = await two_revision_setup(prepared)
    pinned = await p.prepare_sources(e.ctx, 'manual-wiki', [ROOT], input_kind='uri',
                                     idempotency_key='pinned-reader')
    with pytest.raises(FailedPreconditionError, match='prepared build plan'):
        await prepare(e, p, m)
    assert await e.fs.read_file_bytes(LIVE, ctx=e.ctx) == b'# Old chapter'
    assert await p.check_current_inputs(e.ctx, pinned.build_id) == pinned


async def test_replacement_capture_requires_markdown_output(prepared):
    e, p = prepared
    await e.fs.write_file_bytes(LIVE, b'# Old chapter', ctx=e.ctx)
    empty = AsyncMock()  # parser wrote nothing under staging
    with pytest.raises(SchemaError):
        await prepare(e, p, empty)
    empty.assert_awaited_once()
    assert await e.fs.read_file_bytes(LIVE, ctx=e.ctx) == b'# Old chapter'
