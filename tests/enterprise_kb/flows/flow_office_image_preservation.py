"""Live I051 regression for source-visible image omission and strict provenance.

This preserves a synthetic XLS visible raster. It cannot accept model quality,
legacy font completeness, vector-only figures or unverified chapter assignment.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from tests.enterprise_kb.flows import flow_d1, flow_w1_pdf, flow_office_legacy
from openviking_sdk.client import AsyncHTTPClient

ROOT=Path('/tmp/ov-flow/office-image-I051')
OUT=Path('docs/spec/evidence/office-image-preservation-I051-2026-10-05.json')

async def list_files(client,root):
    files=[]
    for entry in await client.ls(root):
        uri=entry.get('uri') or root.rstrip('/')+'/'+entry['name']
        if entry.get('isDir'):files.extend(await list_files(client,uri))
        else:files.append(uri)
    return files


def registry_image_receipts(prefix,expected_account):
    configuration=json.loads(Path('/Users/wh/.openviking/ov.conf').read_text())
    path=Path(configuration['embedding']['chunking']['publication_path']).expanduser()
    result=[]
    with sqlite3.connect(f'file:{path}?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        for snapshot in db.execute('SELECT s.account,s.id,s.manifest FROM heads h JOIN snapshots s ON s.id=h.snapshot WHERE s.account=?',(expected_account,)):
            for uri,item in json.loads(snapshot['manifest']).items():
                if not uri.startswith(prefix+'/') or not str((item.get('content') or {}).get('image_plan_version','')).startswith('image-occurrences'):continue
                generation=item.get('generation');build=db.execute('SELECT expected,state FROM builds WHERE generation=?',(generation,)).fetchone()
                receipts=[row[0] for row in db.execute('SELECT child FROM receipts WHERE generation=?',(generation,))]
                content=item.get('content') or {}
                result.append({'uri':uri,'generation':generation,'snapshot':snapshot['id'],'build_state':build['state'] if build else None,'expected':json.loads(build['expected']) if build else [],'receipts':receipts,'content':content})
    return result

async def main():
    ROOT.mkdir(parents=True,exist_ok=True)
    key=flow_office_legacy.read_key(Path('/tmp/ov-flow/probe_w1.py'))
    original=Path('docs/spec/evidence/office-legacy-I050-artifacts/legacy-d1-I050-xls.xls')
    files={flow:ROOT/f'legacy-{flow}-I051-visible-image.xls' for flow in ['d1','w1']}
    for path in files.values():
        if not path.exists():shutil.copyfile(original,path)
    report={'sample_class':'synthetic_not_gold','original_model_quality_failure_retained':True,
        'legacy_binary_font_validation':'NOT_RUN','enabled_backend':'mineru','other_backend_preservation':'NOT_RUN',
        'legacy_sha256':hashlib.file_digest(original.open('rb'),'sha256').hexdigest(),'expected_account_id':'flowtest','expected_visible_image_count':1,'runs':{}}
    for flow in ['d1','w1']:
        cache=ROOT/f'api-{flow}.json'
        if cache.exists():result=json.loads(cache.read_text())
        elif flow=='d1':
            result=await flow_d1.run(SimpleNamespace(pdf=str(files[flow]),url='http://127.0.0.1:1933',key=key,account=None,user=None,backend='mineru',timeout=1800,query='source PDF image SY215C hydraulic pump pressure'))
        else:
            result=await flow_w1_pdf.run(SimpleNamespace(pdf=str(files[flow]),url='http://127.0.0.1:1933',key=key,collection='sany',job_id=None,build_id=None,ingest_key='I051-visible-image-xls-ingest',exec_key='I051-visible-image-xls-exec',timeout=1800,deadline_seconds=1800,poll_attempts=None,poll_interval=3,target_prefix='viking://resources/wiki/sany',out=None))
        cache.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str));report['runs'][flow]=result
        OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str));print(flow,result.get('passed'),flush=True)
    client=AsyncHTTPClient(url='http://127.0.0.1:1933',api_key=key,timeout=1800);await client.initialize()
    try:
        root=report['runs']['d1']['add_resource']['root_uri'];all_files=await list_files(client,root)
        images=[uri for uri in all_files if uri.lower().endswith(('.png','.jpg','.jpeg'))]
        records=[]
        for uri in images:
            data=await client.download_bytes(uri)
            records.append({'uri':uri,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'signature_hex':data[:12].hex()})
        manifest_text=await client.read_raw(root+'/_manifest.json');manifest=json.loads(manifest_text)
        report['d1_materialized_images']=records;report['d1_manifest']=manifest
        w1_uris=report['runs']['w1'].get('captured_uris',[])
        w1_images=[uri for uri in await list_files(client,f'viking://resources/raw/{files["w1"].stem}') if uri.lower().endswith(('.png','.jpg','.jpeg'))]
        report['w1_materialized_images']=w1_images
        report['assert_d1_visible_image_materialized']=len(records)==report['expected_visible_image_count'] and all(record['signature_hex'].startswith('89504e47') for record in records)
        report['assert_w1_visible_image_materialized']=len(w1_images)==report['expected_visible_image_count']
        report['joint_image_publication']=registry_image_receipts(root,report['expected_account_id'])
        report['assert_joint_image_published']=bool(report['joint_image_publication']) and all(record['build_state']=='published' and set(record['expected'])==set(record['receipts']) and any(member.startswith('image:') for member in record['receipts']) and bool(record['content'].get('image_semantics')) and bool(record['content'].get('image_context_epochs')) for record in report['joint_image_publication'])
        results=await client.search(query='source PDF preserved image red rectangle',target_uri=root,limit=20)
        report['image_search_matches']=[hit for hit in results.get('resources',[]) if hit.get('uri') in images]
        report['assert_image_uri_searchable']=bool(report['image_search_matches'])
    finally:await client.close()
    report['passed']=all(result.get('passed') is True for result in report['runs'].values()) and all(value is True for key,value in report.items() if key.startswith('assert_'))
    OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str));print('complete',report['passed'],flush=True)
    return report

if __name__=='__main__':
    result=asyncio.run(main());raise SystemExit(0 if result['passed'] else 1)
