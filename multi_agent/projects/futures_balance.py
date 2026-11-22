"""期货收支平衡表项目：BalanceAI 的 A/B/T/F/Q/C/D/E/J 接到框架上。

继承方式（这就是"照这个抄"的模板）
--------------------------------
    class MyFutures(FuturesBalanceProject):
        name = "my-futures"
        roles = FuturesBalanceProject.roles + [RoleSpec("X", ["extra"], "新增角色")]

- **角色表**：直接改 `roles`，框架把它变成 Agent manifest；
- **流水线**：改 `pipeline["steps"]`，框架把它塞进 `PIPELINES` 由 Coordinator 跑；
- **真正的实现**：注入 `executor(capability, action, params, ctx)`。
  本机执行就直接调对应函数；跨机执行就 `submit("runner", "py", {...})`，
  框架不关心你在哪跑，只关心回包形状。

没注入 executor 时不是报错，而是返回 `degraded=True` + 明确原因：
流水线能先跑通、审计能接上、报告能出骨架，实现可以后补。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .base import Project, RoleSpec
from .news import DataHubNewsCollector, NewsCollector
from .reporter import BalanceSheetReporter, Reporter

Executor = Callable[[str, str, Dict[str, Any], Dict[str, Any]], Any]

#: 平衡表流水线：取数 → 入库 → 新闻 → 修正 → 冲击 → 审校 → 报告 → 审计 → 终审
BALANCE_PIPELINE: Dict[str, Any] = {
    "name": "futures_balance",
    "project": "futures-balance",
    "description": "大宗收支平衡表：建表 → 入库 → 新闻 → 判定 → 测算 → 审校 → 报告 → 审计",
    "steps": [
        {"name": "build", "capability": "A", "action": "build_sheet",
         "params": {"commodity": "${params.commodity}",
                    "category": "${params.category}"}, "timeout_s": 900},

        {"name": "store_balance", "capability": "db", "action": "write",
         "params": {"collection": "balance",
                    "key": "${params.commodity}:latest",
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

        {"name": "shock", "capability": "C", "action": "shock",
         "params": {"commodity": "${params.commodity}"},
         "depends_on": ["vet_news"], "timeout_s": 600},

        {"name": "review", "capability": "D", "action": "review",
         "params": {"commodity": "${params.commodity}"},
         "depends_on": ["shock"]},

        {"name": "report", "capability": "E", "action": "report",
         "params": {"commodity": "${params.commodity}"},
         "depends_on": ["review"]},

        # ★ 风控官：出 RISK_BRIEF（硬计算，零 token）
        # 只依赖建表：风控不得因"当日无新闻事件"而沉默，也不受 DB/报告链故障牵连
        {"name": "risk", "capability": "risk", "action": "brief",
         "params": {"data": "${params.market_data}",
                    "asset_class": "futures"},
         "depends_on": ["build"]},

        # ★ 审计师复核 RISK_BRIEF，输出 AUDIT_VERDICT
        {"name": "risk_review", "capability": "auditor", "action": "review_brief",
         "params": {"brief": "${steps.risk.result}",
                    "modeler": "${params.proposal}",
                    "data": "${params.market_data}"},
         "depends_on": ["risk"]},

        {"name": "audit", "capability": "auditor", "action": "report",
         "params": {"project": "balance_sheet", "level": "${params.audit_level}",
                    "report": "${steps.report.result}",
                    "balance": "${steps.build.result}"},
         "depends_on": ["report", "risk_review"]},

        {"name": "final", "capability": "J", "action": "final_report",
         "params": {"commodity": "${params.commodity}"},
         "depends_on": ["audit"]},
    ],
}


class FuturesBalanceProject(Project):
    name = "futures-balance"
    title = "大宗商品收支平衡表（BalanceAI）"
    description = ("A 建表 / B 新闻 / T 期次 / F 库存 / Q 品种终审 / C 测算 / "
                   "D 审校 / E 报告 / J 整轮终审；口径由 balance_sheet profile 审计")
    audit_project = "balance_sheet"
    pipeline = BALANCE_PIPELINE
    default_params = {"commodity": "copper", "hours": 24, "audit_level": "rule"}

    roles = [
        RoleSpec("A", ["build_sheet", "fetch_official", "inventory"],
                 "取数建表：官方平衡表快照 → 固定字段 JSON，挂库存/CFTC"),
        RoleSpec("B", ["scan_news"], "新闻采集与初筛：DataHub 新闻池 → 事件 JSON"),
        RoleSpec("T", ["check_period"], "期次/新鲜度判定：数据日期与年度期次"),
        RoleSpec("F", ["check_inventory"], "库存偏移判定：confirm/diverge/neutral"),
        RoleSpec("Q", ["verdict"], "品种级终审：accept/correct/question/reject"),
        RoleSpec("C", ["shock"], "冲击测算：事件量化 → 三档情景 → 置信度"),
        RoleSpec("D", ["review"], "审校：口径一致性、量级、反证条件"),
        RoleSpec("E", ["report"], "报告：7 段式 Markdown，消费已落盘结果"),
        RoleSpec("J", ["final_report"], "整轮终审：ok/degraded/blocked，纯离线"),
        RoleSpec("risk", ["brief"],
                "风控官：regime/五大指数/流动性/结构混沌/硬否决 → RISK_BRIEF"),
    ]

    def __init__(self, executor: Optional[Executor] = None,
                 fetch_news: Optional[Callable[[float], List[Dict[str, Any]]]] = None,
                 reporter: Optional[Reporter] = None) -> None:
        self._executor = executor
        self._fetch_news = fetch_news
        self._reporter = reporter or BalanceSheetReporter()

    # ---------------- 实现 ----------------
    def _handler(self, capability: str, action: str):
        def run(params: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
            if self._executor is None:
                return {"degraded": True, "ok": False,
                        "capability": capability, "action": action,
                        "reason": (f"{self.name} 未注入 executor："
                                   f"请在有 BalanceAI 的机器上执行 {capability}/{action}，"
                                   f"或注册 FuturesBalanceProject(executor=...)"),
                        "params": params or {}}
            return self._executor(capability, action, params or {}, ctx or {})
        return run

    def handlers(self) -> Dict[str, Dict[str, Any]]:
        return {r.name: {a: self._handler(r.name, a) for a in r.actions}
                for r in self.role_specs()}

    def news(self) -> Optional[NewsCollector]:
        return DataHubNewsCollector(fetch=self._fetch_news)

    def reporter(self) -> Optional[Reporter]:
        return self._reporter