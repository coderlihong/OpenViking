# SPDX-License-Identifier: AGPL-3.0
"""Explicit recovery API; request models intentionally contain no proof/hash."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from openviking.indexing.publication import configured_store
from openviking.server.auth import get_request_context
from openviking.server.dependencies import get_service
from openviking.server.identity import RequestContext
from openviking.server.models import Response
from openviking.service.write_recovery_service import WriteRecoveryService
from openviking.wiki.schema import SchemaError
from openviking_cli.exceptions import FailedPreconditionError, InvalidArgumentError

router = APIRouter(prefix='/api/v1/kb/write-recovery', tags=['write-recovery-experimental'])


class RecoveryDiagnosisRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    uri: StrictStr


class RecoveryRequest(RecoveryDiagnosisRequest):
    tokens: list[StrictStr] = Field(min_length=1, max_length=128)
    idempotency_key: StrictStr = Field(pattern=r'^[A-Za-z0-9_.+\-]{1,128}$')


def get_write_recovery_service():
    fs, registry = get_service().viking_fs, configured_store()
    if fs is None or registry is None:
        raise FailedPreconditionError('Write recovery requires initialized VikingFS and authority registry')
    return WriteRecoveryService(fs, registry)


@router.post('/diagnostics')
async def diagnose(body: RecoveryDiagnosisRequest, ctx: RequestContext = Depends(get_request_context),
                   service: WriteRecoveryService = Depends(get_write_recovery_service)):
    try:
        result = await service.diagnose(ctx, body.uri)
    except SchemaError as exc:
        raise InvalidArgumentError(str(exc)) from exc
    return Response(status='ok', result=result)


@router.post('/recoveries')
async def recover(body: RecoveryRequest, ctx: RequestContext = Depends(get_request_context),
                  service: WriteRecoveryService = Depends(get_write_recovery_service)):
    try:
        result = await service.recover(ctx, **body.model_dump())
    except SchemaError as exc:
        raise InvalidArgumentError(str(exc)) from exc
    return Response(status='ok', result=result)


@router.get('/recoveries/{recovery_id}')
async def get_recovery(recovery_id: str, ctx: RequestContext = Depends(get_request_context),
                       service: WriteRecoveryService = Depends(get_write_recovery_service)):
    return Response(status='ok', result=await service.get(ctx, recovery_id))
