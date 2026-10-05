# 未知写入恢复的受控后端能力

2026-10-05 的实现只验证了 `ControlledLocalWriteBackend`。普通本地或远程 AGFS 尚未提供可信的终态查询和禁止旧 writer 再次写入的能力，因此保持 `WRITE_RECOVERY_REQUIRED`，不能凭超时、客户端 hash 或人工声明清除保护。

协调者 operator 在创建 `RegistryAuthority` 时注入 `recovery_backends` 和 `recovery_authorizer`。这两项不是 RPC 命令，worker 和 HTTP 请求不能安装 verifier，也不能提交完成证明。后端目录必须是 operator 管理的 0700 私有目录；所有受保护正文路径的 writer 都必须使用该后端的一次性 operation。外部直接写文件不在这个信任模型内。

`recovery_authorizer(account, actor, uri)` 必须读取当前身份和权限，确认源 WRITE 及注册知识域 MANAGE 后严格返回 `True`。该函数会在后端确认前和 registry 最终事务中各调用一次；它不能重入 registry 或调用任何会获取同一 registry 写事务的 API。部署方需要提供有界、独立读取的身份/ACL provider。现有独立协调者测试使用实时读取 operator policy 文件的 provider；尚未接通完整企业身份系统，因此不能宣称覆盖其授权撤回链路。

恢复 intent 绑定 account、原 actor、注册 namespace/root、当前 policy revision、完整源 token 集合、目标 token 子集和 draft epoch。协调者从自己的后端 operation journal 查询终态或 fencing，持有该后端 mutation lock 读取实际正文，然后进行最后一次权限复核和完整 CAS。审计、epoch/hash 更新、清除 token/ref 和 intent 完成在同一个 SQLite 事务中提交；审计失败全部回滚。恢复后仍要求重新构建，不会提升失败的 job/build 为 ready。

现有实验 API：

- `POST /api/v1/kb/write-recovery/diagnostics`：提交 URI，返回受保护 writer 和服务器是否具有可信恢复能力。
- `POST /api/v1/kb/write-recovery/recoveries`：提交 URI、当前目标 token 和幂等 key；额外 proof/hash 字段被拒绝。
- `GET /api/v1/kb/write-recovery/recoveries/{recovery_id}`：按 account 读取，并重新执行当前 source/domain 权限检查。

测试包含重开后端/registry、独立迟到 writer 被 fencing、独立协调者 HTTP、并发 intents、授权撤回、scope/CAS 变化和故障审计。它们没有证明普通 AGFS 恢复、跨机部署、多协调者高可用、完整备份恢复或所有受保护路径的物理删除能力。
