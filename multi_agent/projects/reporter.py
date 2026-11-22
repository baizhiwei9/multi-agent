"""报告器接口：不同项目的报告长什么样，由项目自己定；流水线只认"给我一份 Markdown"。

契约
----
    build(context) -> str        # 纯函数式：context 里的数据 → Markdown
    save(md, path) -> str        # 落盘，返回路径

三条硬规矩
----------
1. **不重算**：报告器只消费上游结果（平衡表、事件、审计结论），缺数据就留空并标注，
   绝不自己补数、不自己估；
2. **可复算**：报告里出现的每个数字都应能在 context 里找到（审计官会核对）；
3. **零 token**：默认纯模板拼接；要用模型润色是调用方的事（显式注入），不在基类里。

继承方式：`MarkdownReporter` 已经处理了标题/时间/空段落，子类只需要实现 `sections()`。
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple


class Reporter:
    """报告器基类。"""

    name: str = "reporter"
    description: str = ""
    project: str = ""

    def build(self, context: Dict[str, Any]) -> str:
        raise NotImplementedError

    def save(self, md: str, path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(md)
        return path

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "project": self.project,
                "description": self.description}


class MarkdownReporter(Reporter):
    """Markdown 报告骨架：子类只实现 `sections()`。

    `sections(context)` 返回 `[(标题, [行...]), ...]`；空行列表会自动写成"（无数据）"，
    避免看起来像漏了一整段。
    """

    title: str = "报告"
    order: Sequence[str] = ()

    def sections(self, context: Dict[str, Any]) -> List[Tuple[str, List[str]]]:
        raise NotImplementedError

    def build(self, context: Dict[str, Any]) -> str:
        ctx = context or {}
        title = ctx.get("title") or self.title
        as_of = ctx.get("as_of") or datetime.now().strftime("%Y-%m-%d %H:%M")
        lines: List[str] = [f"# {title}", "", f"- 生成时间：{as_of}"]
        for key in ("period", "unit", "subject"):
            if ctx.get(key):
                lines.append(f"- {key}：{ctx[key]}")
        lines.append("")

        if not ctx.get("data_ok", True):
            lines.append(f"> 数据缺口：{ctx.get('gap') or '上游未提供完整数据'}")
            lines.append("")

        for name, body in self.sections(ctx):
            lines.append(f"## {name}")
            lines.append("")
            lines.extend(body or ["（无数据）"])
            lines.append("")

        audit = ctx.get("audit")
        if audit:
            lines.append("## 审计结论")
            lines.append("")
            lines.append(f"- 判定：**{audit.get('verdict', 'n/a')}**")
            for f in (audit.get("findings") or [])[:10]:
                lines.append(f"- [{f.get('severity', 'info')}] {f.get('code')}："
                             f"{f.get('message', '')}")
            if not audit.get("findings"):
                lines.append("- 未发现结构性问题")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


# ───────────────────────────────────────────── 期货：平衡表报告
class BalanceSheetReporter(MarkdownReporter):
    """期货收支平衡表报告：供需 → 库存 → 事件 → 结论。"""

    name = "balance-sheet-md"
    description = "平衡表 7 段式（供需/库存/事件/影响/结论/反证/数据口径）"
    project = "futures-balance"
    title = "大宗商品收支平衡表"

    def sections(self, context: Dict[str, Any]) -> List[Tuple[str, List[str]]]:
        bal = context.get("balance") or {}
        supply = bal.get("supply") or {}
        demand = bal.get("demand") or {}
        diff = bal.get("balance") or {}
        unit = context.get("unit") or bal.get("unit") or ""

        return [
            ("供需平衡", [
                f"- 供给：{_fmt(supply.get('refined_total'))} {unit}",
                f"- 需求：{_fmt(demand.get('refined_usage'))} {unit}",
                f"- 缺口：{_fmt(diff.get('surplus_deficit'))} {unit}",
                f"- 库存变化：{_fmt(diff.get('stock_change'))} {unit}",
            ]),
            ("库存与结构", [
                f"- 期末库存：{_fmt(diff.get('stocks_end'))} {unit}",
                f"- 库存校验：{(context.get('inventory') or {}).get('verdict', '未校验')}",
            ]),
            ("关键事件", [
                f"- {e.get('title')}（{e.get('published') or '时间未知'}）"
                for e in (context.get("events") or [])[:10]
            ]),
            ("影响测算", [
                f"- 事件：{s.get('event_id')}，影响 {_fmt(s.get('impact'))} {unit}，"
                f"置信度 {_fmt(s.get('confidence'))}"
                for s in (context.get("shocks") or [])[:10]
            ]),
            ("数据口径", [
                f"- 数据日期：{bal.get('data_date') or '未知'}",
                f"- 期次：{bal.get('period') or '未知'}",
                f"- 新闻源：{context.get('news_source') or '未采集'}",
            ]),
        ]


# ───────────────────────────────────────────── 股票：MATT 日报
class MattReporter(MarkdownReporter):
    """股票日报：市场 → 宏观 → 微观 → 消息 → 校验。"""

    name = "matt-stock-md"
    description = "A 股日报（市场/宏观/微观/消息/风险）"
    project = "stock-matt"
    title = "A 股日报"

    def sections(self, context: Dict[str, Any]) -> List[Tuple[str, List[str]]]:
        market = context.get("market") or {}
        macro = context.get("macro") or {}
        micro = context.get("micro") or {}
        return [
            ("市场", [
                f"- 指数：{k} {_pct(v)}" for k, v in (market.get("indices") or {}).items()
            ] or ["- 指数数据缺失"]),
            ("宏观", [
                f"- 利率：{macro.get('rates') or '未知'}",
                f"- 流动性：{macro.get('liquidity') or '未知'}",
                f"- 风格：{macro.get('style') or '未知'}",
            ]),
            ("微观结构", [
                f"- 宽度：{micro.get('breadth') or '未知'}",
                f"- 涨跌停：{micro.get('limit_stats') or '未知'}",
                f"- 行业：{micro.get('sectors') or '未知'}",
            ]),
            ("消息面", [
                f"- {e.get('title')}" for e in (context.get("events") or [])[:10]
            ]),
        ]


def _fmt(value: Any, nd: int = 1) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):,.{nd}f}"
    except (TypeError, ValueError):
        return str(value)


def _pct(value: Any) -> str:
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return str(value)