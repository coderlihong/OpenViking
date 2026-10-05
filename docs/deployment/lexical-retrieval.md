# 权威协调服务 BM25 接入

这是现有 `find/search` 的实验性词法召回分支。启用 `embedding.chunking.lexical_enabled=true` 还必须启用父子 chunk 和 `publish_content`，配置模型 tokenizer 的路径与 SHA256，以及 publication authority（或单机兼容 publication_path）。配置验证不允许无内容版本保护的词法索引。

文本 producer 为每个 generation 声明 `lexical:<plan_id>` 回执；协调者在同一 SQLite 事务中写入全部子 chunk 的词法内容和回执。向量回执未齐或 producer 未封存时，词法回执不会让版本提前公开。分词版本为 `zh-lexical-v2`，正文保留词频；旧版本需要重新索引，不能直接混用。

检索复用一次查询的固定 manifest/租约，先检查当前资源权限，再在协调者内只用获准来源建立 FTS5 排名语料，返回前再次复核权限。来源删除标记即时生效。回包保留原 Viking URI、chunk id、父块和文本，不创建另一套公共检索 API。

当前接入只覆盖文本 chunk。显式 metadata 过滤尚未接入此分支，过滤存在时词法分支明确拒绝并记录 `LEXICAL_RECALL_FAILED`；现有向量分支继续应用原过滤，不能丢弃限制后召回。图片摘要、多向量空间、业务 meta、完整共同 ready 门禁和词法物理 GC 仍需完成。

每次查询只用获准语料计算 BM25，可避免未授权内容影响词频统计；当前实现扫描获准语料建立临时 FTS，尚未完成大语料性能定标。不能以专项契约测试替代生产质量或延迟验收。

可复核测试：`tests/enterprise_kb/test_authority_lexical.py` 使用真实独立协调进程、SQLite FTS、文本 producer、现有检索器和当前权限复核；包括关闭 dense 后仍由实际 BM25 返回原 URI 的用例。
