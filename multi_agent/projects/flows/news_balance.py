"""新闻 → 评估 → 平衡表：跨项目编排的最小样子。

三个角色分别在三个项目里：

    collector/scan   → projects/roles/news_collector.py
    assess/classify  → projects/roles/news_assessor.py
    balance/build    → projects/roles/balance_sheet.py

编排只写名字。离线 demo 里 `balance` 会拿到 degraded（没注入 BalanceAI executor），
这正好演示"少一个角色时不假装跑通"：`check_capabilities()` 报缺 `balance`，
或者它在线但没接后端，结果里写明 reason。
"""
from __future__ import annotations

from typing import Any, Dict

NEWS_BALANCE_FLOW: Dict[str, Any] = {
    "name": "news_to_balance",
    "description": "新闻采集 → 事件入库 → 评估（是否大事/方向）→ 平衡表 → 入库",
    "audit_project": "balance_sheet",
    "steps": [
        {"name": "collect", "capability": "collector", "action": "scan",
         "params": {"hours": "${params.hours}",
                    "commodities": "${params.commodities}"}, "timeout_s": 300},

        {"name": "store_news", "capability": "db", "action": "append",
         "params": {"stream": "news", "payload": "${steps.collect.result}"},
         "depends_on": ["collect"]},

        # 经理调参：权重 / 大事阈值 / 最低级别（零 token），再交给评估
        {"name": "tune", "capability": "supervisor", "action": "tune",
         "params": {"weights": "${params.news_weights}",
                    "major_weight": "${params.major_weight}",
                    "min_level": "${params.min_level}"},
         "depends_on": ["collect"]},

        {"name": "assess", "capability": "assess", "action": "classify",
         "params": {"events": "${steps.collect.result}",
                    "tuning": "${steps.tune.result}"},
         "depends_on": ["tune"], "timeout_s": 120},

        {"name": "store_assess", "capability": "db", "action": "write",
         "params": {"collection": "news_assessments",
                     "key": "${params.date}:${params.commodity}",
                     "value": "${steps.assess.result}"},
         "depends_on": ["assess"]},

        # 只有"算得上大事"才往下走；不够大就停在评估，不硬算平衡表
        {"name": "balance", "capability": "balance", "action": "build",
         "params": {"commodity": "${params.commodity}",
                    "category": "${params.category}"},
         "depends_on": ["store_assess"],
         "when": {"step": "assess", "path": "result.major", "op": "eq", "value": True},
         "timeout_s": 900},

        {"name": "store_balance", "capability": "db", "action": "write",
         "params": {"collection": "balance",
                     "key": "${params.commodity}:latest",
                     "value": "${steps.balance.result}"},
         "depends_on": ["balance"]},
    ],
}