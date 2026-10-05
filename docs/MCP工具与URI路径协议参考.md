# OpenViking MCP 工具 & 路径协议（白话版）

OpenViking 是个"带记忆的存储服务"，通过 MCP 给 AI 用。本文用大白话讲清楚三件事：**连到哪、什么传输方式、有哪些工具**，以及**它的文件路径是咋写的**。参数细节不展开，交给大模型自己看 schema 理解即可。

---

## 一、连接信息

| 项 | 值 |
|----|----|
| **MCP 链接** | `http://viking.ai.rootcloud.info/mcp`（已实测可用） |
| **传输方式** | Streamable HTTP（实测响应为 SSE 事件流） |
| **认证** | API Key：`X-Api-Key: <key>` 或 `Authorization: Bearer <key>` |
| **版本** | MCP serverInfo `openviking v1.27.0`，16 个工具全部在线 |

客户端配置示例（任何支持 HTTP 的 MCP 客户端通用）：

```json
{
  "mcpServers": {
    "openviking": {
      "url": "http://viking.ai.rootcloud.info/mcp",
      "headers": { "Authorization": "Bearer your-api-key-here" }
    }
  }
}
```

Claude Code 记得加 `"type": "http"`；OpenCode 用 `"type": "remote"`（详见 `docs/zh/guides/06-mcp-integration.md`）。

---

## 二、对外暴露的 MCP 工具：共 16 个

> `tools/list` 返回的 `inputSchema` 已转成扁平可移植类型（无 `anyOf`/`$ref`），联合类型按 `array > object > string > number > integer > boolean` 收敛为单类型，`null` 分支被移除。所以 `str | list[str]` 对外声明为 `array`，但传单个字符串也能用。

### 1. 找东西（检索）
| 工具 | 一句话 |
|------|--------|
| `find` | 快速语义检索记忆/资料，不带上下文，返回带 URI、摘要、得分的排序结果 |
| `search` | 深度检索，可选带会话上下文 + 意图分析 |
| `recall` | 按类型配额召回记忆（事件/实体/偏好/经验分开搜、有上限） |
| `grep` | 在 `viking://` 文件里按正则精确搜内容（可多个 pattern 并发） |
| `glob` | 按文件名通配符找文件（如 `**/*.md`） |
| `code_search` | 按类名/函数名子串搜代码符号，返回符号类型、所在文件、行号区间（最多扫 200 个文件） |

### 2. 看东西（读写）
| 工具 | 一句话 |
|------|--------|
| `read` | 读一个或多个 `viking://` 文件内容（单个 URI 或列表） |
| `list` | 列出 `viking://` 目录下的条目（可递归） |
| `code_outline` | 看文件符号骨架（类/函数 + 行号区间），不读全文 |
| `code_expand` | 返回单个命名符号的完整源码（顶层 `bar` / 方法 `Foo.bar`） |

### 3. 存东西
| 工具 | 一句话 |
|------|--------|
| `remember` | 把消息写入长期记忆（触发抽取） |
| `add_resource` | 加资料（网页 URL 或本地文件），后台异步消化；本地文件走"给一次性上传链接→客户端 POST 文件"两步流程 |
| `forget` | 永久删除某个 `viking://` 路径（不可逆） |

### 4. 定时自动更新（watch）
| 工具 | 一句话 |
|------|--------|
| `list_watches` | 列出当前用户下在跑的定时刷新任务 |
| `cancel_watch` | 按目标 URI 取消定时刷新 |

### 5. 健康检查
| 工具 | 一句话 |
|------|--------|
| `health` | 检查服务是否健康 |

---

## 三、路径协议：`viking://` 就是它的文件系统

OpenViking 把记忆和资料**组织成一套文件系统**，每个东西都有个唯一地址，格式：

```
viking://{大类}/{路径}
```

就像 `file:///Users/xxx/a.md` 一样，只不过前缀是 `viking://`。

### 大类（第一个斜杠后的词）

| 大类 | 是啥 | 例子 |
|------|------|------|
| `resources` | 公开知识库（文档/代码/论文，谁都能看） | `viking://resources/my-project/docs/api.md` |
| `user` | 当前用户的私人数据（记忆、私有资料、会话） | `viking://user/memories/preferences/coding` |
| `agent` | 全局共享的 AI 能力配置（技能等） | `viking://agent/skills/search-web` |
| `session` | 老版本的别名（现在归到 user 下） | `viking://session/abc123` |
| `temp`/`queue`/`upload` | 内部用的临时空间，外部碰不到 | — |

### 关键记法

- **短写法**：`resources/docs` 或 `/resources/docs` 都能写，服务端自动补成 `viking://resources/docs`。
- **带 / 结尾 = 目录**，不带 = 文件：`viking://resources/docs/` 是文件夹，`viking://resources/docs/api.md` 是文件。
- **`viking://` 根目录**：全都在这下面。实测 `list viking://` 返回 `resources`、`user` 两个顶层目录。
- **user 目录有固定结构**：
  ```
  viking://user/{用户名}/
  ├── profile.md          # 用户画像
  ├── memories/           # 记忆（还细分 preferences/entities/events/...）
  ├── resources/          # 私有资料
  ├── skills/             # 技能
  ├── peers/{对方}/       # 关于某个交互对象的记忆
  └── sessions/{会话号}/  # 会话记录
  ```
- **日期变量**：路径里能写 `{calendar:today}` 之类，服务端自动换成今天日期。比如
  `viking://resources/emails/{calendar:today}/inbox` → `viking://resources/emails/2026/05/07/inbox`。还有 `yesterday`/`tomorrow`/`year`/`month`/`ym`/`week` 等。
- **每个目录有几个隐藏文件**：`.abstract.md`（摘要）、`.overview.md`（概览）、`.relations.json`（相关资源）、`.meta.json`（元数据）。

> **实测真实目录示例**（远端 `viking.ai.rootcloud.info` 已实测）：
> - 故障案例：`viking://resources/730/raw/cases/` → 下含 `5T-9.5T/`、`sy215/` 等机型目录
> - 手册：`viking://resources/raw/vikingmenu/` → 下含 `sy215c/`、`sy75/` 等机型手册
> - wiki：`viking://resources/730/wikitest/` → 下含 `Fault/`、`Symptom/`、`DTC/`、`Component/`、`Procedure/` 等模块目录
>
> 即每个知识库/项目在 `resources` 下各占一个目录，`viking://resources/{项目}/{子路径}/` 一层层钻取即可定位到具体文件。

### 工具与路径的关系

- `read`/`list`/`grep`/`glob`/`forget` 全是 `viking://` 原生操作。
- `code_outline`/`code_search`/`code_expand` **只认 `viking://`**，传别的路径会被拒。
- `search`/`find` 可用 `target_uri`（常为 `viking://` 前缀）限定搜索范围。
- `add_resource` 输入接受 `http(s)://`、`git@`、`ssh://`、`git://`，但 `to`/`parent` 目标是 `viking://resources/...`。

### 规则一句话

- 地址里 `viking://` 是唯一合法开头，别的不收。
- 公开 API 只认 `resources` / `user` / `agent`（外加老的 `session`），内部的碰不到。
- `resources` 只放"客观知识"；配置文件、技能定义这类得放 `agent`。

---

## 四、一句话总结

> OpenViking 在 **`http://<服务器>:1933/mcp`** 提供服务，走 **Streamable HTTP** 传输，暴露 **16 个 MCP 工具**（检索 6 + 读写 4 + 存储 3 + watch 2 + 健康 1）；所有内容挂在 **`viking://` 文件系统** 上，`viking://{大类}/{路径}` 一层层下钻，支持日期变量和 `user`/`resources`/`agent` 三大公共空间。
