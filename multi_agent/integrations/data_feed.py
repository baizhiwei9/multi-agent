"""取数角色（capability = `data`）：大模型只说"要什么"，取数在这里做，零 token。

为什么要单独一个角色
--------------------
估值分位、资金流、库存、供需这些是**取数**问题，不是推理问题。让模型去"记住"或
"估算"这些数字，既贵又容易编。正确分工是：

    编排层/模型：从 `catalog` 里挑要哪几项（指标名 + 标的）
        → `data/evidence` 去本地库取、算、打包成证据条（零 token）
        → 多空辩论与裁决只读证据条

各领域绑各自的实现，能力名一致：

| 领域 | 角色 | 取什么 |
| --- | --- | --- |
| 股票 | 分析师 | 行情、估值分位、资金流 |
| 期货 | 平衡表 | 库存、供需、基差、开工 |
| 游戏 | 数值策划 | 数值表、成长曲线、掉落期望 |

本文件给的是**通用默认实现**：从工作区 DataHub 的本地缓存读日线（`indices/*.json`、
`stocks/*.csv`），算动量/回撤/位置/波动分位。缺的数据源会明说缺，而不是补零。

动作
----
· `catalog`：能取什么、缺什么（供编排层决定要哪几项）
· `metrics`：{market, code} → 指标原值（含 as_of）
· `evidence`：{market, code|codes} → 证据条（直接喂 adversary / equity_pick）
"""
from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .equity_pick import evidence_for, metrics as price_metrics

#: 指标名 → 说明（catalog 里回给编排层，模型不必猜字段）
ITEMS: Dict[str, str] = {
    "mom20": "20 日动量",
    "mom60": "60 日动量",
    "dd60": "距 60 日高点回撤",
    "pos250": "250 日区间位置（越高越拥挤）",
    "vol_pct": "20 日波动率在 250 日内的分位（越高风险越大）",
}

#: 仍然缺的域（DataHub 也没有）：明说缺失，调用方据此决定要不要另接源
MISSING: Dict[str, str] = {
    "valuation_pb": "PB 历史分位：巨潮行业口径只给静态 PE，要 PB 得另接个股/行业 PB 源",
    "moneyflow_realtime": "盘中实时资金流：东财 push2 限流时不补抓，只用库内定稿数据",
}

DEFAULT_ROOT = os.environ.get("DATAHUB_ROOT") or "/home/work/dumate/5eaa88f7d2d5403f9ae855f5fcfd7f34/workspace/datahub"

#: DataHub 主仓库（datahub-multi-tier）。估值/库存/资金流都落在这里的持久层，
#: 取数优先读库；只有库里没有时才轮到本地 CSV 缓存（价格域）或采集器实时抓。
DEFAULT_HUB = os.environ.get("DATAHUB_PATH") or (
    "/home/work/dumate/5eaa88f7d2d5403f9ae855f5fcfd7f34/workspace/projects/datahub-multi-tier")

#: 口径限制（不是缺数据，是口径只有这么细；catalog 里照实说）
GAPS = {
    "valuation": "证监会行业口径，只有静态 PE（无 PB）；分位按库内历史自算",
    "moneyflow": "东财板块资金流，盘中限流时读库内最近一日，不做实时补抓",
    "inventory": "东财期货库存，源只给约 3 个月窗口，历史靠日更累积",
}


def _root(params: Dict[str, Any]) -> Path:
    return Path(str(params.get("datahub_root") or DEFAULT_ROOT))


def _read_json_series(path: Path) -> Optional[List[Tuple[str, float]]]:
    """DataHub 的 index/concept 缓存是 [{trade_date, close}, ...]。"""
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        date = str(row.get("trade_date") or row.get("date") or "")
        close = row.get("close")
        if date and isinstance(close, (int, float)):
            out.append((date, float(close)))
    return sorted(out) or None


def _read_csv_series(path: Path) -> Optional[List[Tuple[str, float]]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return None
    out = []
    for row in rows:
        date = str(row.get("trade_date") or row.get("date") or "")
        close = row.get("close")
        if date and close not in (None, ""):
            try:
                out.append((date, float(close)))
            except ValueError:
                continue
    return sorted(out) or None


def series(params: Dict[str, Any]) -> Dict[str, Any]:
    """按 market/code 找本地缓存序列；找不到就说找不到，不编。"""
    market = str(params.get("market") or "index").strip().lower()
    code = str(params.get("code") or "").strip()
    if not code:
        return {"ok": False, "error": "series 需要 code"}
    root = _root(params)
    if market == "index":
        cands = [root / "indices" / f"{code}.json", root / "concepts" / f"{code}.json"]
        for path in cands:
            pts = _read_json_series(path)
            if pts:
                return {"ok": True, "market": market, "code": code, "points": pts,
                        "source": str(path)}
    elif market == "stock":
        for path in (root / "stocks" / f"{code}.csv", root / "stocks" / f"{code}.json"):
            pts = _read_csv_series(path) if path.suffix == ".csv" else _read_json_series(path)
            if pts:
                return {"ok": True, "market": market, "code": code, "points": pts,
                        "source": str(path)}
    return {"ok": False, "error": f"本地缓存没有 {market}/{code}（目录 {root}）",
            "hint": "可先在有网环境用 DataHub 抓取写入缓存，或改 params.datahub_root"}


def _hub_path(params: Dict[str, Any]) -> str:
    return str(params.get("datahub_path") or DEFAULT_HUB)


def _persisted(params: Dict[str, Any]):
    """拿 DataHub 的持久层读函数；环境缺 pandas/data_hub 时明确失败，不静默。"""
    root = _hub_path(params)
    if not os.path.isdir(root):
        raise RuntimeError(f"DataHub 仓库不存在：{root}（检查 DATAHUB_PATH 或 params.datahub_path）")
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from data_hub.core.persist import load_persisted
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"DataHub 持久层不可用（{root}）：{type(e).__name__}: {e}") from e
    return load_persisted


def _rows(params: Dict[str, Any], data_type: str, start: Optional[str] = None,
          end: Optional[str] = None, series_key: Optional[str] = None):
    """读库内定稿行（final_only=True），返回 list[dict]。"""
    load = _persisted(params)
    df = load(data_type, start=start, end=end, series_key=series_key, final_only=True)
    if df is None or len(df) == 0:
        return []
    return df.to_dict("records")


#: 分位最少要多少个样本才给（低于这个数不给分位，避免 "1 个样本=100% 分位" 的假精确）
MIN_PCT_SAMPLES = 60


def _pct_rank(values: List[float], current: float,
              min_samples: int = MIN_PCT_SAMPLES) -> Optional[float]:
    vals = [v for v in values if isinstance(v, (int, float))]
    if len(vals) < min_samples:
        return None
    return round(sum(1 for v in vals if v <= current) / len(vals), 3)


def catalog(params: Dict[str, Any]) -> Dict[str, Any]:
    """能取什么：域、指标、已有多少标的、缺什么（缺的明说，不补零）。"""
    root = _root(params)
    counts = {d: 0 for d in ("index", "stock", "concept")}
    for domain, sub, pattern in (("index", "indices", "*.json"), ("stock", "stocks", "*.csv"),
                                 ("concept", "concepts", "*.json")):
        folder = root / sub
        if folder.is_dir():
            counts[domain] = len(list(folder.glob(pattern)))
    available = {"index": counts["index"], "stock": counts["stock"],
                 "concept": counts["concept"], "valuation": 0, "moneyflow": 0,
                 "inventory": 0}
    store = ""
    try:
        root_hub = _hub_path(params)
        if root_hub not in sys.path:
            sys.path.insert(0, root_hub)
        from data_hub.core.persist import stats as hub_stats
        st = hub_stats()
        store = str(st.get("db_path") or "")
        types = st.get("types") or {}
        for name in ("industry_valuation", "sector_flow", "futures_inventory"):
            key = {"industry_valuation": "valuation", "sector_flow": "moneyflow",
                   "futures_inventory": "inventory"}[name]
            available[key] = int(types.get(name, 0))
    except Exception as e:  # noqa: BLE001
        store = f"不可用：{type(e).__name__}: {e}"
    return {"ok": True, "root": str(root), "hub": _hub_path(params), "store": store,
            "items": ITEMS, "gaps": GAPS, "missing": MISSING,
            "available": available,
            "note": "库优先：估值/资金流/库存读 DataHub 持久层（定稿行）；"
                    "价格域读本地缓存；都不发网络请求"}


def metrics(params: Dict[str, Any]) -> Dict[str, Any]:
    """单个标的的价格派生指标（零 token）。"""
    got = series(params)
    if not got.get("ok"):
        return got
    m = price_metrics(got["points"], params.get("as_of"))
    if not m.get("ok"):
        return {"ok": False, "error": m.get("reason"), "code": got["code"]}
    m.update({"market": got["market"], "code": got["code"], "source": got["source"]})
    return {"ok": True, **m}


def evidence(params: Dict[str, Any]) -> Dict[str, Any]:
    """批量出证据条：{名字: {指标: {value, signal, weight}}}，直接喂辩论。

    `codes` 传代码列表，`names` 传 {code: 名字} 做显示名（行业名/标的名）。
    """
    codes = params.get("codes") or ([params["code"]] if params.get("code") else [])
    names = params.get("names") if isinstance(params.get("names"), dict) else {}
    if not codes:
        return {"ok": False, "error": "evidence 需要 code 或 codes"}
    out: Dict[str, Any] = {}
    failed: Dict[str, str] = {}
    stamps = []
    for code in codes:
        one = dict(params, code=code)
        m = metrics(one)
        if not m.get("ok"):
            failed[str(code)] = str(m.get("error"))
            continue
        label = str(names.get(code) or code)
        ev = evidence_for(m)
        ev["_close"] = m.get("close")
        ev["_mom60"] = round(float(m.get("mom60") or 0.0), 4)
        out[label] = ev
        stamps.append(m.get("as_of"))
    if not out:
        return {"ok": False, "error": "没有任何标的取到数据", "failed": failed}
    # `_close/_mom60` 是给组合层看的旁注；辩论侧会过滤掉下划线字段，不进证据
    return {"ok": True, "evidence": out, "as_of": max(stamps) if stamps else None,
            "n": len(out), "failed": failed, "market": params.get("market") or "index"}


def valuation(params: Dict[str, Any]) -> Dict[str, Any]:
    """行业估值与分位（证监会口径，读库）。`industry` 支持行业名或编码。"""
    want = str(params.get("industry") or params.get("code") or "").strip()
    try:
        rows = _rows(params, "industry_valuation")
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    if not rows:
        return {"ok": False, "error": "库内没有 industry_valuation 定稿行",
                "hint": "先跑采集：get_data('industry_valuation', date=...)"}
    picked = []
    for r in rows:
        payload = json.loads(r.get("payload") or "{}")
        if want and not (want.upper() == str(payload.get("code", "")).upper()
                         or want in str(payload.get("industry", ""))):
            continue
        picked.append({"date": r.get("obs_date"), "code": payload.get("code"),
                       "industry": payload.get("industry"), "level": payload.get("level"),
                       "pe_weighted": payload.get("pe_weighted"),
                       "pe_median": payload.get("pe_median"),
                       "company_count": payload.get("company_count")})
    if not picked:
        return {"ok": False, "error": f"库内没有匹配 {want!r} 的行业估值"}
    latest_date = max(p["date"] for p in picked)
    out = []
    for p in picked:
        hist = [q["pe_weighted"] for q in picked
                if q["code"] == p["code"] and isinstance(q["pe_weighted"], (int, float))]
        pct = _pct_rank(hist, p["pe_weighted"]) if isinstance(p["pe_weighted"], (int, float)) else None
        out.append({**p, "pe_pct": pct, "n_history": len(hist),
                    "pct_note": (f"历史样本 {len(hist)} 个，不足 {MIN_PCT_SAMPLES}，分位不可用"
                                 if pct is None else None)})
    return {"ok": True, "as_of": latest_date, "rows": out, "n": len(out),
            "metrics_source": "datahub:industry_valuation", "has_pb": False}


def inventory(params: Dict[str, Any]) -> Dict[str, Any]:
    """期货库存与分位（读库）。`symbol` 必填。"""
    symbol = str(params.get("symbol") or "").strip()
    if not symbol:
        return {"ok": False, "error": "inventory 需要 symbol（如 沪铜）"}
    try:
        rows = _rows(params, "futures_inventory", series_key=symbol)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    if not rows:
        return {"ok": False, "error": f"库内没有 {symbol} 的库存定稿行",
                "hint": "先跑采集：get_data('futures_inventory', symbol=...)"}
    series = []
    for r in rows:
        payload = json.loads(r.get("payload") or "{}")
        if isinstance(payload.get("inventory"), (int, float)):
            series.append({"date": r.get("obs_date"), "inventory": payload["inventory"],
                           "change": payload.get("change")})
    series.sort(key=lambda x: x["date"])
    if not series:
        return {"ok": False, "error": f"{symbol} 库内库存值不可解析"}
    last = series[-1]
    pct = _pct_rank([s["inventory"] for s in series], last["inventory"])
    chg20 = (last["inventory"] - series[-21]["inventory"]) if len(series) > 21 else None
    return {"ok": True, "symbol": symbol, "as_of": last["date"],
            "inventory": last["inventory"], "change": last.get("change"),
            "change_20d": chg20, "inv_pct": pct, "n_history": len(series),
            "pct_note": (f"分位窗口仅 {len(series)} 个交易日（源只给约 3 个月，历史靠日更累积）"
                         if pct is not None else
                         f"历史样本 {len(series)} 个，不足 {MIN_PCT_SAMPLES}，分位不可用"),
            "metrics_source": "datahub:futures_inventory"}


def moneyflow(params: Dict[str, Any]) -> Dict[str, Any]:
    """板块资金流（读库最近一日；盘中不补抓）。`sector` 可选，按名称过滤。"""
    want = str(params.get("sector") or "").strip()
    try:
        rows = _rows(params, "sector_flow")
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    if not rows:
        return {"ok": False, "error": "库内没有 sector_flow 定稿行",
                "hint": "东财限流时只能等收盘后采集；或改用北向 northbound_flow"}
    latest = max(r.get("obs_date") or "" for r in rows)
    items = []
    for r in rows:
        if (r.get("obs_date") or "") != latest:
            continue
        payload = json.loads(r.get("payload") or "{}")
        name = str(payload.get("名称") or payload.get("板块") or payload.get("name") or "")
        if want and want not in name:
            continue
        items.append({"name": name, "payload": payload})
    return {"ok": True, "as_of": latest, "n": len(items), "rows": items,
            "metrics_source": "datahub:sector_flow",
            "note": "字段随东财口径，未做统一改列"}


# ───────────────────────────────────────────── Hub 接入
def run_action(action: str, params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None
               ) -> Dict[str, Any]:
    if action == "catalog":
        return catalog(params or {})
    if action == "series":
        return series(params or {})
    if action == "metrics":
        return metrics(params or {})
    if action == "evidence":
        return evidence(params or {})
    if action == "valuation":
        return valuation(params or {})
    if action == "inventory":
        return inventory(params or {})
    if action == "moneyflow":
        return moneyflow(params or {})
    raise ValueError(f"data 不支持的动作：{action}")


def register(runtime) -> None:
    """把一个 Agent 注册成取数角色（零 token）。

    ```python
    from multi_agent.integrations.data_feed import register
    register(runtime)      # 动作：data/catalog、data/series、data/metrics、data/evidence
    ```
    """
    for action in ("catalog", "series", "metrics", "evidence",
                   "valuation", "inventory", "moneyflow"):
        runtime.add_handler("data", action, lambda p, c, a=action: run_action(a, p, c))