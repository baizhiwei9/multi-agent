# 编排对比：LangGraph 与 multi-agent

这份文档回答一个问题：**同样是"图 + 多 Agent"，LangGraph 的机制和我们这套（Hub 派活 + DBManager 取数）差在哪，哪些该学、哪些不该学。**

结论先说：两者都把工作流画成有向图，但**状态放在哪**决定了其余一切——
LangGraph 的节点在同进程里读写一份共享 state；我们的节点是独立进程，靠
`capability + action` 消息和 DBManager 交换数据。所以**能学的是"状态合并语义"和"续跑"，不是"共享内存"**。

---

## 1. 一张表看完

| 维度 | LangGraph | multi-agent（本项目） |
| --- | --- | --- |
| 执行单元 | 节点 = 同进程函数，读 state 返回增量 | 步骤 = 一条 `capability/action` 任务，由 Hub 派给某个 Agent 进程 |
| 共享状态 | `State` schema + channels，节点直接读写 | 步骤结果进 run 记录；业务数据走 DBManager（唯一写入口） |
| 分支合并 | channel 的 **reducer**（默认 last-write-wins，可 append/自定义） | 各步骤各写各的 key，join 靠 `depends_on`；写同一份数据靠 `db/append` |
| 并行 | 一个节点多条出边 → 同一 super-step 并行 | `depends_on` 同时满足即并行提交（Hub 内多个 worker 抢任务） |
| 动态扇出 | `Send` 按列表派发 N 个分支 | 之前只能手写 N 个步骤 → 本轮加 **`foreach`** |
| 条件路由 | 条件边 / `Command`（节点内决定下一步） | 声明式 `when`（协调者判断，节点不能私自跳步） |
| 断点续跑 | checkpointer + `thread_id`，`interrupt()` 可停数天 | run 状态落 `pipeline_runs`，本轮加 **`resume(run_id)`** |
| 人工介入 | `interrupt()` 一等公民 | 审计未通过 → 终态 `degraded`（结果可看不可直接用）；暂停等人工仍靠外部流程 |
| 子图复用 | subgraph 当节点用 | 多条 `extra_pipelines`；暂不支持"流水线嵌套" |
| 进度观测 | `stream()` 节点级事件 | `task_events` 时间线 + `pipeline_events` 轮询/`trace` 命令 |
| 跨机 | 库内执行，多机要自己搭 | 原生：HTTP / TCP / JSON-RPC / MCP / UDP 打洞，Hub 路由 |
| 成本 | 节点内调 LLM 时按 token 计 | 派活与审计默认零 token（`audit_level=rule`） |
| 故障隔离 | 同进程，一个节点崩可能带走整个图 | 进程隔离，Agent 挂了换 worker、TTL 回收、任务重试 |

一句话：**LangGraph 优化的是"单进程内状态怎么流"，我们优化的是"跨进程任务怎么派"。**

---

## 2. 本轮学过来的三件

### 2.1 通道 + 合并策略（对应 State channel / reducer）

以前两个采集官各写各的 key，下游要自己知道去 `steps.news_balance.result` 和
`steps.news_event.result` 里拼。现在步骤可以 `emit` 到同一条通道：

```python
{"name": "news_balance", "capability": "B", "action": "scan_news",
 "emit": {"channel": "news", "path": "events", "merge": "append"}},
{"name": "news_event", "capability": "B", "action": "scan_events",
 "emit": {"channel": "news", "path": "events", "merge": "append"}},
{"name": "screen", "capability": "moderator", "action": "screen",
 "params": {"news": "${channels.news}"}, "depends_on": ["news_balance", "news_event"]},
```

- `merge`：`replace`（默认，last-write-wins）/ `append`（列表追加）/ `sum`（数值累加）/ `merge`（对象合并）；
- 配错（比如对字符串用 `sum`）→ 该步骤 `emit_error` 失败，**不静默吞**；
- 通道值随 run 一起落 `pipeline_runs`，`${channels.news}` 可在任意下游步骤引用。

这是 LangGraph reducer 的等价物，但落在"跨进程结果"上：合并发生在协调者进程，而不是某个 Agent 的内存里。

### 2.2 动态扇出（对应 Send）

"对 5 个品种各跑一次建表"以前要写 5 个步骤；现在一步展开：

```python
{"name": "build_each", "capability": "A", "action": "build_sheet",
 "foreach": {"from": "${params.commodities}", "as": "item", "max": 20},
 "params": {"commodity": "${item}"},
 "emit": {"channel": "balances", "merge": "append"}}
```

- 提交时 `wait=False` 一次全发出去，由 Hub 按 worker 并发抢；
- 全部到终态才放行下游（join），分片结果按列表顺序收在 `steps.build_each.items`；
- `result` 只含成功的分片；任一分片失败 → 该步骤 `failed`（`fanout_failed`），下游按失败传播；
- `max` 防手滑（默认 20）。

### 2.3 断点续跑（对应 checkpointer）

`run` 状态本来就在 `pipeline_runs` 里，但以前只能从头 `start`。现在：

```bash
multi-agent pipeline --hub $HUB --resume r_ab12cd34ef56 --wait
```

```python
coord.resume(run_id)        # 进程内
# 或 submit("coordinator", "resume", {"run_id": ...})
```

- 已 `succeeded` / `skipped` 的步骤**不重跑**（`task_id` / 结果原样保留）；
- `failed` / `running` / `pending` 的步骤重新排队，标记 `resumed: true`；
- 只在 `TERMINAL`（含 `degraded`）状态下允许 resume，跑着的 run 拒绝（防双跑）；
- 配合任务幂等键 `run_id:step`（扇出是 `run_id:step:index`），即使进程崩在提交与回执之间，也不会重复副作用。

---

## 3. 有意不学的三件

| LangGraph 能力 | 为什么不照搬 |
| --- | --- |
| `Command` 节点内动态路由 | 节点是独立进程，只拿到本步参数，看不到完整图状态；让节点决定"下一步去哪"会把编排权从协调者手里拿走，也失去"谁派活、谁记账"的审计线。条件路由保留为声明式 `when`，由协调者统一判断。 |
| subgraph 嵌套 | 我们的"子图"单位是流水线（`pipeline` + `extra_pipelines`），跨机执行靠 Hub 任务；真嵌套需要把父 run 的上下文传给子 run、再把子 run 结果合并回父通道——收益不如直接把两条流水线拆开并复用 `emit`。 |
| `interrupt()` 暂停等人工 | 框架已有 `degraded` 终态（审计不过 → 结果不可直接用）与 `resume`（修完再跑）；"停下来等人点确认"属于上层流程（谁审、在哪审、审完改什么），硬塞进协调者会把业务规则写进框架。需要时用 `paused` 状态 + 外部审批系统对接。 |

另外，LangGraph 的 `stream()` 对我们意义有限：跨进程的进度天然是任务事件，
`trace` / `pipeline_events` 已经是同一件事的轮询版。

---

## 4. 我们保留的优势（别为了像 LangGraph 而丢掉）

1. **进程边界**：某个 Agent 的依赖坏了只影响它自己的任务，Hub 换 worker 重试；
2. **协议化**：HTTP / TCP / JSON-RPC / MCP 都能接，跨机、跨 NAT；
3. **单写入口**：业务数据只走 DBManager（WAL + `BEGIN IMMEDIATE` + 文件锁），不靠"每个节点都改同一份内存"；
4. **零 token 默认**：派活、初筛、送审、规则审计都不调模型，`audit_level=llm` 才烧钱；
5. **可对账**：`task_events`（提交/派发/重试/完成）+ `pipeline_runs`（每步状态/结果/通道）双份账。

---

## 5. 落地位置

| 能力 | 代码 | 测试 |
| --- | --- | --- |
| `emit` + 通道合并 | `multi_agent/coordinator.py`（`_emit` / `_merge_value`） | `tests/test_coordinator.py::test_channels_reducer_append` |
| `foreach` 扇出 | `multi_agent/coordinator.py`（`_execute_foreach`） | `tests/test_coordinator.py::test_foreach_fanout` |
| `resume` 续跑 | `multi_agent/coordinator.py`（`resume` / `_execute(resume=True)`）、CLI `pipeline --resume` | `tests/test_coordinator.py::test_resume_skips_succeeded_steps` |
| 步骤字段说明 | `multi_agent/pipelines.py` 模块文档 | — |