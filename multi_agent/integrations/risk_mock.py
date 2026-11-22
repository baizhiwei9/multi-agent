"""风控官（risk_manager）的 mock_data：四类场景 + 期货双边。

需求文档第 7 条要求"提供 mock_data 和单元测试：正常/波动/危机/混沌各一个场景"。
本模块就是那份 mock_data：**确定性合成数据**（无随机、无网络、无 pandas），
每个场景返回一份**全新的 dict**（深拷贝），调用方随便改不会污染别人。

用法
----
```python
from multi_agent.integrations.risk import RiskManager, Auditor
from multi_agent.integrations import risk_mock

data = risk_mock.normal()
brief = RiskManager().generate_report(data)
verdict = Auditor().review(brief, data["proposal"], data)
```

场景一览（数值是构造出来的，不代表任何真实行情）
--------------------------------------------------
| 场景 | 意图 | regime | 关键风险 | 预期动作 |
| --- | --- | --- | --- | --- |
| normal   | 低波动、主线清晰、流动性好 | trending | 无 | full_open |
| volatile | 中等波动 + 轮动 2 天 + 盘口变薄 | choppy | chaos>0.7 + 趋势模型 → 硬否决 | flat_only |
| crisis   | 连续下跌 + 相关性 broken + 追保 2% | panic | 流动性 frozen、保证金 85% | forced_liquidation |
| chaos    | 相关性 broken + 4 品种分散 + 配对 2.6σ | unknown/choppy | 结构失效 → 禁止多品种分散 | flat_only |
| futures(side) | 期货做多/做空，OI 连续降 + 价格涨 | — | 流动性 tight、空头回补不可持续 | reduce_* / flat_only |

期货做多与做空返回**同一套流动性字段**（需求第 8 条：流动性对双边同样重要），
只有 `proposal.side` 不同，另外做空场景把 `ret_5d` 取负以匹配方向。
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List

D = 260  # 历史长度：够算 250 日分位 + 20 日波动


# ───────────────────────────────────────── 合成序列（确定性）
def _ramp(n: int = D, start: float = 100.0, step: float = 0.25) -> List[float]:
    return [start + i * step for i in range(n)]


def _chop(n: int = D, start: float = 100.0, amp: float = 1.2) -> List[float]:
    return [start + (amp if i % 2 else -amp) * 0.5 for i in range(n)]


def _crash(n: int = D, start: float = 100.0) -> List[float]:
    out = [start + i * 0.2 for i in range(n - 40)]
    last = out[-1]
    for _ in range(40):
        last *= 0.985
        out.append(last)
    return out


def _medium_vol(n: int = D, start: float = 100.0) -> List[float]:
    """波动率分位落在 50-85%：先放大、再收敛到中等，最后小幅震荡。"""
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


def _base(indices: Dict[str, Any], market: Dict[str, Any], futures: Dict[str, Any],
          structure: Dict[str, Any], proposal: Dict[str, Any],
          asset_class: str = "equity", **extra: Any) -> Dict[str, Any]:
    return {
        "asset_class": asset_class,
        "as_of": "2026-11-21",
        "indices": indices,
        "market": market,
        "futures": futures,
        "structure": structure,
        "proposal": proposal,
        **extra,
    }


# ───────────────────────────────────────── 场景 1：正常趋势
def normal() -> Dict[str, Any]:
    """五大指数同向、主线清晰、流动性充裕、趋势模型满仓。"""
    data = _base(
        indices={
            "沪深300": {"code": "000300.SH", "close": _ramp(step=0.25), "breadth": 0.62},
            "中证500": {"code": "000905.SH", "close": _ramp(step=0.22), "breadth": 0.60},
            "中证1000": {"code": "000852.SH", "close": _ramp(step=0.20), "breadth": 0.58},
            "国证2000": {"code": "399303.SZ", "close": _ramp(step=0.18), "breadth": 0.57},
            "上证50": {"code": "000016.SH", "close": _ramp(step=0.28), "breadth": 0.64},
        },
        market={"breadth": 0.62, "rotation_days": 15.0, "volume": 1.2e6,
                "spread_bps": 6, "slippage_pct": 0.05, "exit_hours": 2.5,
                "ret_5d": 0.021},
        futures={"oi_change_1d": 0.03, "oi_change_5d": 0.05, "basis": "contango 0.3%",
                 "delivery_days": 20, "margin_ratio": 0.35, "margin_call_distance": 0.35},
        structure={"corr_state": "stable", "leadership": "clear",
                   "pairs": "high", "mean_reversion": "working"},
        proposal={"side": "long", "action": "full_open", "model": "trend", "positions": 1},
    )
    return copy.deepcopy(data)


# ───────────────────────────────────────── 场景 2：波动 / 混沌
def volatile() -> Dict[str, Any]:
    """中等波动 + 轮动 2 天 + 盘口变薄：趋势模型满仓应被硬否决。"""
    data = _base(
        indices={
            "沪深300": {"code": "000300.SH", "close": _medium_vol(), "breadth": 0.35},
            "中证500": {"code": "000905.SH", "close": _chop(), "breadth": 0.32},
        },
        market={"breadth": 0.30, "rotation_days": 2.0, "volume": 8e5,
                "spread_bps": 25, "slippage_pct": 0.15, "exit_hours": 10.0,
                "ret_5d": 0.0},
        futures={"oi_change_1d": -0.04, "oi_change_5d": -0.08, "basis": "normal",
                 "delivery_days": 18, "margin_ratio": 0.45, "margin_call_distance": 0.2},
        structure={"corr_state": "diverging", "leadership": "unclear",
                   "pairs": "low", "mean_reversion": "broken"},
        proposal={"side": "long", "action": "full_open", "model": "trend", "positions": 2},
        asset_class="both",
    )
    return copy.deepcopy(data)


# ───────────────────────────────────────── 场景 3：危机（期货追保）
def crisis() -> Dict[str, Any]:
    """连续下跌 + 相关性 broken + 流动性 frozen + 保证金 85%、距追保 2%。"""
    data = _base(
        indices={
            "沪深300": {"code": "000300.SH", "close": _crash(), "breadth": 0.20},
            "中证500": {"code": "000905.SH", "close": _crash(), "breadth": 0.18},
            "中证1000": {"code": "000852.SH", "close": _crash(), "breadth": 0.15},
            "国证2000": {"code": "399303.SZ", "close": _crash(), "breadth": 0.14},
            "上证50": {"code": "000016.SH", "close": _crash(), "breadth": 0.22},
        },
        market={"breadth": 0.10, "rotation_days": 1.0, "volume": 5e5,
                "spread_bps": 30, "slippage_pct": 0.30, "exit_hours": 30.0,
                "ret_5d": -0.08},
        futures={"oi_change_1d": -0.15, "oi_change_5d": -0.25,
                 "basis": "backwardation -1.2%", "basis_change_pct": -1.2,
                 "delivery_days": 12, "margin_ratio": 0.85, "margin_call_distance": 0.02},
        structure={"corr_state": "broken", "leadership": "unclear",
                   "pairs": "low", "mean_reversion": "broken"},
        proposal={"side": "long", "action": "full_open", "model": "trend", "positions": 2},
        asset_class="both",
    )
    return copy.deepcopy(data)


# ───────────────────────────────────────── 场景 4：结构混沌 + 多品种分散
def chaos() -> Dict[str, Any]:
    """相关性 broken、配对 2.6σ 不回归、四品种分散：结构失效下分散不再降风险。"""
    data = _base(
        indices={
            "沪深300": {"code": "000300.SH", "close": _chop(), "breadth": 0.50},
            "中证500": {"code": "000905.SH", "close": _chop(amp=1.4), "breadth": 0.48},
        },
        market={"breadth": 0.40, "rotation_days": 3.0, "volume": 1e6,
                "spread_bps": 8, "slippage_pct": 0.05, "exit_hours": 2.0,
                "ret_5d": 0.0},
        futures={"oi_change_1d": -0.02, "oi_change_5d": -0.03, "basis": "normal",
                 "delivery_days": 25, "margin_ratio": 0.40, "margin_call_distance": 0.25},
        structure={"corr_state": "broken", "leadership": "unclear",
                   "pairs": "low", "pairs_spread_sigma": 2.6,
                   "mean_reversion": "broken"},
        proposal={"side": "short", "action": "full_open", "model": "mean_reversion",
                  "positions": 4},
        asset_class="both",
    )
    return copy.deepcopy(data)


# ───────────────────────────────────────── 期货：做多 / 做空
def futures(side: str = "long") -> Dict[str, Any]:
    """期货单边：盘口变薄 + OI 连续下降 + 价格上行（空头回补），多空共用同一套检查。"""
    if side not in ("long", "short"):
        raise ValueError("side 只能是 long 或 short")
    ret5 = 0.02 if side == "long" else -0.02
    data = _base(
        indices={"沪深300": {"code": "000300.SH", "close": _ramp(), "breadth": 0.50}},
        market={"breadth": 0.50, "rotation_days": 12.0, "volume": 2e5,
                "spread_bps": 35, "slippage_pct": 0.20, "exit_hours": 10.0,
                "ret_5d": ret5},
        futures={"oi_change_1d": -0.10, "oi_change_5d": -0.20, "oi_down_streak": 3,
                 "basis": "contango 0.6%", "delivery_days": 10,
                 "margin_ratio": 0.50, "margin_call_distance": 0.20},
        structure={"corr_state": "stable", "leadership": "clear",
                   "pairs": "high", "mean_reversion": "working"},
        proposal={"side": side, "action": "full_open", "model": "trend", "positions": 1},
        asset_class="futures",
    )
    return copy.deepcopy(data)


#: 四个基准场景：名字 → 工厂（每次调用都返回新 dict）
SCENARIOS = {"normal": normal, "volatile": volatile, "crisis": crisis, "chaos": chaos}

#: 场景预期（文档用；测试自己断言，避免"照着表白改断言"）
EXPECTATIONS = {
    "normal":   {"veto": False, "action": "full_open"},
    "volatile": {"veto": True,  "action": "flat_only"},
    "crisis":   {"veto": True,  "action": "forced_liquidation"},
    "chaos":    {"veto": True,  "action": "flat_only"},
}


def all_scenarios() -> Dict[str, Dict[str, Any]]:
    """一次性拿到全部四份 mock 数据（各自独立副本）。"""
    return {name: factory() for name, factory in SCENARIOS.items()}


__all__ = ["SCENARIOS", "EXPECTATIONS", "all_scenarios",
           "normal", "volatile", "crisis", "chaos", "futures"]