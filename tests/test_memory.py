"""memory 端到端测试：临时 SQLite，不依赖网络与 DataHub。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from multi_agent.integrations import memory  # noqa: E402


def _p(tmp_path: Path, **over) -> dict:
    base = {"db_path": str(tmp_path / "mem.db"), "symbol": "copper", "direction": "long",
            "horizon_days": 5, "as_of": "2026-11-20", "price_at": 100.0,
            "basis": "库存连降三周", "run_id": "r1", "model": "m1"}
    base.update(over)
    return base


def test_record_and_idempotent(tmp_path):
    first = memory.record(_p(tmp_path))
    assert first["ok"] is True and first["created"] is True
    assert first["due_date"] == "2026-11-25"

    again = memory.record(_p(tmp_path))
    assert again["ok"] is True and again["created"] is False
    assert again["id"] == first["id"]


def test_record_rejects_bad_input(tmp_path):
    assert memory.record(_p(tmp_path, symbol=""))["ok"] is False
    assert memory.record(_p(tmp_path, direction="buy"))["ok"] is False
    assert memory.record(_p(tmp_path, horizon_days=0))["ok"] is False
    assert memory.record(_p(tmp_path, as_of="2026/11/20"))["ok"] is False


def test_settle_long_with_alpha(tmp_path):
    memory.record(_p(tmp_path))
    res = memory.settle({
        "db_path": str(tmp_path / "mem.db"), "as_of": "2026-11-26",
        "prices": {"copper": {"2026-11-20": 100.0, "2026-11-25": 102.0}},
        "benchmark": {"2026-11-20": 1000.0, "2026-11-25": 1010.0},
    })
    assert res["ok"] is True and res["settled_count"] == 1
    item = res["settled"][0]
    assert item["realized_return"] == 0.02
    assert item["alpha"] == 0.01
    assert item["price_end_date"] == "2026-11-25"
    assert "判断成立" in item["reflection"] and "+2.00%" in item["reflection"]


def test_settle_short_and_flat(tmp_path):
    memory.record(_p(tmp_path, symbol="silver", direction="short", price_at=200.0, run_id="r2"))
    memory.record(_p(tmp_path, symbol="gold", direction="flat", price_at=300.0, run_id="r3"))
    res = memory.settle({
        "db_path": str(tmp_path / "mem.db"), "as_of": "2026-11-26",
        "prices": {"silver": {"2026-11-25": 190.0}, "gold": {"2026-11-25": 330.0}},
    })
    got = {x["symbol"]: x for x in res["settled"]}
    assert got["silver"]["realized_return"] == 0.05      # 空头：价格跌 5%
    assert got["gold"]["realized_return"] == 0.0         # 空仓不计涨跌
    assert got["gold"]["alpha"] is None                   # 没给基准就不编 alpha


def test_settle_waits_for_due_date(tmp_path):
    memory.record(_p(tmp_path))
    res = memory.settle({"db_path": str(tmp_path / "mem.db"), "as_of": "2026-11-22",
                         "prices": {"copper": {"2026-11-22": 101.0}}})
    assert res["settled_count"] == 0 and res["not_due_count"] == 1


def test_settle_never_backfills_with_decision_day_price(tmp_path):
    memory.record(_p(tmp_path))
    # 只给决策日与更早的价：不能当作到期价，必须跳过
    res = memory.settle({"db_path": str(tmp_path / "mem.db"), "as_of": "2026-11-26",
                         "prices": {"copper": {"2026-11-19": 99.0, "2026-11-20": 100.0}}})
    assert res["settled_count"] == 0
    assert res["skipped"][0]["reason"] == "missing_price"

    recalled = memory.recall({"db_path": str(tmp_path / "mem.db"), "symbol": "copper"})
    assert recalled["items"][0]["status"] == "open"


def test_settle_skips_missing_price_at(tmp_path):
    memory.record(_p(tmp_path, price_at=None))
    res = memory.settle({"db_path": str(tmp_path / "mem.db"), "as_of": "2026-11-26",
                         "prices": {"copper": {"2026-11-25": 102.0}}})
    assert res["settled_count"] == 0
    assert res["skipped"][0]["reason"] == "missing_price_at"


def test_recall_returns_recent_and_lessons(tmp_path):
    memory.record(_p(tmp_path, run_id="r1"))
    memory.record(_p(tmp_path, symbol="silver", direction="short", price_at=200.0,
                      run_id="r2", as_of="2026-11-21"))
    memory.settle({"db_path": str(tmp_path / "mem.db"), "as_of": "2026-11-27",
                   "prices": {"silver": {"2026-11-26": 190.0}}})

    got = memory.recall({"db_path": str(tmp_path / "mem.db"), "symbol": "copper"})
    assert got["ok"] is True and got["count"] == 1
    assert got["items"][0]["symbol"] == "copper"
    assert got["lessons"], "应带上其它标的的已结算教训"
    assert got["lessons"][0]["symbol"] == "silver"

    recent = memory.recall({"db_path": str(tmp_path / "mem.db"), "limit": 1})
    assert recent["count"] == 1


def test_register_exposes_actions():
    class FakeRuntime:
        def __init__(self):
            self.handlers = []

        def add_handler(self, capability, action, handler):
            self.handlers.append((capability, action, handler))

    runtime = FakeRuntime()
    memory.register(runtime)
    assert [(c, a) for c, a, _ in runtime.handlers] == [("memory", a) for a in memory.ACTIONS]
    out = runtime.handlers[0][2]({}, {})
    assert out["ok"] is False and "symbol" in out["error"]

def test_price_on_or_before_accepts_datetime_keys():
    """回归：行情序列 key 带时间（ISO datetime 串）时，取数要用原始 key，
    不能用归一化后的日期串索引（原实现 KeyError）。"""
    from multi_agent.integrations.memory import _price_on_or_before
    series = {"2026-11-24 00:00:00": 100.0, "2026-11-25 00:00:00": 101.0}
    price, used = _price_on_or_before(series, "2026-11-25")
    assert price == 101.0
    assert used == "2026-11-25"              # 返回归一化日期，便于比较
    assert _price_on_or_before(series, "2026-11-23") == (None, None)
