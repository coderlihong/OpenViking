"""Explicit local full-manual Wiki-plan replay using test disk/ACL substitutes.

Run with PYTHONPATH=tests/enterprise_kb .venv/bin/python -m pytest
  docs/spec/tools/replay_wiki_plan.py --rootdir=. --junitxml=<report.xml>
OV_WIKI_PLAN_REPLAY_SOURCE may point to the already parsed manual directory.
This verifies preparation, not production authorization or Wiki extraction.
"""
import os
import json, hashlib, shutil, zipfile
from pathlib import Path
from dataclasses import replace
import pytest
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding
from openviking.server.identity import Role
from openviking.wiki.schema import SchemaBundle
from openviking.wiki.catalog import WikiCatalog
from openviking.wiki.build_plan import WikiBuildPlanner

@pytest.mark.asyncio
async def test_full_manual_preparation(environment):
 e=environment
 e.fs._async_agfs.root.mkdir(parents=True,exist_ok=True)
 async def listing(path,offset=0,limit=None,sort_by=None,sort_order='asc'):
  children=sorted(e.fs._async_agfs.path(path).iterdir(),key=lambda p:p.name,reverse=sort_order=='desc')
  children=children[offset:] if limit is None else children[offset:offset+limit]
  return [{'name':p.name,'isDir':p.is_dir(),'size':p.stat().st_size} for p in children]
 e.fs._async_agfs.ls=listing
 repo=Path(__file__).resolve().parents[3];source=Path(os.environ.get('OV_WIKI_PLAN_REPLAY_SOURCE', '/tmp/openviking-enterprise-pdf/full-chapters/full-manual-chapters/SY215C_SY021NCB62788_维修手册_zh'))
 root='viking://resources/raw/SY215C_SY021NCB62788_维修手册_zh'
 shutil.copytree(source,e.fs._async_agfs.path(e.fs._uri_to_path(root,ctx=e.ctx)))
 z=repo/'docs/spec/evidence/sany-schema-adapter-I020.zip'
 with zipfile.ZipFile(z) as archive:
  files={n:archive.read(n).decode('utf8') for n in archive.namelist() if not n.endswith('/')}
 bundle=SchemaBundle.freeze('sany','2+openviking.1',files)
 catalog=WikiCatalog(e.fs);admin=replace(e.ctx,role=Role.ADMIN)
 await catalog.register_schema(admin,bundle)
 await catalog.register_collection(admin,collection_id='sany',namespace_id='sany-wiki',target_uri='viking://resources/wiki/sany',raw_sources_prefix='viking://resources/raw',schema_id=bundle.schema_id,schema_version=bundle.version)
 planner=WikiBuildPlanner(e.fs)
 a=await planner.prepare_sources(e.ctx,'sany',[root],input_kind='upload',idempotency_key='upload')
 b=await planner.prepare_sources(e.ctx,'sany',[root],input_kind='uri',idempotency_key='uri')
 assert a.sources==b.sources
 expected={root+'/'+str(p.relative_to(source)) for p in source.rglob('*.md') if not any(part.startswith('.') for part in p.relative_to(source).parts)}
 assert {s.uri for s in a.sources}==expected
 assert len(a.sources)==968
 assert await planner.check_current_inputs(e.ctx,b.build_id)==b
 revisions={s.uri:s for s in a.sources}
 streamed=set()
 async for uri,text in planner.iter_source_texts(e.ctx,a.build_id):
  assert hashlib.sha256(text.encode()).hexdigest()==revisions[uri].sha256
  streamed.add(uri)
 assert streamed==expected
 result={'run_id':'I025-full-manual-plan','status':'PARTIAL_PASS','sources':len(a.sources),'source_bytes':sum(s.size for s in a.sources),'directories':len(json.loads(a.manifest)['directories']),'schema_digest':bundle.digest,'schema_capability_state':json.loads(a.manifest)['schema_capability_state'],'namespace_authorization_state':json.loads(a.manifest)['authorization_state'],'upload_digest':a.digest,'uri_digest':b.digest,'equivalence':'Same fixed schema binding and all source revisions','verification':'All expected Markdown URIs captured; every streamed immutable source matches its digest; current-source preflight passes','fixtures':['Actual full MinerU-derived manual and adapted Sany schema','Disk AGFS transport, SQLite and VikingFS methods; ACL mock'],'excluded':['Actual extraction/domain rules, namespace/export registry, Wiki commit/index/HTTP E2E','Full-manual chapter alignment quality, production AGFS encryption/ACL']}
 (repo/'docs/spec/evidence/wiki-plan-full-I025-2026-10-04.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
