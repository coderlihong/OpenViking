# 单机权威 registry 进程（KB-002 部分实现）

此实现将 PublicationStore 的发布、来源写入保护和读取租约收敛到一个 POSIX 协调进程。多个同机 worker 经私有 Unix socket 报告，协调者持有本机 SQLite/WAL 和进程生命周期独占锁。相同数据库的第二个协调者、以及绕过 socket 的普通 PublicationStore 事务会失败。不同主机仍不能依靠各自 SQLite 或网络共享目录获得协调保证。

## 启动与 worker 配置

在协调者和 worker 使用的受信任系统账号下准备一个短路径的私有目录（例如 `/var/run/openviking-registry`，权限 `0700`），并生成至少 32 字符的随机 token，写入当前用户拥有的普通文件，权限 `0600`。token 内容不能写入日志、命令行参数或产品配置；产品配置只包含 token 文件路径。Unix socket 的完整路径不得超过 103 字节。

协调者启动命令：

```sh
python -m openviking.indexing.registry_authority \
  --socket /var/run/openviking-registry/registry.sock \
  --database /var/lib/openviking-registry/registry.db \
  --token-file /var/run/openviking-registry/token
```

数据库与 lock、WAL、SHM 文件必须置于协调者本机持久磁盘。不能放在 NFS、SMB 或其他网络共享挂载，也不能复制到各 worker 当成独立权威库。实现不自动识别所有挂载类型，部署者必须落实这个边界。

worker 的 `embedding.chunking` 保留原 tokenizer、预算及 enabled 配置，并设置：

```json
{
  "publication_path": "",
  "publication_authority_socket": "/var/run/openviking-registry/registry.sock",
  "publication_authority_token_file": "/var/run/openviking-registry/token",
  "publish_content": true
}
```

authority 与 local `publication_path` 互斥；socket 和 token 文件须一起配置。部署参数不会改变文本、目录语义或图片计划的模型身份。原来只有 publication_path 的单机模式仍保留，不能把它当成多主机模式。切换时先排空旧版写入并暂停 worker，启动唯一协调者，再启动仅配置 socket 的 worker。

## 故障、读租约与恢复

socket 命令有显式白名单、token 认证、4 MiB 请求/响应上限和 5 秒连接/读取超时；不开放裸 SQL、任意 Python 方法或备份命令。持有 token 的 worker 属于可信内部边界，token 不替代用户来源 ACL 校验。WikiCatalog、NamespaceCatalog 和 WikiBuildPlanner 已迁移为固定领域命令，真实 schema/collection、W1/W2 prepared 和 namespace/export HTTP/SDK 入口可以在 authority worker 模式运行。命令在协调者内完成 immutable identity、账号隔离、来源 epoch、binding digest 与幂等 key 的事务检查；backend ACL 仍在 worker 以当前请求上下文实时检查，不作为永久授权 receipt。其他未知直接 SQL 调用仍明确失败，不能退回另一份 local DB。

协调进程不可用或响应丢失时，worker 抛出 `AuthorityUnavailable`，不自动重放、不启用 local fallback。响应丢失可能意味着已写入，需先查询状态和保护 token，不能假定未执行。已有查询仍须续租；无法续租时也停止返回结果，本轮未实现断连读缓存。来源未知写入在重启后继续保留 `uncertain`，不擅自解除。

进程崩溃后操作系统释放所有权锁。相同数据库的新协调者可收回已无监听者的旧 socket 并读取原状态；活跃监听者的 endpoint 不会被抢占。线程处理完成后正常关闭才释放锁。需要固定唯一服务配置和同一数据库路径：单机进程锁不能防止运维误配成两份不同数据库，更不能提供多协调者高可用。

## 备份

协调者的 operator-local `RegistryAuthority.backup(destination)` 使用 SQLite backup API，产生可独立重开的事务一致快照。它不对 worker RPC 开放。禁止以活动数据库作为目标。备份需与原件/不可变正文、AGFS、ACL 和模型配置联合编排；本轮只有 registry 快照和重开验证，未获得完整企业灾备验收。

## 已验证与剩余边界

真实独立协调进程与两个 worker 共同写入同一来源，两个 token 保持独立；终止协调者、断连写入失败、重新启动后 uncertain token 保留。真实发布/批次命令、非法身份/命令拒绝、同库双协调者排斥、local 绕过拒绝、备份恢复与配置往返均有测试。实际 VikingFS 和文本/图片/L0L1 producer 经独立进程 authority 发布，计划身份与 local 配置一致；AGFS transport 和向量服务仍是测试替代。

KB-002 维持 PARTIAL，TC-13.16 和 TC-16.2 仅获得局部证据。跨主机受认证 TLS 内部接口、ACL 变更 intent/发布权限事务 fencing、真实 AGFS 延迟写故障、未知结果确认恢复、完整联合备份及多协调者事务/CAS/选主/切换仍未完成。15 个 Feature 和四条完整 FLOW 的验收状态不因此提升。

第二批新增独立协调进程下真实异步/同步 SDK 访问 HTTP 的 schema、collection、namespace/export、W1 上传 prepared 与 W2 URI prepared 测试。未经执行的准备接口继续返回 prepared；导出策略撤回即时使执行授权失效，原准备计划只保留私有检视能力。新增领域命令不执行抽取、不颁发可信实体身份，也不提供完整 Wiki 发布门禁。

## Collection 域门禁与历史数据迁移

新 collection 的 target 必须属于它指定的已注册 namespace；raw prefix 必须属于另一独立已注册域。两个根须存在并使用当前启用的 restricted ACL。每个 Wiki namespace 只允许一个 collection，目标和原材料写入 prefix 的冲突在协调事务内拒绝。即使账号尚无 namespace，也不会豁免登记。注册记录仅保存域身份，读取/pin 和执行仍检查当前根成员、子资源权限和 export。

升级后既有 collection 缺少域登记记录时，协调者持久标为 `legacy_domain_pending`。常规 collection GET、pin、准备和执行全部拒绝；账号管理员可通过 `GET /api/v1/kb/wiki/collections/{collection_id}/migration-inspection` 检视绑定、状态和 digest。确认根域与 ACL 后，调用 `POST /api/v1/kb/wiki/collections/{collection_id}/migrate-domains`，提交 `expected_binding_digest` 执行显式 CAS 迁移。digest 已变、域不匹配或与其他 collection 冲突时失败，不自动修正原绑定。SDK 同步/异步接口分别是 `kb.inspect_wiki_collection_migration` 与 `kb.migrate_wiki_collection_domains`。

合法迁移只允许旧绑定继续走 prepared 和后续完整执行检查，不会发布 Wiki 事实或豁免实时权限。配置检视接口只允许本账号管理员，并拒绝 bypass ACL 上下文；它不返回任何来源正文。
