"""FLOW-W1 acceptance harness: upload a Markdown source and build a Wiki.

Uploads a Markdown document through the temp-upload channel, materializes it under
the registered collection raw prefix, prepares the immutable source plan, executes
the W1 extraction job, and asserts the reachable acceptance criteria:

* the upload is materialized at ``raw_sources_prefix/<name>/<name>``
* the prepared plan is bound to the registered collection/schema
* execution reaches the terminal ``draft_ready`` state
* the draft carries entity candidates whose evidence quotes the source verbatim
  and whose ``source_uri`` is the materialized Viking URI

Usage:
    python tests/enterprise_kb/flows/flow_w1.py \
        --url http://127.0.0.1:1933 --key <user_key>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "sdk" / "python"))

from openviking_sdk.client import AsyncHTTPClient  # noqa: E402

RAW = "viking://resources/raw"
TARGET = "viking://resources/wiki/sany"
NAME = "w1-manual.md"
MATERIALIZED = f"{RAW}/{NAME}/{NAME}"

SRC_BODY = """# SY215C 行走系统维修

## 行走跑偏诊断

行走跑偏多由行走马达内泄或履带张紧度不一致引起，应先测量两侧压力差再判断。
"""

TERMINAL = {"draft_ready", "failed", "cancelled", "completed"}


async def run(args) -> dict:
    log: dict = {"args": {k: v for k, v in vars(args).items() if k != "key"}}
    client = AsyncHTTPClient(url=args.url, api_key=args.key, timeout=args.timeout)
    await client.initialize()
    try:
        tmp = Path(args.tmp_dir)
        tmp.mkdir(parents=True, exist_ok=True)
        local = tmp / NAME
        local.write_text(SRC_BODY, encoding="utf-8")
        temp_file_id = await client._upload_temp_file(str(local))
        log["temp_file_id"] = temp_file_id

        plan = await client.kb.ingest_wiki(
            temp_file_id=temp_file_id, collection_id=args.collection,
            update_policy="replace", idempotency_key=args.ingest_key)
        log["plan"] = {"build_id": plan.get("build_id"), "state": plan.get("state")}
        log["assert_plan_prepared"] = plan.get("state") == "prepared"

        manifest = json.loads(plan.get("manifest") or "{}")
        source_uris = [s.get("uri") for s in manifest.get("sources", [])]
        log["materialized_uris"] = source_uris
        log["assert_materialized"] = source_uris == [MATERIALIZED]

        job = await client.kb.execute_wiki_build(
            plan["build_id"], expected_plan_digest=plan["digest"],
            idempotency_key=args.exec_key, profile="draft")
        job_id = job.get("job_id") or job.get("id")
        log["job_id"] = job_id
        log["job_kind"] = job.get("kind")
        log["assert_job_kind_w1"] = job.get("kind") == "W1"

        final = None
        for _ in range(args.poll_attempts):
            state = await client.kb.get_ingestion_job(job_id)
            final = state
            if (state.get("state") or state.get("status")) in TERMINAL:
                break
            await asyncio.sleep(args.poll_interval)
        log["job_final"] = {"state": final.get("state") or final.get("status"),
                            "kind": final.get("kind"), "error": final.get("error")}
        log["assert_draft_ready"] = log["job_final"]["state"] == "draft_ready"

        if log["assert_draft_ready"]:
            draft = await client.kb.get_wiki_draft(job_id, 0)
            candidates = draft.get("candidates") or []
            log["candidate_count"] = len(candidates)
            log["candidate_uris"] = [c.get("uri") for c in candidates]
            log["assert_has_candidate"] = bool(candidates)
            log["assert_source_traceable"] = all(
                any(e.get("source_uri") == MATERIALIZED for e in (c.get("evidence") or []))
                for c in candidates) and bool(candidates)
            log["assert_evidence_verbatim"] = all(
                e.get("quote") in SRC_BODY
                for c in candidates for e in (c.get("evidence") or [])) and bool(candidates)
            log["assert_draft_under_target"] = all(
                str(c.get("uri", "")).startswith(TARGET + "/") for c in candidates) and bool(candidates)
    finally:
        await client.close()
    log["passed"] = all(v for k, v in log.items() if k.startswith("assert_"))
    return log


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:1933")
    p.add_argument("--key", required=True)
    p.add_argument("--collection", default="sany")
    p.add_argument("--ingest-key", default="flow-w1-ingest")
    p.add_argument("--exec-key", default="flow-w1-exec")
    p.add_argument("--tmp-dir", default="/tmp/ov-flow/w1")
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument("--poll-attempts", type=int, default=100)
    p.add_argument("--poll-interval", type=float, default=3.0)
    p.add_argument("--out", default="/tmp/flow_w1.json")
    args = p.parse_args()
    log = asyncio.run(run(args))
    Path(args.out).write_text(json.dumps(log, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(log, ensure_ascii=False, indent=2, default=str))
    return 0 if log["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())