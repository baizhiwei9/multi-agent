# game-server bugfix：线上 bug → 定位 → 补丁 → 审计

同一条底座上的第二条游戏链路。和 `examples/game_server`（发布链）比，入口不是"开发报告"，
而是**服务器日志里的报错**；出口不是"发布包"，而是"修复报告 + 补丁 + 审计结论"。

```
bug/collect → triage/classify → dev/locate → dev/propose
            → supervisor/assign（经理派单）→ dev/implement
            → supervisor/pack → auditor/code → server_risk/gate → report → db/write
```

| 步骤 | 项目（一角色一项目） | 花不花 token | 说明 |
|---|---|---|---|
| `bug/collect` | `bug-collector`（继承 `frameworks/netdev`） | 0 | 关键词扫日志 → 命中行 + 上下文 → bug |
| `triage/classify` | `bug-triage` | 0 | 按接口聚簇、按严重度×次数排序 |
| `dev/locate` | `bugfix-dev` | 0 | 栈顶帧 → 路由表 → 关键词，三级定位到文件行 |
| `dev/propose` | `bugfix-dev` | 0（默认） | 规则模板产 `edits`；`use_llm=True` 时才问模型 |
| `supervisor/assign` | `supervisor` | 0 | 经理派单：每个工作流交给谁、干什么、验收什么 |
| `dev/implement` | `bugfix-dev` | 0 | find/replace + 语法检查 + 出 diff（框架实现） |
| `auditor/code` 等 | `auditor` / `supervisor` / `server-risk` | 0 | 与发布链同一套审计/风控/送审 |

## 进程怎么起：一个角色一个 Agent

demo 里每个 `AgentRuntime` 只挂一个角色项目（`projects/roles/`）：

```
bug-collector   →  bug/collect
bug-triage      →  triage/classify
bugfix-dev      →  dev/locate|propose|implement
bugfix-reporter →  report/build
auditor / supervisor / server-risk  →  框架角色项目
db              →  DBManager
```

想把 bug 采集换成 Sentry：只改 `BugCollectProject(collector=...)` 那一个进程；
想多开两个开发：`bugfix-dev` 起两份，Hub 按 capability 轮询分流。
编排（`projects/flows/bugfix.py`）一行都不用动。

## 跑

```bash
python examples/game_server_bugfix/run_demo.py          # 规则补丁（0 token）
python examples/game_server_bugfix/run_demo.py --llm    # 让模型补模板覆盖不到的
python examples/game_server_bugfix/run_demo.py --log path/to/server.log
```

产物在 `examples/game_server_bugfix/out/bugfix/`：

```
bugfix_report.md     结论前置：闸门 / bug 清单 / 分诊 / 定位 / 修复方案 / 审计 / 未决项
manifest.json        每个文件的 path/bytes/sha256
checksums.sha256     可直接 sha256sum -c
artifacts/*.patch    自动生成的补丁（unified diff）
evidence/*.json      bugs / triage / locate / propose / audit / gate 原始回执
```

补丁只写 `out/work/repo`（fixture 副本），不改示例仓库。

## 日志格式

文本（带 traceback）：

```
2026-11-26T03:12:41Z ERROR service=shop endpoint=/v1/shop/buy code=SHOP_TX_FAILED msg="扣费失败"
Traceback (most recent call last):
  File "server/shop.py", line 9, in buy_item
TypeError: 'NoneType' object is not subscriptable
```

JSONL（崩溃上报）：

```json
{"ts": "...", "level": "ERROR", "service": "shop", "endpoint": "/v1/shop/buy",
 "error_code": "E1", "message": "boom", "traceback": ["File \"server/shop.py\", line 9, in buy_item"]}
```

换数据源只改一处：`BugCollectProject(collector=...)` 传另一个 `BugCollector` 子类
（`fetch=` 注入远端拉取也行）。`INFO` 不计 bug，只计入 `ignored`。

## 三条边界

1. **不编造**：日志不存在 / 拉不到 → `degraded=True + reason`，`bugs` 留空。
2. **不猜位置**：栈帧里的文件不在仓库就不算命中，退到路由表、再退到关键词，
   最后如实写"未定位"，不报一个假行号。
3. **不越权改**：模型只产 `edits`（file/find/replace），落地前必须 find 命中、
   路径在仓库内、语法检查通过；否则丢弃并记 `dropped`。

## 接模型：`llm/chat`

```bash
# HTTP（OpenAI 兼容）
export LLM_URL=http://127.0.0.1:18799/v1/chat/completions
export LLM_MODEL=openclaw/yuanbao-agent
export LLM_TOKEN=...
python examples/game_server_bugfix/run_demo.py --llm

# MCP（stdio server 暴露一个 chat 工具）
export LLM_BACKEND=mcp
export LLM_MCP_COMMAND="python my_chat_mcp_server.py"
export LLM_MCP_TOOL=chat
```

未配置时 `llm/chat` 直接 `degraded`，`propose` 自动退回规则模板——所以"不接模型"
和"接了模型但端点挂了"是同一个结果，不会卡住链路。

## 已知不覆盖

- `match/join` 的 `ROOM_EXHAUSTED` 属于容量问题，不是代码 bug → 进 `deferred` 建议单；
- 协议缺接口（如登出 `P-03` 没实现）仍由 `auditor/code` 报，本链不自动补新接口；
- 修复后闸门仍可能 `block`：`fixture` 仓库本身就有故意留的缺陷（硬编码密钥等），
  演示只保证"补丁真的落地、审计 error 不增加"。