"""审计 Profile 测试：同引擎、不同口径（平衡表 / 股票 / 债券）。

要验证的核心是"**框架真的留出来了**"：
1. 同一套 Auditor 引擎，换 `project=` 就换整套检查口径；
2. 新项目可以只写一个 Profile 子类注册进来，引擎/Coordinator 一行不用改；
3. 各 profile 的检查互不串味（股票检查不会在债券数据上乱报）。
"""
import pytest

from multi_agent.integrations.audit import Auditor, run_action
from multi_agent.integrations.audit_profiles import (
    PROFILES, AuditProfile, BalanceSheetProfile, BondProfile, StockProfile,
    get_profile, profile_names, register_profile,
)


# ───────────────────────────────────────────── 夹具
def _balance():
    return {
        "commodity": "copper", "period": "2025A", "unit": "万吨",
        "supply": {"mine": 2318.0, "refined_primary": 2322.9,
                   "refined_secondary": 534.8, "refined_total": 2857.8},
        "demand": {"refined_usage": 2815.9},
        "balance": {"surplus_deficit": 41.9, "stock_change": 41.0, "stocks_end": 180.9},
        "data_date": "2026-11-21", "as_of": "2026-11-22",
    }


def _stock():
    return {
        "period": "2025Q3",
        "revenue": 1000.0, "cost_of_revenue": 600.0, "gross_profit": 400.0,
        "operating_profit": 200.0, "net_income": 160.0,
        "operating_cash_flow": 155.0,
        "total_assets": 5000.0, "total_liabilities": 2000.0, "total_equity": 3000.0,
        "eps": 1.60, "shares_outstanding": 100.0,
        "revenue_growth": 0.12, "gross_margin": 0.40, "gross_margin_prev": 0.39,
    }


def _bond():
    return {
        "bond_code": "240215", "coupon_rate": 2.55, "ytm": 2.60,
        "maturity_date": "2034-06-15", "coupon_frequency": 2,
        "macaulay_duration": 7.85, "modified_duration": 7.65,
        "convexity": 72.5, "credit_spread_bp": 35,
        "curve": {"1Y": 1.85, "2Y": 2.05, "5Y": 2.35, "10Y": 2.60},
    }


# ───────────────────────────────────────────── 注册表
def test_three_builtin_profiles():
    assert set(profile_names()) >= {"balance_sheet", "stock", "bond"}


def test_get_profile_unknown_raises():
    with pytest.raises(KeyError) as e:
        get_profile("does_not_exist")
    assert "可选" in str(e.value)


def test_profiles_have_descriptions():
    for name in profile_names():
        p = get_profile(name)
        assert p.description and isinstance(p.thresholds, dict)


def test_register_custom_profile(monkeypatch):
    class MyProfile(AuditProfile):
        name = "my_project"
        description = "自定义"
        thresholds = {"limit": 10}

        def check_balance(self, data, rep):
            if (data or {}).get("v", 0) > self.thresholds["limit"]:
                rep.add("too_big", "error", "v 超限", {"v": data["v"]}, where="v")

    register_profile(MyProfile())
    try:
        assert "my_project" in profile_names()
        rep = Auditor(project="my_project", level="rule").audit_balance({"v": 100})
        assert rep.verdict == "fail"
        assert any(f.code == "too_big" for f in rep.findings)
    finally:
        PROFILES.pop("my_project", None)


def test_register_rejects_non_profile():
    with pytest.raises(TypeError):
        register_profile({"name": "x"})


# ───────────────────────────────────────────── 平衡表 profile
def test_balance_profile_clean_passes():
    rep = Auditor(project="balance_sheet", level="rule").audit_balance(_balance())
    assert rep.verdict == "pass", rep.as_dict()
    assert rep.project == "balance_sheet"


def test_balance_profile_catches_mp_mismatch():
    bal = _balance()
    bal["supply"]["refined_total"] = 3000.0
    rep = Auditor(project="balance_sheet", level="rule").audit_balance(bal)
    assert rep.verdict == "fail"
    assert any(f.code == "internal_mismatch" for f in rep.findings)


def test_balance_report_narrative_conflict():
    md = "# 铜\n\n## 结论\n本期明显去库。\n## 库存\n库存下降"
    rep = Auditor(project="balance_sheet", level="rule").audit_report(md, _balance())
    assert any(f.code == "narrative_conflict" for f in rep.findings)


# ───────────────────────────────────────────── 股票 profile
def test_stock_profile_clean_passes():
    rep = Auditor(project="stock", level="rule").audit_balance(_stock())
    assert rep.verdict == "pass", rep.as_dict()


def test_stock_catches_gross_profit_mismatch():
    fin = _stock()
    fin["gross_profit"] = 500.0        # 应为 400
    rep = Auditor(project="stock", level="rule").audit_balance(fin)
    assert rep.verdict == "fail"
    assert any(f.code == "gross_profit_mismatch" for f in rep.findings)


def test_stock_catches_balance_sheet_mismatch():
    fin = _stock()
    fin["total_equity"] = 3500.0       # 资产5000 ≠ 负债2000+权益3500
    rep = Auditor(project="stock", level="rule").audit_balance(fin)
    assert any(f.code == "balance_sheet_mismatch" for f in rep.findings)


def test_stock_catches_poor_accruals():
    fin = _stock()
    fin["net_income"] = 160.0
    fin["operating_cash_flow"] = 60.0   # 应计比例 62.5%
    rep = Auditor(project="stock", level="rule").audit_balance(fin)
    assert any(f.code == "accruals_poor" for f in rep.findings)
    assert rep.metrics["accruals_ratio"] > 0.5


def test_stock_accruals_warn_level():
    fin = _stock()
    fin["net_income"] = 160.0
    fin["operating_cash_flow"] = 135.0   # 15.6% → warn 不 error
    rep = Auditor(project="stock", level="rule").audit_balance(fin)
    sev = {f.code: f.severity for f in rep.findings}
    assert sev.get("accruals_weak") == "warn"


def test_stock_catches_growth_margin_divergence():
    fin = _stock()
    fin["revenue_growth"] = 0.30       # +30%
    fin["gross_margin"] = 0.30
    fin["gross_margin_prev"] = 0.42    # -12pp
    rep = Auditor(project="stock", level="rule").audit_balance(fin)
    assert any(f.code == "growth_margin_divergence" for f in rep.findings)


def test_stock_catches_eps_mismatch():
    fin = _stock()
    fin["eps"] = 5.0                   # 应为 1.6
    rep = Auditor(project="stock", level="rule").audit_balance(fin)
    assert any(f.code == "eps_mismatch" for f in rep.findings)


def test_stock_report_conflict_on_cashflow_claim():
    fin = _stock()
    fin["operating_cash_flow"] = -20.0
    md = "# 公司\n\n## 现金流\n公司现金流健康，造血能力强。\n## 结论\n稳定"
    rep = Auditor(project="stock", level="rule").audit_report(md, fin)
    assert any(f.code == "narrative_conflict" for f in rep.findings)


def test_stock_audit_financials_alias():
    rep = Auditor(project="stock", level="rule").audit_financials(_stock())
    assert rep.kind == "balance" and rep.project == "stock"


# ───────────────────────────────────────────── 债券 profile
def test_bond_profile_clean_passes():
    rep = Auditor(project="bond", level="rule").audit_balance(_bond())
    assert rep.verdict == "pass", rep.as_dict()


def test_bond_catches_duration_mismatch():
    b = _bond()
    b["modified_duration"] = 5.0        # 应为 7.85/(1+0.026/2)=7.75
    rep = Auditor(project="bond", level="rule").audit_balance(b)
    assert any(f.code == "duration_mismatch" for f in rep.findings)


def test_bond_catches_negative_convexity():
    b = _bond()
    b["convexity"] = -3.0
    rep = Auditor(project="bond", level="rule").audit_balance(b)
    assert any(f.code == "negative_convexity" for f in rep.findings)


def test_bond_catches_curve_inversion():
    b = _bond()
    b["curve"] = {"1Y": 2.50, "2Y": 2.10, "5Y": 2.35, "10Y": 2.60}
    rep = Auditor(project="bond", level="rule").audit_balance(b)
    assert any(f.code == "curve_inversion" for f in rep.findings)


def test_bond_catches_matured():
    b = _bond()
    b["maturity_date"] = "2020-01-01"
    rep = Auditor(project="bond", level="rule").audit_balance(b)
    assert any(f.code == "already_matured" for f in rep.findings)


def test_bond_catches_spread_outlier():
    b = _bond()
    b["credit_spread_bp"] = 1500
    rep = Auditor(project="bond", level="rule").audit_balance(b)
    assert any(f.code == "spread_outlier" for f in rep.findings)


def test_bond_report_curve_narrative_conflict():
    b = _bond()
    b["curve"] = {"1Y": 2.60, "10Y": 2.20}     # 走平/倒挂
    md = "# 债市\n\n## 曲线\n曲线明显陡峭化。\n## 结论\n看多"
    rep = Auditor(project="bond", level="rule").audit_report(md, b)
    assert any(f.code == "narrative_conflict" for f in rep.findings)


# ───────────────────────────────────────────── 口径隔离
def test_stock_profile_does_not_run_balance_checks():
    """股票 profile 不该在股票数据上报"精炼总"这种平衡表才有的问题。"""
    rep = Auditor(project="stock", level="rule").audit_balance(_stock())
    codes = {f.code for f in rep.findings}
    assert "internal_mismatch" not in codes
    assert "balance_inconsistent" not in codes


def test_bond_profile_does_not_run_stock_checks():
    rep = Auditor(project="bond", level="rule").audit_balance(_bond())
    codes = {f.code for f in rep.findings}
    assert "gross_profit_mismatch" not in codes
    assert "accruals_poor" not in codes


def test_balance_profile_does_not_run_bond_checks():
    rep = Auditor(project="balance_sheet", level="rule").audit_balance(_balance())
    codes = {f.code for f in rep.findings}
    assert "duration_mismatch" not in codes


# ───────────────────────────────────────────── 引擎与 project 解耦
def test_same_levels_work_for_every_profile():
    for project in ("balance_sheet", "stock", "bond"):
        rep = Auditor(project=project, level="rule").audit_balance({})
        assert rep.verdict == "fail"           # 空数据每个 profile 都该拒
        assert rep.layers["L1_rule"]["ran"] is True


def test_0token_guarantee_holds_for_all_profiles():
    for project in ("balance_sheet", "stock", "bond"):
        rep = Auditor(project=project, level="stat").audit_balance({})
        assert rep.layers["L1_rule"]["ok"] is True
        assert rep.layers["L1_rule"]["llm_modules_loaded"] == []


def test_threshold_override_reaches_profile():
    bal = _balance()
    bal["data_date"] = "2026-01-01"
    bal["as_of"] = "2026-01-01"
    strict = Auditor(project="balance_sheet", level="rule",
                     thresholds={"stale_days_warn": 1, "stale_days_error": 2})
    rep = strict.audit_balance(bal)
    assert any(f.code == "data_stale" for f in rep.findings)


def test_projects_action_lists_all():
    out = run_action("projects", {})
    names = {p["name"] for p in out["projects"]}
    assert names >= {"balance_sheet", "stock", "bond"}
    assert out["default"] == "balance_sheet"


def test_run_action_passes_project():
    rep = run_action("balance", {"balance": _stock(), "project": "stock",
                                 "level": "rule"})
    assert rep["project"] == "stock"


def test_report_dict_has_project():
    rep = Auditor(project="bond", level="rule").audit_balance(_bond())
    assert rep.as_dict()["project"] == "bond"
    assert "project=bond" in rep.to_markdown()
