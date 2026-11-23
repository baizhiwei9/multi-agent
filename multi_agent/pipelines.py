"""内置流水线：把"角色动作"编排成 DAG。

步骤字段
--------
- name / capability / action / params
- depends_on: 依赖的步骤名（默认空）
- when: 条件分支，如 {"step": "news", "path": "result.events", "op": "gt", "value": 0}
- timeout_s / max_retries
- on_fail: 上游失败时是否仍要跑（默认不跑）
- emit: 把结果合并进共享通道，下游用 `${channels.x}` 取
  （`"news"` 或 `{"channel": "news", "path": "events", "merge": "append"}`；
  merge = replace / append / sum / merge）。多个并行分支写同一通道时用它合并，
  不用各自约定 key —— 对应 LangGraph 的 reducer。
- foreach: 动态扇出，`{"from": "${params.commodities}", "as": "item", "max": 20}`
  —— 列表每项注入同名变量，展开成 N 个并行任务，全部结束后按序收结果
  （对应 LangGraph 的 Send）。

参数模板
--------
`${params.x}` 取运行参数；`${steps.build.result.foo}` 取上游结果；
`${channels.news}` 取合并后的通道值；
整个字符串就是一个模板时，返回原始类型（数字/对象），不是字符串。

断点续跑
--------
`coordinator.resume(run_id)` 用落库状态续跑：已成功/已跳过的步骤不重做，
只补未完成的（failed / running / pending）。对应 LangGraph 的 checkpointer。

审计（audit）接入
-----------------
每个流水线在「产出之后」都有一个 `audit` 步骤（capability=auditor），
级别由运行参数 `audit_level` 控制，**默认 `rule`（纯规则，0 网络 0 token）**：

| audit_level | 跑什么 | 成本 |
|---|---|---|
| `rule`（默认） | L1 规则：字段/单位/量级/一致性/新鲜度 | 0 token，纯本地 |
| `stat` | L1 + L2 统计：ADF/KPSS/断点/自相关/过拟合 | 0 token，需 numpy |
| `llm` | L1 + L2 + L3 语义审计 | **烧 token**，需 auditor 侧配 AUDIT_LLM_URL |

协调者（Coordinator）可以通过 `submit("coordinator","start", {... "audit_level":"llm"})`
直接把级别传下来；不传就是最省的 `rule`。
"""

from __future__ import annotations

BALANCE_SHEET = {
    "name": "balance_sheet",
    "description": "大宗收支平衡表：抓取 → 入库 → 新闻 → 修正判定 → 估算 → 报告 → **审计**",
    "steps": [
        {"name": "build", "capability": "A", "action": "build_sheet",
         "params": {"category": "${params.category}"}, "timeout_s": 900},

        {"name": "store_balance", "capability": "db", "action": "write",
         "params": {"collection": "balance", "key": "${params.commodity}:latest",
                    "value": "${steps.build.result.results.${params.commodity}.balance}"},
         "depends_on": ["build"]},

        {"name": "news", "capability": "B", "action": "scan_news",
         "params": {"hours": "${params.hours}"}, "timeout_s": 300,
         "depends_on": ["build"]},

        {"name": "store_news", "capability": "db", "action": "append",
         "params": {"stream": "news", "payload": "${steps.news.result}"},
         "depends_on": ["news"]},

        {"name": "vet_news", "capability": "Q", "action": "verdict",
         "params": {"commodity": "${params.commodity}"},
         "depends_on": ["news"],
         "when": {"step": "news", "path": "result.events", "op": "gt", "value": 0}},

        {"name": "store_verdict", "capability": "db", "action": "write",
         "params": {"collection": "verdicts", "key": "${params.commodity}:latest",
                    "value": "${steps.vet_news.result}"},
         "depends_on": ["vet_news"]},

        {"name": "report", "capability": "E", "action": "report",
         "params": {"commodity": "${params.commodity}"},
         "depends_on": ["store_balance", "store_news"]},

        # ★ 审计：报告出完先审，再进终审 J。级别由 params.audit_level 控制
        {"name": "audit", "capability": "auditor", "action": "full",
         "params": {"balance": "${steps.build.result}",
                    "report": "${steps.report.result}",
                    "level": "${params.audit_level}"},
         "depends_on": ["report"], "timeout_s": 300},

        {"name": "final", "capability": "J", "action": "final_report",
         "params": {}, "depends_on": ["report", "audit"]},
    ],
}

STOCK_DAILY = {
    "name": "stock_daily",
    "description": "股票：宏观状态 + 微观辩证 → 入库 → 合并报告 → **审计**",
    "steps": [
        {"name": "macro", "capability": "macro", "action": "regime",
         "params": {"pmi": "${params.pmi}", "cpi_yoy": "${params.cpi_yoy}",
                    "policy_rate_delta": "${params.rate_delta}"}},

        {"name": "store_macro", "capability": "db", "action": "write",
         "params": {"collection": "stock_daily", "key": "${params.date}:macro",
                    "value": "${steps.macro.result}"},
         "depends_on": ["macro"]},

        {"name": "micro", "capability": "micro", "action": "dialectic",
         "params": {"revenue_growth": "${params.revenue_growth}",
                    "gross_margin_delta": "${params.margin_delta}",
                    "ocf_to_ni": "${params.ocf_to_ni}",
                    "accruals_ratio": "${params.accruals_ratio}"}},

        {"name": "store_micro", "capability": "db", "action": "write",
         "params": {"collection": "stock_daily", "key": "${params.date}:micro",
                    "value": "${steps.micro.result}"},
         "depends_on": ["micro"]},

        {"name": "merge", "capability": "report", "action": "merge",
         "params": {"date": "${params.date}"},
         "depends_on": ["store_macro", "store_micro"]},

        # ★ 审计：合并报告后审一遍（默认 rule，不联网）
        {"name": "audit", "capability": "auditor", "action": "report",
         "params": {"report": "${steps.merge.result}",
                    "level": "${params.audit_level}"},
         "depends_on": ["merge"], "timeout_s": 120},
    ],
}

DEMO = {
    "name": "demo",
    "description": "测试用最小流水线",
    "steps": [
        {"name": "produce", "capability": "demo", "action": "produce",
         "params": {"n": "${params.n}"}},
        {"name": "store", "capability": "db", "action": "write",
         "params": {"collection": "demo", "key": "${params.key}",
                    "value": "${steps.produce.result}"},
         "depends_on": ["produce"]},
        {"name": "consume", "capability": "demo", "action": "consume",
         "params": {"input": "${steps.store.result}"},
         "depends_on": ["store"]},
    ],
}

PIPELINES = {p["name"]: p for p in (BALANCE_SHEET, STOCK_DAILY, DEMO)}