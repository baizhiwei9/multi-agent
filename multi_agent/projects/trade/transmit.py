"""A 股传导角色：事件压力 → 产业链环节 → 标的（规则 + 配置，零 token）。

为什么单开一个角色
----------------
交易域已有"采集 → 评估 → 平衡表"，那是商品口径（供给 / 需求 / 库存）。
A 股要的是另一件事：**同一个信用事件，沿产业链传到哪些环节、哪些标的、压力多大**。
这一步既不是评估（不判方向），也不是平衡表（不算供需），所以单开 `transmit/map`。

模型（可复算，不是预测）
----------------------
    severity      0-100，事件本身的信用压力（例：按隐含违约概率换算）
    stress(环节)  severity × financing_dependence（该环节多依赖外部融资）
    score(标的)   stress × exposure（该标的在该环节的敞口）
    档位          ≥50 高 / ≥35 中高 / ≥20 中 / <20 低
    营收影响(标的) -capex_cut × capex_sensitivity(环节) × ai_revenue_share(标的)

前者答"压力有多大"，后者答"若资本开支下修 X%，营收大约少几个点"，
两套口径分开列，不合成一个无法解释的分数。

四个边界
--------
1. **配置驱动**：环节与标的写 `config/transmit/ai_ashare.json`，改映射不改代码；
2. **承认假设**：`financing_dependence` / `exposure` / `capex_sensitivity` /
   `ai_revenue_share` 是示例权重，输出带 `assumption=True` 与免责说明；
3. **不冒充涨跌幅**：`score` 是传导压力分，输出 `unit="pressure"` 写清口径；
4. **营收影响只到收入**：不含毛利、费用、估值变化，也不等于股价涨跌。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from ..base import RoleProject

_CHAIN_REL = os.path.join("config", "transmit", "ai_ashare.json")
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

#: 压力分档位（不是涨跌幅）
_BANDS = ((50, "高"), (35, "中高"), (20, "中"), (0, "低"))

#: 情景默认：AI 资本开支下修 10%（可被 capex_cut 参数覆盖）
_DEFAULT_CAPEX_CUT = 0.10


def _num(value: Any, default: float = 0.0) -> float:
    """取 0-1 的比例值；缺失或非法一律落 default，不猜。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, num))


def load_chain(path: Optional[str] = None) -> Dict[str, Any]:
    """读传导映射；读不到就返回空骨架，不编造环节。"""
    target = path or os.path.join(_REPO_ROOT, _CHAIN_REL)
    try:
        with open(target, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"name": "", "chains": [], "assumption": True,
                "reason": f"读不到传导配置：{target}"}
    if not isinstance(data, dict):
        return {"name": "", "chains": [], "assumption": True, "reason": "配置顶层必须是对象"}
    data.setdefault("chains", [])
    return data


def band(score: float) -> str:
    for threshold, name in _BANDS:
        if float(score) >= threshold:
            return name
    return "低"


def transmit(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """`transmit/map`：事件压力 → 环节 → 标的压力分。

    入参：
        severity    0-100，事件信用压力（必填；0 表示没有压力，直接返回空）
        capex_cut   情景：AI 资本开支下修幅度，默认 0.10
        chain       传导映射（可选，默认读 `config/transmit/ai_ashare.json`）
        chain_path  指定另一个配置文件
    出参：`{ok, severity, items, top, top_revenue, by_segment, scenario,
           assumption, disclaimer, unit, source}`
    """
    params = params or {}
    chain = params.get("chain") if isinstance(params.get("chain"), dict) else None
    if chain is None:
        chain = load_chain(params.get("chain_path"))
    try:
        severity = float(params.get("severity") or 0)
    except (TypeError, ValueError):
        severity = 0.0
    severity = max(0.0, min(100.0, severity))
    capex_cut = _num(params.get("capex_cut"), _DEFAULT_CAPEX_CUT)

    segments = [s for s in (chain.get("chains") or []) if isinstance(s, dict)]
    if not segments:
        return {"ok": False, "degraded": True, "severity": severity,
                "items": [], "top": [], "by_segment": [],
                "reason": chain.get("reason") or "传导配置里没有环节",
                "scenario": {"capex_cut": capex_cut, "unit": "revenue_pct"},
                "unit": "pressure", "assumption": True}

    items: List[Dict[str, Any]] = []
    by_segment: List[Dict[str, Any]] = []
    for seg in segments:
        dep = _num(seg.get("financing_dependence"))
        sens = _num(seg.get("capex_sensitivity"))
        stress = round(severity * dep, 1)
        picked = 0
        seg_rev: List[float] = []
        for st in (seg.get("ashare") or []):
            if not isinstance(st, dict):
                continue
            exp = _num(st.get("exposure"))
            share = _num(st.get("ai_revenue_share"))
            score = round(stress * exp, 1)
            rev = round(-capex_cut * sens * share * 100, 2)
            seg_rev.append(rev)
            items.append({
                "segment": str(seg.get("id") or ""),
                "segment_name": str(seg.get("name") or ""),
                "financing_dependence": dep,
                "ticker": str(st.get("ticker") or ""),
                "name": str(st.get("name") or ""),
                "exposure": exp,
                "ai_revenue_share": share,
                "score": score,
                "band": band(score),
                "revenue_impact_pct": rev,
                "why": str(seg.get("rationale") or ""),
            })
            picked += 1
        by_segment.append({"segment": str(seg.get("id") or ""),
                           "name": str(seg.get("name") or ""),
                           "financing_dependence": dep,
                           "capex_sensitivity": sens,
                           "stress": stress,
                           "band": band(stress),
                           "revenue_impact_range": [min(seg_rev), max(seg_rev)]
                           if seg_rev else [0.0, 0.0],
                           "tickers": picked})

    items.sort(key=lambda x: (-float(x["score"]), x["ticker"]))
    by_segment.sort(key=lambda x: -float(x["stress"]))
    by_revenue = sorted(items, key=lambda x: float(x["revenue_impact_pct"]))

    return {
        "ok": bool(items), "degraded": not items,
        "severity": severity,
        "items": items,
        "top": items[:5],
        "top_revenue": by_revenue[:5],
        "by_segment": by_segment,
        "counts": {"segments": len(segments), "tickers": len(items)},
        "scenario": {"capex_cut": capex_cut,
                     "unit": "revenue_pct",
                     "note": "营收影响 = -capex_cut × capex_sensitivity × "
                             "ai_revenue_share，只到收入，不含毛利与估值"},
        "assumption": bool(chain.get("assumption", True)),
        "disclaimer": chain.get("disclaimer") or "示例权重，不代表实测敞口",
        "unit": "pressure",
        "scale": "0-100，50 以上为高压；不是涨跌幅、不是目标价",
        "source": chain.get("name") or "",
    }


class TransmitProject(RoleProject):
    name = "transmit"
    title = "A 股传导"
    description = "信用/供给事件 → 产业链环节 → A 股标的压力分（示例映射可配置）"
    capability = "transmit"
    actions = ("map",)
    role_description = "传导：只折压力分，不判方向、不预测涨跌"

    def __init__(self, chain: Optional[Dict[str, Any]] = None) -> None:
        super().__init__()
        self._chain = chain

    def handlers(self) -> Dict[str, Dict[str, Any]]:
        def _map(params: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
            p = dict(params or {})
            if self._chain is not None and not p.get("chain"):
                p["chain"] = self._chain
            return transmit(p, ctx)

        return {"transmit": {"map": _map}}