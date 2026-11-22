"""风控官（risk_manager）↔ 审计师（auditor）协议与规则测试。

四个场景：正常趋势 / 波动混沌 / 危机 / 结构混沌（多品种分散）。
另外覆盖：期货做多与做空共用流动性检查、审计查漏报（veto 漏标、指数升级）、
brief 被篡改时复算不一致、verdict 协议可解析、不依赖大模型。
"""
from __future__ import annotations

import math
import re

import pytest

from multi_agent.integrations.risk import (Auditor, RiskManager, parse_brief,
                                           parse_verdict, render_brief)

D = 260  # 历史长度


def _ramp(n=D, start=100.0, step=0.25):
    return [start + i * step for i in range(n)]


def _choppy(n=D, start=100.0, amp=1.2):
    return [start + (amp if i % 2 else -amp) * 0.5 for i in range(n)]


def _crash(n=D, start=100.0):
    out = [start + i * 0.2 for i in range(n - 40)]
    last = out[-1]
    for i in range(40):
        last *= 0.985
        out.append(last)
    return out


def _flat(n=D, start=100.0):
    return [start for _ in range(n)]


def _medium_vol(n=D, start=100.0):
    """波动率分位落在 50-85% 区间：历史先低、再高、最后回到中等。"""
    close = [start]
    for i in range(n):
        if i < 0.2 * n:
            env = 0.2 + 0.8 * (i / (0.2 * n))
        elif i < 0.4 * n:
            env = 1.0 - 0.4 * ((i - 0.2 * n) / (0.2 * n))
        else:
            env = 0.6
        close.append(close[-1] * (1 + 0.004 * env * (1 if i % 2 else -1)))
    return close


def _data(indices, market=None, futures=None, structure=None, proposal=None,
          asset_class="equity", **kw):
    return {
        "asset_class": asset_class,
        "as_of": "2026-11-21",
        "indices": indices,
        "market": market or {"breadth": 0.55, "rotation_days": 15, "volume": 1.0e6,
                             "spread_bps": 6, "slippage_pct": 0.05, "exit_hours": 3,
                             "ret_5d": 0.01},
        "futures": futures or {},
        "structure": structure or {"corr_state": "stable", "leadership": "clear",
                                   "pairs": "high", "mean_reversion": "working"},
        "proposal": proposal or {"side": "long", "action": "full_open",
                                 "model": "trend", "positions": 1},
        **kw,
    }


EQUITY_3 = {
    "沪深300": {"code": "000300.SH", "close": _ramp(), "breadth": 0.62},
    "中证500": {"code": "000905.SH", "close": _ramp(step=0.2), "breadth": 0.58},
    "中证1000": {"code": "000852.SH", "close": _ramp(step=0.18), "breadth": 0.55},
}


# ═══════════════════════════════ 场景 1：正常趋势
def test_normal_trending_approves():
    rm = RiskManager()
    brief = rm.generate_report(_data(EQUITY_3))
    b = parse_brief(brief)
    assert b["regime"]["state"] in ("trending", "unknown")
    assert b["veto"]["triggered"] is False

    verdict = Auditor().review(brief, {"model": "trend", "side": "long"}, _data(EQUITY_3))
    v = parse_verdict(verdict)
    assert v["decision"] in ("APPROVE", "MODIFY")
    assert "RISK_BRIEF" in verdict or "AUDIT_VERDICT" in verdict


# ═══════════════════════════════ 场景 2：波动/混沌
def test_choppy_triggers_veto_and_auditor_accepts_flat():
    data = _data(
        {"沪深300": {"code": "000300.SH", "close": _medium_vol(), "breadth": 0.35}},
        market={"breadth": 0.3, "rotation_days": 2.0, "volume": 8e5,
                "spread_bps": 9, "slippage_pct": 0.1, "exit_hours": 5, "ret_5d": 0.0},
        structure={"corr_state": "diverging", "leadership": "unclear",
                   "pairs": "low", "mean_reversion": "broken"},
        proposal={"side": "long", "action": "full_open", "model": "trend", "positions": 1},
    )
    brief = RiskManager().generate_report(data)
    b = parse_brief(brief)
    assert b["veto"]["triggered"] is True
    assert b["summary"]["action"] in ("flat_only", "forced_liquidation")

    verdict = parse_verdict(Auditor().review(brief, data["proposal"], data))
    assert verdict["decision"] in ("APPROVE", "MODIFY")


# ═══════════════════════════════ 场景 3：危机（含期货追保）
def test_crisis_forced_liquidation():
    data = _data(
        {"沪深300": {"code": "000300.SH", "close": _crash(), "breadth": 0.2},
         "中证500": {"code": "000905.SH", "close": _crash(), "breadth": 0.18},
         "中证1000": {"code": "000852.SH", "close": _crash(), "breadth": 0.15}},
        market={"breadth": 0.1, "rotation_days": 1.0, "volume": 5e5,
                "spread_bps": 30, "slippage_pct": 0.3, "exit_hours": 30, "ret_5d": -0.08},
        futures={"oi_change_1d": -0.15, "oi_change_5d": -0.25,
                 "basis": "backwardation -1.2%", "delivery_days": 12,
                 "margin_ratio": 0.85, "margin_call_distance": 0.02},
        structure={"corr_state": "broken", "leadership": "unclear",
                   "pairs": "low", "mean_reversion": "broken"},
        proposal={"side": "long", "action": "full_open", "model": "trend", "positions": 2},
        asset_class="both",
    )
    brief = RiskManager().generate_report(data)
    b = parse_brief(brief)
    assert b["veto"]["triggered"] is True
    assert b["summary"]["action"] == "forced_liquidation"
    assert b["liquidity"]["state"] == "frozen"
    assert b["structure"]["chaos"] >= 0.7

    verdict = parse_verdict(Auditor().review(brief, data["proposal"], data))
    assert verdict["decision"] in ("APPROVE", "MODIFY")


# ═══════════════════════════════ 场景 4：结构混沌 + 多品种分散
def test_structure_broken_vetoes_diversification():
    data = _data(
        {"沪深300": {"code": "000300.SH", "close": _ramp(), "breadth": 0.5}},
        market={"breadth": 0.4, "rotation_days": 3.0, "volume": 1e6,
                "spread_bps": 8, "slippage_pct": 0.05, "exit_hours": 2, "ret_5d": 0.0},
        structure={"corr_state": "broken", "leadership": "unclear",
                   "pairs": "low", "mean_reversion": "broken"},
        proposal={"side": "long", "action": "full_open", "model": "mean_reversion",
                  "positions": 4},
    )
    b = parse_brief(RiskManager().generate_report(data))
    assert b["veto"]["triggered"] is True
    assert "多品种" in b["veto"]["reason"]


# ═══════════════════════════════ 期货：多空都要验流动性
@pytest.mark.parametrize("side", ["long", "short"])
def test_futures_both_directions_check_liquidity(side):
    data = _data(
        {"沪深300": {"code": "000300.SH", "close": _ramp(), "breadth": 0.5}},
        market={"breadth": 0.5, "rotation_days": 12, "volume": 2e5,
                "spread_bps": 35, "slippage_pct": 0.2, "exit_hours": 10, "ret_5d": 0.02},
        futures={"oi_change_1d": -0.1, "oi_change_5d": -0.2,
                 "basis": "contango 0.6%", "delivery_days": 10,
                 "margin_ratio": 0.5, "margin_call_distance": 0.2},
        proposal={"side": side, "action": "full_open", "model": "trend", "positions": 1},
        asset_class="futures",
    )
    b = parse_brief(RiskManager().generate_report(data))
    assert b["liquidity"]["state"] == "tight"
    # OI 下降 + 价格涨 → 标不可持续
    assert b["liquidity"]["oi_price_divergence"] is True


# ═══════════════════════════════ 审计：篡改 brief → 复算不一致
def test_auditor_catches_tampered_volatility():
    data = _data(EQUITY_3)
    brief = RiskManager().generate_report(data)
    tampered = brief.replace("vol=", "vol=99%ile_", 1)  # 让首个指数 vol 显示成 99%ile

    v = parse_verdict(Auditor().review(tampered, {"model": "trend"}, data))
    assert v["decision"] in ("MODIFY", "REJECT")


# ═══════════════════════════════ 审计：veto 漏标 → 强制 REJECT
def test_auditor_forces_reject_when_veto_suppressed():
    data = _data(
        {"沪深300": {"code": "000300.SH", "close": _crash(), "breadth": 0.2},
         "中证500": {"code": "000905.SH", "close": _crash(), "breadth": 0.18},
         "中证1000": {"code": "000852.SH", "close": _crash(), "breadth": 0.15}},
        market={"breadth": 0.1, "rotation_days": 1.0, "volume": 5e5,
                "spread_bps": 30, "slippage_pct": 0.3, "exit_hours": 30, "ret_5d": -0.08},
        futures={"oi_change_1d": -0.15, "oi_change_5d": -0.25,
                 "delivery_days": 12, "margin_ratio": 0.85,
                 "margin_call_distance": 0.02},
        structure={"corr_state": "broken", "leadership": "unclear",
                   "pairs": "low", "mean_reversion": "broken"},
        proposal={"side": "long", "action": "full_open", "model": "trend", "positions": 2},
        asset_class="both",
    )
    brief = RiskManager().generate_report(data)
    assert "veto_triggered: true" in brief
    suppressed = brief.replace("veto_triggered: true", "veto_triggered: false")

    v = parse_verdict(Auditor().review(suppressed, data["proposal"], data))
    assert v["decision"] == "REJECT"


# ═══════════════════════════════ 审计：指数 danger 未升级 → REJECT
def test_auditor_upgrades_underreported_index_danger():
    data = _data(
        {"沪深300": {"code": "000300.SH", "close": _crash(), "breadth": 0.2},
         "中证500": {"code": "000905.SH", "close": _crash(), "breadth": 0.18},
         "中证1000": {"code": "000852.SH", "close": _crash(), "breadth": 0.15}},
        market={"breadth": 0.1, "rotation_days": 6.0, "volume": 9e5,
                "spread_bps": 10, "slippage_pct": 0.1, "exit_hours": 4, "ret_5d": -0.03},
        proposal={"side": "long", "action": "full_open", "model": "trend", "positions": 1},
    )
    brief = RiskManager().generate_report(data)
    downgraded = re.sub(r"aggregate_signal: (danger|crisis)",
                        "aggregate_signal: warning", brief, count=1)
    assert downgraded != brief

    v = parse_verdict(Auditor().review(downgraded, data["proposal"], data))
    assert v["decision"] == "REJECT"


# ═══════════════════════════════ 协议本身
def test_protocol_roundtrip():
    brief = RiskManager().generate_report(_data(EQUITY_3))
    for head in ("=== RISK_BRIEF v2.0 ===", "[REGIME]", "[MARKET_RISK_INDEX]",
                 "[LIQUIDITY_RISK]", "[STRUCTURE_RISK]", "[POSITION_CONSTRAINTS]",
                 "[HARD_VETO]", "[SUMMARY]", "=== END RISK_BRIEF ==="):
        assert head in brief
    b = parse_brief(brief)
    assert b["regime"]["state"] in ("trending", "choppy", "panic", "euphoria", "unknown")
    assert b["summary"]["risk_score"] is not None
    assert b["summary"]["action"] in ("full_open", "reduce_30", "reduce_50",
                                      "flat_only", "forced_liquidation")

    verdict = Auditor().review(brief, {"model": "trend"}, _data(EQUITY_3))
    for head in ("=== AUDIT_VERDICT v2.0 ===", "[RECHECK]", "[CONSISTENCY]",
                 "[CROSS_CHECK]", "[VERDICT]", "[REASON]", "=== END AUDIT_VERDICT ==="):
        assert head in verdict
    assert parse_verdict(verdict)["decision"] in ("APPROVE", "MODIFY", "REJECT")


def test_no_llm_needed_and_hard_numbers_are_deterministic():
    data = _data(EQUITY_3)
    a = RiskManager().generate_report(data)
    b = RiskManager().generate_report(data)
    assert a == b                                    # 同输入同输出，没有随机性
    assert "risk_score" in a


def test_render_is_parseable_with_all_sections():
    b = RiskManager().analyze(_data(EQUITY_3))
    text = render_brief(b)
    parsed = parse_brief(text)
    assert parsed["aggregate_signal"] == b["aggregate_signal"]
    assert parsed["summary"]["action"] == b["summary"]["action"]

# ═══════════════════════════════ mock_data：四场景（需求第 7 条）
from multi_agent.integrations import risk_mock                    # noqa: E402
from multi_agent.integrations.risk import coerce_brief, run_action, register  # noqa: E402


@pytest.mark.parametrize("name", ["normal", "volatile", "crisis", "chaos"])
def test_mock_scenario_matches_expectation(name):
    data = risk_mock.SCENARIOS[name]()
    expect = risk_mock.EXPECTATIONS[name]

    brief = RiskManager().generate_report(data)
    b = parse_brief(brief)
    assert b["veto"]["triggered"] is expect["veto"], f"{name} 的否决结论不对"
    assert b["summary"]["action"] == expect["action"], f"{name} 的动作档位不对"
    assert brief.startswith("=== RISK_BRIEF v2.0 ===") and "=== END RISK_BRIEF ===" in brief

    verdict = Auditor().review(brief, data["proposal"], data)
    v = parse_verdict(verdict)
    assert v["decision"] in ("APPROVE", "MODIFY", "REJECT")
    # 风控官已经否决时，审计官不能放行成 APPROVE
    if expect["veto"] and b["summary"]["action"] == "forced_liquidation":
        assert v["decision"] != "APPROVE"


@pytest.mark.parametrize("side", ["long", "short"])
def test_mock_futures_both_sides_share_liquidity_checks(side):
    data = risk_mock.futures(side)
    b = parse_brief(RiskManager().generate_report(data))
    # 做多/做空走同一套流动性判定：价差 35bps、退出 10h → tight
    assert b["liquidity"]["state"] == "tight"
    assert b["liquidity"]["oi_change_5d"] == -0.2
    # 只有价格上行（做多场景）才构成"OI 降 + 价格涨"的空头回补背离
    assert b["liquidity"]["oi_price_divergence"] is (side == "long")


def test_mock_data_returns_isolated_copies():
    a = risk_mock.normal()
    a["market"]["rotation_days"] = 0.5
    a["proposal"]["action"] = "flat_only"
    b = risk_mock.normal()
    assert b["market"]["rotation_days"] == 15.0
    assert b["proposal"]["action"] == "full_open"
    assert risk_mock.all_scenarios()["crisis"]["asset_class"] == "both"


# ═══════════════════════════════ Hub 接入：动作与注册
class _FakeRuntime:
    def __init__(self):
        self.handlers = {}

    def add_handler(self, capability, action, fn):
        self.handlers[(capability, action)] = fn

    def call(self, capability, action, params):
        return self.handlers[(capability, action)](params, {})


def test_register_exposes_brief_and_review_roundtrip():
    rt = _FakeRuntime()
    register(rt)
    assert ("risk", "brief") in rt.handlers

    out = rt.call("risk", "brief", {"data": risk_mock.normal(), "asset_class": "equity"})
    assert out["ok"] is True and out["veto"] is False
    assert out["action"] in ("full_open", "reduce_30", "reduce_50")
    assert "=== RISK_BRIEF v2.0 ===" in out["brief"]

    # 流水线里 ${steps.risk.result} 是 dict，审计官必须能吃下这种形态
    assert coerce_brief(out) == out["brief"]
    assert coerce_brief({"text": "x"}) == "x"
    verdict = run_action("review", {"brief": out, "modeler": {"model": "trend"},
                                   "data": risk_mock.normal()})
    assert verdict["ok"] is True
    assert verdict["decision"] in ("APPROVE", "MODIFY", "REJECT")


def test_brief_action_rejects_missing_data():
    out = run_action("brief", {})
    assert out["ok"] is False and "params.data" in out["error"]


def test_single_index_aggregate_uses_that_index():
    data = risk_mock.crisis()
    data["indices"] = {"沪深300": data["indices"]["沪深300"]}
    b = parse_brief(RiskManager().generate_report(data))
    assert b["aggregate_signal"] in ("warning", "danger", "crisis")
    assert "五大指数未填全" in RiskManager().generate_report(data)
