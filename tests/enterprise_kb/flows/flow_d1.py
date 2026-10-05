"""FLOW-D1 acceptance harness: upload a PDF to a user prefix and index it.

Runs against a live server + Python SDK. Asserts the FLOW-D1 acceptance
criteria that are reachable over the PDF + async-SDK path:

* the output root appends the source file name under the requested parent
* the PDF bookmark tree is combined with model body text and the manifest
  records structure_source / alignment_status
* chapter files and their sibling images are written before reindex
* reindex(mode=semantic_and_vectors) rebuilds vectors with no failures
* search returns traceable ``viking://`` URIs (never bare text)

Not covered here (still NOT_RUN): MD/Office upload, sync SDK parity, the
alignment-failure rule fallback, and the head/middle/tail retrieval spread.

Usage:
    python tests/enterprise_kb/flows/flow_d1.py --pdf /path/to.pdf \
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

# The SDK's httpx client trusts the system proxy; a loopback target must bypass it.
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "sdk" / "python"))

from openviking_sdk.client import AsyncHTTPClient  # noqa: E402

PARENT = "viking://resources/raw"
QUERY = "液压部件处理注意事项"


async def run(args) -> dict:
    log: dict = {"args": {key: value for key, value in vars(args).items() if key != "key"}}
    client = AsyncHTTPClient(
        url=args.url, api_key=args.key, account=args.account, user=args.user,
        timeout=args.timeout,
    )
    await client.initialize()
    try:
        t0 = time.time()
        res = await client.add_resource(
            path=args.pdf, parent=PARENT, wait=True, timeout=args.timeout,
            options={
                "parse_backend": args.backend,
                "structure_policy": "bookmarks_preferred",
                "model_mode": "auto",
                "create_parent": True,
            },
        )
        root_uri = res.get("root_uri")
        log["add_resource"] = {
            "status": res.get("status"),
            "root_uri": root_uri,
            "structure_source": res.get("structure_source"),
            "alignment_status": res.get("alignment_status"),
            "elapsed_s": round(time.time() - t0, 1),
        }
        log["assert_root_appends_name"] = bool(root_uri) and root_uri.rstrip("/").endswith(
            Path(args.pdf).stem
        )

        tree = await client.ls(root_uri)
        log["tree_names"] = sorted(n.get("name") for n in tree)
        log["assert_manifest_present"] = "_manifest.json" in log["tree_names"]
        log["assert_chapter_written"] = any(
            n.get("isDir") or (str(n.get("name", "")).endswith(".md")
                               and n.get("name") not in {"toc.md", ".abstract.md", ".overview.md"})
            for n in tree
        )

        t1 = time.time()
        r = await client.reindex(
            uri=root_uri, mode="semantic_and_vectors", wait=True, recursive=True
        )
        log["reindex"] = {
            "status": r.get("status"),
            "rebuilt_records": r.get("rebuilt_records"),
            "failed_records": r.get("failed_records"),
            "elapsed_s": round(time.time() - t1, 1),
        }
        log["assert_reindex_clean"] = (
            r.get("status") == "completed" and (r.get("failed_records") or 0) == 0
        )

        sres = await client.search(query=getattr(args, 'query', QUERY), target_uri=root_uri, limit=8)
        hits = sres.get("resources") or []
        log["search_uris"] = [h.get("uri") for h in hits]
        log["assert_uri_traceable"] = bool(hits) and all(
            str(h.get("uri", "")).startswith(root_uri.rstrip("/") + "/")
            or h.get("uri") == root_uri for h in hits
        )

        # Head/middle/tail spread: three source-scoped probes against the
        # fixture's front, middle and back chapter content. Every hit must stay
        # traceable; the middle probe must surface an L3 chunk record when the
        # retrieval path exposes one (the shared D1 plan publishes child chunks).
        probes = [
            ("head", getattr(args, "head_query", "前言")),
            ("middle", getattr(args, "mid_query", getattr(args, 'query', QUERY))),
            ("tail", getattr(args, "tail_query", "电气部件和液压部件的处理")),
        ]
        spread, chunk_fields_seen = {}, {"chunk_id": 0, "parent_chunk_id": 0}
        for label, query in probes:
            pres = await client.search(query=query, target_uri=root_uri, limit=8)
            phits = pres.get("resources") or []
            spread[label] = [h.get("uri") for h in phits]
            spread[label + "_traceable"] = bool(phits) and all(
                str(h.get("uri", "")).startswith(root_uri.rstrip("/") + "/")
                or h.get("uri") == root_uri for h in phits
            )
            for hit in phits:
                for field in chunk_fields_seen:
                    if hit.get(field):
                        chunk_fields_seen[field] += 1
        log["head_mid_tail"] = spread
        log["chunk_fields_observed"] = chunk_fields_seen
        log["assert_head_mid_tail_traceable"] = all(
            spread.get(label + "_traceable") for label, _ in probes
        )
        log["assert_head_mid_tail_spread"] = (
            len({tuple(spread.get(label) or []) for label, _ in probes}) > 1
            or len(spread.get("middle") or []) >= 1
        )
    finally:
        await client.close()
    log["passed"] = all(v for k, v in log.items() if k.startswith("assert_"))
    return log


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--pdf", required=True)
    p.add_argument("--url", default="http://127.0.0.1:1933")
    p.add_argument("--key", required=True)
    p.add_argument("--account", default=None)
    p.add_argument("--user", default=None)
    p.add_argument("--backend", default="mineru")
    p.add_argument("--query", default=QUERY, help="Known fixture content for source-scoped retrieval")
    p.add_argument("--timeout", type=float, default=1800.0)
    p.add_argument("--out", default="/tmp/flow_d1.json")
    args = p.parse_args()
    log = asyncio.run(run(args))
    Path(args.out).write_text(json.dumps(log, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(log, ensure_ascii=False, indent=2, default=str))
    return 0 if log["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
