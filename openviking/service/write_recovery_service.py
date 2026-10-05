# SPDX-License-Identifier: AGPL-3.0
"""Authorized facade; execution proof always comes from coordinator adapters."""
from openviking.storage.acl import AclAction
from openviking.wiki.namespaces import NamespaceCatalog
from openviking.wiki.validation import source_uri
from openviking_cli.exceptions import PermissionDeniedError


class WriteRecoveryService:
    def __init__(self, fs, registry):
        self.fs, self.registry = fs, registry

    async def _authorize(self, ctx, uri):
        if ctx.bypass_acl:
            raise PermissionDeniedError('Original actor context required for write recovery')
        await self.fs._ensure_access(uri, ctx, action=AclAction.WRITE)
        await NamespaceCatalog(self.fs, registry=self.registry).lookup(ctx, uri, action=AclAction.MANAGE)
        await self.fs._ensure_access(uri, ctx, action=AclAction.WRITE)

    async def diagnose(self, ctx, uri):
        uri = source_uri(uri)
        await self._authorize(ctx, uri)
        return self.registry.write_recovery_diagnose(ctx.account_id, uri)

    async def recover(self, ctx, *, uri, tokens, idempotency_key):
        uri = source_uri(uri)
        await self._authorize(ctx, uri)
        intent = self.registry.write_recovery_prepare(ctx.account_id, uri, ctx.user.user_id, tokens, idempotency_key)
        await self._authorize(ctx, uri)
        # Current authorization is checked again by the coordinator's own
        # configured identity/ACL provider around backend verification and CAS.
        return self.registry.write_recovery_execute(ctx.account_id, intent['id'], ctx.user.user_id)

    async def get(self, ctx, recovery):
        intent = self.registry.write_recovery_get(ctx.account_id, recovery)
        await self._authorize(ctx, intent['uri'])
        return {'recovery_id': intent['id'], 'uri': intent['uri'], 'state': intent['state'], 'result': intent['result']}
