"""Build the immutable SANY path adapter and replay its actual examples."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import zipfile

from openviking.wiki.schema import SchemaBundle, SchemaError, adapt_sany_path_policies, compile_bundle
from openviking.wiki.policies import policy_coverage, check_build_profile
from openviking.wiki.validation import inspect_entity
from openviking.wiki.path_policy import execute_path_policy
from openviking.wiki.schema import _yaml

PATHS = {
    'Component': 'Component/动臂油缸/动臂油缸.md',
    'ConfigSpec': 'ConfigSpec/SSY100000402/index.md',
    'Device': 'Device/SY135/SY135C/index.md',
    'DTC': 'DTC/SY35E_SSY100000823/E828.md',
    'Fault': 'Fault/ControlFailure/诊断测试.md',
    'Procedure': 'Procedure/inspection/诊断测试.md',
    'Profile': 'Symptom/动力与发动机/发动机冒白烟/profiles/国四和新欧五.md',
    'Symptom': 'Symptom/动力与发动机/发动机冒白烟/index.md',
}


def build(input_zip, output_dir):
    with zipfile.ZipFile(input_zip) as z:
        members = [i for i in z.infolist() if not i.is_dir()]
        if (len(members) > 256 or len({i.filename for i in members}) != len(members)
                or any(i.file_size > 1024*1024 for i in members)
                or sum(i.file_size for i in members) > 16*1024*1024):
            raise ValueError('Invalid schema archive bounds')
        old = SchemaBundle.freeze('sany', '2+openviking.1+structural.1',
                                  {i.filename:z.read(i).decode() for i in members})
    new = adapt_sany_path_policies(old)
    output = Path(output_dir); output.mkdir(parents=True,exist_ok=True)
    archive = output/'sany-schema-adapter-I049.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for n,t in new.members:
            i = zipfile.ZipInfo(n,date_time=(2026,10,5,0,0,0));i.compress_type=zipfile.ZIP_DEFLATED
            z.writestr(i,t.encode())
    root='viking://resources/sany-path-test';results=[]
    for schema in compile_bundle(new):
        markdown=dict(new.members)[schema.kind+'.fewshot.md'].replace('viking://resources/<ns>',root)
        uri=root+'/'+PATHS[schema.kind]
        metadata=_yaml(markdown.split('---',2)[1])
        layout=execute_path_policy(schema,uri,metadata,markdown,root)
        try:
            draft=inspect_entity(schema,uri,markdown,namespace_root=root)
            structure={'state':'PASS','policy_receipts':[asdict(r) for r in draft.policy_receipts]}
        except SchemaError as exc:
            structure={'state':'REJECTED','reason':str(exc)}
        results.append({'kind':schema.kind,'path_receipt':asdict(layout),'structure':structure,
                        'pending_rules':list(schema.uncompiled_rules)})
    try:
        check_build_profile(new,'strict_diagnostic');strict='PASS'
    except SchemaError as exc:
        strict={'state':'REJECTED','reason':str(exc)}
    report={'implementation':'I049','input_digest':old.digest,'adapter_digest':new.digest,
            'adapter_version':new.version,'zip_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
            'pending_before':sum(len(s.uncompiled_rules) for s in compile_bundle(old)),
            'pending_after':sum(len(s.uncompiled_rules) for s in compile_bundle(new)),
            'entity_example_replay':results,'coverage':policy_coverage(new),'strict_profile':strict,
            'scope':'Eight path layouts and local field alignment executed; no regex/prose execution or source mapping invented',
            'dtc_source_mapping':'NOT_RUN: original kb://chapters source remains explicitly rejected by structural validation',
            'quality_gold':'NOT_RUN','diagnostic_publication':'NOT_RUN','live_api_deployment':'NOT_RUN'}
    (output/'wiki-schema-I049-2026-10-05.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--input',default='docs/spec/evidence/sany-schema-adapter-I026.zip');p.add_argument('--output',default='docs/spec/evidence');a=p.parse_args()
    r=build(a.input,a.output)
    print(json.dumps({k:r[k] for k in ['adapter_digest','pending_before','pending_after','adapter_version']},ensure_ascii=False))
