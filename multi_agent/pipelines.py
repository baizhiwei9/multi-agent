"""内置流水线：把"角色动作"编排成 DAG。

步骤字段
--------
- name / capability / action / params
- depends_on: 依赖的步骤名（默认空）
- when: 条件分支，如 {"step": "news", "path": "result.events", "op": "gt", "value": 0}
- timeout_s / max_retries

参数模板
--------
`${params.x}` 取运行参数；`${steps.build.result.foo}` 取上游结果；
整个字符串就是一个模板时，返回原始类型（数字/对象），不是字符串。
"""

from __future__ import annotations

BALANCE_SHEET = {
    "name": "balance_sheet",
    "description": "大宗收支平衡表：抓取 → 入库 → 新闻 → 修正判定 → 估算 → 报告",
    "steps": [
        {"name": "build", "capability": "A", "action": "build_sheet",
         "params": {"category": "${params.category}"}, "timeout_s": 900},

        {"name": "store_balance", "capability": "db", "action": "write",
         "params": {"collection": "balance", "key": "${params.commodity}:latest",
                    "value": "${steps.build.result}"}},

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

        {"name": "final", "capability": "J", "action": "final_report",
         "params": {}, "depends_on": ["report"]},
    ],
}

STOCK_DAILY = {
    "name": "stock_daily",
    "description": "股票：宏观状态 + 微观辩证 → 入库 → 合并报告",
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