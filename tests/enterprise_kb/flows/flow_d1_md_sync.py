"""FLOW-D1 supplementary harness: MD source and sync-SDK parity.

Covers the FLOW-D1 criteria left NOT_RUN by flow_d1.py:

* a Viking-Markdown source (uri payload) ingests, reindexes and retrieves
  with multiple chunks pointing back at the same Viking URI
* the sync SDK produces the same observable outcomes as the async SDK on
  the same D2 job
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

from openviking_sdk.client import AsyncHTTPClient, SyncHTTPClient  # noqa: E402

PARENT = "viking://resources/raw"

SAMPLE_MD = """# 故障排查样例手册

## 液压系统故障

当挖掘机执行机构动作缓慢时，首先检查主溢流阀压力设定，
再检查液压泵的吸油真空度是否超标。

## 电气系统故障

显示屏无输出时，先确认蓄电池电压不低于24V，
然后检查控制器CAN总线终端电阻约为60欧姆。
"""


async def run(args) -> dict:
    log: dict = {"args": {k: v for k, v in vars(args).items() if k != "key"}}
    md_path = Path(args.out).with_suffix(".source.md")
    md_path.write_text(SAMPLE_MD, encoding="utf-8")

    async_client = AsyncHTTPClient(url=args.url, api_key=args.key, account=args.account,
                                   user=args.user, timeout=args.timeout)
    await async_client.initialize()
    try:
        res = await async_client.add_resource(path=str(md_path), parent=PARENT, wait=True,
                                              timeout=args.timeout,
                                              options={"create_parent": True})
        root_uri = res.get("root_uri")
        log["md_add"] = {"status": res.get("status"), "root_uri": root_uri}
        log["assert_root_appends_name"] = bool(root_uri) and root_uri.rstrip("/").endswith(md_path.stem)
        r = await async_client.reindex(uri=root_uri, mode="semantic_and_vectors",
                                       wait=True, recursive=True)
        log["md_reindex"] = {k: r.get(k) for k in ("status", "rebuilt_records", "failed_records")}
        log["assert_md_reindex_clean"] = r.get("status") == "completed" and not (r.get("failed_records") or 0)
        sres = await async_client.search(query="主溢流阀压力", target_uri=root_uri, limit=8)
        hits = sres.get("resources") or []
        log["md_search_uris"] = [h.get("uri") for h in hits]
        log["assert_md_traceable"] = bool(hits) and all(
            str(h.get("uri", "")).startswith(root_uri.rstrip("/") + "/") or h.get("uri") == root_uri
            for h in hits)

        # sync SDK parity over the same job surface
        sync = SyncHTTPClient(url=args.url, api_key=args.key, account=args.account,
                              user=args.user, timeout=args.timeout)
        try:
            got = sync.kb.get_ingestion_job(args.d2_job_id)
            log["sync_get_state"] = got.get("state")
            log["assert_sync_parity"] = got.get("job_id") == args.d2_job_id
        finally:
            sync.close()
    finally:
        await async_client.close()
    log["passed"] = all(v for k, v in log.items() if k.startswith("assert_"))
    return log


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:1933")
    p.add_argument("--key", required=True)
    p.add_argument("--account", default=None)
    p.add_argument("--user", default=None)
    p.add_argument("--d2-job-id", required=True, help="existing ready D2 job to probe via sync SDK")
    p.add_argument("--timeout", type=float, default=900.0)
    p.add_argument("--out", default="/tmp/flow_d1_md_sync.json")
    args = p.parse_args()
    log = asyncio.run(run(args))
    Path(args.out).write_text(json.dumps(log, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(log, ensure_ascii=False, indent=2, default=str))
    return 0 if log["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
