"""规则化结算账本：决策 → 持有期 → 收益 / alpha / 回撤 / 命中（零 token、零网络）。

为什么要它
---------
LLM 给出的评级（BUY/HOLD/SELL、5 档）本身不可验证；可验证的是"这个决策在持有期
结束后到底赚没赚、跑没跑赢基准"。所以结算只认数字，不认措辞：

· **结算归规则**：收益、超额、回撤、命中全部这里算，大模型不参与；
· **反思归大模型**：`reflect` 只允许引用本模块算好的数字（`facts`），不许自己编；
· **缺价不猜**：拿不到到期价就保持 `pending` 并写 `missing_reason`，绝不估算。

口径（写进 docs/SETTLEMENT.md，改口径先改文档）
-----------------------------------------------
1. **按交易日对齐**，不按自然日：到期日 = `entry_date` 在交易日序列中的位置 + `horizon_days`；
2. **方向**：LONG(+1) / SHORT(-1) / FLAT(0)；FLAT 收益记 0，不计命中；
3. **收益** = `sign * (exit / entry - 1)`；
4. **基准 alpha** = `收益 - sign * 基准收益`（做空时基准也取反，避免"做空跌市白送 alpha"）；
5. **MFE / MAE** = 持有期内相对入场价的最大有利 / 最大不利偏移（按方向，正数表示幅度）；
6. **命中** = `sign * 收益 > 0`；`|收益| < 1e-12` 记 `flat`；
7. **回撤** = 持有期内按方向的峰值回撤（`(peak - close) / peak`，做空时用反向序列）。

存储
----
`Ledger(store=...)` 里 store 只要实现 `append(record)`、`items()`、`update(key, fields)`
三个方法即可；内置 `MemoryStore`（测试/一次性）与 `JsonStore`（文件，stdlib json）。
接 DBManager 时写一个薄适配器，不要改这里的口径。

命令行
------
    python -m multi_agent.integrations.settlement <ledger.json> \
        --prices prices.json [--benchmark bench.json] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

DIRECTIONS = ("LONG", "SHORT", "FLAT")
_SIGN = {"LONG": 1.0, "SHORT": -1.0, "FLAT": 0.0, "BUY": 1.0, "SELL": -1.0, "HOLD": 0.0}
_EPS = 1e-12


class SettlementError(ValueError):
    """决策记录或行情数据不合法（字段缺失、方向非法、价格非正数）。"""


# ───────────────────────────────────────────── 价格序列
def _series(prices: Any) -> Tuple[List[str], Dict[str, float]]:
    """把两种常见行情形态统一成 `(按日期升序的交易日, {日期: 收盘})`。

    支持：
    · `{"2026-11-21": 1680.0, ...}`（日期 → 收盘）
    · `{"dates": ["2026-11-21", ...], "close": [1680.0, ...]}`（平行数组，DataHub 常见）
    · `[{"date": "...", "close": 1680.0}, ...]`
    """
    raw: Dict[str, float] = {}
    if isinstance(prices, dict):
        if "dates" in prices and "close" in prices:
            dates, close = prices["dates"], prices["close"]
            if not isinstance(dates, (list, tuple)) or not isinstance(close, (list, tuple)):
                raise SettlementError("prices.dates / prices.close 必须是数组")
            if len(dates) != len(close):
                raise SettlementError(f"prices.dates 与 prices.close 长度不一致"
                                      f"（{len(dates)} vs {len(close)}）")
            for d, c in zip(dates, close):
                raw[str(d)[:10]] = _price(c, f"prices[{d}]")
        else:
            for d, c in prices.items():
                if isinstance(c, (list, tuple, dict)):
                    raise SettlementError(f"prices[{d}] 不是单值收盘价")
                raw[str(d)[:10]] = _price(c, f"prices[{d}]")
    elif isinstance(prices, (list, tuple)):
        for bar in prices:
            if not isinstance(bar, dict):
                raise SettlementError("prices 数组元素必须是 {date, close} 对象")
            date = bar.get("date") or bar.get("trade_date") or bar.get("dt")
            if not date:
                raise SettlementError("prices 元素缺 date")
            raw[str(date)[:10]] = _price(bar.get("close") or bar.get("price"),
                                         f"prices[{date}].close")
    else:
        raise SettlementError("prices 必须是对象或数组")
    if not raw:
        raise SettlementError("prices 为空，无法结算")
    dates = sorted(raw)
    return dates, raw


def _price(value: Any, where: str) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise SettlementError(f"{where} 不是数字：{value!r}") from None
    if num != num or num <= 0:  # NaN 或非正数
        raise SettlementError(f"{where} 必须为正数（当前 {value!r}）")
    return num


def _sign(direction: str) -> float:
    key = str(direction or "").strip().upper()
    if key not in _SIGN:
        raise SettlementError(f"未知方向 {direction!r}（只认 {DIRECTIONS} 或 BUY/SELL/HOLD）")
    return _SIGN[key]


def _check_record(record: Dict[str, Any]) -> None:
    for key in ("ticker", "entry_date", "direction", "entry_price"):
        if record.get(key) in (None, ""):
            raise SettlementError(f"决策记录缺字段 {key}")
    _price(record.get("entry_price"), "entry_price")
    _sign(record.get("direction"))
    horizon = record.get("horizon_days", 5)
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 1:
        raise SettlementError(f"horizon_days 必须是 ≥1 的整数（当前 {horizon!r}）")


# ───────────────────────────────────────────── 单笔结算
def settle_one(record: Dict[str, Any], prices: Any,
               benchmark: Any = None) -> Dict[str, Any]:
    """结算一笔决策。缺价时返回 `status=pending` + `missing_reason`，不猜。"""
    _check_record(record)
    dates, close = _series(prices)
    bench_dates, bench_close = _series(benchmark) if benchmark is not None else ([], {})

    entry_date = str(record["entry_date"])[:10]
    horizon = int(record.get("horizon_days", 5))
    ticker = str(record["ticker"])
    sign = _sign(record["direction"])
    out: Dict[str, Any] = {
        "ticker": ticker,
        "direction": str(record["direction"]).upper(),
        "rating": record.get("rating"),
        "entry_date": entry_date,
        "entry_price": float(record["entry_price"]),
        "horizon_days": horizon,
        "status": "pending",
    }

    if entry_date not in close:
        out["missing_reason"] = f"entry_date {entry_date} 不在行情序列里"
        return out
    idx = dates.index(entry_date)
    exit_idx = idx + horizon
    if exit_idx >= len(dates):
        out["missing_reason"] = (f"持有 {horizon} 个交易日需要 {dates[exit_idx] if exit_idx < len(dates) else '更晚'} "
                                 f"的收盘价，当前序列只到 {dates[-1]}")
        return out

    exit_date = dates[exit_idx]
    entry = float(record["entry_price"])
    exit_price = close[exit_date]

    # 持有期内逐日收盘（含到期日）
    path = [close[d] for d in dates[idx:exit_idx + 1]]
    if any(p <= 0 for p in path):  # 防御：已由 _price 保证，这里只兜底
        raise SettlementError(f"{ticker} 持有期内出现非正收盘价")

    moved = [(p - entry) / entry * (sign if sign else 1.0) for p in path]
    ret = (exit_price / entry - 1.0) * sign
    # 峰值回撤：按方向的累计净值序列（做空时把价格取反）
    oriented = [p * (1.0 if sign >= 0 else -1.0) for p in path]
    peak, max_dd = oriented[0], 0.0
    for p in oriented:
        peak = max(peak, p)
        max_dd = max(max_dd, (peak - p) / peak)

    out.update({
        "status": "settled",
        "exit_date": exit_date,
        "exit_price": exit_price,
        "trading_days_held": horizon,
        "return": ret,
        "mfe": max(moved),
        "mae": min(moved),
        "max_drawdown": max_dd,
        "benchmark": record.get("benchmark"),
        "hit": bool(ret > 0) if sign else False,
        "flat": bool(abs(ret) < _EPS),
    })
    if sign == 0:
        out["hit"] = False
        out["note"] = "FLAT 决策不计命中，只记 0 收益"

    if bench_dates:
        bench_symbol = str(record.get("benchmark") or "benchmark")
        if entry_date not in bench_close or exit_date not in bench_close:
            out["benchmark_return"] = None
            out["missing_reason"] = (f"基准 {bench_symbol} 缺 {entry_date} 或 {exit_date} 收盘价，"
                                     f"alpha 未计算")
        else:
            bench_ret = bench_close[exit_date] / bench_close[entry_date] - 1.0
            out["benchmark_return"] = bench_ret
            out["alpha"] = ret - sign * bench_ret
    return out


def settle_all(records: Iterable[Dict[str, Any]], prices: Any,
               benchmark: Any = None) -> Dict[str, Any]:
    """结算一批记录，返回 `{settled, pending, summary}`（原记录不改）。"""
    settled: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []
    for record in records:
        one = settle_one(record, prices, benchmark)
        one["key"] = record.get("key") or _key_of(record)
        (settled if one.get("status") == "settled" else pending).append(one)
    return {"settled": settled, "pending": pending,
            "summary": stats(settled + pending)}


def _key_of(record: Dict[str, Any]) -> str:
    return f"{record.get('ticker')}@{record.get('entry_date')}#{record.get('direction')}"


# ───────────────────────────────────────────── 聚合
def stats(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """聚合：条数、命中率、平均收益/alpha、按方向与评级分组（只统计已结算）。"""
    rows = [r for r in records if isinstance(r, dict)]
    done = [r for r in rows if r.get("status") == "settled"]
    hit = [r for r in done if r.get("hit")]
    flat = [r for r in done if r.get("flat")]
    bets = [r for r in done if not r.get("flat") and _sign_direction(r.get("direction")) != 0]
    rets = [float(r.get("return") or 0.0) for r in done]
    alphas = [float(r["alpha"]) for r in done if isinstance(r.get("alpha"), (int, float))]

    def _group(key: str) -> Dict[str, Dict[str, Any]]:
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for r in done:
            buckets.setdefault(str(r.get(key) or "-"), []).append(r)
        return {k: {"count": len(v),
                    "hit_rate": _rate([x for x in v if x.get("hit")], len([x for x in v if not x.get("flat") and _sign_direction(x.get("direction")) != 0])),
                    "avg_return": _avg([float(x.get("return") or 0.0) for x in v])}
                for k, v in sorted(buckets.items())}

    return {
        "count": len(rows),
        "settled": len(done),
        "pending": len(rows) - len(done),
        "bets": len(bets),
        "win": len(hit),
        "loss": len([r for r in done if not r.get("hit") and not r.get("flat")]),
        "flat": len(flat),
        "hit_rate": _rate(hit, len(bets)),
        "avg_return": _avg(rets),
        "avg_alpha": _avg(alphas) if alphas else None,
        "avg_max_drawdown": _avg([float(r.get("max_drawdown") or 0.0) for r in done]),
        "by_direction": _group("direction"),
        "by_rating": _group("rating"),
    }


def _sign_direction(direction: Any) -> float:
    try:
        return _sign(str(direction or ""))
    except SettlementError:
        return 0.0


def _rate(part: Sequence[Any], total: int) -> Optional[float]:
    return round(len(part) / total, 4) if total else None


def _avg(values: Sequence[float]) -> Optional[float]:
    return round(sum(values) / len(values), 6) if values else None


def facts(record: Dict[str, Any]) -> Dict[str, Any]:
    """给 reflect（大模型反思）用的可核验数字。

    反思只能引用这些字段，避免大模型自己编收益或事后叙事。
    """
    return {k: record.get(k) for k in (
        "ticker", "direction", "rating", "entry_date", "exit_date",
        "entry_price", "exit_price", "return", "benchmark_return", "alpha",
        "mfe", "mae", "max_drawdown", "hit", "flat", "missing_reason",
    ) if record.get(k) is not None}


# ───────────────────────────────────────────── 存储
class MemoryStore:
    """内存账本（测试、一次性结算用）。"""

    def __init__(self) -> None:
        self._rows: Dict[str, Dict[str, Any]] = {}

    def append(self, record: Dict[str, Any]) -> str:
        key = record.get("key") or _key_of(record)
        self._rows[key] = dict(record, key=key)
        return key

    def items(self) -> List[Dict[str, Any]]:
        return [dict(v) for v in self._rows.values()]

    def update(self, key: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if key not in self._rows:
            return None
        self._rows[key].update(fields)
        return dict(self._rows[key])


class JsonStore:
    """JSON 文件账本：`{"records": [...]}`，写入用临时文件 + 原子替换。"""

    def __init__(self, path: str) -> None:
        self.path = path

    def items(self) -> List[Dict[str, Any]]:
        if not os.path.isfile(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as e:
            raise SettlementError(f"账本不是合法 JSON：{self.path}（{e}）") from None
        rows = data.get("records") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise SettlementError(f"账本格式应为 {{\"records\": [...]}}：{self.path}")
        return [r for r in rows if isinstance(r, dict)]

    def _save(self, rows: List[Dict[str, Any]]) -> None:
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".ledger-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"records": rows}, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def append(self, record: Dict[str, Any]) -> str:
        rows = self.items()
        key = record.get("key") or _key_of(record)
        for i, row in enumerate(rows):
            if (row.get("key") or _key_of(row)) == key:
                rows[i] = dict(record, key=key)
                self._save(rows)
                return key
        rows.append(dict(record, key=key))
        self._save(rows)
        return key

    def update(self, key: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        rows = self.items()
        for i, row in enumerate(rows):
            if (row.get("key") or _key_of(row)) == key:
                merged = dict(row)
                merged.update({k: v for k, v in fields.items() if k != "key"})
                merged["key"] = key
                rows[i] = merged
                self._save(rows)
                return dict(merged)
        return None


class Ledger:
    """决策账本：录入 pending → 结算 → 聚合。

    ```python
    led = Ledger(JsonStore("ledger.json"))
    led.add({"ticker": "600519", "entry_date": "2026-11-21", "direction": "LONG",
             "entry_price": 1680.0, "horizon_days": 5, "benchmark": "000300"})
    out = led.settle(prices, benchmark_prices)   # 只结算能算的，其余保持 pending
    out["summary"]["hit_rate"]
    ```
    """

    def __init__(self, store: Any = None) -> None:
        self.store = store or MemoryStore()

    def add(self, record: Dict[str, Any]) -> str:
        """录入决策（不做结算）。字段不合法直接抛错，不静默收下。"""
        _check_record(record)
        row = dict(record)
        row.setdefault("horizon_days", 5)
        row.setdefault("status", "pending")
        row["key"] = row.get("key") or _key_of(row)
        return self.store.append(row)

    def pending(self) -> List[Dict[str, Any]]:
        return [r for r in self.store.items() if r.get("status") != "settled"]

    def settle(self, prices: Any, benchmark: Any = None) -> Dict[str, Any]:
        """结算所有 pending；已结算的不重算（口径变了要显式 reset）。"""
        fresh: List[Dict[str, Any]] = []
        for row in self.pending():
            done = settle_one(row, prices, benchmark)
            done["key"] = row.get("key") or _key_of(row)
            self.store.update(done["key"], done)
            fresh.append(done)
        rows = self.store.items()
        return {"settled": [r for r in fresh if r.get("status") == "settled"],
                "pending": [r for r in rows if r.get("status") != "settled"],
                "summary": stats(rows)}

    def summary(self) -> Dict[str, Any]:
        return stats(self.store.items())


# ───────────────────────────────────────────── 反思（只喂事实，不喂结论）
def reflect_prompt(record: Dict[str, Any]) -> str:
    """拼给大模型的反思提示：只放可核验数字，禁止它自己推收益。"""
    f = facts(record)
    lines = [f"- {k}: {v}" for k, v in f.items() if v is not None]
    return ("下面是一笔已结算决策的可核验数字。请只基于这些数字写 2-3 条教训，"
            "不要推断未给出的价格、不要编造收益，也不要评价模型能力：\n"
            + "\n".join(lines))


# ───────────────────────────────────────────── CLI
def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m multi_agent.integrations.settlement",
        description="规则化结算账本：算收益/alpha/回撤/命中，不调模型")
    parser.add_argument("ledger", help="账本 JSON（不存在则视为空账本）")
    parser.add_argument("--prices", required=True, help="标的行情 JSON")
    parser.add_argument("--benchmark", help="基准行情 JSON（给了才算 alpha）")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON")
    args = parser.parse_args(argv)

    try:
        with open(args.prices, "r", encoding="utf-8") as fh:
            prices = json.load(fh)
        benchmark = None
        if args.benchmark:
            with open(args.benchmark, "r", encoding="utf-8") as fh:
                benchmark = json.load(fh)
        ledger = Ledger(JsonStore(args.ledger))
        out = ledger.settle(prices, benchmark)
    except (OSError, SettlementError, json.JSONDecodeError) as e:
        print(f"结算失败：{type(e).__name__}: {e}")
        return 2

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    s = out["summary"]
    print(f"账本：{args.ledger}")
    print(f"  总记录 {s['count']} / 已结算 {s['settled']} / 待结算 {s['pending']}")
    rate = "—" if s["hit_rate"] is None else f"{s['hit_rate'] * 100:.1f}%"
    print(f"  命中率 {rate}（胜 {s['win']} / 负 {s['loss']} / 平 {s['flat']}）")
    print(f"  平均收益 {_pct(s['avg_return'])}  平均 alpha {_pct(s['avg_alpha'])}"
          f"  平均最大回撤 {_pct(s['avg_max_drawdown'])}")
    for r in out["settled"]:
        print(f"  ✓ {r['ticker']} {r['direction']} {r['entry_date']}→{r['exit_date']}"
              f"  收益 {_pct(r['return'])}  alpha {_pct(r.get('alpha'))}")
    for r in out["pending"]:
        print(f"  … {r['ticker']} 仍待结算：{r.get('missing_reason')}")
    return 0


def _pct(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"{value * 100:.2f}%"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())