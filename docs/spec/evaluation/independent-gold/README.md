# 独立 gold 评测底座（KB-031，2026-10-04）

本目录包含**可执行的审核、泄漏扫描、冻结和评分流程**，以及 I003 当前真实候选的审核结果。50 条样本仍为 `synthetic / unassigned / title_derived_lookup`；独立人工 gold 为 **0**，检索质量与性能均为 **NOT_RUN**。同一 SY215C 手册、同一机型不能拆成独立 train/test；本次没有强行划分。

## 当前真实结果

| 内容 | 实际结果 |
|---|---|
| Synthetic 候选 | 50；均源自标题，不认定为已核验精确术语或语义问题 |
| 文档 / 机型独立组 | 1 / 1 |
| 人工 gold / 测试 gold | 0 / 0 |
| 跨分区泄漏 | 0；原因是全组 unassigned，并非证明已经得到独立划分 |
| 已冻结 generator | 当前导入器版本 1、代码 hash、候选输入 hash；**原始 I003 generator 版本未知**，provenance_verified=false |
| 来源 | 原 PDF SHA256 与 MinerU artifact SHA256 分字段存储；原 PDF hash来自本轮 spec 的已核验输入记录 |
| 生产 URI | 50 条均未经生产读回验证 |
| 置信区间 | 未执行评测；CI=null，不报告虚假召回率、延迟或置信度 |
| 最低 gold 缺口 | 检索100（精确术语/口语改写/模糊机型/图片/负例各15）、图片用途30、章节50、诊断20；全部仍缺 |

[审核报告](audit-v1.json)给出各类别显式样本数、分组数、逐项偏差警告和 gold 覆盖缺口。[冻结 manifest](frozen-v1/manifest.json)保留样本、配置和工具 hash；[gold 报告](gold-report-v1.json)明确 NOT_RUN。空的 `frozen-v1/gold.jsonl` 是真实零样本状态。

## 使用

工具 `docs/spec/tools/evaluate_gold.py` 仅依赖 Python 标准库。在仓库根目录执行：

```sh
python3 docs/spec/tools/evaluate_gold.py audit dataset.jsonl audit.json
python3 docs/spec/tools/evaluate_gold.py freeze dataset.jsonl frozen-v2 --config evaluation-config.json
python3 docs/spec/tools/evaluate_gold.py verify frozen-v2
python3 docs/spec/tools/evaluate_gold.py score frozen-v2 results.json report.json
```

`freeze` 遇到 schema 错误、无独立人工 review 的 gold、未绑定样本的 review、跨分区泄漏时拒绝写出；已有版本目录拒绝覆盖。`verify` 重读文件、校验两个人口分文件 hash、总数据 hash 与配置 hash，然后再审核。Hash 提供完整性检测，不是数字签名或评审真实性证明。

I003 重现入口（输出须选用新的路径）：

```sh
python3 docs/spec/tools/evaluate_gold.py import-candidates \
  docs/spec/evaluation/sy215c-candidates-I003.jsonl new-synthetic.jsonl \
  --original-sha256 99a3a0d37696c7f76f8364a641e06b6a100d8e57f7030c1715beae0487196534 \
  --machine-id SY215C --document-id SY215C_SY021NCB62788_维修手册_zh
```

## 样本与人工审定约定

JSONL 每条必须包括：`id`、`population`（gold/synthetic）、`sample_kind`（retrieval/image_annotation/section_boundary/diagnostic）、`split`（train/validation/test/unassigned）、`query`、`categories`、`document_id`、`machine_id`、`passage_group`、`generator` 和 `source`。检索类别支持 model/dtc/semantic/table/image/tail/fuzzy_meta/exact_term/paraphrase/fuzzy_model/negative/title_derived_lookup；章节和诊断标签可以使用 section_boundary/diagnostic。不同样本类型**不得互相充数**。

`generator` 必须提供 name/version/code_sha256/input_sha256；gold 必须 provenance_verified=true。导入器的 hash 不替代原始 generator 的版本证明。`source` 必须包括 locator/content_sha256/original_sha256；解析输出 hash 与原件 hash 分开。人工必须在受控环境独立核对原件、章节边界、图用途、来源支持或负例查询范围，而不是采信待测 splitter/LLM 的输出。

创建 gold 需要人工签注 `review`：kind=human、reviewer、reviewed_at、decision=approved、oracle=original_source 或 independent_human、source_supported=true、evidence_locator、original_sha256 和 sample_sha256；同时附明确 `expected` 标签。`sample_sha256` 用工具的 `review_digest(record)` 计算，覆盖除 review 外全部字段，复核后改 query/来源/标签/分区会使审核失效。该 hash 必须由真实评审流程在完成复核后记录；给记录填上字段不构成人工核验。工具无自动晋升 synthetic 的功能，已评审 synthetic 也继续保留其人口，直到独立人工明确建立新版本 gold。

`passage_group` 应由原件侧标注的稳定段落身份给定，同段的不同问法必须共用。工具扫描文档、机型、段落组、来源内容 hash、去标点/空白后的重复 query 和摘录。跨任何 train/validation/test 分区均拒绝冻结，也覆盖 synthetic 调参对 gold test 的污染。任意语义改写的自动识别无法保证，仍需人工核对段落组；不要给同段改写伪造不同 group。`unassigned` 可供候选冻结，不能作为 held-out 质量样本。泄漏项在审核报告逐项留痕，剔除或重新整组划分后保存新冻结版本，不静默丢弃。

## 结果与置信区间

`results.json` 引用 manifest 中的 dataset_sha256 / evaluation_config_sha256，提供 `run`（hardware、versions 对象、正整数 concurrency、temperature=cold 或 warm）与 `observations`。每条 observation 为 id/passed（bool）/failed（bool）/latency_ms（有限非负数）；failed=true 不允许 passed=true。选定人口与分区必须有且仅有完整结果，缺测、额外ID和重复结果均拒绝，防止挑选成功样本。passed 必须由独立评测 runner 对 expected 标签打分，这个底座不冒充生产检索或原件核验器。

默认仅报告 gold test，synthetic 必须显式 `--population synthetic`；不混入 gold 结论。每类报告 n、通过数、比率和 Wilson 描述区间，同时按机型整组重采样给出可复现的 bootstrap CI；p50/p95、失败率也报告机型分组区间。少于两个机型组时分组 CI 为 null。Wilson 区间假定独立，相关段落/同文档会违反此假定，不能据此声称可推广；两组或很少组的 bootstrap 同样不足以支持稳健总体结论。冷暖运行分开保存，不混算。

R/F、模型/space、chunking、schema 和 runner配置必须进入 evaluation_config；任何配置改变产生不同 hash，旧结果输出 STALE 并要求重跑。这里输出可机读 STALE，不修改共享 Feature 台账；实际更新台账与关联 TC 的状态仍由集成验收执行。

## 尚未完成的验收

TC-15.1/15.2 的候选分离、审核守卫、分组防泄漏和冻结机制已有局部测试，但真实人工 gold、跨文档/机型划分、来源生产读回和独立质量检验尚未完成。TC-15.3 尚未运行固定硬件冷暖性能/检索质量；TC-15.4 具备结果配置 stale 检查，未联动全仓共享台账。F15、KB-031 与四条 FLOW 均不因此变为 PASS。人工审核证据与受控原件应留在许可允许的环境，本目录保留原来已经入仓的候选摘录与 hash，不获取或复制敏感完整 PDF。
