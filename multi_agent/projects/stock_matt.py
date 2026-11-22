"""股票 MATT 项目：stocks_matt_report 的 M1..M5 接到框架上。

与期货项目的差别（正好说明"审计口径按项目分"）
--------------------------------------------
- 角色是 M1 信息 / M2 宏观 / M3 微观 / M4 报告 / M5 校验 + 常驻新闻采集官；
- 审计走 `stock` profile（三表勾稽、应计质量、毛利率背离、EPS 一致），
  与 `balance_sheet` 的供需勾稽完全不同；
- 新闻是**正则规则**（零 token），不是期货那套供应链硬事件；
- 报告是 A 股日报模板，不是平衡表 7 段式。

继承方式一样：只改 roles / pipeline / audit_project / news / reporter。
注意 M5（校验）与审计官是两件事：M5 是流水线里的一个步骤（可打回），
审计官是流水线末尾的独立判定（pass/suspect/fail），两者可以同时存在。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .base import Project, RoleSpec
from .news import NewsCollector, RuleNewsCollector
from .reporter import MattReporter, Reporter

Executor = Callable[[str, str, Dict[str, Any], Dict[str, Any]], Any]

MATT_PIPELINE: Dict[str, Any] = {
    "name": "stock_matt",
    "project": "stock-matt",
    "description": "A 股日报：信息 → 宏观 → 微观 → 报告 → 校验 → 审计",
    "steps": [
        {"name": "info", "capability": "M1", "action": "info_collect",
         "params": {"hours": "${params.hours}"}, "timeout_s": 600},

        {"name": "macro", "capability": "M2", "action": "macro_collect",
         "params": {"as_of": "${params.as_of}"}},

        {"name": "micro", "capability": "M3", "action": "micro_collect",
         "params": {"as_of": "${params.as_of}"}},

        {"name": "store_snapshot", "capability": "db", "action": "write",
         "params": {"collection": "stock_snapshot",
                    "key": "${params.as_of}:latest",
                    "value": {"info": "${steps.info.result}",
                              "macro": "${steps.macro.result}",
                              "micro": "${steps.micro.result}"}},
         "depends_on": ["info", "macro", "micro"]},

        {"name": "report", "capability": "M4", "action": "render_report",
         "params": {"as_of": "${params.as_of}"},
         "depends_on": ["store_snapshot"], "timeout_s": 300},

        {"name": "validate", "capability": "M5", "action": "validate",
         "params": {"strict": "${params.strict}"},
         "depends_on": ["report"]},

        {"name": "risk", "capability": "risk", "action": "brief",
         "params": {"data": "${params.market_data}", "asset_class": "equity"},
         "depends_on": ["validate"]},

        {"name": "risk_review", "capability": "auditor", "action": "review_brief",
         "params": {"brief": "${steps.risk.result}",
                    "modeler": "${params.proposal}",
                    "data": "${params.market_data}"},
         "depends_on": ["risk"]},

        {"name": "audit", "capability": "auditor", "action": "report",
         "params": {"project": "stock", "level": "${params.audit_level}"},
         "depends_on": ["validate", "risk_review"]},

        {"name": "final", "capability": "J", "action": "final_report",
         "params": {"as_of": "${params.as_of}"},
         "depends_on": ["audit"]},
    ],
}


class StockMattProject(Project):
    name = "stock-matt"
    title = "A 股日报（MATT）"
    description = ("M1 信息 / M2 宏观 / M3 微观 / M4 报告 / M5 校验 + 新闻采集官；"
                   "口径由 stock profile 审计")
    audit_project = "stock"
    pipeline = MATT_PIPELINE
    default_params = {"as_of": "", "hours": 24, "strict": False, "audit_level": "rule"}

    roles = [
        RoleSpec("M1", ["info_collect"], "消息面：抓取 → 正则过滤 → 事件抽取"),
        RoleSpec("M2", ["macro_collect"], "宏观：指数/利率/流动性/风格/全球"),
        RoleSpec("M3", ["micro_collect"], "微观：宽度/涨跌停/行业/个股异动/量价"),
        RoleSpec("M4", ["render_report"], "报告：模板填空 + 规则叙述（零 token）"),
        RoleSpec("M5", ["validate"], "校验：结构/数值/规则逻辑三层，可打回"),
        RoleSpec("news", ["watch"], "常驻新闻采集官：定时抓取，命中重大事件才通知"),
        RoleSpec("J", ["final_report"], "整轮终审：ok/degraded/blocked，纯离线"),
        RoleSpec("risk", ["brief"],
                "风控官：regime/指数信号/流动性/结构混沌/硬否决 → RISK_BRIEF"),
    ]

    def __init__(self, executor: Optional[Executor] = None,
                 fetch_news: Optional[Callable[[float], List[Dict[str, Any]]]] = None,
                 rules: Optional[Dict[str, List[str]]] = None,
                 reporter: Optional[Reporter] = None) -> None:
        self._executor = executor
        self._fetch_news = fetch_news
        self._rules = rules
        self._reporter = reporter or MattReporter()

    def _handler(self, capability: str, action: str):
        def run(params: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
            if self._executor is None:
                return {"degraded": True, "ok": False,
                        "capability": capability, "action": action,
                        "reason": (f"{self.name} 未注入 executor："
                                   f"请在有 stocks_matt_report 的机器上执行 "
                                   f"{capability}/{action}，或注册 StockMattProject(executor=...)"),
                        "params": params or {}}
            return self._executor(capability, action, params or {}, ctx or {})
        return run

    def handlers(self) -> Dict[str, Dict[str, Any]]:
        return {r.name: {a: self._handler(r.name, a) for a in r.actions}
                for r in self.role_specs()}

    def news(self) -> Optional[NewsCollector]:
        return RuleNewsCollector(rules=self._rules, fetch=self._fetch_news)

    def reporter(self) -> Optional[Reporter]:
        return self._reporter