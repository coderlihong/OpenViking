"""Actual VikingFS reads/SQL epochs with disk AGFS and trusted-catalog fixtures."""
import hashlib
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from test_content_publication import environment as environment, URI, finish, stage
from test_index_plan import embedding as embedding

from openviking.wiki.resolver import ReferenceBinding, VikingReferenceResolver
from openviking.wiki.schema import SchemaError
from openviking_cli.exceptions import PermissionDeniedError, FailedPreconditionError

RAW='viking://resources/raw/source.md'
FAULT='viking://resources/wiki/Fault/f.md'
SCHEMA='a'*64


async def resolver(e,uri=RAW,body='real source',binding=None):
    await e.fs.write(uri,body,ctx=e.ctx)
    binding=binding or ReferenceBinding('raw','viking://resources/raw')
    return VikingReferenceResolver(e.fs,e.ctx,AsyncMock(return_value=binding))


async def test_real_source_capture_reads_current_authorized_bytes_and_epoch(environment):
    e=environment;r=await resolver(e)
    target=await r(RAW)
    assert target.account_id=='acct' and target.namespace_id=='raw'
    assert target.authorized and target.has_content and not target.entity_validated
    assert hashlib.sha256(b'real source').hexdigest() in target.revision
    assert str(e.store.observe_draft('acct',RAW)) == target.revision.rsplit(':',1)[-1]
    assert e.fs._ensure_access.await_count >= 3


@pytest.mark.parametrize('valid', [True,False])
async def test_entity_validation_is_bound_to_exact_current_body(environment,valid):
    e=environment
    binding=ReferenceBinding('wiki','viking://resources/wiki','Fault',hashlib.sha256(b'entity body').hexdigest(),SCHEMA)
    r=await resolver(e,FAULT,'entity body' if valid else 'changed body',binding)
    target=await r(FAULT)
    assert target.entity_validated is valid
    assert target.validation_schema_hash==(SCHEMA if valid else None)


async def test_capture_uses_latest_working_source_not_published_projection(environment):
    e=environment
    await finish(e,await stage(e,'published old'))
    await e.fs.write(URI,'working new',ctx=e.ctx)
    binding=ReferenceBinding('manual',URI)
    r=VikingReferenceResolver(e.fs,e.ctx,AsyncMock(return_value=binding))
    target=await r(URI)
    assert hashlib.sha256(b'working new').hexdigest() in target.revision
    assert await e.fs.read_file(URI,ctx=e.ctx)=='published old'


async def test_reference_capture_rejects_completed_write_during_read(environment):
    e=environment;r=await resolver(e)
    read=e.fs._async_agfs.read
    async def changed(path,*args,**kwargs):
        body=await read(path,*args,**kwargs)
        await e.fs.write(RAW,'new body',ctx=e.ctx)
        return body
    e.fs._async_agfs.read=changed
    with pytest.raises(SchemaError,match='changed'):
        await r(RAW)


async def test_inflight_and_uncertain_fences_block_reference_capture(environment):
    e=environment;r=await resolver(e)
    token=e.store.begin_write('acct',RAW)
    with pytest.raises(FailedPreconditionError,match='in-flight'):
        await r(RAW)
    e.store.uncertain_write(token)
    with pytest.raises(FailedPreconditionError,match='uncertain'):
        await r(RAW)


async def test_access_denial_prevents_catalog_and_content_reads(environment):
    e=environment;r=await resolver(e)
    e.fs._ensure_access=AsyncMock(side_effect=PermissionDeniedError('denied'))
    with pytest.raises(PermissionDeniedError):
        await r(RAW)
    r.lookup.assert_not_awaited()


async def test_access_revoked_after_read_is_not_attested(environment):
    e=environment;r=await resolver(e)
    e.fs._ensure_access=AsyncMock(side_effect=[None,None,PermissionDeniedError('revoked')])
    with pytest.raises(PermissionDeniedError):
        await r(RAW)


async def test_catalog_root_mismatch_and_empty_content_fail_closed(environment):
    e=environment
    r=await resolver(e,binding=ReferenceBinding('wiki','viking://resources/wiki'))
    with pytest.raises(SchemaError,match='outside'):
        await r(RAW)
    r=await resolver(e,body='  \n')
    assert not (await r(RAW)).has_content


def test_background_bypass_cannot_replace_original_request_authorization(environment):
    e=environment
    with pytest.raises(SchemaError,match='original authorized'):
        VikingReferenceResolver(e.fs,replace(e.ctx,bypass_acl=True),AsyncMock())
