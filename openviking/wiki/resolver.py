# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Authorized working-content reference capture for the shared Wiki builder.

Bindings must come from a trusted namespace/entity catalog, never model output.
This module does not provide that catalog, export authorization or publication.
"""
from dataclasses import dataclass
import hashlib
from typing import Awaitable, Callable

from openviking.indexing.content import draft_sources
from openviking.indexing.publication import configured_store
from openviking.wiki.schema import SchemaError
from openviking.wiki.validation import ResolvedReference, source_uri


@dataclass(frozen=True)
class ReferenceBinding:
    namespace_id: str
    root_uri: str
    kind: str | None = None
    validated_sha256: str | None = None
    validation_schema_hash: str | None = None


BindingLookup = Callable[[str], Awaitable[ReferenceBinding]]


class VikingReferenceResolver:
    def __init__(self, fs, ctx, lookup: BindingLookup, *, registry=None):
        if ctx.bypass_acl:
            raise SchemaError('Wiki reference capture requires the original authorized request context')
        self.fs, self.ctx, self.lookup = fs, ctx, lookup
        self.registry = registry if registry is not None else configured_store()
        if self.registry is None:
            raise SchemaError('Durable source registry required for Wiki reference capture')

    async def __call__(self, uri):
        uri = source_uri(uri)
        # Prevent a catalog lookup from bypassing current source authorization.
        await self.fs._ensure_access(uri, self.ctx)
        binding = await self.lookup(uri)
        if not isinstance(binding, ReferenceBinding) or not binding.namespace_id:
            raise SchemaError('Source namespace is not registered')
        root = source_uri(binding.root_uri)
        if uri != root and not uri.startswith(root + '/'):
            raise SchemaError('Reference is outside its registered namespace root')
        before = self.registry.observe_draft(self.ctx.account_id, uri)
        with draft_sources():
            raw = await self.fs.read_file_bytes(uri, ctx=self.ctx)
        # Recheck authorization and all standard writes spanning the actual read.
        await self.fs._ensure_access(uri, self.ctx)
        if self.registry.observe_draft(self.ctx.account_id, uri) != before:
            raise SchemaError('Reference changed while reading')
        digest = hashlib.sha256(raw).hexdigest()
        verified = bool(binding.kind and binding.validated_sha256 == digest
                        and isinstance(binding.validation_schema_hash, str)
                        and len(binding.validation_schema_hash) == 64
                        and all(c in '0123456789abcdef' for c in binding.validation_schema_hash))
        return ResolvedReference(binding.kind, self.ctx.account_id, binding.namespace_id,
                                 f'sha256:{digest}:epoch:{before}', True, bool(raw.strip()),
                                 verified, binding.validation_schema_hash if verified else None)
