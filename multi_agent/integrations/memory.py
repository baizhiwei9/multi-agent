"""决策记忆（memory）：把每次判断记下来，到期用真实行情结算，再把教训带回下一次分析。

为什么单独一个角色
-------------------
TradingAgents 最值钱的不是它的角色分工，而是**记忆闭环**：每次决策落盘，后续
run 结算已过持有期的收益与相对基准 alpha，生成一段反思，下次分析先读最近决策。
我们这边以前跑完只写台账和报告，从不回看“上次那个判断最后对不对”——
`ledger` 记的是送审与回执，不是判断与结果，两者互补。

动作
----
· `record` —— 记录一条决策（标的 / 方向 / 持有期 / 依据摘要 / run_id / 模型版本 / 当时价格）
· `settle` —— 对到期决策用调用方给的真实行情结算：收益、相对基准 alpha、反思文本
· `recall` —— 按标的召回最近决策（含已结算结果），并可选带其它标的的教训

红线
----
- 取数不在本模块做：行情由调用方（DataHub / 流水线上游）取好后传入，memory 只结算；
- 结算必须用**持有期结束后**的价格，到期日价格缺失就跳过（报 missing_price），
  绝不拿决策日或更早的价格回填——否则就是前视；
- 反思文本由规则生成并记录模型版本，保证可复现；不偷偷调用大模型。
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ACTIONS: tuple = ("record", "settle", "recall")

DEFAULT_DB = "~/.multi_agent/memory.db"
_DIRECTIONS = ("long", "short", "flat")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    horizon_days INTEGER NOT NULL,
    basis TEXT DEFAULT '',
    run_id TEXT DEFAULT '',
    model TEXT DEFAULT '',
    as_of TEXT NOT NULL,
    price_at REAL,
    status TEXT NOT NULL DEFAULT 'open',
    settled_at TEXT,
    due_date TEXT,
    price_end REAL,
    price_end_date TEXT,
    realized_return REAL,
    benchmark_return REAL,
    alpha REAL,
    reflection TEXT DEFAULT '',
    created REAL NOT NULL,
    UNIQUE(run_id, symbol, as_of)
);
CREATE INDEX IF NOT EXISTS idx_decisions_symbol ON decisions(symbol, as_of DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status);
"""


# ---------- 基础设施 ----------
def _db_path(params: Dict[str, Any]) -> Path:
    raw = str(params.get("db_path") or DEFAULT_DB)
    path = Path(raw).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _conn(params: Dict[str, Any]) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path(params)), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _today(params: Dict[str, Any]) -> str:
    raw = str(params.get("as_of") or "").strip()
    if raw:
        return _iso_date(raw)
    return date.today().isoformat()


def _iso_date(value: Any) -> str:
    """接受 'YYYY-MM-DD' 或 ISO datetime，统一成日期串。"""
    text = str(value).strip()
    if not text:
        raise ValueError("日期为空")
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except ValueError as exc:
            raise ValueError(f"非法日期：{text}") from exc
    raise ValueError(f"非法日期：{text}（要 YYYY-MM-DD）")


def _round(value: Optional[float], n: int = 6) -> Optional[float]:
    return None if value is None else round(float(value), n)


def _pct(value: Optional[float]) -> str:
    return "-" if value is None else f"{value * 100:+.2f}%"


def _price_on_or_before(series: Dict[str, Any], due: str) -> Tuple[Optional[float], Optional[str]]:
    """取不晚于到期日的最近一个价（价格按日期串索引）。

    key 可能带时间（如 ISO datetime 串），取数必须用原始 key，否则 KeyError；
    返回里给归一化后的日期串，方便后续比较。
    """
    best_key: Optional[str] = None
    best_date: Optional[str] = None
    for key in series:
        try:
            day = _iso_date(key)
        except ValueError:
            continue
        if day <= due and (best_date is None or day > best_date):
            best_date = day
            best_key = key
    if best_key is None or best_date is None:
        return None, None
    try:
        return float(series[best_key]), best_date
    except (TypeError, ValueError):
        return None, None


def _reflection(row: sqlite3.Row) -> str:
    """用规则写反思：保留数字与当时的依据摘要，便于回看是否自欺。"""
    realized = row["realized_return"]
    alpha = row["alpha"]
    verdict = "判断成立" if realized is not None and realized > 0 else "判断未成立"
    if row["direction"] == "flat":
        verdict = "空仓判断成立" if (realized or 0) == 0 else "空仓判断待查"
    parts = [
        f"{row['as_of']} {row['direction']} {row['symbol']}，持有 {row['horizon_days']} 天，"
        f"到期 {row['due_date']}（结算价取 {row['price_end_date'] or '-'}）",
        f"实际收益 {_pct(realized)}" + (f"，相对基准 alpha {_pct(alpha)}" if alpha is not None else ""),
        verdict,
    ]
    if row["basis"]:
        parts.append(f"当时依据：{row['basis'][:120]}")
    return "；".join(parts)


# ---------- 动作 ----------
def record(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """记录一条决策。params: symbol / direction / horizon_days / as_of / basis / run_id /
    model / price_at / db_path。同一 (run_id, symbol, as_of) 只记一次（幂等）。"""
    symbol = str(params.get("symbol") or "").strip()
    direction = str(params.get("direction") or "").strip().lower()
    if not symbol:
        return {"ok": False, "error": "缺少 symbol"}
    if direction not in _DIRECTIONS:
        return {"ok": False, "error": f"direction 必须是 {_DIRECTIONS} 之一"}
    try:
        horizon = int(params.get("horizon_days") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "horizon_days 必须是整数"}
    if horizon <= 0:
        return {"ok": False, "error": "horizon_days 必须大于 0"}
    try:
        as_of = _today(params)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    price_at = params.get("price_at")
    try:
        price_at = None if price_at in (None, "") else float(price_at)
    except (TypeError, ValueError):
        return {"ok": False, "error": "price_at 必须是数字"}

    due = (date.fromisoformat(as_of) + timedelta(days=horizon)).isoformat()
    conn = _conn(params)
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO decisions"
            " (symbol, direction, horizon_days, basis, run_id, model, as_of, price_at,"
            "  status, due_date, created)"
            " VALUES (?,?,?,?,?,?,?,?, 'open', ?, ?)",
            (symbol, direction, horizon, str(params.get("basis") or ""),
             str(params.get("run_id") or ""), str(params.get("model") or ""),
             as_of, price_at, due, time.time()))
        conn.commit()
        created = cur.rowcount > 0
        row = conn.execute("SELECT * FROM decisions WHERE run_id=? AND symbol=? AND as_of=?",
                           (str(params.get("run_id") or ""), symbol, as_of)).fetchone()
        return {"ok": True, "created": created, "id": row["id"] if row else None,
                "symbol": symbol, "direction": direction, "as_of": as_of, "due_date": due,
                "status": row["status"] if row else "open"}
    finally:
        conn.close()


def settle(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """结算到期决策。params: prices {symbol: {date: price}} / benchmark {date: price} /
    as_of / db_path。没有到期价格就跳过，不回填。"""
    try:
        as_of = _today(params)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    prices = params.get("prices") or {}
    benchmark = params.get("benchmark") or {}
    if not isinstance(prices, dict):
        return {"ok": False, "error": "prices 必须是 {symbol: {date: price}} 对象"}

    settled: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    conn = _conn(params)
    try:
        rows = conn.execute("SELECT * FROM decisions WHERE status='open'").fetchall()
        for row in rows:
            due = row["due_date"] or (
                date.fromisoformat(row["as_of"]) + timedelta(days=row["horizon_days"])).isoformat()
            if due > as_of:
                skipped.append({"id": row["id"], "symbol": row["symbol"], "due_date": due,
                                "reason": "not_due"})
                continue
            if row["price_at"] is None:
                skipped.append({"id": row["id"], "symbol": row["symbol"], "due_date": due,
                                "reason": "missing_price_at"})
                continue
            series = prices.get(row["symbol"]) or {}
            if not isinstance(series, dict):
                skipped.append({"id": row["id"], "symbol": row["symbol"], "due_date": due,
                                "reason": "bad_price_series"})
                continue
            price_end, used_date = _price_on_or_before(series, due)
            if price_end is None:
                skipped.append({"id": row["id"], "symbol": row["symbol"], "due_date": due,
                                "reason": "missing_price"})
                continue
            if used_date is None or used_date <= row["as_of"]:
                # 只有决策日及更早的价：拿它当到期价就是前视/回填，宁可不结算
                skipped.append({"id": row["id"], "symbol": row["symbol"], "due_date": due,
                                "reason": "missing_price"})
                continue
            raw = price_end / float(row["price_at"]) - 1.0
            realized = 0.0 if row["direction"] == "flat" else (
                raw if row["direction"] == "long" else -raw)
            bench_return = None
            bench_series = benchmark if isinstance(benchmark, dict) else {}
            if bench_series:
                b_end, _ = _price_on_or_before(bench_series, due)
                b_start, _ = _price_on_or_before(bench_series, row["as_of"])
                if b_end is not None and b_start:
                    bench_return = b_end / b_start - 1.0
            alpha = None if bench_return is None else realized - bench_return

            conn.execute(
                "UPDATE decisions SET status='settled', settled_at=?, price_end=?,"
                " price_end_date=?, realized_return=?, benchmark_return=?, alpha=? WHERE id=?",
                (as_of, price_end, used_date, _round(realized), _round(bench_return),
                 _round(alpha), row["id"]))
            fresh = conn.execute("SELECT * FROM decisions WHERE id=?", (row["id"],)).fetchone()
            text = _reflection(fresh)
            conn.execute("UPDATE decisions SET reflection=? WHERE id=?", (text, row["id"]))
            settled.append({"id": row["id"], "symbol": row["symbol"], "direction": row["direction"],
                            "realized_return": _round(realized), "alpha": _round(alpha),
                            "price_end": price_end, "price_end_date": used_date,
                            "reflection": text})
        conn.commit()
        return {"ok": True, "as_of": as_of, "settled": settled,
                "settled_count": len(settled),
                "skipped": [s for s in skipped if s["reason"] != "not_due"],
                "not_due_count": sum(1 for s in skipped if s["reason"] == "not_due")}
    finally:
        conn.close()


def recall(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """召回最近决策。params: symbol（可选）/ limit（默认 5）/ include_lessons（默认 True）。"""
    symbol = str(params.get("symbol") or "").strip()
    try:
        limit = int(params.get("limit") or 5)
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, 50))
    conn = _conn(params)
    try:
        if symbol:
            rows = conn.execute(
                "SELECT * FROM decisions WHERE symbol=? ORDER BY as_of DESC, id DESC LIMIT ?",
                (symbol, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM decisions ORDER BY as_of DESC, id DESC LIMIT ?", (limit,)).fetchall()
        items = [dict(r) for r in rows]
        lessons: List[Dict[str, Any]] = []
        if params.get("include_lessons", True) and symbol:
            lessons = [dict(r) for r in conn.execute(
                "SELECT * FROM decisions WHERE status='settled' AND symbol<>?"
                " ORDER BY id DESC LIMIT 3", (symbol,)).fetchall()]
        return {"ok": True, "symbol": symbol or None, "items": items,
                "count": len(items), "lessons": lessons}
    finally:
        conn.close()


def run_action(action: str, params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None
               ) -> Dict[str, Any]:
    table = {"record": record, "settle": settle, "recall": recall}
    fn = table.get(action)
    if fn is None:
        raise ValueError(f"memory 不支持的动作：{action}")
    return fn(params, ctx)


def register(runtime, capability: str = "memory",
             actions: Sequence[str] = ACTIONS) -> None:
    """把一个 Agent 注册成决策记忆。

    ```python
    from multi_agent.integrations.memory import register
    register(runtime)
    ```

    它只存决策与结算结果，不取行情：`settle` 的 prices 由调用方（DataHub / 上游
    步骤）准备好传入，避免 memory 变成第二个数据源。
    """
    for action in actions:
        runtime.add_handler(capability, action,
                            (lambda a: (lambda p, c: run_action(a, p, c)))(action))