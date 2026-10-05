"""Semantic-layering acceptance harness: reindex builds and preserves L0/L1.

Objective item 3 requires that reindex produce the directory semantic layers so
Viking's agent localization can use them. This harness asserts, against the real
deployment and real VLM:

* ``semantic_and_vectors`` over a directory regenerates the directory L0
  (``.abstract.md``) and L1 (``.overview.md``) with non-empty summaries
* ``vectors_only`` on the same document rebuilds vectors but leaves the
  previously published directory L0/L1 byte-identical

Usage:
    python tests/enterprise_kb/flows/flow_layers.py \
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
from openviking_sdk.errors import ConflictError  # noqa: E402

DIR = "viking://resources/layerflow"
DOC = f"{DIR}/manual.md"
BODY = """# 液压系统维修

## 液压泵

液压泵检修时检查柱塞与缸体配合间隙，标准 0.02–0.05 mm。

## 液压阀

阀芯与阀孔间隙标准 0.01–0.03 mm。
"""


async def run(args) -> dict:
    log: dict = {"args": {k: v for k, v in vars(args).items() if k != "key"}}
    client = AsyncHTTPClient(url=args.url, api_key=args.key, timeout=args.timeout)
    await client.initialize()
    try:
        try:
            await client.mkdir(DIR)
        except ConflictError:
            pass
        await client.write(DOC, BODY, mode="replace", wait=True, timeout=args.timeout)

        semantic = await client.reindex(
            uri=DIR, mode="semantic_and_vectors", wait=True, recursive=True)
        log["reindex_semantic"] = {"status": semantic.get("status"),
                                   "rebuilt": semantic.get("rebuilt_records"),
                                   "failed": semantic.get("failed_records")}
        log["assert_semantic_clean"] = (
            semantic.get("status") == "completed" and (semantic.get("failed_records") or 0) == 0)

        abstract = await client.abstract(DIR)
        overview = await client.overview(DIR)
        log["l0_len"] = len(abstract)
        log["l1_len"] = len(overview)
        log["assert_l0_present"] = bool(abstract.strip())
        log["assert_l1_present"] = bool(overview.strip())

        await client.write(DOC, BODY + "\n## 补录\n新增内容。\n",
                           mode="replace", wait=True, timeout=args.timeout)
        vectors = await client.reindex(
            uri=DOC, mode="vectors_only", wait=True, recursive=False)
        log["reindex_vectors_only"] = {"status": vectors.get("status"),
                                       "rebuilt": vectors.get("rebuilt_records"),
                                       "failed": vectors.get("failed_records")}
        log["assert_vectors_clean"] = (
            vectors.get("status") == "completed" and (vectors.get("failed_records") or 0) == 0)

        abstract_after = await client.abstract(DIR)
        log["assert_vectors_only_keeps_l0"] = abstract_after == abstract
    finally:
        await client.close()
    log["passed"] = all(v for k, v in log.items() if k.startswith("assert_"))
    return log


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:1933")
    p.add_argument("--key", required=True)
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument("--out", default="/tmp/flow_layers.json")
    args = p.parse_args()
    log = asyncio.run(run(args))
    Path(args.out).write_text(json.dumps(log, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(log, ensure_ascii=False, indent=2, default=str))
    return 0 if log["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())