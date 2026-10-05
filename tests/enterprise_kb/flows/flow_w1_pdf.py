"""FLOW-W1 (PDF materialized upload) acceptance harness.

Uploads a PDF through the temp-upload channel; the Wiki ingestion endpoint must
run the shared document parser and materialize the derived Markdown tree under
the collection raw prefix, then capture it as an immutable W1 source plan and
execute it to the terminal ``draft_ready`` state.

Asserts:
* the plan is prepared and captures one or more nested Markdown documents under
  ``raw_sources_prefix/<stem>/``
* execution reaches ``draft_ready`` as a W1 job
* every draft and candidate is checked against captured materialized Viking URIs
* terminal receipts remain private and cannot publish diagnostic knowledge

Usage:
    python tests/enterprise_kb/flows/flow_w1_pdf.py --pdf /path/to.pdf \
        --url http://127.0.0.1:1933 --key <user_key>

Resume without changing source epochs:
    python tests/enterprise_kb/flows/flow_w1_pdf.py --job-id <job_id> \
        --build-id <build_id> --url http://127.0.0.1:1933 --key <user_key>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "sdk" / "python"))

from openviking_sdk.client import AsyncHTTPClient  # noqa: E402

RAW = "viking://resources/raw"
TARGET = "viking://resources/wiki/sany"

TERMINAL = {"draft_ready", "failed", "cancelled", "completed", "write_recovery_required"}


async def poll_job(kb, job_id, *, deadline_seconds, poll_interval, poll_attempts=None,
                   clock=time.monotonic, sleep=asyncio.sleep):
    """Return a terminal job or an explicit observation timeout, never invent failure."""
    started, attempts = clock(), 0
    while True:
        final = await kb.get_ingestion_job(job_id)
        attempts += 1
        if (final.get("state") or final.get("status")) in TERMINAL:
            return final, None
        elapsed = clock() - started
        if elapsed >= deadline_seconds or (poll_attempts is not None and attempts >= poll_attempts):
            return final, {"reason": "observation_deadline_exceeded" if elapsed >= deadline_seconds
                           else "poll_attempt_limit_exceeded", "elapsed_seconds": round(elapsed, 3),
                           "poll_count": attempts, "job_still_active": True,
                           "next_step": f"Resume validation with --job-id {job_id}; do not upload again."}
        await sleep(min(poll_interval, deadline_seconds - elapsed))


async def validate_drafts(kb, job_id, final, captured, target, source_texts):
    checkpoint = final.get("checkpoint") or {}
    receipt = checkpoint.get("draft_receipt") or {}
    receipts = receipt.get("receipts") or []
    candidates, counts_match, sources_match = [], True, True
    for index, item in enumerate(receipts):
        draft = await kb.get_wiki_draft(job_id, index)
        items = draft.get("candidates") or []
        counts_match &= type(item.get("candidate_count")) is int and item["candidate_count"] == len(items)
        sources_match &= (draft.get("source_uri") == item.get("source_uri")
                          and draft.get("source_uri") in captured
                          and draft.get("source_chunk") == item.get("source_chunk")
                          and draft.get("build_id") == receipt.get("build_id")
                          and draft.get("plan_digest") == receipt.get("plan_digest"))
        candidates.extend(items)
    traceable = bool(candidates) and all(
        bool(c.get("evidence")) and all(e.get("source_uri") in captured
            and bool(e.get("quote")) for e in c["evidence"])
        and bool(c.get("contributors")) and all(u in captured for u in c["contributors"])
        for c in candidates)
    return {"draft_count": len(receipts), "candidate_count": len(candidates),
        "candidate_uris": [c.get("uri") for c in candidates],
        "assert_all_drafts_read": bool(receipts),
        "assert_candidate_counts_match": counts_match and type(receipt.get("candidate_count")) is int
            and receipt["candidate_count"] == len(candidates),
        "assert_receipt_sources_match": bool(receipts) and sources_match,
        "assert_has_candidate": bool(candidates), "assert_source_traceable": traceable,
        "assert_quotes_verbatim": bool(candidates) and all(
            bool(c.get("evidence")) and all(isinstance(e.get("quote"), str)
                and bool(e["quote"].strip()) and e.get("source_uri") in source_texts
                and e["quote"] in source_texts[e["source_uri"]]
                for e in c["evidence"]) for c in candidates),
        "assert_draft_under_target": bool(candidates) and all(
            str(c.get("uri", "")).startswith(target.rstrip("/") + "/") for c in candidates),
        "assert_private_no_diagnostic_publish": receipt.get("private") is True
            and receipt.get("state") == "draft_ready"
            and receipt.get("diagnostic_publishable") is False
            and checkpoint.get("diagnostic_published") is False
            and bool(receipts) and all(r.get("private") is True
                and r.get("state") == "draft_ready" for r in receipts)}


async def run(args) -> dict:
    log: dict = {"args": {k: v for k, v in vars(args).items() if k != "key"}}
    client = AsyncHTTPClient(url=args.url, api_key=args.key, timeout=args.timeout)
    await client.initialize()
    try:
        if args.job_id:
            job = await client.kb.get_ingestion_job(args.job_id)
            build_id = args.build_id or (job.get("payload") or {}).get("build_id")
            if not build_id:
                raise ValueError("Existing job does not expose a build_id; supply --build-id")
            plan = await client.kb.get_wiki_build(build_id)
            log["resumed_existing_job"] = True
        else:
            temp_file_id = await client._upload_temp_file(args.pdf)
            log["temp_file_id"] = temp_file_id
            plan = await client.kb.ingest_wiki(
                temp_file_id=temp_file_id, collection_id=args.collection,
                update_policy="replace", idempotency_key=args.ingest_key)
            job = await client.kb.execute_wiki_build(
                plan["build_id"], expected_plan_digest=plan["digest"],
                idempotency_key=args.exec_key, profile="draft")
        log["plan"] = {"build_id": plan.get("build_id"), "state": plan.get("state")}
        log["assert_plan_prepared"] = plan.get("state") == "prepared"
        manifest_value = plan.get("manifest") or {}
        manifest = json.loads(manifest_value) if isinstance(manifest_value, str) else manifest_value
        captured = sorted(s.get("uri", "") for s in manifest.get("sources", []))
        root = f"{RAW}/{Path(args.pdf).stem}" if args.pdf else None
        log["captured_uris"], log["captured_count"] = captured, len(captured)
        log["assert_captured_nested_md"] = bool(captured) and all(
            u.startswith((root or RAW) + "/") and u.lower().endswith(".md")
            and len(u.removeprefix(RAW + "/").split("/")) >= 2 for u in captured)
        log["assert_captured_multiple"] = len(captured) >= 2
        job_id = job.get("job_id") or job.get("id")
        log["job_id"], log["job_kind"] = job_id, job.get("kind")
        log["assert_job_kind_w1"] = job.get("kind") == "W1"
        final, observation_timeout = await poll_job(client.kb, job_id,
            deadline_seconds=args.deadline_seconds, poll_interval=args.poll_interval,
            poll_attempts=args.poll_attempts)
        log["job_final"] = {"state": final.get("state") or final.get("status"),
                            "kind": final.get("kind"), "error": final.get("error"),
                            "stage": final.get("stage")}
        if observation_timeout:
            log["observation_timeout"] = observation_timeout
        log["assert_draft_ready"] = log["job_final"]["state"] == "draft_ready"
        if log["assert_draft_ready"]:
            # Read each captured source once; do not store private source bodies in logs.
            source_texts = {uri: await client.read_raw(uri) for uri in set(captured)}
            log.update(await validate_drafts(client.kb, job_id, final, set(captured),
                                             args.target_prefix, source_texts))
    finally:
        await client.close()
    log["passed"] = all(v for k, v in log.items() if k.startswith("assert_"))
    return log


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--pdf", help="PDF path for a new upload; optional when resuming")
    p.add_argument("--job-id", help="Validate an existing job without uploading or executing again")
    p.add_argument("--build-id", help="Existing prepared plan ID, if absent from the job payload")
    p.add_argument("--target-prefix", default=TARGET)
    p.add_argument("--url", default="http://127.0.0.1:1933")
    p.add_argument("--key", required=True)
    p.add_argument("--collection", default="sany")
    p.add_argument("--ingest-key", default="flow-w1pdf-ingest")
    p.add_argument("--exec-key", default="flow-w1pdf-exec")
    p.add_argument("--timeout", type=float, default=1800.0)
    p.add_argument("--deadline-seconds", type=float, default=1800.0,
                   help="Total observation window; extraction of many fragments can exceed 5 minutes")
    p.add_argument("--poll-attempts", type=int, default=None,
                   help="Optional additional poll cap; default is governed by the deadline")
    p.add_argument("--poll-interval", type=float, default=3.0)
    p.add_argument("--out", default="/tmp/flow_w1_pdf.json")
    args = p.parse_args()
    if not args.job_id and not args.pdf:
        p.error("provide --pdf for a new upload or --job-id to resume")
    if args.deadline_seconds <= 0 or args.poll_interval <= 0 or (args.poll_attempts is not None and args.poll_attempts <= 0):
        p.error("deadline, interval and optional poll cap must be positive")
    log = asyncio.run(run(args))
    Path(args.out).write_text(json.dumps(log, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(log, ensure_ascii=False, indent=2, default=str))
    return 0 if log["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
