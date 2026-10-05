"""Real-service rejection contract for the complete frozen SANY schema.

This checks refusal of unsafe diagnostic execution, not diagnostic quality or publication.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'sdk' / 'python'))
from openviking_sdk.client import AsyncHTTPClient
from openviking_sdk.errors import NotFoundError

async def run(args):
    from openviking.wiki.schema import SchemaBundle
    from openviking.wiki.policies import policy_coverage
    path = Path(args.schema_archive)
    with zipfile.ZipFile(path) as archive:
        files = {n: archive.read(n).decode('utf-8') for n in archive.namelist()}
    bundle = SchemaBundle.freeze('sany-policy-flow', 'i026', files)
    coverage = policy_coverage(bundle)
    client = AsyncHTTPClient(url=args.url, api_key=args.key, timeout=120)
    await client.initialize()
    target = 'viking://resources/wiki/policy-flow'
    try:
        try:
            await client.stat(target)
        except NotFoundError:
            await client.mkdir(target)
        # ACL roots require a real context record. Bootstrap an innocuous page
        # and its directory summaries before registering the restricted domain.
        await client.write(target + '/index.md', '# Policy gate acceptance fixture\n', wait=True)
        await client.reindex(target, recursive=True, mode='semantic_and_vectors', wait=True)
        target_acl = await client.acl_set(target, entries=[{'principal':args.principal,'level':'manage'}], acl_mode='restricted')
        await client.kb.register_namespace(namespace_id='sany-policy-flow', root_uri=target)
        # Each collection owns an independent upload write root. Existing raw
        # sources remain usable by W2 through the explicit export below.
        raw = 'viking://resources/policy-raw'
        try:
            await client.stat(raw)
        except NotFoundError:
            await client.mkdir(raw)
        await client.write(raw + '/index.md', '# Independent policy upload fixture\n', wait=True)
        await client.reindex(raw, recursive=True, mode='semantic_and_vectors', wait=True)
        await client.acl_set(raw, entries=[{'principal':args.principal,'level':'manage'}], acl_mode='restricted')
        await client.kb.register_namespace(namespace_id='sany-policy-raw', root_uri=raw)
        registered = await client.kb.register_schema(schema_id=bundle.schema_id, version=bundle.version, files=files)
        await client.kb.create_wiki_collection(collection_id='sany-policy-flow', namespace_id='sany-policy-flow',
            target_uri=target, raw_sources_prefix=raw,
            schema_id=bundle.schema_id, schema_version=bundle.version)
        await client.kb.set_export(source_namespace='sany-raw', target_namespace='sany-policy-flow',
            principal=args.principal, allowed=True)
        plan = await client.kb.build_wiki(source_uris=[args.source_uri], collection_id='sany-policy-flow',
            recursive=False, idempotency_key='policy-flow-prepare')
        before = await client.kb.list_ingestion_jobs(namespace_id='sany-policy-flow')
        response = await client._request('POST', f"/api/v1/kb/wiki/builds/{plan['build_id']}/execute", json={
            'expected_plan_digest':plan['digest'], 'idempotency_key':'policy-flow-strict', 'profile':'strict_diagnostic'})
        error = response.json().get('error', {})
        after = await client.kb.list_ingestion_jobs(namespace_id='sany-policy-flow')
        after_plan = await client.kb.get_wiki_build(plan['build_id'])
        report = {'scope':'real API strict policy rejection only, not complete FLOW or quality acceptance',
            'schema_digest':bundle.digest,'archive_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'entity_kinds':[e['kind'] for e in coverage['entities']],
            'pending_rule_instances':sum(len(e['uncompiled_rules']) for e in coverage['entities']),
            'build_id':plan['build_id'],'http_status':response.status_code,'error_code':error.get('code'),
            'reason':'SCHEMA_POLICY_PENDING' if error.get('message','').startswith('SCHEMA_POLICY_PENDING:') else 'unexpected',
            'assertions':{'acl_after_reindex_succeeds':target_acl.get('acl_mode')=='restricted',
                'schema_pending':registered.get('capability_state')=='draft_rules_pending',
                'rejected_before_execution':response.status_code==400 and error.get('code')=='INVALID_ARGUMENT'
                    and error.get('message','').startswith('SCHEMA_POLICY_PENDING:'),
                'no_job_created':before==after, 'fixed_plan_unchanged':after_plan==plan}}
        report['passed']=all(report['assertions'].values())
        return report
    finally:
        await client.close()

if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--url',default='http://127.0.0.1:1933')
    p.add_argument('--key',required=True)
    p.add_argument('--principal',default='user:flowadmin')
    p.add_argument('--source-uri',default='viking://resources/raw/manual.md')
    p.add_argument('--schema-archive',default='docs/spec/evidence/sany-schema-adapter-I026.zip')
    p.add_argument('--out',default='/tmp/flow-wiki-strict-gate.json')
    args=p.parse_args();r=asyncio.run(run(args))
    Path(args.out).write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(r,ensure_ascii=False,indent=2))
    raise SystemExit(0 if r['passed'] else 1)
