# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Knowledge root/export configuration over existing enabled resource ACLs."""
from dataclasses import asdict

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from openviking.indexing.publication import configured_store
from openviking.server.auth import get_request_context
from openviking.server.dependencies import get_service
from openviking.server.identity import RequestContext
from openviking.server.models import Response
from openviking.wiki.namespaces import NamespaceCatalog
from openviking.wiki.schema import SchemaError
from openviking_cli.exceptions import FailedPreconditionError, InvalidArgumentError

router = APIRouter(prefix='/api/v1/kb/namespaces', tags=['knowledge-namespace-configuration'])


class NamespaceRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    namespace_id: StrictStr = Field(min_length=1, max_length=128)
    root_uri: StrictStr = Field(min_length=1, max_length=4096)


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target_namespace: StrictStr = Field(min_length=1, max_length=128)
    principal: StrictStr = Field(min_length=1, max_length=256)
    allowed: StrictBool


def get_namespace_catalog():
    fs = get_service().viking_fs
    registry = configured_store()
    if fs is None or registry is None:
        raise FailedPreconditionError('Namespace configuration requires initialized VikingFS and durable publication registry')
    return NamespaceCatalog(fs, registry=registry)


@router.post('')
async def register_namespace(body: NamespaceRequest, ctx: RequestContext = Depends(get_request_context), catalog: NamespaceCatalog = Depends(get_namespace_catalog)):
    try:
        result = await catalog.register(ctx, **body.model_dump())
    except SchemaError as exc:
        raise InvalidArgumentError(str(exc)) from exc
    return Response(status='ok', result=asdict(result))


@router.get('/{namespace_id}')
async def get_namespace(namespace_id: str, ctx: RequestContext = Depends(get_request_context), catalog: NamespaceCatalog = Depends(get_namespace_catalog)):
    try:
        result = await catalog.get(ctx, namespace_id)
    except SchemaError as exc:
        raise InvalidArgumentError(str(exc)) from exc
    return Response(status='ok', result=asdict(result))


@router.post('/{source_namespace}/exports')
async def set_export(source_namespace: str, body: ExportRequest, ctx: RequestContext = Depends(get_request_context), catalog: NamespaceCatalog = Depends(get_namespace_catalog)):
    try:
        result = await catalog.set_export(ctx, source_namespace=source_namespace, **body.model_dump())
    except SchemaError as exc:
        raise InvalidArgumentError(str(exc)) from exc
    return Response(status='ok', result=result)
