# 照这个目录抄，就能把业务接进多 Agent 框架

这是一个**可运行的示例项目**（`commodity-monitor`），只依赖框架本身，不连任何外部服务。
把 `project.py` 复制到你自己的目录、改掉名字和三个 handler，就是一个新项目。

```
examples/my_project/
├── README.md       ← 本文件：继承步骤 + 每个角色做什么
├── project.py      ← 要抄的就是它（角色表 / 流水线 / 三个 handler / 注册钩子）
└── run_demo.py     ← 端到端跑一遍：Hub + Agent + 协调官全在本地内存里
```

跑起来：

```bash
python examples/my_project/run_demo.py
python -m pytest tests/test_example_project.py -q
```

---

## 一、继承四步

**第 1 步：声明角色表。** 一个 `RoleSpec` = 一个 capability，Hub 只按 `capability + action`
选人；`instances=N` 表示"这个角色要起几个 Agent"（会出现在 `multi-agent project agents` 里）。

```python
roles = [
    RoleSpec("collector", ["scan"],  "采集官：新闻/公告 → 事件 JSON"),
    RoleSpec("risk",      ["brief"], "风控官：事件 + 行情 → 风险简报"),
    RoleSpec("report",    ["build"], "报告官：结论 → Markdown"),
    RoleSpec("supervisor", ["pack", "handoff", "ledger"], "管理员/监理（框架实现）"),
]
```

**第 2 步：画流水线。** 步骤只有四个字段：`capability`、`action`、`params`、`depends_on`；
上游结果用 `${steps.名字.result.字段}` 注入，参数用 `${params.xxx}` 注入。

```python
{"name": "risk", "capability": "risk", "action": "brief",
 "params": {"events": "${steps.collect.result.events}"},
 "depends_on": ["collect"]}
```

可选字段：`when`（条件跳过）、`foreach`（按列表扇出）、`timeout_s`、`max_retries`、
`run_on_skipped`、`emit`（把结果并进共享通道 `${channels.x}`）。

**第 3 步：只写该你写的 handler。**

```python
def handlers(self):
    return {
        "collector": {"scan": ...},   # 你的业务
        "risk":      {"brief": ...},  # 你的业务
        "report":    {"build": ...},  # 你的业务
    }
```

**内置角色千万别在这里实现**（`supervisor` / `debate` / `moderator` / `auditor`）——
它们由 `integrations/` 提供，写进 `handlers()` 会覆盖掉真实现，`project agent` 起出来就是空壳。

**第 4 步：注册钩子。**

```python
def register(self, runtime):
    super().register(runtime)                       # 业务动作
    register_supervisor(runtime, client=runtime.client)   # 送审/台账（内置）
```

然后 `install()` 把项目与流水线登记进框架，`multi-agent project show commodity-monitor`
就能看到它，`multi-agent project agents commodity-monitor` 会打印要起哪些进程。

---

## 二、每个角色到底做什么

| 角色（capability） | 做什么 | 谁实现 | 输入 → 输出 |
| --- | --- | --- | --- |
| **采集官** `collector` / `B` | 把外部信息变成结构化事件：多站点新闻池 → 按品种分流、去重、截断；取不到就 `degraded=True`+`reason`，**不许编造** | 项目（`news()` 返回 `NewsCollector`） | `{commodities, hours}` → `{events: [...], scanned, deduped, degraded}` |
| **风控官** `risk` | 硬计算风险：读行情/持仓/波动率/事件，出**风险简报**（`RISK_BRIEF`）。零 token、可复算；它是"闸门"，不是"润色" | 项目 | `{data, events}` → `{aggregate_signal, notes, ...}` |
| **审核官** `auditor` | 审别人的产出：数据层（L1/L2 规则+统计）与语义层（L3，可选）→ `{issues, verdict}`；`review_brief` 专审风控简报，`report` 审报告，`run` 审整轮快照 | **框架** `integrations/audit.py` | `{brief/report, project, level}` → `{issues, verdict:{decision}}` |
| **管理员/监理** `supervisor` | 把报告/初筛稿**压成送审件**（按审计限额截断、引用超限点名）→ `handoff` 交给审核官并**等回执** → `ledger` 记台账（每轮谁送了什么、多长、结论是什么） | **框架** `integrations/supervisor.py` | `pack(payload)` → `{payload, chars}`；`handoff` → `{delivered, verdict}`；`ledger` → `{entries, blocked}` |
| **评议器** `debate` | 采集官 ⇄ 审核官**多轮评议**直到通过或触顶（轮数/字数见 `config/limits.json`），记录每轮 `transcript` | **框架** `integrations/debate.py` | `{report, no_news, ...}` → `{rounds, decision, stopped_reason, transcript}` |
| **初筛** `moderator` | 零 token 规则初筛：事件分级、判 `no_news`（无重大事件就不烧多轮评议） | **框架** `integrations/debate.py`（`screen`） | `{balance_news, event_news}` → `{topics, major_count, no_news, verdict}` |
| **报告官** `report` | 只消费上游结果拼 Markdown：**不重算、可复算、零 token**；缺数据就留空并标注 | 项目（`reporter()` 返回 `Reporter`） | `{risk, review, events}` → `{markdown, chars}` |
| **协调官** `coordinator` | 按 DAG 派活、注入参数、落状态、处理条件/扇出/续跑；它**不认识业务**，只认 `capability+action` | **框架** `coordinator.py` | 流水线名+参数 → `run`（`steps/state/warnings`） |

三句话记住分工：

- **项目写"算什么"**：采集、风控、报告；
- **框架写"怎么审、怎么送、怎么评"**：审核官、监理、评议器、协调官；
- **谁都不许编数据**：拿不到就 `degraded` + `reason`，下游按"无数据"分支走。

---

## 三、加进来的三种常见改法

1. **加角色**：`RoleSpec("macro", ["view"], "...")` + 流水线里加一步，`handlers()` 里给实现。
   角色多了想合包，改 `config/limits.json` 的 `roles.co_locate`（如 `[["collector", "supervisor"]]`）。
2. **加第二套流程**：`extra_pipelines = (MY_DEBATE_PIPELINE,)`，两条流程共用同一批角色。
3. **换数据源/报告样式**：只覆写 `news()` / `reporter()`，流水线一行都不用改。

## 四、自检命令

```bash
multi-agent project list                     # 有哪些项目
multi-agent project show commodity-monitor   # 角色/流水线/实现的动作/新闻源/报告器
multi-agent project pipelines                # 把项目的流水线装进 PIPELINES
multi-agent project agents commodity-monitor # 要起几个 Agent、每个绑哪些角色
multi-agent project agent commodity-monitor --hub http://127.0.0.1:8787   # 真起一个
```