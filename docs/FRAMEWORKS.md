# 采集框架：关键词怎么触发，命中后交给谁

两套采集都是**框架**，不是一个写死的采集器：

| 框架 | 目录 | 业务继承它的例子 |
|---|---|---|
| 新闻采集 | `multi_agent/frameworks/news/` | BalanceAI（`projects/trade/futures_balance.py`）、股票日报 |
| 互联网开发采集 | `multi_agent/frameworks/netdev/` | 游戏服务端（`projects/netdev/`），也可接 Web / App 后端 |

两者共用 `frameworks/keywords.py`：**加一行关键词，就多一条触发**。

## 1. 关键词表长什么样

放在 `config/keywords/<name>.json`，一组关键词就是一条规则：

```json
{
  "name": "netdev",
  "max_hits": 50,
  "groups": [
    {
      "group": "crash",
      "keywords": ["Traceback (most recent call last)", "panic:", "Segmentation fault"],
      "level": "critical",
      "mode": "program",
      "target": {"capability": "dev", "action": "locate"}
    }
  ]
}
```

四个字段决定"关键词多少、怎么触发、调哪个"：

| 字段 | 作用 |
|---|---|
| `keywords` | 命中这些词就算触发；`regex: true` 时按正则 |
| `level` | `info` / `warn` / `error` / `critical`，决定优先级 |
| `mode` | `program` 零 token 直接算；`llm` 先粗筛再让模型复核 |
| `target` | 命中后交给谁：`{"capability": "dev", "action": "locate"}` |

## 2. 命中后怎么上报

采集官不"喊人"，它把命中行连同 `target` 一起交出去，由 coordinator 按编排派单：

```
关键词命中行 → bug/collect（整行 + group + target）
            → coordinator 按 DAG 派活
            → supervisor/assign（经理分派：谁干、干什么、验收什么）
            → dev/implement（开发）
            → auditor/code（审计）
            → supervisor/pack + ledger（送审、台账）
            → report/build（报告）
```

`target` 不是摆设：`BUGFIX_FLOW` 里 `deploy` 的那一步由 `supervisor/assign` 按
`target` 决定交给 `dev`；如果这一组是 `{"capability": "server_risk", "action": "capacity"}`，
同样的命中会先走容量核查而不是改代码（见 `config/keywords/netdev.json` 的 `timeout` 组）。

## 3. 两条判定路径

| mode | 怎么判 | 成本 | 适用 |
|---|---|---|---|
| `program` | 关键词/正则命中即算 | 0 token | 错误码、崩溃、明确事件 |
| `llm` | 关键词先粗筛，再问 `llm/chat` "这条算不算" | 烧 token | 模糊可疑行（slow / retry / 降级） |

`llm` 模式下端点没配、或判定抛异常，自动按"命中"处理（`judged=False`）——宁可多留一条，
也不会因为模型不可用而漏采。

## 4. 加一组关键词要改哪里

只改一处：`config/keywords/<name>.json` 加一个 `group`。框架、编排、角色项目都不用动。

要改用代码里的关键词集（测试/临时）：

```python
from multi_agent.frameworks.netdev import BugWatchFramework, examples

fw = BugWatchFramework(examples.ERROR_WATCH)      # 只盯错误行
fw = BugWatchFramework(examples.CRASH_WATCH)      # 只盯崩溃
fw = BugWatchFramework(examples.PERF_WATCH)       # 只盯超时 → 交风控
fw.collect({"log_path": "server.log"})
```

`examples.py` 里的四个例子就是"关键词多少、怎么触发、调哪个"的样板：
错误行 → `dev/locate`、崩溃 → `dev/locate`、超时 → `server_risk/capacity`、
可疑行 → 规则粗筛 + 模型复核。

## 5. 目录怎么分

```
multi_agent/
  frameworks/
    keywords.py          关键词引擎（两组共用）
    news/                新闻采集框架（BalanceAI 继承它）
    netdev/              互联网开发采集框架（游戏这头，附 4 个例子）
  projects/
    trade/               交易域：新闻采集 / 评估 / 平衡表 / 期货 / 股票
    netdev/              互联网开发域：bug 采集 / 分诊 / 开发 / 报告 / 游戏发布
    platform/            平台角色：审计 / 经理 / 风控 / 模型通道
    flows/               编排：只写 capability + action
```

一个角色一个项目、一个域一个文件夹：想知道"这条链是谁在干活"，看 `projects/<域>/`
和 `projects/flows/<链路>.py` 就够了。

## 6. 两个采集器的取舍

`BugWatchFramework`（关键词）与 `LogBugCollector`（结构化解析）可以并存：

| 采集器 | 认什么 | 适合 |
|---|---|---|
| `LogBugCollector` | `level=... endpoint=... code=...` + traceback 块 | 格式规范的自建服务 |
| `BugWatchFramework` | 只要关键词命中 | 杂牌日志、第三方服务、只想快速加规则 |

前者字段齐、能直接定位；后者"加个词就能采"，代价是字段要从行里正则回捞。

## 7. 模型配置：一处改，Key 直接填

以前三处模型调用各读各的（`AUDIT_LLM_URL`、`GAME_TEST_LLM_URL`、`LLM_URL`），
切"大模型版"要改三处。现在只有一份 `config/llm.json`，外加一份可选的本地产覆盖
`config/llm.local.json`（已在 `.gitignore`，专门放 Key）：

```json
{
  "enabled": true,
  "default": {"backend": "http", "url": "https://api.example.com/v1",
              "model": "your-model", "token": "${ENV:LLM_TOKEN}"},
  "profiles": {
    "chat":      {"url": "…", "model": "…", "token": "${ENV:LLM_TOKEN}"},
    "audit":     {"url": "…", "model": "…", "token": "sk-直接写也行"},
    "test_plan": {"url": "…", "model": "…"},
    "judge":     {"url": "…", "model": "…"}
  },
  "mcp": {"command": "${ENV:LLM_MCP_COMMAND}", "tool": "${ENV:LLM_MCP_TOOL}"}
}
```

Key 三种填法，按场景选：

| 填法 | 写法 | 适用 |
|---|---|---|
| 直接填 | `"token": "sk-..."` | 本机 / 私有部署，最省事 |
| 环境变量引用 | `"token": "${ENV:LLM_TOKEN}"` | 要提交仓库、CI 注入 |
| 留空回落 | `"token": ""` | 沿用旧部署的 `LLM_TOKEN` / `AUDIT_LLM_TOKEN` |

**不想让明文 Key 出现在主配置里**：复制 `config/llm.local.example.json` 成
`config/llm.local.json`，把 Key 填进去。同名字段覆盖主配置，文件不进仓库，也不用 export。
也可以只写需要覆盖的那一小段：

```json
{"profiles": {"chat": {"token": "sk-..."}}}
```

四条规矩：

- **一处生效**：`llm/chat`、审计 L3、`test_plan`、辩论裁决 / 关键词复核共用这份配置；
- **本地覆盖优先**：先读 `config/llm.json`，再读同目录 `llm.local.json`，后者覆盖前者；
- **没配就降级**：`url` 空 → 直接 `degraded` 退回规则级，不烧 token、不卡链路；
- **`enabled` 只是标记**：真正决定是否调用看 `url`；`enabled=false` 不会拦住已填的端点。

自检（只报"从哪来"，不回显 Key）：

```bash
python -m multi_agent.cli llm-status
# 配置文件：…/config/llm.json
# 本地覆盖：（无；可建 config/llm.local.json 直接填 Key）
#   chat       url=https://api.example.com/v1  model=your-model  key=inline
```

## 8. 新闻链路：谁调权重

```
collector/scan → supervisor/tune（经理调权重）→ assess/classify → balance/build
```

采集只说"命中了什么"，**权重和"算不算大事"由经理定**：

```python
{"weights": {"supply_shock": 0.95, "trade_shock": 0.6},
 "major_weight": 0.8,     # ≥0.8 才算大事，才往下走算平衡表
 "min_level": "warn"}     # info 级事件不参与
```

`tune` 零 token：默认按关键词表的组各给 1.0，调用方覆盖就覆盖。评估官拿到 `tuning`
按新权重打分，并把 `tuning` 回写进结果，报告里可复算。

bug 链路没有调权重这一步：定级由分诊的"严重度×出现次数"决定，
顺序是 `bug/collect → triage/classify → dev/locate → dev/propose →
supervisor/assign → dev/implement → auditor/code → server_risk/gate`。
开发侧收单接口与框架基类见 `docs/DEV_INTERFACE.md`，审计怎么自己出用例见
`docs/AUDIT_INTERFACE.md`。

## 9. 工具调用：模型先查代码，再出补丁

`llm/chat` 带 `tools` 时走 function call 有界循环（默认 4 轮）。工具实现由调用方注入，
本层只负责收发与边界：

```python
from multi_agent.integrations.dev_tools import build

specs, impls = build(repo_root)      # read_file / grep_code / list_dir，只读
res = chat({"prompt": "…", "tools": specs, "tool_impls": impls,
            "max_tool_rounds": 4})
# res["tool_trace"]: [{round, tool, args, ok, result_chars, error}]
```

四条边界，全部降级不炸整轮：超轮次 → `degraded + truncated`；未知工具名 → 回
`unknown_tool`；参数不是合法 JSON → 回 `bad_arguments`；工具自己抛错 → 回错误文本。
模型据此可以改参数再试。

`dev/propose` 传 `tools=True` 即启用，产出仍只认 `edits`（find/replace），
写操作不经过工具。三形态对照见 `examples/login_bug_watch/`。