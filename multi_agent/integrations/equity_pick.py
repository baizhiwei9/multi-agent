"""行业多空 → 名单 → 仓位：把辩论结论落成可执行组合（组合层）。

为什么单独一层
--------------
`debate` 只回答"这个行业看多还是看空"，`adversary` 只回答"这条证据站哪边"。
"买哪几个、各买多少、留多少现金"是第三件事：要跨行业比较、要做权重上限、
要和现金比。把它塞进辩论里会让辩论既算逻辑又算仓位，两头都不干净。

流程（默认全程规则，零 token）
------------------------------
    行业日线 → 每行业 5 条证据（动量/回撤/位置/波动）→ 规则对手打分
        → 多空差 ≥ min_margin 才判 BULL → 取 (多-空) 最强的 top_n
        → 权重 = (多-空) 归一化，单行业封顶 max_weight，余下为现金

指标只用收盘价
--------------
沙箱里只有行业指数收盘（`sw_l1_daily.csv`），所以位置、波动都是价格派生，
**不是估值分位、也不是真实资金流**。返回里写 `metrics_source: "close-only"`，
免得后面把"价格高位"当"估值贵"用。要换真口径，把 evidence 按同样结构传进来即可
（`pick(params={"evidence": {...}})`），组合层不用改。
"""
from __future__ import annotations

import csv
import math
from typing import Any, Dict, List, Optional, Tuple

from . import adversary

MIN_MARGIN = 0.15        # 多空差低于这个就不算有方向（与 adversary 的规则裁决一致）
LOOKBACK = 250           # 位置/波动分位的回看窗口（交易日）


# ───────────────────────────────────────────── 数据
def load_series(csv_path: str) -> Dict[str, List[Tuple[str, float]]]:
    """读行业日线 CSV（列：date,close,name,code）→ {行业: [(日期, 收盘)]}。

    只认这三个列，其它列忽略；同一行业按日期升序，重复日期保留最后一条。
    """
    series: Dict[str, Dict[str, float]] = {}
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("name") or row.get("行业") or "").strip()
            date = (row.get("date") or row.get("trade_date") or "").strip()
            raw = row.get("close") or row.get("收盘")
            if not name or not date or raw is None:
                continue
            try:
                close = float(raw)
            except (TypeError, ValueError):
                continue
            series.setdefault(name, {})[date] = close
    return {name: sorted(points.items()) for name, points in series.items()}


def metrics(series: List[Tuple[str, float]], as_of: Optional[str] = None) -> Dict[str, Any]:
    """单个行业的全部指标；数据不足时返回 `{"ok": False, "reason": ...}`。"""
    points = [p for p in series if as_of is None or p[0] <= as_of] if series else []
    closes = [p[1] for p in points]
    if len(closes) < 62:
        return {"ok": False, "reason": f"数据不足（{len(closes)} 条，至少 62 条）"}
    last = closes[-1]
    win = closes[-LOOKBACK:]
    vol_series = _rolling_vol(closes)
    vol_now = vol_series[-1]
    return {
        "ok": True,
        "as_of": points[-1][0],
        "n": len(closes),
        "close": round(last, 2),
        "mom20": last / closes[-21] - 1 if len(closes) > 21 else 0.0,
        "mom60": last / closes[-61] - 1,
        "dd60": last / max(closes[-60:]) - 1,
        "pos250": ((last - min(win)) / (max(win) - min(win))) if max(win) > min(win) else 0.5,
        "vol20": vol_now,
        "vol_pct": _rank(vol_series[-LOOKBACK:], vol_now),
    }


def _rolling_vol(closes: List[float], window: int = 20) -> List[float]:
    out = [0.0] * len(closes)
    for i in range(window, len(closes)):
        rets = [closes[j] / closes[j - 1] - 1 for j in range(i - window + 1, i + 1)]
        mean = sum(rets) / len(rets)
        out[i] = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))
    return out


def _rank(values: List[float], current: float) -> float:
    """当前值在历史里的分位（0~1）。"""
    vals = [v for v in values if v > 0]
    if not vals:
        return 0.5
    return sum(1 for v in vals if v <= current) / len(vals)


# ───────────────────────────────────────────── 证据
def _sig(value: float, bull_at: float, bear_at: float) -> str:
    if value >= bull_at:
        return "bullish"
    if value <= bear_at:
        return "bearish"
    return "neutral"


def _sig_inv(value: float, bull_at: float, bear_at: float) -> str:
    """反向指标：越低越是多头证据（位置高=拥挤、波动大=风险）。"""
    if value <= bull_at:
        return "bullish"
    if value >= bear_at:
        return "bearish"
    return "neutral"


def evidence_for(m: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """把指标翻成带方向与权重的证据条（权重是"这条有多算数"，不是概率）。"""
    return {
        "20日动量": {"value": f"{m['mom20']:+.1%}", "signal": _sig(m["mom20"], 0.02, -0.02),
                 "weight": 1.0},
        "60日动量": {"value": f"{m['mom60']:+.1%}", "signal": _sig(m["mom60"], 0.05, -0.05),
                 "weight": 1.2},
        "60日回撤": {"value": f"{m['dd60']:+.1%}", "signal": _sig(m["dd60"], -0.02, -0.08),
                 "weight": 1.0},
        # 位置越高=越拥挤（空头证据），波动越大=风险越高（空头证据）→ 反向判
        "250日位置": {"value": f"{m['pos250']:.2f}",
                  "signal": _sig_inv(m["pos250"], 0.30, 0.90), "weight": 0.8},
        "波动分位": {"value": f"{m['vol_pct']:.2f}",
                 "signal": _sig_inv(m["vol_pct"], 0.30, 0.80), "weight": 0.6},
    }


# ───────────────────────────────────────────── 组合
def _cap_weights(raw: Dict[str, float], cap: float) -> Dict[str, float]:
    """归一化后单行业封顶；超出部分按剩余比例分给没到顶的，迭代到稳定。

    已封顶的标的进入 `capped` 集合后不再参与后续重分配，避免"封顶→又被当 free
    压低→权重和缩水"的震荡；迭代终止于没有新标的触顶或剩余无可分。
    """
    total = sum(raw.values())
    if total <= 0:
        return {}
    weights = {k: v / total for k, v in raw.items()}
    capped: set = set()
    for _ in range(10):
        over = {k: w for k, w in weights.items()
                if k not in capped and w > cap + 1e-12}
        if not over:
            break
        for k in over:
            weights[k] = cap
            capped.add(k)
        fixed = sum(weights[k] for k in capped)
        free = [k for k in weights if k not in capped]
        rest = 1.0 - fixed
        free_total = sum(raw[k] for k in free)
        if not free or free_total <= 0 or rest <= 1e-12:
            break
        for k in free:
            weights[k] = rest * raw[k] / free_total
    return {k: round(v, 4) for k, v in weights.items()}


def pick(params: Dict[str, Any], client: Any = None) -> Dict[str, Any]:
    """多空辩论驱动的行业选择 + 仓位。

    params：
        `csv_path`（行业日线）或 `evidence`（{行业: 证据条}，自己算好传进来）
        `as_of`（截止日，默认数据最后一天）、`top_n`（默认 5）
        `max_weight`（单行业上限，默认 0.25）、`min_margin`（默认 0.15）
        `profile`（默认 equity）、`industry`（只跑指定行业，调试用）
        `data_ref`（{capability, action, params}）：自己不读文件，让取数角色去取
            （编排层只决定"要哪些标的"，取数与计算都在 `data` 角色里，零 token）
    """
    top_n = max(1, int(params.get("top_n") or 5))
    max_weight = min(1.0, max(0.0, float(params.get("max_weight") or 0.25)))
    gross = min(1.0, max(0.0, float(params.get("gross") if params.get("gross") is not None
                                    else 1.0)))
    min_margin = float(params.get("min_margin") or MIN_MARGIN)
    profile = str(params.get("profile") or "equity")
    only = params.get("industry")

    evidence = params.get("evidence")
    as_of = params.get("as_of")
    data_ref = params.get("data_ref")
    if evidence is None and isinstance(data_ref, dict):
        if client is None:
            return {"ok": False, "error": "data_ref 需要 client（由 register(runtime) 注入）"}
        ref = dict(data_ref)
        cap = str(ref.get("capability") or "data")
        action = str(ref.get("action") or "evidence")
        dparams = ref.get("params") if isinstance(ref.get("params"), dict) else {
            k: v for k, v in ref.items() if k not in ("capability", "action", "timeout_s")}
        try:
            task = client.submit(cap, action, dparams, wait=True,
                                 timeout_s=float(ref.get("timeout_s") or 120))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"取数失败：{type(e).__name__}: {e}"}
        if not isinstance(task, dict) or task.get("state") != "succeeded":
            return {"ok": False, "error": f"取数角色 {cap}/{action} 失败："
                                          f"{(task or {}).get('error')}"}
        result = task.get("result") or {}
        if not result.get("ok"):
            return {"ok": False, "error": f"取数未成功：{result.get('error') or result}"}
        evidence = result.get("evidence")
        as_of = as_of or result.get("as_of")
    if evidence is None:
        csv_path = params.get("csv_path")
        if not csv_path:
            return {"ok": False, "error": "pick 需要 csv_path 或 evidence"}
        series = load_series(csv_path)
        evidence, stamps = {}, []
        for name, points in series.items():
            m = metrics(points, as_of)
            if not m.get("ok"):
                continue
            evidence[name] = evidence_for(m)
            stamps.append(m["as_of"])
            evidence[name]["_close"] = m["close"]
            evidence[name]["_mom60"] = round(m["mom60"], 4)
        as_of = as_of or (max(stamps) if stamps else None)
    else:
        evidence = {k: dict(v) for k, v in evidence.items() if isinstance(v, dict)}

    rows = []
    for name, ev in evidence.items():
        if only and name != only:
            continue
        clean = {k: v for k, v in ev.items() if not k.startswith("_")}
        bull = adversary.argue({"side": "pro", "profile": profile, "evidence": clean})
        bear = adversary.argue({"side": "con", "profile": profile, "evidence": clean})
        diff = round(bull["score"] - bear["score"], 2)
        total = bull["score"] + bear["score"]
        margin = abs(diff) / total if total > 0 else 0.0
        stance = "NEUTRAL"
        if diff > 0 and margin >= min_margin:
            stance = "BULL"
        elif diff < 0 and margin >= min_margin:
            stance = "BEAR"
        rows.append({"name": name, "stance": stance, "bull": bull["score"],
                     "bear": bear["score"], "diff": diff, "margin": round(margin, 2),
                     "close": ev.get("_close"), "mom60": ev.get("_mom60"),
                     # 让人能看依据：多头最强的两条 + 空头最强的一条
                     "why": bull["points"][:2], "against": bear["points"][:1]})

    rows.sort(key=lambda r: (-r["diff"], r["name"]))
    picked = [r for r in rows if r["stance"] == "BULL"][:top_n]
    weights = _cap_weights({r["name"]: max(r["diff"], 1e-6) for r in picked}, max_weight)
    weights = {k: round(v * gross, 4) for k, v in weights.items()}   # 按总仓上限缩放
    for r in rows:
        r["weight"] = weights.get(r["name"], 0.0)
    cash = round(max(0.0, 1.0 - sum(weights.values())), 4)
    return {"ok": True, "as_of": as_of, "metrics_source": "close-only",
            "profile": profile, "top_n": top_n, "max_weight": max_weight,
            "gross": gross, "min_margin": min_margin,
            "picks": [r for r in rows if r["weight"] > 0],
            "all": rows, "weights": weights, "cash": cash,
            "n_industries": len(rows),
            "note": ("只做多、按多空差分配权重；指标取自行业指数收盘，"
                     "不含估值分位与真实资金流")}


#: 组合流水线：选行业（pick）→ 送审台账（结论留痕）
PIPELINE: Dict[str, Any] = {
    "name": "equity_pick",
    "description": "行业多空辩论 → 名单 → 仓位（只做多，权重按多空差归一化并封顶）",
    "steps": [
        {"name": "pick", "capability": "equity_pick", "action": "pick",
         "params": {"csv_path": "${params.csv_path}",
                    "as_of": "${params.as_of}",
                    "top_n": "${params.top_n}",
                    "max_weight": "${params.max_weight}",
                    "gross": "${params.gross}"},
         "timeout_s": 300},
        {"name": "ledger", "capability": "supervisor", "action": "ledger",
         "params": {"entries": "${steps.pick.result}"}, "depends_on": ["pick"]},
    ],
}


# ───────────────────────────────────────────── Hub 接入
def run_action(action: str, params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None,
               client: Any = None) -> Dict[str, Any]:
    if action == "pick":
        return pick(params or {}, client=client)
    raise ValueError(f"equity_pick 不支持的动作：{action}")


def register(runtime, client: Any = None) -> None:
    """注册组合层；`client` 用来执行 `data_ref`（让取数角色去取，不在这里读文件）。"""
    hub_client = client if client is not None else getattr(runtime, "client", None)
    runtime.add_handler("equity_pick", "pick",
                        lambda p, c: run_action("pick", p, c, client=hub_client))