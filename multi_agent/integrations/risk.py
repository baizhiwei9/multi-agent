"""风控官（RiskManager）与审计师（Auditor）：RISK_BRIEF v2.0 ⇄ AUDIT_VERDICT v2.0。

两个角色**只用结构化文本通信**，不共享内存对象：风控官出 `RISK_BRIEF`，
审计师回 `AUDIT_VERDICT`。协议细节见 docs/RISK_PROTOCOL.md。

设计约束（对应需求文档）
------------------------
- 硬计算（波动率、回撤、流动性、相关性、保证金）用纯 Python / numpy，**不依赖大模型**；
- 大模型只允许用于 `evidence` / `reason` 这类文字归纳，通过 `llm=` 显式注入，
  默认关闭（零 token）；
- 期货侧做多/做空**同一套流动性检查**（流动性对双边都重要）；
- 风控官的硬否决（HARD_VETO）触发后，审计师必须能将 `veto_triggered=false` 的漏报纠正为 REJECT。

数据契约（`RiskManager.generate_report(data)`）
--------------------------------------------
```
{
  "asset_class": "equity" | "futures" | "both",
  "as_of": "2026-11-21",
  "indices": {"沪深300": {"code": "000300.SH", "close": [..], "breadth": 0.55}, ...},
  "market": {"breadth": 0.28, "rotation_days": 2.1, "volume": 1.2e6,
             "spread_bps": 8, "slippage_pct": 0.08, "exit_hours": 6,
             "position_notional": 1e7},
  "futures": {"oi_change_1d": -0.12, "oi_change_5d": -0.2,
              "basis": "contango 0.5%", "delivery_days": 12,
              "margin_ratio": 0.55, "margin_call_distance": 0.18},
  "structure": {"corr_state": "broken", "leadership": "unclear",
                "pairs": "low", "mean_reversion": "broken"},
  "proposal": {"side": "long", "action": "full_open", "model": "trend", "positions": 1},
  "valuation_extreme": false
}
```
字段缺失不会报错：缺失项按"未提供"处理，并在 brief 的 evidence 里写明，**不猜数**。
"""
from __future__ import annotations

import math
import re
import statistics
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

RISK_BRIEF_VERSION = "v2.0"
AUDIT_VERDICT_VERSION = "v2.0"

SIGNAL_ORDER = {"normal": 0, "warning": 1, "danger": 2, "crisis": 3}
ACTION_ORDER = {"full_open": 0, "open": 0, "add": 0, "reduce_30": 1, "reduce_50": 2,
                "flat_only": 3, "forced_liquidation": 4}

#: 风控官不得跳过的硬否决（与需求文档 E 节一一对应）
VETO_RULES = (
    "regime=panic 且提案做多/满仓",
    "regime=choppy 且 structure_chaos>0.7 且模型为趋势跟踪",
    "五大指数 ≥3 个 danger 且提案满仓",
    "期货 liquidity=frozen 或 margin_call_distance<3%",
    "交割<5天且未平仓且提案加仓",
    "相关性 broken 且提案多品种分散",
    "保证金占用>80%",
)


# ═══════════════════════════════════════════ 数值工具（不依赖 LLM）
def _nums(x: Any) -> List[float]:
    out: List[float] = []
    for v in x or []:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out.append(f)
    return out


def _mean(xs: Iterable[float]) -> Optional[float]:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def _stdev(xs: Iterable[float]) -> Optional[float]:
    xs = list(xs)
    return statistics.pstdev(xs) if len(xs) >= 2 else None


def _returns(close: Sequence[float], n: int = 20) -> List[float]:
    c = _nums(close)[-(n + 1):]
    return [c[i] / c[i - 1] - 1 for i in range(1, len(c)) if c[i - 1]]


def _vol_pct(close: Sequence[float], window: int = 20, lookback: int = 250) -> Optional[float]:
    """20 日波动率在近 lookback 日中的分位（0-1）；不足数据返回 None。"""
    c = _nums(close)
    if len(c) < window + 2:
        return None
    series: List[float] = []
    for end in range(window, len(c)):
        seg = c[max(0, end - lookback - window):end + 1]
        r = _returns(seg, window)
        v = _stdev(r)
        if v is not None:
            series.append(v)
    if len(series) < 2:
        return None
    latest = series[-1]
    return sum(1 for v in series if v <= latest) / len(series)


def _ret_n(close: Sequence[float], n: int = 20) -> Optional[float]:
    c = _nums(close)
    if len(c) < n + 1 or not c[-n - 1]:
        return None
    return c[-1] / c[-n - 1] - 1


def _ma(close: Sequence[float], n: int) -> Optional[float]:
    c = _nums(close)
    return sum(c[-n:]) / n if len(c) >= n else None


def _max_drawdown(close: Sequence[float]) -> Optional[float]:
    c = _nums(close)
    if len(c) < 2:
        return None
    peak, mdd = c[0], 0.0
    for v in c:
        peak = max(peak, v)
        if peak:
            mdd = min(mdd, v / peak - 1)
    return mdd


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _fmt(v: Optional[float], nd: int = 2, suffix: str = "") -> str:
    return "—" if v is None else f"{v:.{nd}f}{suffix}"


# ═══════════════════════════════════════════ 指数打分（需求 B 节）
def score_index(close: Sequence[float], breadth: Optional[float]) -> Dict[str, Any]:
    """对单个指数独立打分 0-100；返回 score / signal / detail。"""
    c = _nums(close)
    if not c:
        return {"score": 0, "signal": "normal", "detail": "无价格数据"}

    score, parts = 0, []
    last = c[-1]
    for span, pts in ((60, 20), (120, 20), (250, 20)):
        ma = _ma(c, span)
        if ma is not None and last < ma:
            score += pts
            parts.append(f"跌破{span}日")
    r20 = _ret_n(c, 20)
    if r20 is not None:
        if r20 < 0:
            score += 15
            parts.append(f"20d {r20:.1%}")
        if r20 < -0.05:
            score += 15
            parts.append("20d<-5%")
    volp = _vol_pct(c)
    if volp is not None:
        if volp > 0.8:
            score += 15
            parts.append(f"vol {volp:.0%}ile")
        if volp > 0.9:
            score += 15
            parts.append("vol>90%ile")
    if breadth is not None and breadth < 0.4:
        score += 15
        parts.append(f"广度 {breadth:.0%}")

    score = min(100, score)
    signal = ("crisis" if score > 90 else "danger" if score > 80
              else "warning" if score > 60 else "normal")
    vol_s = f"vol={volp * 100:.0f}%ile" if volp is not None else "vol=—"
    dd = _max_drawdown(c)
    dd_s = f"dd={dd * 100:.1f}%" if dd is not None else "dd=—"
    detail = ", ".join(parts) or "均线之上、动量为正"
    return {"score": score, "signal": signal,
            "detail": f"{vol_s}, {dd_s}, {detail}",
            "vol_pct": volp, "drawdown": dd}


# ═══════════════════════════════════════════ 风控官
class RiskManager:
    """生成 `RISK_BRIEF v2.0`。

    `llm` 只用于 evidence / reason 的文字归纳（可选、默认关闭）；
    任何数值判定都不经过它。
    """

    def __init__(self, llm: Optional[Callable[[str], str]] = None) -> None:
        self.llm = llm

    # ---------------- 主入口 ----------------
    def generate_report(self, data: Dict[str, Any]) -> str:
        fields = self.analyze(data or {})
        return render_brief(fields)

    # ---------------- 判定 ----------------
    def analyze(self, data: Dict[str, Any]) -> Dict[str, Any]:
        indices = self._indices(data)
        market = dict(data.get("market") or {})
        futures = dict(data.get("futures") or {})
        structure = dict(data.get("structure") or {})
        proposal = dict(data.get("proposal") or {})
        asset_class = str(data.get("asset_class") or "equity")
        as_of = str(data.get("as_of") or datetime.now(timezone.utc).isoformat(timespec="seconds"))

        # 1) 指数
        idx_rows: List[Dict[str, Any]] = []
        for name, item in indices.items():
            item = dict(item or {})
            scored = score_index(item.get("close") or [], item.get("breadth"))
            idx_rows.append({"name": name, "code": item.get("code") or "",
                             "signal": scored["signal"], "metric": scored["detail"],
                             "score": scored["score"]})
        signals = [SIGNAL_ORDER[r["signal"]] for r in idx_rows] or [0]
        worst_two = sorted(signals, reverse=True)[:2]
        aggregate = next((k for k, v in SIGNAL_ORDER.items() if v == worst_two[-1]), "normal"
                         ) if len(worst_two) >= 2 else "normal"
        danger_count = sum(1 for s in signals if s >= 2)

        # 2) 流动性
        liquidity = self._liquidity(market, futures, asset_class)
        # 3) 结构
        struct = self._structure(structure, market, futures)
        # 4) 市场状态
        regime = self._regime(idx_rows, market, struct, data)
        # 5) 仓位约束
        risk_score = self._risk_score(idx_rows, liquidity, struct, regime)
        veto, veto_reason = self._veto(regime, struct, liquidity, idx_rows, proposal,
                                       futures, data)
        action = self._action(risk_score, veto, futures)
        constraints = self._constraints(asset_class, risk_score, veto)

        evidence = self._evidence(regime, struct, liquidity, idx_rows)
        reason = self._reason(action, risk_score, veto, regime, liquidity, struct)

        return {
            "sender": "risk_manager", "recipient": "auditor",
            "asset_class": asset_class, "timestamp": as_of,
            "regime": {"state": regime["state"], "choppy_score": round(regime["choppy"], 2),
                       "evidence": evidence},
            "indices": idx_rows,
            "aggregate_signal": aggregate,
            "danger_count": danger_count,
            "market_breadth": _safe_float(market.get("breadth")),
            "sector_rotation_speed": _safe_float(market.get("rotation_days")),
            "liquidity": liquidity,
            "structure": struct,
            "position_constraints": constraints,
            "veto": {"triggered": bool(veto), "reason": veto_reason},
            "summary": {"risk_score": int(round(risk_score)), "action": action,
                        "reason": reason},
            "data_notes": [n for n in (regime.get("note"), liquidity.get("note"),
                                       struct.get("note")) if n],
        }

    # ---------------- 分部 ----------------
    @staticmethod
    def _indices(data: Dict[str, Any]) -> Dict[str, Any]:
        raw = data.get("indices")
        if isinstance(raw, dict):
            return raw
        out: Dict[str, Any] = {}
        for item in raw or []:
            if isinstance(item, dict) and item.get("name"):
                out[str(item["name"])] = item
        return out

    @staticmethod
    def _liquidity(market: Dict[str, Any], futures: Dict[str, Any],
                   asset_class: str) -> Dict[str, Any]:
        volume = _safe_float(market.get("volume"))
        spread = _safe_float(market.get("spread_bps"))
        slippage = _safe_float(market.get("slippage_pct"))
        exit_hours = _safe_float(market.get("exit_hours"))
        notes: List[str] = []

        state = "sufficient"
        if volume is not None and volume <= 0:
            state = "frozen"
        elif exit_hours is not None and exit_hours > 24:
            state = "frozen"
        elif (spread is not None and spread > 20) or (exit_hours is not None and exit_hours > 8):
            state = "tight"

        oi1 = _safe_float(futures.get("oi_change_1d"))
        oi5 = _safe_float(futures.get("oi_change_5d"))
        divergence = bool(oi5 is not None and oi5 < 0 and
                          _safe_float(market.get("ret_5d") or 0) > 0)
        if oi5 is not None and oi5 < 0:
            notes.append(f"持仓量 5d {oi5:+.0%}，资金在撤")
        margin_ratio = _safe_float(futures.get("margin_ratio"))
        margin_gap = _safe_float(futures.get("margin_call_distance"))
        if margin_ratio is not None and margin_ratio > 0.6:
            notes.append(f"保证金占用 {margin_ratio:.0%}（>60% warning）")
        if margin_gap is not None and margin_gap < 0.03:
            notes.append(f"距追保 {margin_gap:.1%}（<3% 强制减仓）")
        if asset_class == "equity" and not futures:
            notes.append("股票侧未填期货字段，流动性权重下调")

        return {
            "avg_daily_volume": volume,
            "bid_ask_spread_bps": spread,
            "slippage_1pct_order": slippage,
            "exit_time_estimate_hours": exit_hours,
            "state": state,
            "futures": {
                "oi_change_1d": oi1, "oi_change_5d": oi5,
                "oi_price_divergence": divergence,
                "basis_risk": futures.get("basis") or "normal",
                "delivery_proximity": _safe_float(futures.get("delivery_days")),
                "margin_ratio_current": margin_ratio,
                "margin_call_distance": margin_gap,
            },
            "note": "；".join(notes) or None,
        }

    @staticmethod
    def _structure(structure: Dict[str, Any], market: Dict[str, Any],
                   futures: Dict[str, Any]) -> Dict[str, Any]:
        corr = str(structure.get("corr_state") or market.get("corr_state") or "stable")
        leadership = str(structure.get("leadership") or "clear")
        pairs = str(structure.get("pairs") or "high")
        mean_rev = str(structure.get("mean_reversion") or "working")
        rotation = _safe_float(market.get("rotation_days"))

        rotation_inv = 0.0 if rotation is None else _clamp((10 - rotation) / 10)
        breakdown = 1.0 if leadership == "unclear" else 0.0
        if rotation is not None and rotation < 5 and leadership == "unclear":
            breakdown = 1.0
        corr_break = {"broken": 1.0, "diverging": 0.5, "converging": 0.5}.get(corr, 0.0)
        chaos = _clamp(rotation_inv * 0.3 + breakdown * 0.3 + corr_break * 0.4)

        notes = []
        if corr == "broken":
            notes.append("相关性矩阵 broken：结构失效")
        if pairs == "low":
            notes.append("配对可交易性 low")
        if mean_rev == "broken":
            notes.append("均值回归失效")

        return {"corr_state": corr, "leadership": leadership, "pairs": pairs,
                "mean_reversion": mean_rev, "chaos": round(chaos, 3),
                "rotation_inv": round(rotation_inv, 3), "note": "；".join(notes) or None}

    @staticmethod
    def _regime(rows: List[Dict[str, Any]], market: Dict[str, Any],
                struct: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        volp = max([r.get("score", 0) for r in rows] or [0])
        # 用第一个指数的 vol 分位代表市场波动（缺失就退回分数）
        first_vol = None
        indices = data.get("indices") or {}
        if isinstance(indices, dict):
            for item in indices.values():
                first_vol = _vol_pct((item or {}).get("close") or [])
                if first_vol is not None:
                    break
        vol_level = first_vol if first_vol is not None else volp / 100
        rotation = _safe_float(market.get("rotation_days"))
        ret5 = _safe_float(market.get("ret_5d"))
        corr = struct["corr_state"]

        note = None
        if vol_level > 0.9 and (ret5 is not None and ret5 < 0) and corr in ("broken", "converging"):
            state = "panic"
        elif vol_level < 0.3 and (ret5 is not None and ret5 > 0.05) and data.get("valuation_extreme"):
            state = "euphoria"
        elif vol_level < 0.6 and rotation is not None and rotation > 10 and struct["leadership"] == "clear":
            state = "trending"
        elif 0.5 <= vol_level <= 0.85 and rotation is not None and rotation < 5:
            state = "choppy"
        else:
            state = "unknown"
            note = "信号互相矛盾（波动/轮动/结构不同向），按 unknown 处理"

        choppy = _clamp(0.4 * (1 - min(rotation or 10, 10) / 10)
                        + 0.3 * (1 if struct["leadership"] == "unclear" else 0)
                        + 0.3 * struct["chaos"])
        return {"state": state, "choppy": choppy, "note": note,
                "vol_level": vol_level}

    @staticmethod
    def _risk_score(rows: List[Dict[str, Any]], liquidity: Dict[str, Any],
                    struct: Dict[str, Any], regime: Dict[str, Any]) -> float:
        scores = sorted([r.get("score", 0) for r in rows] or [0], reverse=True)
        index_risk = sum(scores[:2]) / len(scores[:2])
        liq = {"sufficient": 0, "tight": 55, "frozen": 100}.get(liquidity["state"], 50)
        regime_risk = {"panic": 100, "choppy": 60, "unknown": 50,
                       "euphoria": 40, "trending": 15}.get(regime["state"], 50)
        return _clamp(0.35 * index_risk + 0.25 * liq
                      + 0.2 * struct["chaos"] * 100 + 0.2 * regime_risk, 0, 100)

    @staticmethod
    def _veto(regime: Dict[str, Any], struct: Dict[str, Any], liquidity: Dict[str, Any],
              rows: List[Dict[str, Any]], proposal: Dict[str, Any],
              futures: Dict[str, Any], data: Dict[str, Any]) -> Tuple[bool, str]:
        action = str(proposal.get("action") or "full_open")
        side = str(proposal.get("side") or "long")
        model = str(proposal.get("model") or "trend")
        positions = int(proposal.get("positions") or 1)
        danger = sum(1 for r in rows if SIGNAL_ORDER.get(r.get("signal"), 0) >= 2)
        full = ACTION_ORDER.get(action, 0) <= 0

        reasons: List[str] = []
        if regime["state"] == "panic" and side == "long" and full:
            reasons.append("regime=panic 且提案做多/满仓")
        if (regime["state"] == "choppy" and struct["chaos"] > 0.7
                and model in ("trend", "trend_following")):
            reasons.append("choppy + 结构混沌>0.7 + 趋势模型")
        if danger >= 3 and full:
            reasons.append(f"{danger} 个指数 danger 且提案满仓")
        if liquidity["state"] == "frozen":
            reasons.append("流动性 frozen")
        margin_gap = liquidity["futures"].get("margin_call_distance")
        if margin_gap is not None and margin_gap < 0.03:
            reasons.append(f"距追保 {margin_gap:.1%}<3%")
        delivery = liquidity["futures"].get("delivery_proximity")
        pos_open = bool(data.get("position_open", True))
        if delivery is not None and delivery < 5 and pos_open and action in ("add", "full_open", "open"):
            reasons.append(f"交割 {delivery:.0f} 天<5 且提案加仓")
        if struct["corr_state"] == "broken" and positions > 1:
            reasons.append("相关性 broken 且多品种分散")
        margin_ratio = liquidity["futures"].get("margin_ratio_current")
        if margin_ratio is not None and margin_ratio > 0.8:
            reasons.append(f"保证金占用 {margin_ratio:.0%}>80%")

        return (bool(reasons), "；".join(reasons))

    @staticmethod
    def _action(score: float, veto: bool, futures: Dict[str, Any]) -> str:
        gap = _safe_float(futures.get("margin_call_distance"))
        if veto and gap is not None and gap < 0.03:
            return "forced_liquidation"
        if veto:
            return "flat_only"
        if score >= 85:
            return "flat_only"
        if score >= 70:
            return "reduce_50"
        if score >= 50:
            return "reduce_30"
        return "full_open"

    @staticmethod
    def _constraints(asset_class: str, score: float, veto: bool) -> Dict[str, Any]:
        max_pos = 100 - int(score * 0.7)
        if veto:
            max_pos = 0
        return {
            "max_position_pct": max(0, min(100, max_pos)),
            "stop_loss_pct": 1.5 if asset_class == "futures" else 2.5,
            "max_holding_days": 5 if asset_class == "futures" else 20,
            "leverage_limit": 3 if asset_class == "futures" else 1,
            "blackout_windows": ["交割周", "FOMC", "非农"],
        }

    def _evidence(self, regime: Dict[str, Any], struct: Dict[str, Any],
                  liquidity: Dict[str, Any], rows: List[Dict[str, Any]]) -> str:
        if self.llm:  # 只在显式注入时用模型润色文字
            try:
                return str(self.llm(self._evidence_prompt(regime, struct, liquidity, rows)))[:400]
            except Exception:  # noqa: BLE001
                pass
        worst = ", ".join(f"{r['name']}={r['signal']}" for r in rows[:5]) or "无指数数据"
        return (f"波动分位 {regime['vol_level']:.0%}, 混沌分 {regime['choppy']:.2f}, "
                f"结构 chaos {struct['chaos']:.2f}, 流动性 {liquidity['state']}, "
                f"指数：{worst}")

    @staticmethod
    def _evidence_prompt(regime, struct, liquidity, rows) -> str:
        return (f"用一句话归纳：regime={regime['state']}，结构={struct['corr_state']}，"
                f"流动性={liquidity['state']}，指数={[r['signal'] for r in rows]}。只输出这句话。")

    def _reason(self, action: str, score: float, veto: bool, regime: Dict[str, Any],
                liquidity: Dict[str, Any], struct: Dict[str, Any]) -> str:
        if self.llm:
            try:
                return str(self.llm(
                    f"用一句话说明为什么给 {action}：risk_score={score:.0f}, "
                    f"regime={regime['state']}, 流动性={liquidity['state']}, "
                    f"结构chaos={struct['chaos']}。只输出这句话。"))[:300]
            except Exception:  # noqa: BLE001
                pass
        if veto:
            return f"触发硬否决（{regime['state']}/{liquidity['state']}），只能空仓或强制减仓"
        return (f"{regime['state']} 状态下风险分 {score:.0f}，"
                f"流动性 {liquidity['state']}、结构 chaos {struct['chaos']:.2f} → {action}")


def _safe_float(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


# ═══════════════════════════════════════════ 协议：渲染 / 解析
def render_brief(f: Dict[str, Any]) -> str:
    L: List[str] = ["=== RISK_BRIEF v2.0 ===",
                    f"sender: {f.get('sender', 'risk_manager')}",
                    f"recipient: {f.get('recipient', 'auditor')}",
                    f"asset_class: {f.get('asset_class', 'equity')}",
                    f"timestamp: {f.get('timestamp')}", ""]

    r = f.get("regime") or {}
    L += ["[REGIME]",
          f"state: {r.get('state', 'unknown')}",
          f"choppy_score: {r.get('choppy_score', 0)}",
          f'evidence: "{r.get("evidence", "")}"', ""]

    L += ["[MARKET_RISK_INDEX]  (五大关键指数，股票侧必填)", "indices:"]
    for row in f.get("indices") or []:
        L += [f"  - name: {row.get('name')} | code: {row.get('code')}",
              f"    signal: {row.get('signal')}",
              f'    metric: "{row.get("metric", "")}"']
    L += [f"aggregate_signal: {f.get('aggregate_signal', 'normal')}",
          f"market_breadth: {_fmt(_safe_float(f.get('market_breadth')))}",
          f"sector_rotation_speed: {_fmt(_safe_float(f.get('sector_rotation_speed')), 1, 'd')}",
          ""]

    liq = f.get("liquidity") or {}
    fx = liq.get("futures") or {}
    L += ["[LIQUIDITY_RISK]  (期货侧核心，股票侧也填但权重低)", "market_liquidity:",
          f"  avg_daily_volume: {_fmt(_safe_float(liq.get('avg_daily_volume')), 0)}",
          f"  bid_ask_spread_bps: {_fmt(_safe_float(liq.get('bid_ask_spread_bps')), 1)}",
          f"  slippage_1pct_order: {_fmt(_safe_float(liq.get('slippage_1pct_order')), 2)}",
          f"  exit_time_estimate_hours: {_fmt(_safe_float(liq.get('exit_time_estimate_hours')), 1)}",
          f"liquidity_state: {liq.get('state', 'sufficient')}",
          "futures_specific:",
          f"  open_interest_change_1d: {_fmt(_safe_float(fx.get('oi_change_1d')), 3)}",
          f"  open_interest_change_5d: {_fmt(_safe_float(fx.get('oi_change_5d')), 3)}",
          f"  oi_price_divergence: {str(bool(fx.get('oi_price_divergence'))).lower()}",
          f'  basis_risk: "{fx.get("basis_risk") or "normal"}"',
          f"  delivery_proximity: {_fmt(_safe_float(fx.get('delivery_proximity')), 0)}",
          f"  margin_ratio_current: {_fmt(_safe_float(fx.get('margin_ratio_current')), 2)}",
          f"  margin_call_distance: {_fmt(_safe_float(fx.get('margin_call_distance')), 2)}",
          ""]

    st = f.get("structure") or {}
    L += ["[STRUCTURE_RISK]  (板块/品种结构混沌)",
          f"sector_correlation_matrix_state: {st.get('corr_state', 'stable')}",
          f"leadership_clarity: {st.get('leadership', 'clear')}",
          f"pairs_tradeability: {st.get('pairs', 'high')}",
          f"mean_reversion_state: {st.get('mean_reversion', 'working')}",
          f"structure_chaos_score: {st.get('chaos', 0)}",
          f'structure_evidence: "{st.get("note") or ""}"', ""]

    pc = f.get("position_constraints") or {}
    L += ["[POSITION_CONSTRAINTS]",
          f"max_position_pct: {pc.get('max_position_pct', 0)}",
          f"stop_loss_pct: {pc.get('stop_loss_pct', 2.5)}",
          f"max_holding_days: {pc.get('max_holding_days', 20)}",
          f"leverage_limit: {pc.get('leverage_limit', 1)}",
          f"blackout_windows: {pc.get('blackout_windows') or []}", ""]

    v = f.get("veto") or {}
    L += ["[HARD_VETO]",
          f"veto_triggered: {str(bool(v.get('triggered'))).lower()}",
          f'veto_reason: "{v.get("reason") or ""}"',
          "veto_conditions:"]
    L += [f"  - {rule}" for rule in VETO_RULES]
    L += [""]

    s = f.get("summary") or {}
    L += ["[SUMMARY]",
          f"risk_score: {s.get('risk_score', 0)}",
          f"recommended_action: {s.get('action', 'flat_only')}",
          f'reason: "{s.get("reason", "")}"',
          "=== END RISK_BRIEF ==="]
    return "\n".join(L) + "\n"


def _section_text(text: str, name: str) -> str:
    m = re.search(rf"\[{name}\][^\n]*\n(.*?)(?=\n\[[A-Z_]+\]|\n=== END)", text, re.S)
    return m.group(1) if m else ""


def _kv(block: str, key: str) -> Optional[str]:
    m = re.search(rf"^\s*{re.escape(key)}:\s*(.+)$", block, re.M)
    return m.group(1).strip().strip('"') if m else None


def parse_brief(text: str) -> Dict[str, Any]:
    """把 RISK_BRIEF 文本解析成结构化 dict（供审计师复算，不依赖 LLM）。"""
    out: Dict[str, Any] = {"_raw": text}
    out["asset_class"] = _kv(text, "asset_class")
    out["timestamp"] = _kv(text, "timestamp")

    regime = _section_text(text, "REGIME")
    out["regime"] = {"state": _kv(regime, "state") or "unknown",
                     "choppy_score": _safe_float(_kv(regime, "choppy_score")),
                     "evidence": _kv(regime, "evidence")}

    body = _section_text(text, "MARKET_RISK_INDEX")
    rows: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {}
    for line in body.splitlines():
        s = line.strip()
        m = re.match(r"^-\s*name:\s*(.+?)(?:\s*\|\s*code:\s*(.+))?$", s)
        if m:
            if current:
                rows.append(current)
            current = {"name": m.group(1).strip(), "code": (m.group(2) or "").strip()}
            continue
        if s.startswith("signal:"):
            current["signal"] = s.split(":", 1)[1].strip()
        elif s.startswith("metric:"):
            current["metric"] = s.split(":", 1)[1].strip().strip('"')
    if current:
        rows.append(current)
    out["indices"] = rows
    out["aggregate_signal"] = _kv(body, "aggregate_signal") or "normal"
    out["danger_count"] = sum(1 for r in rows if SIGNAL_ORDER.get(r.get("signal"), 0) >= 2)
    out["market_breadth"] = _safe_float(_kv(body, "market_breadth"))
    out["sector_rotation_speed"] = _safe_float(_kv(body, "sector_rotation_speed", ))

    liq = _section_text(text, "LIQUIDITY_RISK")
    out["liquidity"] = {
        "state": _kv(liq, "liquidity_state"),
        "avg_daily_volume": _safe_float(_kv(liq, "avg_daily_volume")),
        "spread_bps": _safe_float(_kv(liq, "bid_ask_spread_bps")),
        "exit_hours": _safe_float(_kv(liq, "exit_time_estimate_hours")),
        "oi_change_1d": _safe_float(_kv(liq, "open_interest_change_1d")),
        "oi_change_5d": _safe_float(_kv(liq, "open_interest_change_5d")),
        "oi_price_divergence": (_kv(liq, "oi_price_divergence") or "false") == "true",
        "basis_risk": _kv(liq, "basis_risk"),
        "delivery_days": _safe_float(_kv(liq, "delivery_proximity")),
        "margin_ratio": _safe_float(_kv(liq, "margin_ratio_current")),
        "margin_call_distance": _safe_float(_kv(liq, "margin_call_distance")),
    }

    st = _section_text(text, "STRUCTURE_RISK")
    out["structure"] = {
        "corr_state": _kv(st, "sector_correlation_matrix_state"),
        "leadership": _kv(st, "leadership_clarity"),
        "pairs": _kv(st, "pairs_tradeability"),
        "mean_reversion": _kv(st, "mean_reversion_state"),
        "chaos": _safe_float(_kv(st, "structure_chaos_score")),
    }

    v = _section_text(text, "HARD_VETO")
    out["veto"] = {"triggered": (_kv(v, "veto_triggered") or "false") == "true",
                   "reason": _kv(v, "veto_reason")}

    s = _section_text(text, "SUMMARY")
    out["summary"] = {"risk_score": _safe_float(_kv(s, "risk_score")),
                      "action": _kv(s, "recommended_action"),
                      "reason": _kv(s, "reason")}
    out["vol_pct"] = _parse_metric_vol(rows)
    out["drawdown"] = _parse_metric_dd(rows)
    return out


def _parse_metric_vol(rows: List[Dict[str, Any]]) -> Optional[float]:
    for r in rows:
        m = re.search(r"vol=(\d+)%ile", r.get("metric") or "")
        if m:
            return float(m.group(1)) / 100
    return None


def _parse_metric_dd(rows: List[Dict[str, Any]]) -> Optional[float]:
    for r in rows:
        m = re.search(r"dd=(-?\d+(?:\.\d+)?)%", r.get("metric") or "")
        if m:
            return float(m.group(1)) / 100
    return None


# ═══════════════════════════════════════════ 审计师
class Auditor:
    """接收 `RISK_BRIEF` 字符串，输出 `AUDIT_VERDICT v2.0`。

    只做三件事，不改风控官的任何数字：
    1. **复算**：波动率分位、最大回撤、流动性退出时间，与 brief 里的值对比；
    2. **一致性**：regime vs 模型类型、指数信号 vs 提案仓位、veto vs 建议动作；
    3. **查漏报**：指数 danger 数够了但 aggregate 没升级、硬否决满足但 `veto_triggered=false`。

    `llm` 只用于 REASON 的文字组织（可选、默认关闭），所有判定都不经过它。
    """

    def __init__(self, llm: Optional[Callable[[str], str]] = None,
                 vol_tolerance: float = 0.15, dd_tolerance: float = 0.03) -> None:
        self.llm = llm
        self.vol_tolerance = vol_tolerance
        self.dd_tolerance = dd_tolerance

    # ---------------- 入口 ----------------
    def review(self, risk_brief: str, modeler_output: Optional[Dict[str, Any]] = None,
               data: Optional[Dict[str, Any]] = None) -> str:
        return render_verdict(self.audit(risk_brief, modeler_output or {}, data or {}))

    def audit(self, risk_brief: str, modeler: Dict[str, Any],
              data: Dict[str, Any]) -> Dict[str, Any]:
        b = parse_brief(risk_brief or "")
        model = str(modeler.get("model") or "trend")
        side = str(modeler.get("side") or "long")
        positions = int(modeler.get("positions") or 1)
        action = str(b["summary"].get("action") or "flat_only")
        full = ACTION_ORDER.get(action, 0) <= 0
        danger = int(b.get("danger_count") or 0)
        regime = str(b["regime"].get("state") or "unknown")
        aggregate = str(b.get("aggregate_signal") or "normal")
        liq, st = b["liquidity"], b["structure"]
        veto_on = bool(b["veto"].get("triggered"))

        # 1) 复算（同一份数据、纯数值，不调模型）
        recheck = [
            self._cmp("volatility_20d", b.get("vol_pct"), self._recompute_vol(data),
                      self.vol_tolerance),
            self._cmp("max_drawdown", b.get("drawdown"), self._recompute_dd(data),
                      self.dd_tolerance),
            self._cmp("liquidity_exit", liq.get("exit_hours"),
                      _safe_float((data.get("market") or {}).get("exit_hours")), 0.5,
                      ratio=True),
        ]
        if (liq.get("oi_change_5d") or 0) < 0 and not liq.get("oi_price_divergence"):
            ret5 = _safe_float((data.get("market") or {}).get("ret_5d")) or 0
            if ret5 > 0:
                recheck.append({"metric": "oi_price_divergence", "reported": False,
                                "recomputed": True, "result": "mismatch"})
        mismatches = [r for r in recheck if r["result"] == "mismatch"]

        # 2) 一致性
        consistency: List[str] = []
        if model in ("trend", "trend_following") and regime in ("choppy", "unknown"):
            consistency.append(f"regime={regime} 而模型是趋势跟踪 → 可能失效，应降级")
        if model in ("mean_reversion", "mean_rev") and regime == "trending":
            consistency.append(f"regime={regime} 而模型是均值回归 → 可能失效")
        if danger >= 3 and full:
            consistency.append(f"{danger} 个指数 danger 但提案满仓 → 矛盾")
        if veto_on and full:
            consistency.append("veto_triggered=true 但建议仍是开仓 → 内部矛盾")

        # 3) 漏报
        missing: List[str] = []
        if danger >= 3 and aggregate not in ("danger", "crisis"):
            missing.append(f"指数 danger 数={danger} 但 aggregate={aggregate} → 应升级")
        recalc_veto = self._recompute_veto(b, model, side, positions, action)
        if recalc_veto and not veto_on:
            missing.append("硬否决满足但 veto_triggered=false（漏报）：" + "；".join(recalc_veto))

        # 4) 判定
        reasons_reject = list(missing)
        if veto_on and full:
            reasons_reject.append("内部矛盾：风控官已否决，但建议动作仍是开仓")
        reasons_modify = [r["metric"] + " 与复算不一致" for r in mismatches]
        for c in consistency:
            (reasons_reject if "矛盾" in c and "内部" not in c else reasons_modify).append(c)

        if reasons_reject:
            decision = "REJECT"
        elif reasons_modify:
            decision = "MODIFY"
        else:
            decision = "APPROVE"

        modified = self._modified_action(decision, action, danger, veto_on, model, regime)
        binding = self._binding(risk_brief, decision, modified)
        cross = self._cross_check(b, recheck, regime, st, liq, danger)
        reason = self._reason(decision, reasons_reject, reasons_modify, regime, liq, st)

        return {
            "sender": "auditor", "recipient": "risk_manager (抄送: trader / human)",
            "timestamp": b.get("timestamp"),
            "recheck": recheck,
            "consistency": {
                "regime_vs_model": consistency[0] if consistency else "一致",
                "index_signals_vs_position": (f"{danger} 个 danger vs {action}"
                                              if danger else "无 danger"),
                "details": consistency,
            },
            "cross_check": cross,
            "verdict": {"decision": decision, "modified_action": modified,
                        "binding_conditions": binding, "override_available": True},
            "reason": reason,
            "mismatches": [r["metric"] for r in mismatches],
        }

    # ---------------- 复算 ----------------
    @staticmethod
    def _recompute_vol(data: Dict[str, Any]) -> Optional[float]:
        for item in (data.get("indices") or {}).values():
            v = _vol_pct((item or {}).get("close") or [])
            if v is not None:
                return v
        return None

    @staticmethod
    def _recompute_dd(data: Dict[str, Any]) -> Optional[float]:
        for item in (data.get("indices") or {}).values():
            dd = _max_drawdown((item or {}).get("close") or [])
            if dd is not None:
                return dd
        return None

    @staticmethod
    def _cmp(metric: str, reported: Optional[float], recomputed: Optional[float],
             tol: float, ratio: bool = False) -> Dict[str, Any]:
        if reported is None or recomputed is None:
            return {"metric": metric, "reported": reported, "recomputed": recomputed,
                    "result": "n/a"}
        diff = abs(reported - recomputed)
        ok = (diff <= tol) if not ratio else (recomputed and diff / abs(recomputed) <= tol)
        return {"metric": metric, "reported": round(reported, 4),
                "recomputed": round(recomputed, 4), "diff": round(diff, 4),
                "result": "match" if ok else "mismatch"}

    @staticmethod
    def _recompute_veto(b: Dict[str, Any], model: str, side: str, positions: int,
                        action: str) -> List[str]:
        liq, st = b["liquidity"], b["structure"]
        regime = b["regime"].get("state")
        full = ACTION_ORDER.get(action, 0) <= 0
        out: List[str] = []
        if regime == "panic" and side == "long" and full:
            out.append("panic 且做多/满仓")
        if regime == "choppy" and (st.get("chaos") or 0) > 0.7 and model in ("trend", "trend_following"):
            out.append("choppy + chaos>0.7 + 趋势模型")
        if int(b.get("danger_count") or 0) >= 3 and full:
            out.append("≥3 指数 danger 且满仓")
        if liq.get("state") == "frozen":
            out.append("流动性 frozen")
        gap = liq.get("margin_call_distance")
        if gap is not None and gap < 0.03:
            out.append(f"距追保 {gap:.1%}<3%")
        delivery = liq.get("delivery_days")
        if delivery is not None and delivery < 5 and action in ("add", "full_open", "open"):
            out.append(f"交割 {delivery:.0f} 天<5 且加仓")
        if st.get("corr_state") == "broken" and positions > 1:
            out.append("相关性 broken 且多品种分散")
        if (liq.get("margin_ratio") or 0) > 0.8:
            out.append(f"保证金占用 {liq['margin_ratio']:.0%}>80%")
        return out

    @staticmethod
    def _modified_action(decision: str, action: str, danger: int, veto: bool,
                         model: str, regime: str) -> Optional[str]:
        if decision == "APPROVE":
            return None
        if decision == "REJECT":
            return "flat_only"
        if veto or danger >= 3:
            return "flat_only"
        if action in ("full_open", "open", "add") and (danger >= 2 or regime in ("choppy", "unknown")):
            return "reduce_50"
        return action

    @staticmethod
    def _binding(brief: str, decision: str, modified: Optional[str]) -> List[str]:
        pc = _section_text(brief, "POSITION_CONSTRAINTS")
        max_pos = _safe_float(_kv(pc, "max_position_pct"))
        stop = _safe_float(_kv(pc, "stop_loss_pct"))
        out: List[str] = []
        if max_pos is not None and max_pos < 100:
            out.append(f"仓位不得超过 {max_pos:.0f}%")
        if stop is not None:
            out.append(f"止损必须设在 {stop}%")
        if decision != "APPROVE":
            out.append("仅限开盘 30 分钟内执行")
            out.append("regime 变化后必须重新出 RISK_BRIEF")
        if modified:
            out.append(f"实际执行按 {modified}")
        return out

    def _cross_check(self, b: Dict[str, Any], recheck: List[Dict[str, Any]],
                     regime: str, st: Dict[str, Any], liq: Dict[str, Any],
                     danger: int) -> List[str]:
        out: List[str] = []
        choppy = b["regime"].get("choppy_score")
        rotation = b.get("sector_rotation_speed")
        if choppy is not None and rotation is not None:
            out.append(f"choppy_score={choppy:.2f} ↔ 板块轮动 {rotation:.1f}d → "
                       f"{'确认混沌' if rotation < 5 else '轮动尚可'}")
        if liq.get("oi_change_5d") is not None:
            out.append(f"流动性={liq.get('state')} ↔ OI 5d {liq['oi_change_5d']:+.0%} → "
                       f"{'资金流出' if liq['oi_change_5d'] < 0 else '资金流入'}")
        if st.get("chaos") is not None:
            out.append(f"structure_chaos={st['chaos']:.2f} ↔ 相关性 {st.get('corr_state')} → "
                       f"{'结构失效' if st.get('corr_state') == 'broken' else '结构尚存'}")
        mism = [r["metric"] for r in recheck if r["result"] == "mismatch"]
        if mism:
            out.append(f"复算不一致：{mism}")
        if danger >= 3:
            out.append(f"{danger} 个指数 danger：已要求降仓/空仓")
        return out

    def _reason(self, decision: str, rejects: List[str], modifies: List[str],
                regime: str, liq: Dict[str, Any], st: Dict[str, Any]) -> str:
        if self.llm:
            try:
                return str(self.llm(
                    f"用3-5句说明 {decision} 的理由：regime={regime}, "
                    f"流动性={liq.get('state')}, chaos={st.get('chaos')}, "
                    f"问题={rejects + modifies}。"))[:600]
            except Exception:  # noqa: BLE001
                pass
        parts = [f"判定 {decision}。"]
        if rejects:
            parts.append("必须驳回：" + "；".join(rejects) + "。")
        if modifies:
            parts.append("需要修正：" + "；".join(modifies) + "。")
        if decision == "APPROVE":
            parts.append(f"复算与一致性检查均通过（regime={regime}, "
                         f"流动性={liq.get('state')}, chaos={st.get('chaos')}）。")
        parts.append("风控官的数字未被改写；如需覆盖，请人工签字后重出 RISK_BRIEF。")
        return " ".join(parts)


# ═══════════════════════════════════════════ 审计回执协议
def render_verdict(f: Dict[str, Any]) -> str:
    v = f.get("verdict") or {}
    L: List[str] = ["=== AUDIT_VERDICT v2.0 ===",
                    f"sender: {f.get('sender', 'auditor')}",
                    f"recipient: {f.get('recipient', 'risk_manager')}",
                    f"timestamp: {f.get('timestamp')}", "", "[RECHECK]",
                    "recalculated_metrics:"]
    for r in f.get("recheck") or []:
        L.append(f"  - {r['metric']}: 自算 {r.get('recomputed')} vs 风控官 "
                 f"{r.get('reported')} → {r['result']}")
    L += ["", "[CONSISTENCY]",
          f'regime_vs_model: "{f["consistency"]["regime_vs_model"]}"',
          f'index_signals_vs_position: "{f["consistency"]["index_signals_vs_position"]}"',
          "", "[CROSS_CHECK]"]
    L += [f"- {line}" for line in (f.get("cross_check") or ["（无）"])]
    L += ["", "[VERDICT]",
          f"decision: {v.get('decision', 'REJECT')}"]
    if v.get("modified_action"):
        L.append(f"modified_action: {v['modified_action']}")
    L.append("binding_conditions:")
    L += [f'  - "{c}"' for c in (v.get("binding_conditions") or [])]
    L.append(f"override_available: {str(bool(v.get('override_available', True))).lower()}")
    L += ["", "[REASON]", f.get("reason", ""), "=== END AUDIT_VERDICT ==="]
    return "\n".join(L) + "\n"


def parse_verdict(text: str) -> Dict[str, Any]:
    m = re.search(r"^decision:\s*(\w+)", text or "", re.M)
    mod = re.search(r"^modified_action:\s*(\w+)", text or "", re.M)
    binding = re.findall(r'^\s*-\s*"(.+)"$', text or "", re.M)
    return {"decision": m.group(1) if m else None,
            "modified_action": mod.group(1) if mod else None,
            "binding_conditions": binding}