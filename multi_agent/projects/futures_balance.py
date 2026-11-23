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

import os
from typing import Any, Callable, Dict, List, Optional

from .base import Project, RoleSpec
from .news import DataHubNewsCollector, NewsCollector, NullNewsCollector
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
         "params": {"hours": "${params.hours}",
                    "commodity": "${params.commodity}"}, "timeout_s": 300,
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

        # 报告只依赖"数据已入库 + 新闻已入库"；审校被跳过（当日无新闻）时照样出报表，
        # 否则空事件日会连 report → audit → final 一起被连坐跳过，run 却标 succeeded。
        {"name": "report", "capability": "E", "action": "report",
         "params": {"commodity": "${params.commodity}"},
         "depends_on": ["store_balance", "store_news", "review"],
         "run_on_skipped": ["review"]},

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


DEBATE_PIPELINE: Dict[str, Any] = {
    "name": "futures_balance_debate",
    "description": ("多轮评议版：双路新闻 → 协调官初筛 → 采集官⇄审计官（默认 ≤10 轮）"
                    " → 报告 + 风控 → 审计 → 终审；无重大新闻时评议直接 SKIPPED"),
    "steps": [
        {"name": "build", "capability": "A", "action": "build_sheet",
         "params": {"commodity": "${params.commodity}",
                    "category": "${params.category}"}, "timeout_s": 900},

        {"name": "store_balance", "capability": "db", "action": "write",
         "params": {"collection": "balance",
                    "key": "${params.commodity}:latest",
                    "value": "${steps.build.result.results.${params.commodity}.balance}"},
         "depends_on": ["build"]},

        # 两路采集：平衡表口径 + 突发事件口径（矿难/罢工/禁令…）
        {"name": "news_balance", "capability": "B", "action": "scan_news",
         "params": {"hours": "${params.hours}", "commodity": "${params.commodity}"},
         "timeout_s": 300, "depends_on": ["build"]},
        {"name": "news_event", "capability": "B", "action": "scan_events",
         "params": {"hours": "${params.hours}", "commodity": "${params.commodity}"},
         "timeout_s": 300, "depends_on": ["build"]},

        # 协调官初筛：有没有大事、初轮观点（零 token）
        {"name": "moderator_screen", "capability": "moderator", "action": "screen",
         "params": {"balance_news": "${steps.news_balance.result}",
                    "event_news": "${steps.news_event.result}",
                    "major_keywords": "${params.major_keywords}",
                    "max_topics": "${params.max_topics}"},
         "depends_on": ["news_balance", "news_event"]},

        # 管理员/监理：把初筛结论压成送审件（超 long 截断、引用超限点名）
        {"name": "supervisor_pack", "capability": "supervisor", "action": "pack",
         "params": {"payload": "${steps.moderator_screen.result}",
                    "project": "balance_sheet", "kind": "screen_brief"},
         "depends_on": ["moderator_screen"]},

        # 采集官 ⇄ 审计官，多轮互相点评（轮数/字数上限在 config/limits.json）
        {"name": "debate", "capability": "debate", "action": "run",
         "params": {"topic": "${steps.moderator_screen.result}",
                    "no_news": "${steps.moderator_screen.result.no_news}",
                    "collector": {"capability": "B", "action": "scan_news"},
                    "auditor": {"capability": "auditor", "action": "review_brief"},
                    "max_rounds": "${params.max_rounds}"},
         "depends_on": ["supervisor_pack"], "timeout_s": 1800},

        # 送审台账：每轮谁送了什么、多长、回执是什么（报告官/风控官可引用）
        {"name": "supervisor_ledger", "capability": "supervisor", "action": "ledger",
         "params": {"entries": "${steps.debate.result}",
                    "pack": "${steps.supervisor_pack.result}"},
         "depends_on": ["debate"]},

        {"name": "report", "capability": "E", "action": "report",
         "params": {"commodity": "${params.commodity}"},
         "depends_on": ["supervisor_ledger"]},

        {"name": "risk", "capability": "risk", "action": "brief",
         "params": {"data": "${params.market_data}", "asset_class": "futures"},
         "depends_on": ["build"]},
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
         "params": {"commodity": "${params.commodity}"}, "depends_on": ["audit"]},
    ],
}


class FuturesBalanceProject(Project):
    name = "futures-balance"
    title = "大宗商品收支平衡表（BalanceAI）"
    description = ("A 建表 / B 新闻 / T 期次 / F 库存 / Q 品种终审 / C 测算 / "
                   "D 审校 / E 报告 / J 整轮终审；口径由 balance_sheet profile 审计")
    audit_project = "balance_sheet"
    pipeline = BALANCE_PIPELINE
    extra_pipelines = (DEBATE_PIPELINE,)

    def _commodity_debate(self) -> Dict[str, Any]:
        """商品多空辩论流水线（延迟导入：PIPELINES 在 integrations 侧，避免模块环）。"""
        from ..integrations.adversary import PIPELINES
        return PIPELINES["commodity_debate"]
    default_params = {"commodity": "copper", "hours": 24, "audit_level": "rule"}

    roles = [
        RoleSpec("A", ["build_sheet", "fetch_official", "inventory"],
                 "取数建表：官方平衡表快照 → 固定字段 JSON，挂库存/CFTC"),
        RoleSpec("B", ["scan_news", "scan_events"],
                 "新闻采集与初筛：DataHub 新闻池 → 事件 JSON（平衡表口径 / 突发事件口径）",
                 instances=2),
        RoleSpec("T", ["check_period"], "期次/新鲜度判定：数据日期与年度期次"),
        RoleSpec("F", ["check_inventory"], "库存偏移判定：confirm/diverge/neutral"),
        RoleSpec("Q", ["verdict"], "品种级终审：accept/correct/question/reject"),
        RoleSpec("C", ["shock"], "冲击测算：事件量化 → 三档情景 → 置信度"),
        RoleSpec("D", ["review"], "审校：口径一致性、量级、反证条件"),
        RoleSpec("E", ["report"], "报告：7 段式 Markdown，消费已落盘结果"),
        RoleSpec("J", ["final_report"], "整轮终审：ok/degraded/blocked，纯离线"),
        RoleSpec("risk", ["brief"],
                "风控官：regime/五大指数/流动性/结构混沌/硬否决 → RISK_BRIEF"),
        RoleSpec("moderator", ["screen"],
                 "协调官：合并多路新闻、判有无大事、出初筛观点（零 token）"),
        RoleSpec("debate", ["run"],
                 "多轮评议执行器：采集官⇄审计官，轮数/字数按 config/limits.json"),
        RoleSpec("adversary", ["argue", "decide"],
                 "多空辩论参与方：规则对手 argue（零 token）+ 裁决官 decide"
                 "（LLM 优先、规则兜底），不绑定具体 Agent"),
        RoleSpec("supervisor", ["pack", "handoff", "ledger"],
                 "管理员/监理：把报告与采集稿压成送审件交给审计官，记送审台账"),
    ]

    def __init__(self, executor: Optional[Executor] = None,
                 fetch_news: Optional[Callable[[float], List[Dict[str, Any]]]] = None,
                 reporter: Optional[Reporter] = None,
                 enable_datahub: Optional[bool] = None) -> None:
        self._executor = executor
        self._fetch_news = fetch_news
        self._reporter = reporter or BalanceSheetReporter()
        # 多空辩论流水线：debate(adversarial) → 送审台账
        self.extra_pipelines = (DEBATE_PIPELINE, self._commodity_debate())
        # DataHub 新闻池是可选外部数据源：**默认关闭**，显式开启才去读。
        # 私有化部署因此不用改代码就能做到"不连任何外部服务"。
        # 开启：构造参数 enable_datahub=True，或环境变量 MULTI_AGENT_DATAHUB=1。
        if enable_datahub is None:
            enable_datahub = os.environ.get("MULTI_AGENT_DATAHUB", "").strip().lower() \
                in ("1", "true", "yes", "on")
        self._enable_datahub = bool(enable_datahub)

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
        """业务动作由 BalanceAI executor 提供。

        `supervisor` / `moderator` / `debate` 是框架内置角色，分别由
        `integrations.supervisor` 与 `integrations.debate` 注册；不能落到
        executor 桩上，否则 `project agent` 起出来的是空壳（动作永远 degraded）。
        """
        builtin = {"supervisor", "moderator", "debate", "adversary"}
        out = {r.name: {a: self._handler(r.name, a) for a in r.actions}
               for r in self.role_specs() if r.name not in builtin}
        # 新闻采集：没注入 executor 时用项目自己的采集官。
        # 以前这种情况下 B 落的是"只有 degraded、没有 events"的桩，
        # 下游 `when: result.events > 0` 静默为假 → 整条新闻链被跳过，run 还标 succeeded。
        news = self.news()
        if self._executor is None and news is not None:
            def _collect(params: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
                return news.collect(params or {})

            out.setdefault("B", {})
            out["B"]["scan_news"] = _collect
            out["B"]["scan_events"] = _collect
        return out

    def register(self, runtime: Any) -> None:
        """先挂业务动作，再把框架内置角色（送审 / 初筛 / 多轮评议）挂上。"""
        super().register(runtime)
        client = getattr(runtime, "client", None)
        from ..integrations.adversary import register as register_adversary
        from ..integrations.debate import register as register_debate
        from ..integrations.supervisor import register as register_supervisor

        register_supervisor(runtime, client=client)
        register_debate(runtime, client=client)
        register_adversary(runtime)

    def news(self) -> Optional[NewsCollector]:
        if not self._enable_datahub:
            return NullNewsCollector()
        return DataHubNewsCollector(fetch=self._fetch_news)

    def reporter(self) -> Optional[Reporter]:
        return self._reporter