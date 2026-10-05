"""Opt-in real configured model contract probe; synthetic source, not gold.

Run explicitly with OPENVIKING_WIKI_REAL_MODEL_PROBE=1. Credentials remain only
in the configured VLM object; evidence contains allowlisted provider/model data.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

import pytest
from test_content_publication import environment as environment
from test_index_plan import embedding as embedding
from test_wiki_build_plan import prepared as prepared
from test_wiki_namespaces import domains as domains
from test_wiki_extraction import execute_setup

from openviking.wiki.extraction import ExtractionError, ExtractionLimits
from openviking_cli.utils.config.open_viking_config import OpenVikingConfigSingleton


@pytest.mark.skipif(os.environ.get('OPENVIKING_WIKI_REAL_MODEL_PROBE') != '1', reason='explicit real-model probe only')
async def test_real_configured_model_extracts_synthetic_fixed_source(domains):
    report = {'id':'wiki-extraction-real-model-probe', 'timestamp':datetime.now(timezone.utc).isoformat(),
              'sample_class':'synthetic', 'gold':False, 'quality_claim':False, 'performance_claim':False,
              'actual_model':True, 'status':'FAIL'}
    started = time.monotonic()
    output = Path(os.environ.get('OPENVIKING_WIKI_PROBE_REPORT','docs/spec/evidence/wiki-extraction-real-model-2026-10-05.json'))
    try:
        config = OpenVikingConfigSingleton._load_from_file(os.environ.get('OPENVIKING_WIKI_MODEL_CONFIG','/Users/wh/.openviking/ov.conf')).get_wiki_model()
        _, provider = config.get_provider_config()
        model_name = config.credentials[0].model or config.model if config.credentials else config.model
        report['model_profile'] = {'model':model_name, 'provider':provider, 'temperature':config.temperature}
        e,p,c,a,b,plan,model,x = await execute_setup(domains, model=config, limits=ExtractionLimits(timeout_seconds=180,retries=0))
        x.model_profile = report['model_profile']
        report['plan_digest'] = plan.digest
        report['sources'] = [{'uri':source.uri,'sha256':source.sha256,'size':source.size} for source in plan.sources]
        result = await x.extract(e.ctx,plan.build_id,expected_plan_digest=plan.digest)
        report['configuration'] = result['configuration']
        report['candidate_count'] = result['candidate_count']
        report['state'] = result['state']
        report['authorization_revision'] = result['authorization_revision']
        report['diagnostic_publishable'] = result['diagnostic_publishable']
        report['receipts'] = result['receipts']
        # Independent reader uses service-side hash/account/plan/ACL verification.
        verified = [await x.read_draft(e.ctx,plan.build_id,expected_plan_digest=plan.digest,receipt=receipt)
                    for receipt in result['receipts']]
        report['private_read_verified'] = True
        report['entity_types'] = [item['kind'] for payload in verified for item in payload['candidates']]
        report['synthetic_candidate_pages'] = [item for payload in verified for item in payload['candidates']]
        report['prompt_sha256'] = [payload['prompt_sha256'] for payload in verified]
        assert result['candidate_count'] > 0, 'Synthetic fixture contains explicit diagnostic knowledge'
        assert result['private'] and result['state'] == 'draft_ready' and not result['diagnostic_publishable']
        report['status'] = 'CONTRACT_PASS'
    except BaseException as exc:
        # Provider errors can contain credential headers; never persist raw messages.
        report['failure_type'] = type(exc).__name__
        if isinstance(exc, ExtractionError):
            report['failure_code'] = exc.code
        raise
    finally:
        report['elapsed_seconds'] = round(time.monotonic()-started,3)
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
