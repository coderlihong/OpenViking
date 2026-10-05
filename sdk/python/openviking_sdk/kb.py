"""Experimental enterprise knowledge-base configuration namespace.

Schema/collection methods configure inputs; they do not start Wiki builds.
"""
from urllib.parse import quote
from typing import Literal
from ._utils import run_async


class AsyncKBNamespace:
    def __init__(self, client):
        self._client = client

    async def create_ingestion_job(self, *, namespace_id: str, kind: Literal['D1', 'D2'],
                                   idempotency_key: str, payload: dict, options: dict | None = None):
        """Submit durable document ingestion or URI indexing execution."""
        body = {'namespace_id': namespace_id, 'kind': kind,
                'idempotency_key': idempotency_key, 'payload': payload}
        if options is not None:
            body['options'] = options
        response = await self._client._request('POST', '/api/v1/kb/ingestion-jobs', json=body)
        return self._client._handle_response(response)

    async def get_ingestion_job(self, job_id: str):
        response = await self._client._request('GET', f'/api/v1/kb/jobs/{quote(job_id, safe="")}')
        return self._client._handle_response(response)

    async def list_ingestion_jobs(self, *, namespace_id: str | None = None, limit: int = 100):
        """List only current-user jobs still authorized under current source ACLs."""
        params = {'limit': limit}
        if namespace_id is not None:
            params['namespace_id'] = namespace_id
        response = await self._client._request('GET', '/api/v1/kb/jobs', params=params)
        return self._client._handle_response(response)

    async def cancel_ingestion_job(self, job_id: str):
        response = await self._client._request('POST', f'/api/v1/kb/jobs/{quote(job_id, safe="")}/cancel')
        return self._client._handle_response(response)

    async def resolve_parse_quarantine(self, job_id: str):
        """Release a quarantined parse slot; the server refuses while source writes are pending."""
        response = await self._client._request(
            'POST', f'/api/v1/kb/jobs/{quote(job_id, safe="")}/parse-quarantine/resolve')
        return self._client._handle_response(response)

    async def register_schema(self, *, schema_id: str, version: str, files: dict[str, str]):
        response = await self._client._request('POST', '/api/v1/kb/wiki/schemas', json={
            'schema_id': schema_id, 'version': version, 'files': files})
        return self._client._handle_response(response)

    async def get_wiki_schema(self, schema_id: str, version: str):
        response = await self._client._request('GET', f'/api/v1/kb/wiki/schemas/{quote(schema_id, safe="")}/{quote(version, safe="")}')
        return self._client._handle_response(response)

    async def create_wiki_collection(self, *, collection_id: str, namespace_id: str, target_uri: str,
                                     raw_sources_prefix: str, schema_id: str, schema_version: str):
        response = await self._client._request('POST', '/api/v1/kb/wiki/collections', json={
            'collection_id':collection_id, 'namespace_id':namespace_id, 'target_uri':target_uri,
            'raw_sources_prefix':raw_sources_prefix, 'schema_id':schema_id, 'schema_version':schema_version})
        return self._client._handle_response(response)

    async def get_wiki_collection(self, collection_id: str):
        response = await self._client._request('GET', f'/api/v1/kb/wiki/collections/{quote(collection_id, safe="")}')
        return self._client._handle_response(response)

    async def inspect_wiki_collection_migration(self, collection_id: str):
        response = await self._client._request(
            'GET', f'/api/v1/kb/wiki/collections/{quote(collection_id, safe="")}/migration-inspection')
        return self._client._handle_response(response)

    async def migrate_wiki_collection_domains(self, collection_id: str, *, expected_binding_digest: str):
        response = await self._client._request(
            'POST', f'/api/v1/kb/wiki/collections/{quote(collection_id, safe="")}/migrate-domains',
            json={'expected_binding_digest': expected_binding_digest})
        return self._client._handle_response(response)

    async def build_wiki(self, *, source_uris: list[str], collection_id: str, recursive: bool = True,
                         idempotency_key: str):
        """Prepare immutable URI inputs; return a prepared plan, not a job.

        idempotency_key is a 1–128 character ASCII catalog identifier using
        letters, digits, underscore, dot, plus or hyphen. The server validates
        this contract and rejects extra or invalid fields before preparation.
        """
        response = await self._client._request('POST', '/api/v1/kb/wiki/builds', json={
            'source_uris': source_uris, 'collection_id': collection_id,
            'recursive': recursive, 'idempotency_key': idempotency_key})
        return self._client._handle_response(response)

    async def ingest_wiki(self, *, temp_file_id: str, collection_id: str, update_policy: Literal['fail', 'replace'] = 'fail',
                          idempotency_key: str):
        """Prepare an uploaded source; use the same key contract as build_wiki."""
        response = await self._client._request('POST', '/api/v1/kb/wiki/ingestions', json={
            'temp_file_id': temp_file_id, 'collection_id': collection_id,
            'update_policy': update_policy, 'idempotency_key': idempotency_key})
        return self._client._handle_response(response)

    async def get_wiki_build(self, build_id: str):
        response = await self._client._request(
            'GET', f'/api/v1/kb/wiki/builds/{quote(build_id, safe="")}')
        return self._client._handle_response(response)

    async def execute_wiki_build(self, build_id: str, *, expected_plan_digest: str,
                                 idempotency_key: str, profile: Literal['draft', 'strict_diagnostic'] = 'draft'):
        response = await self._client._request('POST',
            f'/api/v1/kb/wiki/builds/{quote(build_id, safe="")}/execute', json={
                'expected_plan_digest': expected_plan_digest, 'idempotency_key': idempotency_key,
                'profile': profile})
        return self._client._handle_response(response)

    async def get_wiki_draft(self, job_id: str, draft_index: int):
        response = await self._client._request('GET',
            f'/api/v1/kb/wiki/jobs/{quote(job_id, safe="")}/drafts/{draft_index}')
        return self._client._handle_response(response)

    async def register_namespace(self, *, namespace_id: str, root_uri: str):
        response = await self._client._request('POST', '/api/v1/kb/namespaces', json={
            'namespace_id': namespace_id, 'root_uri': root_uri})
        return self._client._handle_response(response)

    async def get_namespace(self, namespace_id: str):
        response = await self._client._request('GET', f'/api/v1/kb/namespaces/{quote(namespace_id, safe="")}')
        return self._client._handle_response(response)

    async def set_export(self, *, source_namespace: str, target_namespace: str, principal: str, allowed: bool):
        response = await self._client._request(
            'POST', f'/api/v1/kb/namespaces/{quote(source_namespace, safe="")}/exports',
            json={'target_namespace': target_namespace, 'principal': principal, 'allowed': allowed})
        return self._client._handle_response(response)


class SyncKBNamespace:
    def __init__(self, client):
        self._async = client._async_client.kb

    def create_ingestion_job(self, *, namespace_id: str, kind: Literal['D1', 'D2'],
                             idempotency_key: str, payload: dict, options: dict | None = None):
        return run_async(self._async.create_ingestion_job(namespace_id=namespace_id, kind=kind,
            idempotency_key=idempotency_key, payload=payload, options=options))

    def get_ingestion_job(self, job_id: str):
        return run_async(self._async.get_ingestion_job(job_id))

    def list_ingestion_jobs(self, *, namespace_id: str | None = None, limit: int = 100):
        return run_async(self._async.list_ingestion_jobs(namespace_id=namespace_id, limit=limit))

    def cancel_ingestion_job(self, job_id: str):
        return run_async(self._async.cancel_ingestion_job(job_id))

    def resolve_parse_quarantine(self, job_id: str):
        return run_async(self._async.resolve_parse_quarantine(job_id))

    def register_schema(self, *, schema_id: str, version: str, files: dict[str, str]):
        return run_async(self._async.register_schema(schema_id=schema_id, version=version, files=files))

    def get_wiki_schema(self, schema_id: str, version: str):
        return run_async(self._async.get_wiki_schema(schema_id, version))

    def create_wiki_collection(self, *, collection_id: str, namespace_id: str, target_uri: str,
                               raw_sources_prefix: str, schema_id: str, schema_version: str):
        return run_async(self._async.create_wiki_collection(collection_id=collection_id, namespace_id=namespace_id,
            target_uri=target_uri, raw_sources_prefix=raw_sources_prefix, schema_id=schema_id, schema_version=schema_version))

    def get_wiki_collection(self, collection_id: str):
        return run_async(self._async.get_wiki_collection(collection_id))

    def inspect_wiki_collection_migration(self, collection_id: str):
        return run_async(self._async.inspect_wiki_collection_migration(collection_id))

    def migrate_wiki_collection_domains(self, collection_id: str, *, expected_binding_digest: str):
        return run_async(self._async.migrate_wiki_collection_domains(
            collection_id, expected_binding_digest=expected_binding_digest))

    def build_wiki(self, *, source_uris: list[str], collection_id: str, recursive: bool = True,
                   idempotency_key: str):
        return run_async(self._async.build_wiki(source_uris=source_uris, collection_id=collection_id,
            recursive=recursive, idempotency_key=idempotency_key))

    def ingest_wiki(self, *, temp_file_id: str, collection_id: str, update_policy: Literal['fail', 'replace'] = 'fail',
                    idempotency_key: str):
        return run_async(self._async.ingest_wiki(temp_file_id=temp_file_id, collection_id=collection_id,
            update_policy=update_policy, idempotency_key=idempotency_key))

    def register_namespace(self, *, namespace_id: str, root_uri: str):
        return run_async(self._async.register_namespace(namespace_id=namespace_id, root_uri=root_uri))

    def get_wiki_build(self, build_id: str):
        return run_async(self._async.get_wiki_build(build_id))

    def execute_wiki_build(self, build_id: str, *, expected_plan_digest: str,
                           idempotency_key: str, profile: Literal['draft', 'strict_diagnostic'] = 'draft'):
        return run_async(self._async.execute_wiki_build(build_id, expected_plan_digest=expected_plan_digest,
            idempotency_key=idempotency_key, profile=profile))

    def get_wiki_draft(self, job_id: str, draft_index: int):
        return run_async(self._async.get_wiki_draft(job_id, draft_index))

    def get_namespace(self, namespace_id: str):
        return run_async(self._async.get_namespace(namespace_id))

    def set_export(self, *, source_namespace: str, target_namespace: str, principal: str, allowed: bool):
        return run_async(self._async.set_export(source_namespace=source_namespace, target_namespace=target_namespace,
            principal=principal, allowed=allowed))
