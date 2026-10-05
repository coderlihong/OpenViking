"""Live SDK durable index batch probe; a published batch is never document ready.

Supply credentials through OV_FLOW_API_KEY. This writes a synthetic source under
an already registered test namespace, observes real model/authority execution,
and can later inspect the same durable job after a controlled server restart.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import time

from openviking_sdk import AsyncHTTPClient


async def run(args):
    key = os.environ['OV_FLOW_API_KEY']
    client = AsyncHTTPClient(url=args.url, api_key=key, account=args.account,
                             user=args.user, timeout=120)
    await client.initialize()
    report = {'kind': 'D2', 'profile': 'synthetic_live_protocol_probe',
              'full_flow_acceptance': False, 'started_at': time.time(),
              'source_uri': args.uri, 'job_id': args.job_id}
    if args.job_id and args.output.exists():
        previous = json.loads(args.output.read_text())
        if previous.get('job_id') == args.job_id and previous.get('sources_sha256'):
            report['sources_sha256'] = previous['sources_sha256']
    try:
        if not args.job_id:
            content = '# Durable joint publication probe\n\n'
            content += 'DURABLE-I051: stop the engine before inspecting hydraulic lines.\n' * 20
            sources = {args.uri: content}
            if args.directory:
                await client.mkdir(args.uri)
                sources = {args.uri + '/chapter-one.md': content,
                           args.uri + '/chapter-two.md': '# Inspection\n\nCheck the oil pressure and record the reading.\n'}
            for uri, body in sources.items():
                await client.write(uri, body, wait=True, options={'processing_mode': 'none'})
            report['sources_sha256'] = {uri: hashlib.sha256(body.encode()).hexdigest()
                                        for uri, body in sources.items()}
            if not args.directory:
                report['source_sha256'] = report['sources_sha256'][args.uri]
            submitted = await client.kb.create_ingestion_job(
                namespace_id=args.namespace, kind='D2', idempotency_key=args.key,
                payload={'uri': args.uri, 'mode': args.mode, 'recursive': args.directory})
            args.job_id = submitted['job_id']
            report['job_id'] = args.job_id
        deadline = time.monotonic() + args.timeout
        transitions = []
        while time.monotonic() < deadline:
            job = await client.kb.get_ingestion_job(args.job_id)
            checkpoint = job.get('checkpoint', {})
            publication = checkpoint.get('index_publication', {})
            transition = [job['state'], job['stage'], publication.get('state')]
            if not transitions or transitions[-1] != transition:
                transitions.append(transition)
            report.update(transitions=transitions, final_state=job['state'],
                          stage=job['stage'], attempt=job['attempt'],
                          job_updated_at=job['updated'],
                          publication_batch_id=checkpoint.get('publication_batch_id'),
                          index_result=checkpoint.get('index_result'),
                          index_publication=publication,
                          publication_gate=checkpoint.get('publication_gate'),
                          reason_code=job.get('reason_code'), error=job.get('error'))
            args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False))
            if (publication.get('state') == 'published' and job['attempt'] >= args.min_attempt
                    or job['state'] in {'failed', 'cancelled'}):
                break
            await asyncio.sleep(1)
        read = await client.ls(args.uri) if args.directory else await client.read(args.uri)
        if args.directory:
            report['directory_listing_count'] = len(read)
        elif isinstance(read, dict):
            report['read_type'] = sorted(read)
        else:
            report['read_sha256'] = hashlib.sha256(str(read).encode()).hexdigest()
        publication = report['index_publication']
        gate = report.get('publication_gate') or {}
        gate_verified = gate.get('verified') is True
        report['assertions'] = {
            'batch_published': publication.get('state') == 'published',
            'actual_members_nonempty': publication.get('member_count', 0) > 0,
            'actual_receipts_complete': publication.get('all_submitted_members_complete') is True,
            'receipt_counts_match': publication.get('expected_count', 0) > 0 and
                publication.get('expected_count') == publication.get('received_count'),
            # The evidence gate must state a decision either way: verified means
            # the committed receipts prove every coverage category; otherwise
            # the job stays resumable with a pending reason, never self-ready.
            'gate_decision_present': (gate_verified or gate.get('code') == 'PUBLICATION_GATE_PENDING')
                and bool(gate.get('batch_id')),
            'ready_matches_gate': (report['final_state'] == 'ready') == gate_verified,
            'gate_pending_when_unverified': gate_verified or
                gate.get('code') == 'PUBLICATION_GATE_PENDING',
        }
        if report.get('sources_sha256'):
            report['read_sources_sha256'] = {uri: hashlib.sha256(str(await client.read(uri)).encode()).hexdigest()
                                             for uri in report['sources_sha256']}
            report['assertions']['source_bytes_preserved'] = report['read_sources_sha256'] == report['sources_sha256']
        report['passed'] = all(report['assertions'].values())
    finally:
        report['finished_at'] = time.time()
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        await client.close()
    print(json.dumps(report, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:1933')
    parser.add_argument('--account', default='flowtest')
    parser.add_argument('--user', default='flowadmin')
    parser.add_argument('--namespace', default='sany-raw')
    parser.add_argument('--uri', default='viking://resources/raw/durable-joint-I051.md')
    parser.add_argument('--key', default='durable-joint-I051')
    parser.add_argument('--job-id')
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--min-attempt', type=int, default=1)
    parser.add_argument('--mode', choices=['vectors_only', 'semantic_and_vectors'], default='vectors_only')
    parser.add_argument('--directory', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
    main()
