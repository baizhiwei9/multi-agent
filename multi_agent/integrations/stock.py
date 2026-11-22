"""股票场景示例：一个 Agent 绑「宏观」+「微观辩证」两个角色。

这里的算法是**可替换的模板**，重点是角色/动作的切分与协议投递：
- 宏观角色回答"现在是什么状态"（regime / rates）；
- 微观角色回答"这家公司/这条逻辑有没有硬伤"（dialectic）。

真实生产里把 `macro_regime` / `micro_dialectic` 换成你的模型或数据管线即可，
Hub 不需要改。
"""

from __future__ import annotations

from typing import Any, Dict, List


def register(runtime) -> None:
    runtime.add_handler("macro", "regime", macro_regime)
    runtime.add_handler("macro", "rates", macro_rates)
    runtime.add_handler("micro", "dialectic", micro_dialectic)


def macro_regime(params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    pmi = _num(params.get("pmi"), 50.0)
    cpi = _num(params.get("cpi_yoy"), 2.0)
    cut = _num(params.get("policy_rate_delta"), 0.0)
    credit = _num(params.get("credit_growth"), 0.0)

    score = 0.0
    reasons: List[str] = []
    if pmi >= 50:
        score += 1; reasons.append(f"PMI {pmi} 在荣枯线上")
    else:
        score -= 1; reasons.append(f"PMI {pmi} 低于荣枯线")
    if cpi > 3:
        score -= 0.5; reasons.append(f"CPI {cpi}% 偏高，掣肘宽松")
    if cut < 0:
        score += 0.5; reasons.append(f"政策利率下调 {abs(cut)}bp")
    if credit > 0:
        score += 0.5; reasons.append("信用扩张为正")

    regime = "risk_on" if score >= 1 else ("risk_off" if score <= -1 else "neutral")
    return {"regime": regime, "score": round(score, 2), "reasons": reasons,
            "as_of": params.get("as_of")}


def macro_rates(params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    d10 = _num(params.get("d10"), 2.0)
    d2 = _num(params.get("d2"), 2.0)
    slope = round((d10 - d2) * 100, 1)
    return {"slope_bp": slope, "inverted": slope < 0,
            "hint": "倒挂时银行息差承压，风险资产估值分母不稳" if slope < 0 else "曲线正常"}


def micro_dialectic(params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    revenue = _num(params.get("revenue_growth"), 0.0)
    margin_delta = _num(params.get("gross_margin_delta"), 0.0)
    ocf_ni = _num(params.get("ocf_to_ni"), 1.0)
    accruals = _num(params.get("accruals_ratio"), 0.0)
    related = _num(params.get("related_party_ratio"), 0.0)

    flags: List[str] = []
    if revenue > 0.3 and margin_delta < 0:
        flags.append("增收不增利：增长靠降价/让利换量")
    if ocf_ni < 0.8:
        flags.append(f"经营现金流/净利润 {ocf_ni:.2f} < 0.8，利润含金量不足")
    if accruals > 0.1:
        flags.append(f"应计项占比 {accruals:.0%} 偏高")
    if related > 0.2:
        flags.append(f"关联交易占比 {related:.0%} 偏高")

    verdict = "reject" if len(flags) >= 2 else ("suspect" if flags else "pass")
    return {"verdict": verdict, "flags": flags,
            "rule": ">=2 条硬伤判 reject；1 条判 suspect；否则 pass"}


def _num(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default