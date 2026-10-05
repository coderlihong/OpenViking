# Experimental Wiki draft extraction

The shared `WikiDraftExtractor` consumes an immutable `WikiBuildPlan` for both
upload materialization (W1) and an existing Viking Markdown URI (W2). Source
preparation remains `prepared`; extraction produces private `draft_ready`
candidates. Public entity Markdown and search indexes are not written here.

Requirements:

- The shared durable registry, registered restricted target and independent raw
  source domains, enabled ACLs and the collection's fixed schema bundle.
- The original authenticated request context, current source read / target write
  permissions, and an explicit one-way export grant for each source domain.
- A configured VLM with a text completion interface. The default uses
  `get_openviking_config().vlm`, including the existing credential/failover code.
  Only provider/model/temperature identity is recorded; credentials are omitted.

A worker can invoke the component after its normal current-principal refresh:

```python
extractor = WikiDraftExtractor(planner)
result = await extractor.extract(
    ctx,
    build_id,
    expected_plan_digest=plan_digest,
    profile="draft",
    resume=job_checkpoint.get("extraction_progress"),
    checkpoint=save_extraction_progress,
)
```

`save_extraction_progress` must persist the supplied progress object under the
job's `extraction_progress` key while checking its worker lease. Do not accept
progress or draft receipts from the public execution request. Checkpoint and
result blob references must remain protected by the registry's GC reachability
roots until job retirement; a cancelled or crashed attempt must not release
assets still referenced by an active retry. A model call completed just before a
checkpoint can be repeated after a crash; this component does not claim exactly
once model execution.

Progress includes the fixed build/plan/schema, compiler/extractor versions,
effective model profile and limits, an ordered prefix of source/chunk receipts,
the export authorization revision, and typed counters. Resume checks the account, plan, schema, profile, prompt
hash, exact original chunk position, private blob digest/size, evidence and
current ACL/export policy. A changed configuration or forged/nonsequential
checkpoint fails. A changed export-policy revision also requires a new execution rather than reusing the old checkpoint. Completed chunks are reused without additional model calls.

The default limits are 12,000 source characters per call, 256 total model
attempts, 512 candidates, a 1 MiB response, a 4 MiB complete prompt, and a
120-second call deadline with one timeout retry. Sources are consumed completely
in deterministic chunks, including the full frozen schema in each prompt. A
limit is reported as `EXTRACTION_LIMIT`; source text is never silently truncated.
Invalid JSON, duplicate JSON keys, non-finite JSON values, tool calls, incomplete
model responses, undeclared fields/types/entities, unsupported paths, fabricated
quotes, foreign source citations and missing evidence fail closed. Only timeout
receives bounded retry; format/schema errors do not produce invented repairs.
Model-provider failures, timeout, empty knowledge and format/evidence failures
remain distinguishable. Empty knowledge is a valid private result with zero
entities, not an instruction to fabricate all schema entity types.

Candidate `publication_state` and `validation_state`, when declared, are forced
to `candidate` and `pending`. Source fields follow the schema, including
`Device.raw_refs` instead of a hard-coded generic `sources`. Declared `id` is
recorded only as provisional identity; similar titles, terms or aliases do not
merge candidates. Source URI references use the original Viking URI. Entity
references inside the collection await trusted catalog and closure validation.

Use `read_draft` to inspect a job-owned receipt. It rechecks every source in the
plan and the current target/export permissions before reading private bytes and
again before returning them; a wider target ACL cannot make a source-private
candidate readable. No public URI or implicit search route exists for these
candidate blobs.

`policy_coverage(bundle)` enumerates actual field/body/reference structural
support, explicit structural gates and every uncompiled prose/path policy per
entity. Package text cannot install executable handlers. `strict_diagnostic`
refuses before model use when policies are uncompiled; it also refuses a
prose-free schema while identity, trusted catalog, closed relationships,
semantic evidence, derived permission intersection and joint publication are
pending. A quoted source excerpt proves provenance and does **not** prove that
a generated assertion is semantically entailed. `diagnostic_publishable` stays
false throughout this component.

The opt-in real-model probe uses a synthetic Markdown source and an explicit
simple `Thing` schema with the actual ACL/SQLite/AGFS implementation:

```sh
OPENVIKING_WIKI_REAL_MODEL_PROBE=1 .venv/bin/python -m pytest \
  tests/enterprise_kb/test_wiki_extraction_model_probe.py --no-cov -q --tb=no
```

The default model config path is `/Users/wh/.openviking/ov.conf`; override it with
`OPENVIKING_WIKI_MODEL_CONFIG`. The probe writes a sanitized report under
`docs/spec/evidence` (`OPENVIKING_WIKI_PROBE_REPORT` may override the path),
including synthetic candidate pages for review. It makes no gold, business
quality, diagnostic publication or performance claim. It is skipped during
ordinary regression runs unless explicitly enabled.
# 持久执行接口接入（I034）

已有 Wiki `/builds` 和 `/ingestions` 仍返回 `200 prepared`。新接口 `POST /api/v1/kb/wiki/builds/{build_id}/execute` 接收 `expected_plan_digest`、`idempotency_key` 和 `profile`（默认 `draft`），返回 `202` 持久 job；同输入复用原 job，修改输入或配置需使用新 key。`strict_diagnostic` 在政策或发布门禁不足时明确拒绝。

任务通过已有 `GET /api/v1/kb/jobs/{job_id}` 查询及取消接口管理。已物化上传计划进入 W1，URI 计划进入 W2，两者复用同一个抽取器。模型 profile 和执行限额固定在 job 配置；配置变化拒绝执行。逐片段进度保存在 `extraction_progress`，恢复必须复核固定来源、配置、提示哈希、内容引用和授权 revision。

任务完成只标记私有 `draft_ready`，不会写入公开实体树。读取单个片段使用 `GET /api/v1/kb/wiki/jobs/{job_id}/drafts/{draft_index}`，服务端复核原用户和全部贡献来源权限、export、当前来源及 blob 完整性。候选引用纳入 GC 保护；完成事务再次校验源 epoch 和 export revision。完整 ACL 提交 fence、语义证据、身份归并、关系闭合和共同发布仍待完成。

同步与异步 Python SDK 均可调用：

```python
plan = client.kb.build_wiki(source_uris=["viking://resources/raw/manual.md"],
    collection_id="manual-wiki", idempotency_key="prepare-001")
job = client.kb.execute_wiki_build(plan["build_id"],
    expected_plan_digest=plan["digest"], idempotency_key="execute-001")
status = client.kb.get_ingestion_job(job["job_id"])
# 等实际状态为 draft_ready 后，读取一个私有抽取片段。
draft = client.kb.get_wiki_draft(job["job_id"], 0)
```

异步客户端对上述调用逐项 `await`。上传 PDF/Office 的完整 W1 调度尚未接入这个准备接口；当前持久执行的是已捕获的 Markdown 来源计划。
