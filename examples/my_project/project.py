"""示例项目：照这个文件抄，就能把自己的业务接进多 Agent 框架。

它演示四件事（够用来"继承"整个框架了）：

1. **角色表**：`RoleSpec(名字, [动作...], 说明, instances=N)` —— 一个角色 = 一个 capability，
   Hub 按 `capability + action` 路由；`instances` 是"这个角色要起几个 Agent"。
2. **流水线**：`pipeline = {...}` 是一张 DAG，步骤用 `capability/action/params`，
   结果通过 `${steps.x.result.y}` 注入下游。
3. **谁实现什么**：
   · 采集官 / 风控官 / 报告官 → **项目自己写**（`handlers()` 里给纯函数，零外部依赖）；
   · 审核官（auditor）→ **框架 `integrations/audit.py` 提供**，项目只在流水线里调用；
   · 管理员/监理（supervisor）→ **框架 `integrations/supervisor.py` 提供**（送审打包 + 台账）；
   · 评议器（debate）/ 初筛（moderator）→ **框架 `integrations/debate.py` 提供**，本项目未启用。
4. **注册方式**：`super().register(runtime)` 挂业务动作，再 `register_supervisor(...)` 挂内置角色。

跑起来看效果（不用任何外部服务）：

    python examples/my_project/run_demo.py
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from multi_agent.projects import REGISTRY, Project, RoleSpec, install_pipelines
from multi_agent.projects.news import StaticNewsCollector
from multi_agent.projects.reporter import MarkdownReporter, Reporter

# ───────────────────────────────────────────── 1. 流水线（DAG）
MONITOR_PIPELINE: Dict[str, Any] = {
    "name": "commodity_monitor",
    "project": "commodity-monitor",
    "description": "采集 → 风控 → 送审打包 → 审核 → 报告（示例项目）",
    "steps": [
        # 采集官：新闻/事件 → events（契约见 projects/news.py：events 恒在）
        {"name": "collect", "capability": "collector", "action": "scan",
         "params": {"commodities": "${params.commodities}"}},

        # 风控官：事件 + 市场数据 → 风险简报（纯规则，零 token）
        {"name": "risk", "capability": "risk", "action": "brief",
         "params": {"commodities": "${params.commodities}",
                    "events": "${steps.collect.result.events}"},
         "depends_on": ["collect"]},

        # 管理员/监理：把风控简报压成送审件（框架实现，项目不写代码）
        {"name": "pack", "capability": "supervisor", "action": "pack",
         "params": {"payload": "${steps.risk.result}"},
         "depends_on": ["risk"]},

        # 审核官：审 RISK_BRIEF（框架实现，review_brief 吃 str 或 dict）
        {"name": "review", "capability": "auditor", "action": "review_brief",
         "params": {"brief": "${steps.pack.result.payload}"},
         "depends_on": ["pack"]},

        # 报告官：把三份结果拼成 Markdown
        {"name": "report", "capability": "report", "action": "build",
         "params": {"commodities": "${params.commodities}",
                    "events": "${steps.collect.result.events}",
                    "risk": "${steps.risk.result}",
                    "review": "${steps.review.result}"},
         "depends_on": ["review"]},
    ],
}


# ───────────────────────────────────────────── 2. 报告器（可选覆写）
class MonitorReporter(MarkdownReporter):
    """只实现 `sections()`：标题/时间/空段落由基类处理。"""

    name = "monitor-md"
    project = "commodity-monitor"
    title = "商品异动监控日报"

    def sections(self, context: Dict[str, Any]) -> List[Any]:
        events = context.get("events") or []
        risk = context.get("risk") or {}
        review = context.get("review") or {}
        return [
            ("结论", [f"- 风险等级：{risk.get('aggregate_signal', 'unknown')}",
                      f"- 事件数：{len(events)}",
                      f"- 审核结论：{review.get('verdict') or review.get('decision') or '—'}"],
             ),
            ("事件", [f"- {e.get('title')}（{e.get('source') or '-'}）" for e in events]),
            ("风控要点", [f"- {n}" for n in (risk.get("notes") or [])]),
        ]


# ───────────────────────────────────────────── 3. 项目（继承 Project）
class CommodityMonitorProject(Project):
    """一个"多角色 + 内置审核/监理"的最小真实项目。"""

    name = "commodity-monitor"
    title = "商品异动监控（示例）"
    description = "采集官 → 风控官 → 送审 → 审核官 → 报告官；审核与送审用框架内置实现"
    audit_project: Optional[str] = None      # 只用 review_brief，不挂 balance_sheet 口径
    pipeline = MONITOR_PIPELINE
    default_params = {"commodities": ["copper"]}

    roles = [
        RoleSpec("collector", ["scan"],
                 "采集官：新闻/公告 → 事件 JSON（events 恒在，取不到就 degraded）"),
        RoleSpec("risk", ["brief"],
                 "风控官：事件 + 市场数据 → 风险简报（硬规则计算，零 token）"),
        RoleSpec("report", ["build"],
                 "报告官：把风控/审核结论拼成 Markdown（只消费上游，不重算）"),
        # 下面两个是框架内置角色：项目只在角色表里声明，实现由 integrations 提供
        RoleSpec("supervisor", ["pack", "handoff", "ledger"],
                 "管理员/监理：把报告与采集稿压成送审件交给审核官，记送审台账"),
        RoleSpec("debate", ["run"],
                 "评议器（可选）：采集官 ⇄ 审核官多轮评议，轮数/字数见 config/limits.json"),
    ]

    def __init__(self, reporter: Optional[Reporter] = None,
                 news_items: Optional[List[Dict[str, Any]]] = None) -> None:
        self._reporter = reporter or MonitorReporter()
        self._news_items = list(news_items or DEMO_NEWS)

    # ---------- 采集官：接项目自己的新闻源 ----------
    def news(self):
        """返回 NewsCollector；没有数据源时返回 NullNewsCollector() 即可（不许编造）。"""
        return StaticNewsCollector(self._news_items)

    # ---------- 业务角色实现 ----------
    def handlers(self) -> Dict[str, Dict[str, Any]]:
        # supervisor / debate / moderator 由框架注册，这里必须排除，
        # 否则 `project agent` 起出来的是空壳（动作永远 degraded）。
        collector = self.news()
        return {
            "collector": {"scan": lambda p, c: collector.collect(p or {})},
            "risk": {"brief": self._brief},
            "report": {"build": self._build},
        }

    def _brief(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        """风控官：纯规则的"风险简报"。

        真实项目会去读行情/持仓/波动率；示例只用事件数演示输出契约：
        返回一个 dict，下游用 `${steps.risk.result.xxx}` 取字段。
        """
        events = params.get("events") or []
        commodities = params.get("commodities") or []
        high = [e for e in events if (e.get("impact") or "").lower() == "high"]
        signal = "danger" if len(high) >= 2 else ("warning" if high or events else "normal")
        return {
            "asset_class": "futures",
            "commodities": commodities,
            "aggregate_signal": signal,
            "event_count": len(events),
            "high_impact": [e.get("title") for e in high],
            "notes": [f"覆盖品种：{', '.join(commodities) or '—'}",
                      f"高影响事件：{len(high)} 条",
                      "口径：示例规则（2 条以上高影响 → danger）"],
        }

    def _build(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        """报告官：只消费上游结果，缺什么就留空标注。"""
        md = self.reporter().build({
            "title": self.title,
            "subject": ", ".join(params.get("commodities") or []),
            "events": params.get("events") or [],
            "risk": params.get("risk") or {},
            "review": params.get("review") or {},
        })
        return {"markdown": md, "chars": len(md)}

    # ---------- 注册：业务动作 + 框架内置角色 ----------
    def register(self, runtime: Any) -> None:
        super().register(runtime)
        client = getattr(runtime, "client", None)
        from multi_agent.integrations.supervisor import register as register_supervisor

        register_supervisor(runtime, client=client)   # pack / ledger 零 token

    def reporter(self) -> Optional[Reporter]:
        return self._reporter


DEMO_NEWS: List[Dict[str, Any]] = [
    {"title": "某铜矿因罢工停产", "impact": "high", "source": "demo",
     "published": "2026-11-20"},
    {"title": "港口铜库存连续三周下降", "impact": "high", "source": "demo",
     "published": "2026-11-21"},
    {"title": "美元指数走强，压制有色", "impact": "medium", "source": "demo",
     "published": "2026-11-22"},
]


def install(registry: Any = None) -> CommodityMonitorProject:
    """把项目和它的流水线登记进框架（复制到自己项目里，改成你的名字即可）。"""
    reg = registry or REGISTRY
    project = CommodityMonitorProject()
    reg.register(project, replace=True)
    install_pipelines(reg)
    return project