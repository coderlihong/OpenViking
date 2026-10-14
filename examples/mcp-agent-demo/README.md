# MCP agent demo

Two scripts that prove the OpenViking MCP endpoint is enough for an agent to
work against — no HTTP fallback needed.

| Script | What it does |
|--------|--------------|
| `agent_demo.py` | An LLM in a tool-calling loop whose only tools come from `/mcp`. Ask it a question; it searches, reads and cites on its own. |
| `capability_matrix.py` | Calls every retrieval capability directly and prints pass/fail. Deterministic, no LLM. |

## Prerequisites

The server must be running with the MCP endpoint mounted at `/mcp` on the same
port as HTTP. Get an API key from the tenant you want to query.

## Environment

```
export OV_MCP_URL=http://127.0.0.1:1933/mcp
export OV_MCP_KEY=<your api key>
# only for agent_demo.py — any OpenAI-compatible chat model that supports tool calling
export OV_BRAIN_BASE_URL=http://10.147.254.3:8003/v1
export OV_BRAIN_MODEL=qwen3.8-next-flash
```

## Run

Capability matrix (no model needed):

```
.venv/bin/python examples/mcp-agent-demo/capability_matrix.py
```

Agent, with the built-in demo questions:

```
.venv/bin/python examples/mcp-agent-demo/agent_demo.py
```

Agent, with your own question:

```
.venv/bin/python examples/mcp-agent-demo/agent_demo.py "液压马达的爆炸分解图在哪？"
```

## What the matrix covers

`tree`, `list`, `glob`, `grep`, semantic `find`, level filtering (L0/L1/L2),
`meta_filters` on `category=images|text`, `meta_hints`, time filters, the
parent-child chunk chain, image search via `image_url`, `search` in both `list`
and `context` mode, and `read` (plain, `raw`, and native image block).

Expected: 21 passed, 0 failed.

## Notes for agent integrators

- `meta_filters` items are `{"field", "values": [...], "op"}` (plural `values`);
  `meta_hints` items are `{"field", "value", "op"}` (singular `value`, default
  `op="fuzzy"`).
- Hints may only target fuzzable business fields (e.g. `modelid`). Exact-only
  fields (`category`, ACL, namespace, applicability, `*_uri`) are rejected as
  hints no matter the `op` — express those as `meta_filters` instead.
- `read` takes `uris` (a list). Images come back as native image blocks, so the
  model can see the figure without a second download call.
- `find`/`search` render `meta` on every hit and `provenance` when
  `include_provenance=True`; the parent-child chunk chain renders under each hit.