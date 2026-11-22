"""审计 Profile：把"审什么"从"怎么审"里拆出来，一个项目一个 profile。

设计动机
========
`audit.py` 里的 L1/L2/L3 引擎（规则框架、统计检验、分级短路、0-token 保证）
是**通用的**；但"供需平衡表要查什么"和"股票财报要查什么"完全不同：

  · 供需平衡表：精炼总 = 原生 + 再生、供需差 = 总产 - 用量、单位是万吨
  · 股票：三张表勾稽、经营现金流 vs 净利润（应计质量）、毛利率与营收增速背离
  · 债券：久期/凸性口径、收益率曲线单调性、信用利差与评级错配、票息日

把这些差异写在 `Auditor` 里会变成一坨 if/else。所以拆成 Profile：

    ┌─────────────── Auditor（引擎，通用）───────────────┐
    │ 分级短路 / Finding / Report / L2 统计检验 / L3 入口  │
    └───────────────────────┬──────────────────────────────┘
                            │ project="balance_sheet"
                            ▼
    ┌─────────────── AuditProfile（插件，按项目）─────────┐
    │ name / kinds / thresholds                          │
    │ .balance(data, rep)   数据体检                      │
    │ .report(md, data, rep) 报告 vs 数据交叉核对          │
    │ .series(vals, name, rep) 业务相关的序列检查          │
    │ .run(snapshot, rep)    流水线体检                   │
    └────────────────────────────────────────────────────┘

新增一个项目 = 写一个 Profile 子类 + `PROFILES["xxx"] = XxxProfile()`，
**不用改引擎、不用改 Coordinator、不用改 Hub 协议**。

内置三个
========
| project | 适用 | 典型检查 |
|---|---|---|
| `balance_sheet` | 大宗商品供需平衡表 | 父子项勾稽、供需差、单位量级、库存滞后、报告叙事矛盾 |
| `stock` | 股票 / 财务分析 | 三表勾稽、应计质量、毛利率背离、EPS 一致性、估值分位 |
| `bond` | 债券 / 固收 | 久期凸性口径、曲线单调、信用利差、票息与到期、修正久期合理性 |

用法
====
    from multi_agent.integrations.audit import Auditor

    Auditor(project="balance_sheet").audit_balance(bal)
    Auditor(project="stock").audit_financials(fin)
    Auditor(project="bond").audit_bond(bond)

    # 列出有哪些
    from multi_agent.integrations.audit_profiles import PROFILES, profile_names
    print(profile_names())
"""
from __future__ import annotations

import json
import math
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_SEV = {"info": 0, "warn": 1, "error": 2}


def _sev_max(*sevs: str) -> str:
    best = "info"
    for s in sevs:
        if _SEV.get(s, 0) > _SEV.get(best, 0):
            best = s
    return best


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _num(x: Any) -> Optional[float]:
    if _is_num(x):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x.replace(",", "").replace("%", "").strip())
        except ValueError:
            return None
    return None


def _walk_numbers(obj: Any, path: str = "") -> Iterable[Tuple[str, float]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_numbers(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_numbers(v, f"{path}[{i}]")
    elif _is_num(obj):
        yield path, float(obj)


def _pct_diff(a: float, b: float) -> float:
    """相对偏差（以 |b| 为分母，b=0 时退化为绝对差）。"""
    if abs(b) < 1e-12:
        return abs(a - b)
    return abs(a - b) / abs(b)


# ═══════════════════════════════════════════════════════════ 基类
class AuditProfile:
    """一个项目的审计口径。子类实现自己关心的检查，引擎负责分级与出报告。

    子类可覆盖：
      · name        —— profile 标识（`Auditor(project=name)`）
      · kinds       —— 支持的审计种类（balance/report/series/run 的子集）
      · thresholds  —— 默认阈值（可被 Auditor 实例覆盖）
    方法（都可选，不实现就是跳过）：
      · check_balance(data, rep)
      · check_report(md, data, rep)
      · check_series(values, name, rep)
      · check_run(snapshot, rep)
      · series_from(data)   —— 从数据里捞出值得做统计检验的序列
    """

    name: str = "generic"
    description: str = "通用审计口径（只做最基础的字段/一致性检查）"
    kinds: Tuple[str, ...] = ("balance", "report", "series", "run")
    thresholds: Dict[str, Any] = {}

    # ---- 可覆盖的检查 ----
    def check_balance(self, data: Dict[str, Any], rep) -> None:
        if not isinstance(data, dict) or not data:
            rep.add("data_empty", "error", "数据为空或不是对象", where="data")
            return
        # 通用底线：至少有非空内容 + 没有明显占位
        if len(data) < 2:
            rep.add("data_too_thin", "warn", f"只有 {len(data)} 个字段，可能不完整",
                    where="data")

    def check_report(self, report_md: str, data: Optional[Dict[str, Any]], rep) -> None:
        if not isinstance(report_md, str) or not report_md.strip():
            rep.add("report_empty", "error", "报告为空", where="report")
            return
        if not report_md.lstrip().startswith("#"):
            rep.add("report_no_title", "warn", "报告没有 Markdown 标题", where="report")
        for token in ("None", "nan", "NaN", "TODO", "TBD", "{{", "}}"):
            if token in report_md:
                rep.add("placeholder_in_report", "warn",
                        f"报告里出现占位符 `{token}`", {"token": token}, where="report")

    def check_series(self, values: Sequence[float], name: str, rep) -> None:
        pass    # 默认不加业务判断，L2 统计检验由引擎统一做

    def check_run(self, snapshot: Dict[str, Any], rep) -> None:
        pass

    def series_from(self, data: Dict[str, Any]) -> Dict[str, List[float]]:
        """从数据里捞出值得做统计检验的序列 {name: [float,...]}。默认空。"""
        return {}

    # ---- 工具 ----
    @staticmethod
    def close_enough(a: float, b: float, tol: float = 0.02) -> bool:
        return _pct_diff(a, b) <= tol


# ═══════════════════════════════════════════════════════════ 供需平衡表
_UNIT_FACTORS = {
    "吨": 1.0, "t": 1.0, "千吨": 1e3, "kt": 1e3, "ktons": 1e3,
    "万吨": 1e4, "万t": 1e4, "百万吨": 1e6, "mt": 1e6,
}
_BALANCE_MAGNITUDE = {
    "mine": (1e3, 1e9), "refined_primary": (1e3, 1e9),
    "refined_secondary": (0, 1e9), "refined_total": (1e3, 1e9),
    "refined_usage": (1e3, 1e9), "surplus_deficit": (-1e8, 1e8),
    "stocks_end": (0, 1e9), "stock_change": (-1e8, 1e8),
}
_DATE_PATTERNS = (r"(\d{4})-(\d{2})-(\d{2})", r"(\d{4})/(\d{2})/(\d{2})",
                  r"(\d{4})(\d{2})(\d{2})", r"(\d{4})-(\d{2})")


class BalanceSheetProfile(AuditProfile):
    """大宗商品供需平衡表：父子项勾稽 + 供需差 + 单位量级 + 库存滞后。"""

    name = "balance_sheet"
    description = "大宗商品供需平衡表（精炼总/供需差/库存/CFTC）"
    thresholds = {"stale_days_warn": 45, "stale_days_error": 120,
                  "inventory_stale_days": 120}

    def check_balance(self, data: Dict[str, Any], rep) -> None:
        th = rep.metrics.get("_thresholds") or self.thresholds
        stale_warn = float(th.get("stale_days_warn", 45))
        stale_err = float(th.get("stale_days_error", 120))

        if not isinstance(data, dict) or not data:
            rep.add("balance_empty", "error", "平衡表为空或不是对象", where="balance")
            return
        for field in ("commodity", "period", "unit", "balance", "supply", "demand"):
            if data.get(field) in (None, "", {}, []):
                rep.add("missing_field", "error", f"缺必填字段 `{field}`",
                        {"field": field}, where=field)

        unit = data.get("unit")
        factor = _UNIT_FACTORS.get(str(unit).strip()) if unit else None
        rep.metrics["unit"] = unit
        if unit and factor is None:
            rep.add("unit_unknown", "warn", f"单位 `{unit}` 不在已知列表，量级校验跳过",
                    {"unit": unit, "known": sorted(_UNIT_FACTORS)}, where="unit")

        if factor:
            for section in ("supply", "demand", "balance"):
                node = data.get(section)
                if not isinstance(node, dict):
                    continue
                for key, val in node.items():
                    v = _num(val)
                    if v is None:
                        rep.add("value_not_numeric", "warn",
                                f"`{section}.{key}` 不是数字（{val!r}），可能占位/缺失",
                                {"value": val}, where=f"{section}.{key}")
                        continue
                    tons = v * factor
                    rng = _BALANCE_MAGNITUDE.get(key)
                    if rng and not (rng[0] <= abs(tons) <= rng[1]):
                        rep.add("magnitude_suspect", "error",
                                f"`{section}.{key}`={v} {unit} → {tons:.3g} 吨，超出合理量级",
                                {"value": v, "unit": unit, "tons": tons,
                                 "expected_tons": list(rng)}, where=f"{section}.{key}")

        supply = data.get("supply") or {}
        rp, rs, rt = (_num(supply.get("refined_primary")),
                      _num(supply.get("refined_secondary")),
                      _num(supply.get("refined_total")))
        if None not in (rp, rs, rt):
            diff = abs((rp + rs) - rt)
            tol = max(1e-6, 0.02 * abs(rt))
            if diff > tol:
                rep.add("internal_mismatch", "error",
                        f"精炼总 {rt} ≠ 原生 {rp} + 再生 {rs}（差 {diff:.4g} > {tol:.4g}）",
                        {"refined_primary": rp, "refined_secondary": rs,
                         "refined_total": rt, "diff": diff, "tolerance": tol},
                        where="supply")

        demand = data.get("demand") or {}
        usage = _num(demand.get("refined_usage"))
        bal = data.get("balance") or {}
        sd = _num(bal.get("surplus_deficit"))
        if None not in (rt, usage, sd):
            implied = rt - usage
            if not self.close_enough(sd, implied, 0.02):
                rep.add("balance_inconsistent", "error",
                        f"供需差 {sd} 与「精炼总-用量」{implied:.4g} 不符",
                        {"refined_total": rt, "refined_usage": usage,
                         "surplus_deficit": sd, "implied": implied},
                        where="balance.surplus_deficit")

        sc = _num(bal.get("stock_change"))
        if None not in (sd, sc) and abs(sd) > 1e-9 and abs(sc) > 1e-9 and sd * sc < 0:
            rep.add("sign_conflict", "warn",
                    f"供需差 {sd} 与库存变动 {sc} 符号相反",
                    {"surplus_deficit": sd, "stock_change": sc}, where="balance")

        for path, val in _constant_columns(data):
            rep.add("constant_series", "warn",
                    f"`{path}` 全为常数 {val}，疑似占位或抓取失败",
                    {"path": path, "value": val}, where=path)

        fresh = _freshness(data)
        rep.metrics["freshness"] = fresh
        age = fresh.get("age_days")
        if age is not None:
            if age > stale_err:
                rep.add("data_stale", "error",
                        f"数据日期 {fresh.get('data_date')} 距今 {age:.0f} 天 > {stale_err:.0f}",
                        fresh, where="data_date")
            elif age > stale_warn:
                rep.add("data_aging", "warn",
                        f"数据日期 {fresh.get('data_date')} 距今 {age:.0f} 天 > {stale_warn:.0f}",
                        fresh, where="data_date")

        inv = data.get("inventory_check") or {}
        limit = float(th.get("inventory_stale_days", 120))
        for ex, node in (inv.get("exchanges") or {}).items():
            if not isinstance(node, dict):
                continue
            sd_days = _num(node.get("stale_days"))
            if sd_days is not None and sd_days > limit:
                rep.add("inventory_stale", "warn",
                        f"{ex} 库存滞后 {sd_days:.0f} 天（>{limit:.0f}），只能作绝对水平参考",
                        {"exchange": ex, "stale_days": sd_days},
                        where=f"inventory_check.{ex}")

    def check_report(self, report_md: str, data: Optional[Dict[str, Any]], rep) -> None:
        super().check_report(report_md, data, rep)
        if not isinstance(data, dict) or not data:
            rep.add("no_balance_for_report", "info",
                    "没有提供平衡表，跳过数值交叉核对", where="report")
            return
        if report_md.count("##") < 2:
            rep.add("report_few_sections", "warn",
                    "小标题少于 2 个，7 段式结构可能不完整", where="report")

        bals = [v for _, v in _walk_numbers(data)]
        unmatched: List[float] = []
        for m in re.finditer(r"-?\d+(?:\.\d+)?", report_md):
            val = float(m.group(0))
            if abs(val) < 100:
                continue
            if not any(abs(val - b) <= max(0.02 * abs(b), 1e-9) for b in bals):
                unmatched.append(val)
        rep.metrics["unmatched_numbers"] = unmatched[:20]
        if len(unmatched) >= 2:
            rep.add("report_numbers_unmatched", "warn",
                    f"报告里 {len(unmatched)} 个数字在平衡表中找不到对应值",
                    {"count": len(unmatched), "samples": unmatched[:10]}, where="report")

        sd = _num((data.get("balance") or {}).get("surplus_deficit"))
        if sd is not None:
            destock = any(k in report_md for k in ("去库", "库存下降", "库存减少", "de-stock"))
            restock = any(k in report_md for k in ("累库", "库存上升", "库存增加", "re-stock"))
            if sd > 0 and destock:
                rep.add("narrative_conflict", "error",
                        f"平衡表过剩 {sd}（应累库），报告写去库",
                        {"surplus_deficit": sd}, where="report")
            if sd < 0 and restock:
                rep.add("narrative_conflict", "error",
                        f"平衡表缺口 {sd}（应去库），报告写累库",
                        {"surplus_deficit": sd}, where="report")

        unit = data.get("unit")
        if unit:
            found = _units_in_text(report_md)
            others = [u for u in found if u != unit]
            if others:
                rep.add("unit_mismatch", "warn",
                        f"报告提到 `{others[0]}`，平衡表单位是 `{unit}`",
                        {"balance_unit": unit, "report_units": found}, where="report")

    def series_from(self, data: Dict[str, Any]) -> Dict[str, List[float]]:
        out: Dict[str, List[float]] = {}
        for key in ("stocks_end", "surplus_deficit", "stock_change"):
            s = _series_of(data, key)
            if s and len(s) >= 12:
                out[key] = s
        return out


# ═══════════════════════════════════════════════════════════ 股票 / 财务
class StockProfile(AuditProfile):
    """股票 / 财务：三表勾稽 + 应计质量 + 毛利率背离 + EPS 一致性。"""

    name = "stock"
    description = "股票财务审计（三表勾稽、应计质量、毛利率背离、EPS）"
    thresholds = {"accruals_warn": 0.10, "accruals_error": 0.25,
                  "gm_divergence_warn": 5.0, "eps_tolerance": 0.02}

    def check_balance(self, data: Dict[str, Any], rep) -> None:
        th = dict(self.thresholds)
        th.update(rep.metrics.get("_thresholds") or {})
        if not isinstance(data, dict) or not data:
            rep.add("financials_empty", "error", "财务数据为空", where="financials")
            return
        rep.metrics["period"] = data.get("period") or data.get("report_date")

        revenue = _num(data.get("revenue"))
        cost = _num(data.get("cost_of_revenue"))
        gross = _num(data.get("gross_profit"))
        # 1) 毛利勾稽：毛利 = 营收 - 成本
        if None not in (revenue, cost, gross):
            implied = revenue - cost
            if not self.close_enough(gross, implied, 0.01):
                rep.add("gross_profit_mismatch", "error",
                        f"毛利 {gross} ≠ 营收 {revenue} - 成本 {cost}（应为 {implied:.4g}）",
                        {"gross_profit": gross, "implied": implied},
                        where="gross_profit")

        # 2) 净利勾稽：净利 ≈ 营业利润 + 营业外 - 税
        op = _num(data.get("operating_profit"))
        ni = _num(data.get("net_income"))
        if None not in (op, ni):
            if not self.close_enough(ni, op, 0.35):
                rep.add("net_income_divergent", "warn",
                        f"净利 {ni} 与营业利润 {op} 偏离 >35%，检查营业外/税项",
                        {"net_income": ni, "operating_profit": op},
                        where="net_income")

        # 3) 应计质量：经营现金流 vs 净利润
        ocf = _num(data.get("operating_cash_flow"))
        if None not in (ocf, ni) and abs(ni) > 1e-9:
            accruals = (ni - ocf) / abs(ni)
            rep.metrics["accruals_ratio"] = round(accruals, 4)
            if abs(accruals) > float(th["accruals_error"]):
                rep.add("accruals_poor", "error",
                        f"应计比例 {accruals:.2%}（净利 {ni} vs 经营现金流 {ocf}），"
                        f"利润含金量差", {"net_income": ni, "ocf": ocf,
                                       "accruals_ratio": accruals}, where="accruals")
            elif abs(accruals) > float(th["accruals_warn"]):
                rep.add("accruals_weak", "warn",
                        f"应计比例 {accruals:.2%}，需关注回款质量",
                        {"accruals_ratio": accruals}, where="accruals")

        # 4) 资产负债勾稽：资产 = 负债 + 权益
        assets = _num(data.get("total_assets"))
        liab = _num(data.get("total_liabilities"))
        equity = _num(data.get("total_equity"))
        if None not in (assets, liab, equity):
            if not self.close_enough(assets, liab + equity, 0.01):
                rep.add("balance_sheet_mismatch", "error",
                        f"资产 {assets} ≠ 负债 {liab} + 权益 {equity}"
                        f"（差 {assets - liab - equity:.4g}）",
                        {"total_assets": assets, "liabilities": liab,
                         "equity": equity}, where="balance_sheet")

        # 5) 营收与毛利率背离：营收大增但毛利率大跌 → 可能是低价换量
        rev_g = _num(data.get("revenue_growth"))       # 小数或百分数
        gm = _num(data.get("gross_margin"))
        gm_prev = _num(data.get("gross_margin_prev"))
        if None not in (rev_g, gm, gm_prev):
            rg = rev_g * 100 if abs(rev_g) <= 1 else rev_g
            delta = (gm - gm_prev) * 100 if abs(gm) <= 1 else gm - gm_prev
            rep.metrics["gm_delta_pp"] = round(delta, 2)
            if rg > 20 and delta < -float(th["gm_divergence_warn"]):
                rep.add("growth_margin_divergence", "warn",
                        f"营收 +{rg:.1f}% 但毛利率降 {abs(delta):.1f}pp，低价换量嫌疑",
                        {"revenue_growth_pct": rg, "gm_delta_pp": delta},
                        where="gross_margin")

        # 6) EPS 一致性：EPS ≈ 净利 / 股本
        eps = _num(data.get("eps"))
        shares = _num(data.get("shares_outstanding"))
        if None not in (eps, ni, shares) and abs(shares) > 1e-9:
            implied = ni / shares
            if not self.close_enough(eps, implied, float(th["eps_tolerance"])):
                rep.add("eps_mismatch", "warn",
                        f"EPS {eps} 与「净利/股本」{implied:.4g} 不符",
                        {"eps": eps, "implied": implied, "shares": shares},
                        where="eps")

        # 7) 常量 / 缺失
        for path, val in _constant_columns(data):
            rep.add("constant_series", "warn",
                    f"`{path}` 全为常数 {val}", {"path": path, "value": val}, where=path)

    def check_report(self, report_md: str, data: Optional[Dict[str, Any]], rep) -> None:
        super().check_report(report_md, data, rep)
        if not isinstance(data, dict) or not data:
            return
        # 报告说"业绩超预期/增长"但营收是负增长 → 矛盾
        rg = _num(data.get("revenue_growth"))
        if rg is not None:
            rg_pct = rg * 100 if abs(rg) <= 1 else rg
            claims_growth = any(k in report_md for k in ("营收增长", "收入增长", "业绩增长"))
            claims_decline = any(k in report_md for k in ("营收下滑", "收入下降", "业绩下滑"))
            if rg_pct < -5 and claims_growth and not claims_decline:
                rep.add("narrative_conflict", "error",
                        f"营收实际 {rg_pct:+.1f}%，报告却说增长",
                        {"revenue_growth_pct": rg_pct}, where="report")
            if rg_pct > 5 and claims_decline and not claims_growth:
                rep.add("narrative_conflict", "error",
                        f"营收实际 {rg_pct:+.1f}%，报告却说下滑",
                        {"revenue_growth_pct": rg_pct}, where="report")

        ni = _num(data.get("net_income"))
        ocf = _num(data.get("operating_cash_flow"))
        if None not in (ni, ocf) and ni > 0 and ocf < 0:
            if any(k in report_md for k in ("现金流健康", "现金充裕", "造血能力强")):
                rep.add("narrative_conflict", "error",
                        "经营现金流为负，报告却说现金流健康",
                        {"net_income": ni, "ocf": ocf}, where="report")

    def series_from(self, data: Dict[str, Any]) -> Dict[str, List[float]]:
        out: Dict[str, List[float]] = {}
        for key in ("revenue", "net_income", "operating_cash_flow", "gross_margin"):
            s = _series_of(data, key)
            if s and len(s) >= 12:
                out[key] = s
        return out


# ═══════════════════════════════════════════════════════════ 债券 / 固收
class BondProfile(AuditProfile):
    """债券：久期凸性口径 + 收益率曲线单调 + 信用利差 + 票息/到期。"""

    name = "bond"
    description = "债券审计（久期凸性、曲线单调、信用利差、票息到期）"
    thresholds = {"curve_inversion_bp": -1.0, "spread_outlier_bp": 800,
                  "duration_mismatch": 0.05}

    def check_balance(self, data: Dict[str, Any], rep) -> None:
        th = dict(self.thresholds)
        th.update(rep.metrics.get("_thresholds") or {})
        if not isinstance(data, dict) or not data:
            rep.add("bond_empty", "error", "债券数据为空", where="bond")
            return

        for field in ("bond_code", "coupon_rate", "maturity_date"):
            if data.get(field) in (None, "", {}):
                rep.add("missing_field", "warn", f"缺字段 `{field}`",
                        {"field": field}, where=field)

        # 1) 修正久期 vs 麦考利久期：MD = MacD / (1 + y/f)
        macd = _num(data.get("macaulay_duration"))
        mdd = _num(data.get("modified_duration"))
        ytm = _num(data.get("ytm"))
        freq = _num(data.get("coupon_frequency")) or 1.0
        if None not in (macd, mdd, ytm):
            y = ytm / 100 if abs(ytm) > 1 else ytm
            implied = macd / (1 + y / max(freq, 1e-9))
            rep.metrics["duration_check"] = {"implied_modified": round(implied, 4),
                                             "given": mdd}
            if not self.close_enough(mdd, implied, float(th["duration_mismatch"])):
                rep.add("duration_mismatch", "warn",
                        f"修正久期 {mdd} ≠ 麦考利 {macd}/(1+y/f) = {implied:.4f}",
                        {"macaulay": macd, "modified": mdd, "implied": implied,
                         "ytm": ytm, "freq": freq}, where="modified_duration")

        # 2) 凸性必须为正（负凸性只在含权债/极特殊结构下出现）
        conv = _num(data.get("convexity"))
        if conv is not None and conv < -1e-9:
            rep.add("negative_convexity", "warn",
                    f"凸性为负（{conv}），只有含权债/结构化产品才合理",
                    {"convexity": conv}, where="convexity")

        # 3) 票息 vs 收益率：票息远高于 YTM 且无折价信息 → 口径可疑
        coupon = _num(data.get("coupon_rate"))
        if None not in (coupon, ytm):
            c = coupon / 100 if abs(coupon) > 1 else coupon
            y = ytm / 100 if abs(ytm) > 1 else ytm
            rep.metrics["coupon_minus_ytm"] = round((c - y) * 10000, 1)
            price = _num(data.get("price"))
            if price is not None and c - y > 0.02 and price < 95:
                rep.add("coupon_ytm_conflict", "warn",
                        f"票息 {c:.2%} 远高于 YTM {y:.2%}，但价格 {price} 低于面值 —— 口径可疑",
                        {"coupon": c, "ytm": y, "price": price}, where="coupon_rate")

        # 4) 信用利差越界
        spread = _num(data.get("credit_spread_bp"))
        if spread is not None and abs(spread) > float(th["spread_outlier_bp"]):
            rep.add("spread_outlier", "warn",
                    f"信用利差 {spread}bp 超常见区间，确认评级口径",
                    {"spread_bp": spread}, where="credit_spread")

        # 5) 到期日已过
        from datetime import date
        mat = data.get("maturity_date")
        if isinstance(mat, str):
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})", mat) or re.search(r"(\d{4})(\d{2})(\d{2})", mat)
            if m:
                try:
                    md = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                    if md < date.today():
                        rep.add("already_matured", "error",
                                f"到期日 {mat} 已过，不该出现在存续清单里",
                                {"maturity_date": mat}, where="maturity_date")
                except ValueError:
                    rep.add("bad_date", "warn", f"到期日格式无法解析：{mat}",
                            {"maturity_date": mat}, where="maturity_date")

        # 6) 曲线单调性
        curve = data.get("curve") or {}
        if isinstance(curve, dict):
            points = sorted(((k, _num(v)) for k, v in curve.items()),
                            key=lambda kv: self._tenor_years(kv[0]))
            points = [(k, v) for k, v in points if v is not None]
            for (k1, v1), (k2, v2) in zip(points, points[1:]):
                if v2 < v1 - abs(float(th["curve_inversion_bp"])) / 10000:
                    rep.add("curve_inversion", "warn",
                            f"收益率曲线 {k1}→{k2} 倒挂（{v1}→{v2}）",
                            {"from": k1, "to": k2, "v1": v1, "v2": v2}, where="curve")

        for path, val in _constant_columns(data):
            rep.add("constant_series", "warn", f"`{path}` 全为常数 {val}",
                    {"path": path, "value": val}, where=path)

    @staticmethod
    def _tenor_years(key: str) -> float:
        k = str(key).strip().upper().replace("Y", "").replace("M", "")
        try:
            return float(k)
        except ValueError:
            return 0.0

    def check_report(self, report_md: str, data: Optional[Dict[str, Any]], rep) -> None:
        super().check_report(report_md, data, rep)
        if not isinstance(data, dict) or not data:
            return
        ytm = _num(data.get("ytm"))
        if ytm is not None:
            y = ytm if abs(ytm) > 1 else ytm * 100
            if y < 0 and "负利率" not in report_md and "负收益率" not in report_md:
                rep.add("narrative_conflict", "warn",
                        f"YTM 为负（{y:.3f}%），报告未提及",
                        {"ytm_pct": y}, where="report")
        # 报告说"曲线陡峭化"但数据是平坦化
        curve = data.get("curve") or {}
        if isinstance(curve, dict) and len(curve) >= 2:
            pts = sorted(((self._tenor_years(k), _num(v)) for k, v in curve.items()),
                         key=lambda kv: kv[0])
            pts = [(k, v) for k, v in pts if v is not None]
            if len(pts) >= 2:
                slope = pts[-1][1] - pts[0][1]
                says_steepen = any(k in report_md for k in ("陡峭", "变陡", "steepening"))
                says_flatten = any(k in report_md for k in ("平坦", "变平", "flattening"))
                if slope > 0.001 and says_flatten and not says_steepen:
                    rep.add("narrative_conflict", "warn",
                            f"长端-短端 {slope:+.4f} 走陡，报告说平坦化",
                            {"slope": slope}, where="report")
                if slope < -0.001 and says_steepen and not says_flatten:
                    rep.add("narrative_conflict", "warn",
                            f"长端-短端 {slope:+.4f} 走平/倒挂，报告说陡峭化",
                            {"slope": slope}, where="report")

    def series_from(self, data: Dict[str, Any]) -> Dict[str, List[float]]:
        out: Dict[str, List[float]] = {}
        for key in ("ytm", "credit_spread_bp", "modified_duration"):
            s = _series_of(data, key)
            if s and len(s) >= 12:
                out[key] = s
        return out


# ═══════════════════════════════════════════════════════════ 注册表
PROFILES: Dict[str, AuditProfile] = {
    p.name: p for p in (BalanceSheetProfile(), StockProfile(), BondProfile())
}


def profile_names() -> List[str]:
    return sorted(PROFILES)


def get_profile(name: str) -> AuditProfile:
    """取 profile；未知名字抛 KeyError（明确报错，不静默降级成 generic）。"""
    if name not in PROFILES:
        raise KeyError(f"未知审计 profile：{name}；可选 {profile_names()}")
    return PROFILES[name]


def register_profile(profile: AuditProfile) -> None:
    """注册自定义 profile（新项目扩审计时用这条，不用改引擎）。"""
    if not isinstance(profile, AuditProfile):
        raise TypeError("profile 必须是 AuditProfile 子类实例")
    PROFILES[profile.name] = profile


# ═══════════════════════════════════════════════════════════ 共用小工具
def _constant_columns(obj: Any, path: str = "",
                      out: Optional[List[Tuple[str, Any]]] = None) -> List[Tuple[str, Any]]:
    out = [] if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            _constant_columns(v, f"{path}.{k}" if path else str(k), out)
    elif isinstance(obj, list):
        nums = [x for x in obj if _is_num(x)]
        if len(nums) >= 4 and len(set(nums)) == 1:
            out.append((path, nums[0]))
        for i, v in enumerate(obj):
            _constant_columns(v, f"{path}[{i}]", out)
    return out


def _freshness(data: Dict[str, Any]) -> Dict[str, Any]:
    best: Optional[Dict[str, Any]] = None
    today = time.localtime()
    for key in ("data_date", "as_of", "fetched_at", "period"):
        raw = data.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        for pat in _DATE_PATTERNS:
            m = re.search(pat, raw)
            if not m:
                continue
            parts = [int(g) for g in m.groups()]
            year, month = parts[0], parts[1] if len(parts) > 1 else 1
            day = parts[2] if len(parts) > 2 else 15
            try:
                t = time.mktime((year, month, day, 0, 0, 0, 0, 0, -1))
            except (ValueError, OverflowError):
                continue
            age = (time.mktime(today[:3] + (0, 0, 0, 0, 0, -1)) - t) / 86400.0
            item = {"source": key, "data_date": raw, "age_days": round(age, 1),
                    "approx": len(parts) < 3}
            if best is None or item["age_days"] < best["age_days"]:
                best = item
            break
    return best or {"source": None, "data_date": None, "age_days": None}


def _units_in_text(text: str) -> List[str]:
    units = sorted(_UNIT_FACTORS, key=len, reverse=True)
    hit: List[str] = []
    used: List[Tuple[int, int]] = []
    for u in units:
        start = 0
        while True:
            i = text.find(u, start)
            if i < 0:
                break
            span = (i, i + len(u))
            if not any(s < span[1] and span[0] < e for s, e in used):
                used.append(span)
                if u not in hit:
                    hit.append(u)
            start = i + len(u)
    return hit


def _series_of(obj: Dict[str, Any], key: str) -> List[float]:
    if not isinstance(obj, dict):
        return []
    for holder in ("history", "series", "_history", "history_series"):
        node = obj.get(holder)
        if isinstance(node, dict):
            v = node.get(key)
            if isinstance(v, list):
                return [x for x in (_num(y) for y in v) if x is not None]
        if isinstance(node, list):
            out = []
            for item in node:
                if isinstance(item, dict) and key in item:
                    x = _num(item.get(key))
                    if x is not None:
                        out.append(x)
            if out:
                return out
    node = obj.get(key)
    if isinstance(node, list):
        return [x for x in (_num(y) for y in node) if x is not None]
    return []
