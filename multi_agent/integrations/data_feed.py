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

#: 已知但本实现取不到的域：明说缺失，调用方据此决定要不要另接数据源
MISSING: Dict[str, str] = {
    "valuation": "估值分位（PE/PB 历史分位）：DataHub 未收录，需接估值源",
    "moneyflow": "资金流（主力/北向净流入）：DataHub 未收录，需接资金流源",
    "inventory": "库存与供需（期货口径）：需接平衡表采集器",
}

DEFAULT_ROOT = os.environ.get("DATAHUB_ROOT") or "/home/work/dumate/5eaa88f7d2d5403f9ae855f5fcfd7f34/workspace/datahub"


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


def catalog(params: Dict[str, Any]) -> Dict[str, Any]:
    """能取什么：域、指标、已有多少标的、缺什么（缺的明说，不补零）。"""
    root = _root(params)
    counts = {d: 0 for d in ("index", "stock", "concept")}
    for domain, sub, pattern in (("index", "indices", "*.json"), ("stock", "stocks", "*.csv"),
                                 ("concept", "concepts", "*.json")):
        folder = root / sub
        if folder.is_dir():
            counts[domain] = len(list(folder.glob(pattern)))
    return {"ok": True, "root": str(root), "items": ITEMS, "missing": MISSING,
            "available": {"index": counts["index"], "stock": counts["stock"],
                          "concept": counts["concept"]},
            "note": "本角色只读本地缓存，不发网络请求；要新数据先由抓取任务写入缓存"}


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
    raise ValueError(f"data 不支持的动作：{action}")


def register(runtime) -> None:
    """把一个 Agent 注册成取数角色（零 token）。

    ```python
    from multi_agent.integrations.data_feed import register
    register(runtime)      # 动作：data/catalog、data/series、data/metrics、data/evidence
    ```
    """
    for action in ("catalog", "series", "metrics", "evidence"):
        runtime.add_handler("data", action, lambda p, c, a=action: run_action(a, p, c))