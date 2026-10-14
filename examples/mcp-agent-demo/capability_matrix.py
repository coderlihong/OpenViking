#!/usr/bin/env python
# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Exercise every retrieval capability over MCP and print a pass/fail matrix.

This is the deterministic companion to ``agent_demo.py``: instead of letting an
LLM choose tools, it calls each capability directly so a regression shows up as
one red line, not a vague answer.

Run::

    export OV_MCP_URL=http://127.0.0.1:1933/mcp
    export OV_MCP_KEY=<api key>
    .venv/bin/python examples/mcp-agent-demo/capability_matrix.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Callable, List, Tuple

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = os.environ.get("OV_MCP_URL", "http://127.0.0.1:1933/mcp")
MCP_KEY = os.environ.get("OV_MCP_KEY", "")
TREE = "viking://resources/raw/menus/820676206347751"
IMG = f"{TREE}/4_Structure_and_Function/images/844935592268968.jpg"

# (capability, tool, args, predicate on the returned text)
CHECKS: List[Tuple[str, str, dict, Callable[[str], bool]]] = [
    ("health", "health", {}, lambda t: bool(t.strip())),
    ("tree (hierarchy)", "tree", {"uri": TREE, "level_limit": 1, "node_limit": 20},
     lambda t: "1_Introduction" in t),
    ("list (directory)", "list", {"uri": "viking://", "limit": 20},
     lambda t: "resources" in t),
    ("glob (path match)", "glob", {"pattern": "**/images/**", "uri": TREE, "node_limit": 5},
     lambda t: ".jpg" in t),
    ("grep (index/body)", "grep", {"uri": TREE, "pattern": "torque", "node_limit": 5},
     lambda t: "torque" in t.lower() and "match" in t.lower()),
    ("find semantic", "find", {"query": "hydraulic system", "limit": 5},
     lambda t: "viking://" in t),
    ("find level=L0", "find", {"query": "hydraulic", "level": [0], "limit": 5},
     lambda t: ".abstract.md" in t),
    ("find level=L1", "find", {"query": "hydraulic", "level": [1], "limit": 5},
     lambda t: ".overview.md" in t),
    ("find level=L2", "find", {"query": "hydraulic", "level": [2], "limit": 5},
     lambda t: "viking://" in t),
    ("find meta category=images", "find",
     {"query": "hydraulic motor", "limit": 5,
      "meta_filters": [{"field": "category", "values": ["images"], "op": "exact"}]},
     lambda t: "meta:" in t and '"category": "images"' in t),
    ("find meta category=text", "find",
     {"query": "tightening torque", "limit": 5,
      "meta_filters": [{"field": "category", "values": ["text"], "op": "exact"}]},
     lambda t: "meta:" in t and '"category": "text"' in t),
    ("find meta_hints", "find",
     {"query": "液压马达", "limit": 5, "meta_hints": [{"field": "modelid", "value": "SY215", "op": "fuzzy"}]},
     lambda t: "viking://" in t),
    ("find tags/since/until", "find",
     {"query": "torque", "limit": 5, "since": "2000-01-01", "time_field": "updated_at"},
     lambda t: "viking://" in t),
    ("find include_provenance", "find",
     {"query": "hydraulic motor", "limit": 5, "include_provenance": True},
     lambda t: "provenance:" in t and "chunk_id" in t),
    ("find image_url (search by image)", "find",
     {"image_url": IMG, "limit": 5,
      "meta_filters": [{"field": "category", "values": ["images"], "op": "exact"}]},
     lambda t: ".jpg" in t),
    ("find parent-child chain", "find", {"query": "hydraulic motor exploded", "limit": 5},
     lambda t: "[image:" in t or "\n    [" in t),
    ("search list mode", "search", {"query": "torque", "mode": "list", "limit": 5},
     lambda t: "viking://" in t),
    ("search context mode", "search", {"query": "torque", "mode": "context", "limit": 3},
     lambda t: "memory" in t.lower() or "viking://" in t),
    ("read (file)", "read", {"uris": [f"{TREE}/1_Introduction/index.md"]},
     lambda t: len(t.strip()) > 20),
    ("read (native image block)", "read", {"uris": [IMG]},
     lambda t: "image" in t.lower() or "Source:" in t),
    ("read raw", "read", {"uris": [f"{TREE}/1_Introduction/index.md"], "raw": True},
     lambda t: len(t.strip()) > 20),
]


async def main() -> int:
    headers = {"Authorization": f"Bearer {MCP_KEY}"} if MCP_KEY else {}
    passed = failed = 0
    print(f"MCP endpoint: {MCP_URL}\n")
    print(f"{'CAPABILITY':<34} {'TOOL':<8} RESULT")
    print("-" * 78)
    async with streamablehttp_client(MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for label, tool, args, predicate in CHECKS:
                try:
                    result = await session.call_tool(tool, args)
                    text = "\n".join(
                        getattr(c, "text", f"[{getattr(c, 'type', 'block')}]")
                        for c in result.content
                    )
                    ok = (not result.isError) and predicate(text)
                    detail = "" if ok else f" :: {text.splitlines()[0][:60] if text else 'empty'}"
                except Exception as exc:  # noqa: BLE001
                    ok = False
                    detail = f" :: exception {exc}"
                mark = "PASS" if ok else "FAIL"
                passed += ok
                failed += not ok
                print(f"{label:<34} {tool:<8} {mark}{detail}")
    print("-" * 78)
    print(f"{passed} passed, {failed} failed, {len(CHECKS)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))