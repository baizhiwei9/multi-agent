"""规则化结算账本：口径、缺价不猜、聚合、存储往返、CLI。"""
from __future__ import annotations

import json

import pytest

from multi_agent.integrations import settlement as S


def _bars(pairs):
    return {d: p for d, p in pairs}


DATES = ["2026-11-20", "2026-11-21", "2026-11-24", "2026-11-25", "2026-11-26",
         "2026-11-27", "2026-11-30"]


def _prices(values):
    return _bars(zip(DATES, values))


BASE = dict(ticker="600519", entry_date="2026-11-20", direction="LONG",
            entry_price=100.0, horizon_days=3)


# ───────────────────────────────────────────── 行情解析
def test_series_accepts_dict_parallel_arrays_and_bars():
    a = S._series({"2026-11-20": 100, "2026-11-21": 101})
    assert a[0] == ["2026-11-20", "2026-11-21"] and a[1]["2026-11-21"] == 101.0
    b = S._series({"dates": ["2026-11-21", "2026-11-20"], "close": [101, 100]})
    assert b[0] == ["2026-11-20", "2026-11-21"]          # 自动按日期升序
    c = S._series([{"date": "2026-11-20", "close": 100}])
    assert c[1]["2026-11-20"] == 100.0


def test_series_rejects_bad_input():
    with pytest.raises(S.SettlementError):
        S._series({"dates": ["2026-11-20"], "close": []})
    with pytest.raises(S.SettlementError):
        S._series({"2026-11-20": -1})
    with pytest.raises(S.SettlementError):
        S._series({})


# ───────────────────────────────────────────── 单笔结算
def test_long_win_and_alpha():
    out = S.settle_one(dict(BASE), _prices([100, 101, 103, 110]),
                       _prices([100, 100, 101, 105]))
    assert out["status"] == "settled"
    assert out["exit_date"] == "2026-11-25"               # entry 后第 3 个交易日
    assert out["return"] == pytest.approx(0.10)
    assert out["hit"] is True
    assert out["benchmark_return"] == pytest.approx(0.05)
    assert out["alpha"] == pytest.approx(0.05)


def test_short_win_uses_inverted_benchmark():
    rec = dict(BASE, direction="SHORT")
    out = S.settle_one(rec, _prices([100, 98, 96, 90]),
                       _prices([100, 99, 98, 95]))
    assert out["return"] == pytest.approx(0.10)
    assert out["hit"] is True
    # 做空时基准也取反：alpha = 0.10 - (-1)*(95/100-1) = 0.10 - 0.05
    assert out["alpha"] == pytest.approx(0.05)


def test_flat_scores_zero_and_never_hits():
    rec = dict(BASE, direction="FLAT")
    out = S.settle_one(rec, _prices([100, 105, 99, 110]))
    assert out["return"] == 0.0
    assert out["hit"] is False and out["note"]


def test_mfe_mae_and_drawdown_follow_the_path():
    out = S.settle_one(dict(BASE), _prices([100, 105, 95, 110]))
    assert out["mfe"] == pytest.approx(0.10)
    assert out["mae"] == pytest.approx(-0.05)
    assert out["max_drawdown"] == pytest.approx(10 / 105)


def test_missing_exit_price_stays_pending():
    """序列不够长：必须 pending + 原因，不能拿最后一根当到期价。"""
    out = S.settle_one(dict(BASE), _prices([100, 101]))
    assert out["status"] == "pending"
    assert "只到" in out["missing_reason"] and "return" not in out


def test_missing_entry_price_stays_pending():
    rec = dict(BASE, entry_date="2026-12-31")
    out = S.settle_one(rec, _prices([100, 101, 102, 103]))
    assert out["status"] == "pending" and "不在行情序列" in out["missing_reason"]


def test_benchmark_gap_keeps_return_but_flags_alpha():
    out = S.settle_one(dict(BASE), _prices([100, 101, 103, 110]),
                       {"2026-11-20": 100})
    assert out["status"] == "settled" and out["return"] == pytest.approx(0.10)
    assert out.get("alpha") is None and "alpha 未计算" in out["missing_reason"]


def test_invalid_record_and_direction_raise():
    with pytest.raises(S.SettlementError):
        S.settle_one(dict(BASE, direction="SIDEWAYS"), _prices([100, 1, 2, 3]))
    with pytest.raises(S.SettlementError):
        S.settle_one(dict(BASE, entry_price=0), _prices([100, 1, 2, 3]))
    with pytest.raises(S.SettlementError):
        S.settle_one(dict(BASE, horizon_days=0), _prices([100, 1, 2, 3]))
    with pytest.raises(S.SettlementError):
        S.settle_one({"ticker": "x"}, _prices([100, 1, 2, 3]))


# ───────────────────────────────────────────── 聚合
def test_stats_groups_by_direction_and_rating():
    rows = [
        S.settle_one(dict(BASE, rating="BUY"), _prices([100, 101, 103, 110]),
                     _prices([100, 100, 101, 105])),
        S.settle_one(dict(BASE, direction="SHORT", rating="SELL"),
                     _prices([100, 99, 98, 95]), _prices([100, 99, 98, 95])),
        S.settle_one(dict(BASE, direction="FLAT", rating="HOLD"),
                     _prices([100, 101, 102, 103])),
        S.settle_one(dict(BASE, entry_date="2026-12-31"), _prices([100, 101, 102, 103])),
    ]
    s = S.stats(rows)
    assert s["count"] == 4 and s["settled"] == 3 and s["pending"] == 1
    assert s["bets"] == 2 and s["win"] == 2 and s["hit_rate"] == 1.0
    assert s["flat"] == 1
    assert set(s["by_direction"]) == {"LONG", "SHORT", "FLAT"}
    assert s["by_rating"]["BUY"]["count"] == 1


# ───────────────────────────────────────────── 账本与存储
def test_ledger_add_settle_and_no_resettle(tmp_path):
    path = str(tmp_path / "ledger.json")
    led = S.Ledger(S.JsonStore(path))
    led.add(dict(BASE))
    led.add(dict(BASE, ticker="000001", entry_date="2026-11-21", direction="SHORT"))

    out = led.settle(_prices([100, 101, 103, 110, 111, 112, 113]))
    assert len(out["settled"]) == 2
    assert json.loads(open(path, encoding="utf-8").read())["records"][0]["status"] == "settled"

    again = led.settle(_prices([100, 999, 999, 999, 999, 999, 999]))
    assert again["settled"] == []            # 已结算不重算
    assert again["summary"]["settled"] == 2


def test_ledger_keeps_pending_and_exposes_reason(tmp_path):
    path = str(tmp_path / "ledger.json")
    led = S.Ledger(S.JsonStore(path))
    led.add(dict(BASE))
    out = led.settle(_prices([100, 101]))
    assert out["settled"] == [] and len(out["pending"]) == 1
    assert "只到" in out["pending"][0]["missing_reason"]


def test_json_store_overwrites_same_key(tmp_path):
    store = S.JsonStore(str(tmp_path / "l.json"))
    store.append(dict(BASE, note="v1"))
    store.append(dict(BASE, note="v2"))
    rows = store.items()
    assert len(rows) == 1 and rows[0]["note"] == "v2"


# ───────────────────────────────────────────── 反思提示 & CLI
def test_reflect_prompt_only_carries_numbers():
    out = S.settle_one(dict(BASE), _prices([100, 101, 103, 110]))
    prompt = S.reflect_prompt(out)
    assert "收益" not in prompt or "return" in prompt
    assert "0.1" in prompt and "不要推断" in prompt


def test_cli_settles_and_reports(tmp_path, capsys):
    ledger = tmp_path / "l.json"
    S.Ledger(S.JsonStore(str(ledger))).add(dict(BASE))
    prices = tmp_path / "p.json"
    prices.write_text(json.dumps(_prices([100, 101, 103, 110])), encoding="utf-8")

    assert S.main([str(ledger), "--prices", str(prices)]) == 0
    printed = capsys.readouterr().out
    assert "命中率" in printed and "已结算 1" in printed


def test_cli_returns_2_on_bad_prices(tmp_path):
    ledger = tmp_path / "l.json"
    S.Ledger(S.JsonStore(str(ledger))).add(dict(BASE))
    prices = tmp_path / "p.json"
    prices.write_text(json.dumps({"2026-11-20": -5}), encoding="utf-8")
    assert S.main([str(ledger), "--prices", str(prices)]) == 2