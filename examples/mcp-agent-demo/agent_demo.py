#!/usr/bin/env python
# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""A minimal agent that answers questions by calling the OpenViking MCP server.

The agent is an LLM in a tool-calling loop. The only tool source is the
OpenViking MCP endpoint, so whatever this script can answer is exactly what an
MCP-connected agent (Claude Code, Cursor, OpenClaw, ...) can answer.

Run::

    export OV_MCP_URL=http://127.0.0.1:1933/mcp
    export OV_MCP_KEY=<api key>
    export OV_BRAIN_BASE_URL=http://10.147.254.3:8003/v1
    export OV_BRAIN_MODEL=qwen3.8-next-flash
    .venv/bin/python examples/mcp-agent-demo/agent_demo.py "液压系统的拧紧扭矩是多少？"
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Dict, List

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from openai import AsyncOpenAI

MCP_URL = os.environ.get("OV_MCP_URL", "http://127.0.0.1:1933/mcp")
MCP_KEY = os.environ.get("OV_MCP_KEY", "")
BRAIN_BASE_URL = os.environ.get("OV_BRAIN_BASE_URL", "http://10.147.254.3:8003/v1")
BRAIN_MODEL = os.environ.get("OV_BRAIN_MODEL", "qwen3.8-next-flash")

SYSTEM_PROMPT = """You are an equipment-maintenance assistant.
You answer strictly from the OpenViking knowledge base, which you reach through MCP tools.

Tool guide:
- find: semantic search. Use level=[0] for directory summaries (L0), level=[1] for
  overviews (L1), level=[2] for leaf chunks/figures (L2). Use meta_filters to hard-filter,
  e.g. [{"field":"category","values":["images"],"op":"exact"}] for figures only, or
  ["text"] for prose only. Pass image_url to search by an image.
- grep: regex over file bodies; point uri at a directory to search its index.md.
- glob: find files by path pattern.
- read: read one or more viking:// URIs. uris is a list.
- tree / list: browse the hierarchy.

Rules:
1. Search before answering. Cite the viking:// URI you used.
2. If a figure is relevant, say so and cite its URI.
3. If the knowledge base does not contain the answer, say so plainly.
4. Keep the final answer short and factual.
"""


def _tool_result_text(result: Any) -> str:
    parts: List[str] = []
    for item in result.content or []:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(f"[{getattr(item, 'type', 'block')}]")
    return "\n".join(parts) if parts else "(empty result)"


def _to_openai_tools(tools: List[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for tool in tools:
        schema = tool.inputSchema or {"type": "object", "properties": {}}
        # MCP schemas may carry $defs; inline-free servers (OpenViking) do not, but
        # strip a top-level title which some gateways reject.
        schema = dict(schema)
        schema.pop("title", None)
        out.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": (tool.description or "")[:1024],
                    "parameters": schema,
                },
            }
        )
    return out


async def run_agent(question: str, max_steps: int = 8, verbose: bool = True) -> Dict[str, Any]:
    """Run one question through the MCP tool-calling loop and return the trace."""
    brain = AsyncOpenAI(base_url=BRAIN_BASE_URL, api_key="dummy")
    headers = {"Authorization": f"Bearer {MCP_KEY}"} if MCP_KEY else {}
    trace: List[Dict[str, Any]] = []

    async with streamablehttp_client(MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            openai_tools = _to_openai_tools(tools)
            if verbose:
                print(f"[agent] connected: {len(tools)} MCP tools available")

            messages: List[Dict[str, Any]] = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ]

            for step in range(1, max_steps + 1):
                resp = await brain.chat.completions.create(
                    model=BRAIN_MODEL,
                    messages=messages,
                    tools=openai_tools,
                    tool_choice="auto",
                    temperature=0.0,
                )
                msg = resp.choices[0].message
                messages.append(msg.model_dump(exclude_none=True))

                if not msg.tool_calls:
                    if verbose:
                        print(f"\n[agent] finished in {step - 1} tool step(s)")
                    return {"answer": msg.content or "", "steps": trace, "tools": [t.name for t in tools]}

                for call in msg.tool_calls:
                    name = call.function.name
                    try:
                        args = json.loads(call.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    if verbose:
                        print(f"[step {step}] -> {name}({json.dumps(args, ensure_ascii=False)[:160]})")
                    try:
                        result = await session.call_tool(name, args)
                        text = _tool_result_text(result)
                        ok = not result.isError
                    except Exception as exc:  # noqa: BLE001 - surface tool errors to the model
                        text = f"tool error: {exc}"
                        ok = False
                    trace.append({"step": step, "tool": name, "args": args, "ok": ok, "text": text})
                    if verbose:
                        print(f"          {'ok' if ok else 'ERR'} | {text.splitlines()[0][:120] if text else ''}")
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": text[:6000]}
                    )

            return {"answer": "(max steps reached)", "steps": trace, "tools": [t.name for t in tools]}


DEMO_QUESTIONS = [
    ("semantic find + cite", "液压系统由哪些部分组成？给出你引用的来源。"),
    ("figure search", "找一张液压马达的爆炸分解图，告诉我它的 URI。"),
    ("prose-only filter", "标准拧紧扭矩表里 M12 螺栓的扭矩是多少？"),
]


async def main() -> None:
    if len(sys.argv) > 1:
        questions = [("custom", " ".join(sys.argv[1:]))]
    else:
        questions = DEMO_QUESTIONS

    print(f"MCP : {MCP_URL}")
    print(f"brain: {BRAIN_MODEL} @ {BRAIN_BASE_URL}\n")

    for label, q in questions:
        print("=" * 72)
        print(f"[{label}] Q: {q}")
        print("-" * 72)
        out = await run_agent(q)
        print(f"\nA: {out['answer']}\n")


if __name__ == "__main__":
    asyncio.run(main())