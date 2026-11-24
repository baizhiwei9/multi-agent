"""行业多空选股/仓位测试：指标方向、只做多、权重封顶、总仓与现金、数据不足跳过。"""
from __future__ import annotations

import csv
from datetime import date, timedelta

from multi_agent.integrations import equity_pick as EP


def _dates(n: int, start: str = "2025-01-01") -> list[str]:
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def _series(n: int, step: float, base: float = 1000.0, wobble: float = 0.0) -> list[float]:
    out = []
    price = base
    for i in range(n):
        price *= (1 + step)
        out.append(round(price + (wobble * (1 if i % 2 else -1)), 2))
    return out


def _write(tmp_path, rows: dict[str, list[float]], n: int):
    """写一个最小可用行业日线 CSV（date,close,name,code）。"""
    path = tmp_path / "sw.csv"
    dates = _dates(n)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "close", "name", "code"])
        for name, closes in rows.items():
            for d, c in zip(dates, closes):
                w.writerow([d, c, name, f"{name}.SI"])
    return str(path)


def test_load_series_sorts_and_dedups(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("date,close,name\n2025-01-02,10,A\n2025-01-01,9,A\n"
                    "2025-01-02,11,A\n", encoding="utf-8")
    series = EP.load_series(str(path))
    assert series["A"] == [("2025-01-01", 9.0), ("2025-01-02", 11.0)]


def test_metrics_directions(tmp_path):
    n = 300
    up = EP.metrics(list(zip(_dates(n), _series(n, 0.003))))
    down = EP.metrics(list(zip(_dates(n), _series(n, -0.003))))
    assert up["ok"] and up["mom60"] > 0 and up["pos250"] > 0.9
    assert down["mom60"] < 0 and down["pos250"] < 0.1
    assert up["dd60"] > -0.01 and down["dd60"] < -0.1


def test_metrics_rejects_short_history():
    out = EP.metrics(list(zip(_dates(30), _series(30, 0.01))))
    assert out["ok"] is False and "数据不足" in out["reason"]


def test_high_position_and_high_vol_are_bearish():
    hot = EP.evidence_for({"mom20": 0.05, "mom60": 0.12, "dd60": -0.01,
                           "pos250": 0.97, "vol_pct": 0.95})
    cold = EP.evidence_for({"mom20": -0.05, "mom60": -0.12, "dd60": -0.15,
                            "pos250": 0.05, "vol_pct": 0.05})
    assert hot["250日位置"]["signal"] == "bearish"      # 高位=拥挤
    assert hot["波动分位"]["signal"] == "bearish"       # 高波动=风险
    assert cold["250日位置"]["signal"] == "bullish"
    assert cold["波动分位"]["signal"] == "bullish"
    assert hot["60日动量"]["signal"] == "bullish"


def test_pick_only_long_and_caps_weight(tmp_path):
    n = 300
    rows = {"强A": _series(n, 0.004), "强B": _series(n, 0.003, base=800.0),
            "弱C": _series(n, -0.003), "弱D": _series(n, -0.004, base=1200.0)}
    path = _write(tmp_path, rows, n)
    out = EP.pick({"csv_path": path, "top_n": 2, "max_weight": 0.6, "gross": 0.8})
    assert out["ok"] and out["metrics_source"] == "close-only"
    names = [p["name"] for p in out["picks"]]
    assert names == ["强A", "强B"]                       # 只做多，弱的进不来
    assert all(p["weight"] <= 0.6 + 1e-9 for p in out["picks"])
    assert round(sum(out["weights"].values()), 4) == 0.8  # 总仓按 gross 缩放
    assert out["cash"] == 0.2


def test_cap_leaves_cash_when_only_few_names(tmp_path):
    """只有 2 个方向、单行业封顶 25% → 最多持 50%，其余留现金（不硬凑满仓）。"""
    n = 300
    rows = {"强A": _series(n, 0.004), "强B": _series(n, 0.003, base=800.0),
            "弱C": _series(n, -0.003)}
    path = _write(tmp_path, rows, n)
    out = EP.pick({"csv_path": path, "top_n": 5, "max_weight": 0.25})
    assert round(sum(out["weights"].values()), 4) == 0.5
    assert out["cash"] == 0.5


def test_flat_market_picks_nothing_and_holds_cash(tmp_path):
    n = 300
    rows = {f"横{i}": _series(n, 0.0, wobble=0.2) for i in range(3)}
    path = _write(tmp_path, rows, n)
    out = EP.pick({"csv_path": path})
    assert out["picks"] == [] and out["cash"] == 1.0


def test_short_history_is_skipped_not_fatal(tmp_path):
    n = 300
    rows = {"长": _series(n, 0.004), "短": _series(30, 0.004)}
    path = _write(tmp_path, rows, n)
    out = EP.pick({"csv_path": path})
    assert out["n_industries"] == 1 and [p["name"] for p in out["picks"]] == ["长"]


def test_as_of_truncates_history(tmp_path):
    n = 300
    rows = {"A": _series(n, 0.004)}
    path = _write(tmp_path, rows, n)
    full = EP.pick({"csv_path": path})
    cut = EP.pick({"csv_path": path, "as_of": _dates(n)[-5]})
    assert full["as_of"] == _dates(n)[-1] and cut["as_of"] == _dates(n)[-5]


def test_register_exposes_pick():
    handlers = {}

    class Runtime:
        def add_handler(self, cap, action, fn):
            handlers[(cap, action)] = fn

    EP.register(Runtime())
    assert ("equity_pick", "pick") in handlers


def test_pipeline_shape():
    steps = EP.PIPELINE["steps"]
    assert [s["name"] for s in steps] == ["pick", "ledger"]
    assert steps[0]["params"]["csv_path"] == "${params.csv_path}"
    assert steps[1]["depends_on"] == ["pick"]

def test_cap_weights_mixed_over_and_free_keeps_sum_consistent():
    """回归：部分标的触顶、部分未触发时，封顶后不得把已封顶的再当 free 重新压低
    （原实现把已封顶标的丢回 free 重分配，导致权重和缩水、cash 虚高）。"""
    w = EP._cap_weights({"A": 5, "B": 3, "C": 2}, 0.25)
    assert w == {"A": 0.25, "B": 0.25, "C": 0.25}
    assert round(sum(w.values()), 4) == 0.75        # 全触顶 3×0.25，余 0.25 现金


def test_cap_weights_capped_never_reentered_as_free():
    """回归：已封顶标的（A/B）权重必须保持 cap，不许再被当 free 压低；剩余按 raw 分给 free。"""
    w = EP._cap_weights({"A": 10, "B": 5, "C": 1}, 0.4)
    assert w["A"] == 0.4 and w["B"] == 0.4
    assert w["C"] == 0.2                            # free 拿到剩余 0.2
    assert abs(sum(w.values()) - 1.0) < 1e-6
    # 控制组：若非 free 被错误重分配，A/B 会低于 0.4 且总和错乱

