# 消息传递图（flow.jpg）约定

> 每次调用 Trade Agent 跑框架，自动生成：
> `flow.jpg`（角色消息序列图）+ `flow_trace.json`（消息清单）+ `trade_report.md`（报告）。

## 1. 怎么生成

```bash
python trade_agent.py --date 2026-11-27 \
  --keywords "国常会,资本市场,A股,MSCI,PMI,美联储,韩国央行,统计局,四大行,工业企业利润" \
  --rounds 3 --out-dir trade_out
```

回放/补画（不重跑辩论）：

```bash
python -m multi_agent.observability.flow --run-dir trade_out --out flow.jpg
```

实现：`multi_agent/observability/flow.py`（序列图）+ `roles_map.py`（角色职责图）。
画图需要 Pillow：`pip install -e ".[flow]"`（或 `pip install pillow`）。

## 2. 角色（2026-11-28 已改名对齐）

| 角色 | capability | 本 panel | 职责 |
|---|---|---|---|
| **Coordinator** | `coordinator`（由 Trade Agent 承担） | 已启用 | 接活、拆步骤、往下派；不干活 |
| **Developer / Worker** | EventScout / 4 席 / 行情 | 已启用 | 干活：事件、辩论、行情 |
| **Supervisor（经理/终审）** | `supervisor` / `verdict` | 已启用 | 每轮复核 + 最后签发；**不是** pack |
| **Handoff（送审/台账）** | `handoff` / `pack, ledger` | 已启用 | 把事件+行情压成送审件、记台账 |
| Debate | `debate` / `run` | 已启用 | 4 席 × N 轮；把 Supervisor 复核回灌下一轮 |
| Judge/裁决 | 已并入 Supervisor | — | 旧名字 `judge` 已改成 `supervisor` |
| 风控官 / 审计 A / 审计 B(J) | `risk` / `auditor` / `J` | **未启用** | 在 `futures_balance`；见子图 B |

- 改名对照：`judge` → `supervisor`；`supervisor`（pack/ledger）→ `handoff`。
- MCP 工具：`supervisor__verdict`、`handoff__pack`、`handoff__ledger`。

## 3. 默认轮数与“和 Supervisor 讨论”

- **默认 3 轮**（`trade_agent.py` 与 `panel_params` 都是 3；`limits.debate.max_rounds=10` 只是上限）。
- `judge_every_round=True`：每轮 4 席发言后，Supervisor 复核一次；3 轮 = **3 次复核**。
- 前 2 次复核（R1、R2）通过 `supervisor_feedback` 回灌给下一轮；所以 3 轮里 **Debate 至少收到 2 次 Supervisor 反馈**，R3 是终审。
- 实跑验证：R2/R3 的 8 条发言全部明确写了“回应经理/监理上一轮复核”，不是各说各话。

## 4. 消息清单（2026-11-27 实跑，3 轮 / 12 条发言 / 20 条消息）

| # | 从 → 到 | 传的是什么 |
|---|---|---|
| 1 | TA/Coordinator → Hub | `events/scan {date, keywords, horizon}` |
| 2 | Hub → EventScout | 派发 `events/scan` |
| 3 | EventScout → 数据源 | 6 源并行爬取（只收当天） |
| 4 | 数据源 → EventScout | `{title, content, published, url, source}` + 各源条数 |
| 5 | EventScout → 自己 | EventOfficer：规则 + 事实 → 未来日历 |
| 6 | EventScout → Hub | `{events, scheduled, digest, calendar}` |
| 7 | Hub → TA/Coordinator | 事件结果 |
| 8 | TA/Coordinator → Handoff | `pack(events_digest + 行情)` |
| 9 | Handoff → TA/Coordinator | `{payload(brief), chars, truncated, notes}` |
| 10 | TA/Coordinator → Hub | `debate/run {4 席, 3 轮, judge_every_round=true}` |
| 11 | Hub → Debate | 派发 panel |
| 12 | Debate → 四席 | 每轮每席 `speak(round, transcript, supervisor_feedback)`；共 12 次 |
| 13 | Debate → Supervisor | `review(round r)`：每轮 4 席后复核（loop 1..3） |
| 14 | Supervisor → Debate | `verdict + critique`：3 次；前 2 次回灌下一轮 |
| 15 | Debate → Hub | panel result（12 条发言 + 3 次复核） |
| 16 | Hub → TA/Coordinator | transcript + supervisor verdict |
| 17 | TA/Coordinator → Hub | `report/build` |
| 18 | Hub → Reporter | panel result |
| 19 | Reporter → TA/Coordinator | Markdown |
| 20 | TA/Coordinator → 自己 | 落盘 + 渲染 `flow.jpg` |

> **Agent 之间没有点对点 socket**：注册 / 取任务 / 回结果全部走 Hub（HTTP + 长轮询）；
> 四席之间的“对话”是引擎把 transcript 拼进下一席 prompt，不是直连。

## 5. 图怎么读

- **横轴**：TA/Coordinator → Hub → EventScout → 数据源 → Handoff → Debate → Supervisor → Reporter；
- **纵轴**：消息序号；蓝=请求，绿=返回，紫=自处理；
- **蓝框 loop**：第 12~14 条消息的循环，标注 `loop 1..3（复核×3，回灌×2）`；
- **右侧表**：每条消息的 `from→to` + 载荷摘要；`flow_trace.json` 是完整版；
- **子图 A**：Agent ↔ Hub 注册/取任务/回结果，红叉=禁止 P2P；
- **子图 B**：futures 的风控官 + 双审计（采集⇄审计A、风控→审计A、审计A→审计B→报告）。

## 6. 约束

1. 图只画实际发生的消息；未启用的角色放在子图/说明里，不假装参与；
2. 载荷只放摘要，不把完整 transcript 塞进图；
3. 中文字体：仓库不带字体；`export FLOW_FONT=/path/to/NotoSansSC-Regular.ttf`，或安装 Noto Sans CJK / 文泉驿微米黑；PR 附了渲染好的 `docs/assets/flow.jpg`。
4. 每次运行覆盖 `flow.jpg` / `flow_trace.json`；留档复制整个 `out-dir`；
5. 默认 3 轮；要改轮数用 `--rounds N`，`Supervisor` 仍每轮复核。
