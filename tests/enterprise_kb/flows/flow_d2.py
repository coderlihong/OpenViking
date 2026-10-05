"""FLOW-D2 acceptance harness: reindex an existing Viking Markdown URI.

Writes a long Markdown document directly to a Viking URI (no upload, no
parse/OCR), then reindexes it non-recursively as a single file and again
recursively over its directory, asserting the reachable acceptance criteria:

* reindex does not invoke OCR / does not rewrite the body (byte-identical read)
* vectors are rebuilt with no failures
* search returns hits that reference the same original Viking URI

Not covered here (still NOT_RUN): relative-image shared semantic chain, and
the L0/L1 overview regeneration check.

Usage:
    python tests/enterprise_kb/flows/flow_d2.py \
        --url http://127.0.0.1:1933 --key <user_key> --account <acct> --user <user>
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

DIR_URI = "viking://resources/mdflow"
MD_URI = f"{DIR_URI}/manual.md"
QUERY = "液压泵的检修间隙标准"

MD_BODY = """# 维修手册 Markdown 直入库

本文件用于 FLOW-D2：直接把已经存在于 Viking 的 Markdown 做向量构建，不经过 OCR。

## 液压泵检修

液压泵检修时应检查柱塞与缸体的配合间隙，标准间隙为 0.02–0.05 mm，
超过 0.08 mm 需要更换柱塞副。测量前应清洁表面并保持油温在 20–40 ℃。

## 液压阀检修

液压阀的阀芯与阀孔间隙标准为 0.01–0.03 mm，阀芯拉伤超过 0.05 mm 应更换。

## 紧固力矩

| 部件 | 力矩(N·m) |
| --- | --- |
| 缸盖螺栓 | 120 |
| 泵体螺栓 | 85 |
"""


async def run(args) -> dict:
    log: dict = {"args": vars(args)}
    client = AsyncHTTPClient(
        url=args.url, api_key=args.key, account=args.account, user=args.user,
        timeout=args.timeout,
    )
    await client.initialize()
    try:
        await client.mkdir(DIR_URI)
        await client.write(MD_URI, MD_BODY, mode="replace", wait=True, timeout=args.timeout)
        before = await client.read_raw(MD_URI)

        t0 = time.time()
        r_file = await client.reindex(
            uri=MD_URI, mode="semantic_and_vectors", wait=True, recursive=False
        )
        log["reindex_file"] = {
            "status": r_file.get("status"),
            "scanned_records": r_file.get("scanned_records"),
            "rebuilt_records": r_file.get("rebuilt_records"),
            "failed_records": r_file.get("failed_records"),
            "unsupported_records": r_file.get("unsupported_records"),
            "elapsed_s": round(time.time() - t0, 1),
        }
        log["assert_file_reindex_clean"] = (
            r_file.get("status") == "completed" and (r_file.get("failed_records") or 0) == 0
        )

        t1 = time.time()
        r_dir = await client.reindex(
            uri=DIR_URI, mode="semantic_and_vectors", wait=True, recursive=True
        )
        log["reindex_dir"] = {
            "status": r_dir.get("status"),
            "rebuilt_records": r_dir.get("rebuilt_records"),
            "failed_records": r_dir.get("failed_records"),
            "elapsed_s": round(time.time() - t1, 1),
        }
        log["assert_dir_reindex_clean"] = (
            r_dir.get("status") == "completed" and (r_dir.get("failed_records") or 0) == 0
        )

        after = await client.read_raw(MD_URI)
        log["assert_body_untouched"] = before == after

        sres = await client.search(query=QUERY, limit=8)
        hits = sres.get("resources") or []
        log["search_uris"] = [h.get("uri") for h in hits]
        log["assert_uri_traceable"] = bool(hits) and all(
            str(h.get("uri", "")).startswith("viking://") for h in hits
        )
        log["assert_hits_include_source"] = any(MD_URI in str(h.get("uri", "")) for h in hits)
    finally:
        await client.close()
    log["passed"] = all(v for k, v in log.items() if k.startswith("assert_"))
    return log


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:1933")
    p.add_argument("--key", required=True)
    p.add_argument("--account", default=None)
    p.add_argument("--user", default=None)
    p.add_argument("--timeout", type=float, default=900.0)
    p.add_argument("--out", default="/tmp/flow_d2.json")
    args = p.parse_args()
    log = asyncio.run(run(args))
    Path(args.out).write_text(json.dumps(log, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(log, ensure_ascii=False, indent=2, default=str))
    return 0 if log["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())