# bugfix 链：线上 bug 怎么进入框架

发布链（`docs/GAME_SERVER.md`）处理"想好了要改什么"，bugfix 链处理"线上炸了，不知道改哪"。
两条链共用开发/送审/审计/风控/报告，差别只在**前半段的输入形状**。
采集侧怎么按关键词触发、命中后交给谁，见 `docs/FRAMEWORKS.md`。

## 0. 项目怎么分：一角色一项目

以前一个项目里塞了采集、分诊、开发、报告七八个角色：换一个采集源要动整包、想单独扩
开发实例要全量部署、谁负责什么只能读 `roles` 列表。现在按角色切，一个项目一个 capability：

| 项目 | capability | 动作 |
|---|---|---|
| `bug-collector` | `bug` | collect |
| `bug-triage` | `triage` | classify |
| `bugfix-dev` | `dev` | locate / propose / implement |
| `bugfix-reporter` | `report` | build |

框架角色也有各自的项目壳：`auditor`（code/test_plan）、`supervisor`（pack/handoff/ledger/assign）、
`server-risk`（gate/capacity/replay）、`llm-gateway`（chat）。

三条约束：

- **单角色项目 `pipeline = None`**：不持有流水线，`validate()` 也就不会拿"编排引用了别人"
  判它不自洽；
- **编排在 `projects/flows/`**：只写 `capability + action`，换实现不动编排、换编排不动角色；
- **`check_capabilities(flow, online)`**：编排出问题不再靠跑一遍才发现，少谁直接报。

开发动作（apply edits / 语法检查 / 出 diff）抽到了 `integrations/game_server_dev.py`：
这是"开发角色"的本事，不是某个业务项目的私有逻辑，所以 `bugfix-dev` 和发布链共用同一份实现。

## 1. 为什么 bug 采集要和新闻采集长一样

`frameworks/news/collectors.py` 把"消息面采集"抽象成一个形状：

    collect(params) -> {"events": [...], "scanned": int, "deduped": int, "degraded": bool, ...}

期货、股票、爬虫都能换成自己的源，流水线不动。bug 采集直接复用这个形状
（`frameworks/netdev/collectors.py`）：

    collect(params) -> {"bugs": [...], "scanned": int, "deduped": int,
                        "truncated": int, "ignored": int, "degraded": bool, "reason": str}

三条规矩也一样：不编造（读不到就 degraded）、不判根因（只说哪里炸了）、去重可复现。

**去重指纹**：`错误码 + 接口` 为主键。不用栈顶帧做主键——同一次故障通常只有第一条日志
带 traceback，按栈帧取指纹会把"带栈"和"不带栈"的重复上报拆成两条。错误码为空时才退到
栈顶帧，再退到消息前 60 字。

## 2. 上报不是"喊管理者"

框架里跨 Agent 只有一条路：`submit` 按 `capability + action` 路由，任务落 `tasks` 表，
可重试可续跑（`emit` 只在一次 run 内广播）。所以 bug 采集**作为流水线第一步**就完成了上报：

    bugs → triage → locate → propose → dispatch → fix → pack → audit → gate → ledger → report → store

经理那一步是 `supervisor/assign`：编排把工作流交到经理手上，经理决定每个工作流
交给谁、干什么、验收什么，开发只认这张派工单。派单由 Coordinator 按 DAG 做，
不需要 bug 采集官知道"管理者"是谁。这也是
`docs/GAME_SERVER.md` 里"不新建 Manager"的延续。

## 3. 三段新动作

### triage/classify（零 token）

按接口把去重后的 bug 聚成簇，严重度×出现次数排序，上限 `bug.max_clusters`。
只归并排序，不判根因——根因是 locate 的事，动代码是 implement 的事。

### dev/locate（零 token）

三级定位，逐级降置信度，最后可以"没找到"：

| 级别 | 依据 | 置信度 |
|---|---|---|
| 1 | 栈顶帧 `File "x.py", line N, in f`，文件必须在仓库里存在 | high |
| 2 | 路由表（`game_server_audit.scan_code` 的 method/path → file:line） | medium |
| 3 | 错误码/消息关键词 grep 前 400 个代码文件 | low |

找不到就进 `unlocated` 并写清原因。宁可让人看一眼，也不给假行号。

### dev/propose（默认零 token）

维护一张"模式 → 补丁"表，命中就产 `edits`（file/find/replace），例如：

| 审计规则 | 补丁 |
|---|---|
| `auth_missing` | `@route(..., auth=False)` → `auth=True` |
| `tx_missing` | `@route` 与 `def` 之间插 `@tx` |
| `broad_except` | `except Exception: pass` → 记录并返回失败 |
| `busy_wait` | `while True: pass` → 加 `time.sleep`（并补 `import time`） |

覆盖不到就进 `deferred`（建议单），不硬凑补丁。

`use_llm=True` 时才调 `llm/chat`，要求模型只输出
`{"edits":[{"file","find","replace","why"}]}`；落地前仍要过三道门：路径在仓库内、
`find` 真能命中、语法检查通过。模型失败或回包不合法 → 自动退回规则模板。

## 4. 为什么默认不调模型

拆开看"找代码 + 改代码"：

| 环节 | 能不能 0 token | 依据 |
|---|---|---|
| 定位 | 能 | 栈帧自带 file:line；没有就用路由表；再没有 grep |
| 修改 | 大部分能 | 审计规则表已经说明"该改什么"，规则与补丁一一对应 |
| 验证 | 能 | find 命中 + `compile()` + 回放状态哈希 |

只有"跨文件改写、根因推断"这类非模板场景才需要模型。而且模型只产 `edits`，
不碰文件句柄——写歪了会被三道门挡下，不会污染仓库。

## 5. 接模型的三条路

| 方式 | 配置 | 适用 |
|---|---|---|
| `llm/chat` + HTTP | `LLM_URL`（或复用 `AUDIT_LLM_URL`）、`LLM_MODEL`、`LLM_TOKEN` | 接远程/本地 OpenAI 兼容端点 |
| `llm/chat` + MCP | `LLM_BACKEND=mcp`、`LLM_MCP_COMMAND`、`LLM_MCP_TOOL` | 模型能力已经被某个 MCP server 暴露 |
| `mcp-agent` | `multi-agent mcp-agent --command ... --prefix dev` | 接"会动手改代码"的外部编码 Agent |

前两条在 `integrations/llm.py` 里统一成一个动作 `llm/chat`，调用方只写
`{"capability":"llm","action":"chat"}`，换后端不改调用方。MCP 后端每次调用起一个
stdio 子进程（`MCPClient.start → tools/call → close`），适合一问一答；高频场景把 server
挂 `mcp-agent` 常驻。

未配置端点时 `llm/chat` 直接 `degraded=True`，调用方降级——所以"没接模型"和
"接了但端点挂了"是同一个结果，链路不会卡住。

## 6. 扩展点

- **换 bug 源**：继承 `BugCollector`（或用 `frameworks/netdev` 的 `BugWatchFramework`
  配一套关键词），`fetch=` 注入远端拉取；`INFO` 不计 bug。
- **加补丁模板**：在 `game_server_bugfix._rule_edits` 里加一段，规则码与审计规则对齐；
  新增模板必须能被 `_validate_edits` 的 find 命中校验验证。
- **加限额**：`config/limits.json` 的 `bug.*` 与 `llm.*`（输入/输出字符数、超时）。

## 7. 已知不覆盖

- 容量类问题（`ROOM_EXHAUSTED`）不是代码 bug → `deferred`；
- 协议缺接口（如登出接口未实现）只由审计报，本链不自动补新接口；
- `fixture` 仓库本身带故意缺陷，演示只保证"补丁真的落地、审计 error 不增加"，
  不保证闸门 `pass`。