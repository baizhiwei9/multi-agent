# 多方辩论（`debate/run`）：`participants[]` + `judge`

> 两方评议（采集官 ⇄ 审计官）保持不变；这份文档说明新增的 N 方模式怎么配、怎么停、花多少钱。
> 实现：`multi_agent/integrations/debate.py`；限额：`config/limits.json`（见 docs/LIMITS.md）。

## 1. 两种模式

| 模式 | 触发条件 | 一轮里发生什么 | 结论从哪来 |
| --- | --- | --- | --- |
| `pair`（老路径） | 给了 `collector` + `auditor` | 采集官出稿 → 审计官点评 | 审计回执里的 `decision` |
| `panel`（新） | 给了 `participants[]` | 每个发言者按顺序说一遍，能看到本轮之前的发言 | `judge` 裁决；没有 judge 时按显式立场过半 |

`participants[]` 与 `collector/auditor` 同时给时，**以 `participants[]` 为准**（不报错，便于灰度）。

## 2. 参数

```json
{
  "name": "debate",
  "capability": "debate",
  "action": "run",
  "params": {
    "topic": "${steps.intake.result}",
    "participants": [
      {"capability": "bull", "action": "argue", "role": "bull"},
      {"capability": "bear", "action": "argue", "role": "bear"}
    ],
    "judge": {"capability": "research_manager", "action": "decide"},
    "max_rounds": 3,
    "max_speeches": 12,
    "judge_every_round": true,
    "stop_on_consensus": true
  }
}
```

| 字段 | 缺省 | 说明 |
| --- | --- | --- |
| `participants` | — | 每项 `capability`/`action` 必填；`name` 缺省取 `role`，再缺省用 `capability/action` |
| `judge` | 无 | 有则在每轮末（或全程最后）裁决一次 |
| `max_rounds` | `limits.debate.max_rounds`（10） | 轮数上限，超过按限额截断 |
| `max_speeches` | `limits.debate.max_speeches`（24） | 总发言条数上限；不得小于参与人数 |
| `judge_every_round` | `true` | `false` 时只在最后一轮裁决，省 token |
| `stop_on_consensus` | `true` | 本轮所有发言者的**显式**立场一致就停 |

发言者收到的参数：`topic`、`round`、`speaker`、`role`、`order`、`participants`（名单）、
`transcript`（本轮之前所有发言的压缩上下文，按 `max_message_chars` 截断）、`prior`（上一位发言），
外加透传的 `context`。

## 3. 返回值

```json
{
  "ok": true, "mode": "panel", "rounds": 2, "speeches": 6,
  "decision": "APPROVE", "stopped_reason": "judge_converged",
  "participants": ["bull", "bear", "risk"],
  "judge": {"name": "judge/decide", "decision": "APPROVE", "converged": true, "round": 2, "text": "..."},
  "transcript": [{"round": 1, "speaker": "bull", "text": "...", "stance": "APPROVE", "chars": 812}],
  "chars_used": 5340,
  "limits": {"max_rounds": 10, "max_message_chars": 5000, "max_total_chars": 100000,
             "max_participants": 6, "max_speeches": 24}
}
```

`stopped_reason` 取值：`approved`（pair）、`judge_converged`、`consensus`、`max_speeches`、
`char_budget`、`max_rounds`、`agent_error`、`judge_error`、`no_major_news`。

## 4. 停止顺序（先到先停）

1. 单轮每位发言后：累计字数撞 `max_total_chars` → `char_budget`；发言条数撞 `max_speeches` → `max_speeches`；
2. 本轮全部发完且配了 `judge` 且 `judge_every_round` → 裁判 `converged=true` 或 `APPROVE` → `judge_converged`；
3. 本轮所有显式立场一致 → `consensus`；
4. 轮数到 `max_rounds` → `max_rounds`（未配置 judge 时按本轮显式立场过半给结论，平票 `MODIFY`）。

裁判回执只认显式字段：`decision`（APPROVE/MODIFY/REJECT，也可 `pass/suspect/fail`）、
`converged`（布尔或 true/yes/1/是）。没有显式 `converged` 时，`APPROVE` 视为收敛。

## 5. 成本

- **按发言条数计费，不按轮数**：三方一轮是 3 次调用，两方一轮是 2 次。
- `max_speeches` 是硬闸（默认 24），`max_total_chars` 是字数硬闸；两者都撞就以先到的为准。
- 想省钱：`judge_every_round=false`（只最后裁一次）、`max_rounds=2..3`、参与者用便宜模型，
  裁判用强模型（角色可分开配置，见 `RoleSpec`）。
- 不存在"并行辩论"：每个发言者要读前一位的稿子，只能顺序来；要并行的是**跨标的**
  （DAG 的 `foreach`），不是同一场辩论内部。

## 6. 与流水线衔接

`debate/run` 的结果可直接喂给下游（`supervisor/ledger`、`report/build`、`risk/gate`），
因为它仍然只有 `transcript` 一种事实来源，`decision` 一个结论字段。接 `trading_desk` 时：

```
intake → analyst×N（parallel）→ debate(research: bull/bear + judge)
       → trader → debate(risk: ag/cv/nu + judge) → approval → execution(paper)
```

## 7. 不做什么

- 不做投票计票以外的"加权声誉"：谁说得对由 `judge`（人或模型）判断，框架不猜权重；
- 不替发言者写观点：参与者只回文本/显式 `stance`，没给 `stance` 就不参与共识判断（不猜）；
- 不做仓位与下单：那是 `trading_desk` 的 `trader` 与 `execution` 的事，辩论只管"收不收敛"。

## 8. 测试

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_debate.py` | 老两方路径：无新闻短路、逐轮 critique、轮数/字数上限、截断、缺 client 报错 |
| `tests/test_debate_panel.py` | 顺序发言与上下文传递、judge 收敛早停、consensus 早停、`max_speeches` 闸、judge 异常、`judge_every_round=false`、participants 优先于两方、限额交叉校验 |
| `tests/test_limits.py` | 新字段的默认值与边界（`max_participants` / `max_speeches`） |