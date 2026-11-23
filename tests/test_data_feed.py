"""取数角色测试：catalog 说明缺口、本地缓存读取、批量证据、data_ref 派活。"""
from __future__ import annotations

import csv
import json
from datetime import date, timedelta

import pytest

from multi_agent.integrations import data_feed as D
from multi_agent.integrations import equity_pick as EP

CSV = "/home/work/dumate/5eaa88f7d2d5403f9ae855f5fcfd7f34/workspace/tda_run/sw_l1_daily.csv"


def _dates(n: int, start: str = "2025-01-01") -> list[str]:
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def _write_index(root, code: str, n: int, step: float):
    folder = root / "indices"
    folder.mkdir(parents=True, exist_ok=True)
    rows, price = [], 1000.0
    for d in _dates(n):
        price *= (1 + step)
        rows.append({"trade_date": d.replace("-", ""), "close": round(price, 2)})
    (folder / f"{code}.json").write_text(json.dumps(rows), encoding="utf-8")


def test_catalog_names_the_gaps(tmp_path):
    _write_index(tmp_path, "801010", 300, 0.002)
    out = D.catalog({"datahub_root": str(tmp_path)})
    assert out["ok"] and out["available"]["index"] == 1
    # 已被 DataHub 覆盖的域要在 available 里，口径限制要写在 gaps 里
    assert {"valuation", "moneyflow", "inventory"} <= set(out["gaps"])
    assert {"valuation", "moneyflow", "inventory"} <= set(out["available"])
    assert "mom20" in out["items"]


def test_series_reports_missing_code(tmp_path):
    out = D.series({"datahub_root": str(tmp_path), "market": "index", "code": "000000"})
    assert out["ok"] is False and "没有" in out["error"]


def test_metrics_and_evidence_from_cache(tmp_path):
    _write_index(tmp_path, "801010", 300, 0.003)
    _write_index(tmp_path, "801030", 300, -0.003)
    m = D.metrics({"datahub_root": str(tmp_path), "code": "801010"})
    assert m["ok"] and m["mom60"] > 0 and m["as_of"]
    ev = D.evidence({"datahub_root": str(tmp_path), "codes": ["801010", "801030"],
                     "names": {"801010": "强", "801030": "弱"}})
    assert ev["ok"] and ev["n"] == 2
    assert ev["evidence"]["强"]["60日动量"]["signal"] == "bullish"
    assert ev["evidence"]["弱"]["60日动量"]["signal"] == "bearish"
    assert ev["evidence"]["强"]["_mom60"] > 0      # 旁注留给组合层


def test_evidence_records_failed_codes(tmp_path):
    _write_index(tmp_path, "801010", 300, 0.003)
    ev = D.evidence({"datahub_root": str(tmp_path), "codes": ["801010", "800000"]})
    assert ev["n"] == 1 and "800000" in ev["failed"]


def test_unknown_action_and_register():
    with pytest.raises(ValueError):
        D.run_action("nope", {})
    handlers = {}

    class Runtime:
        def add_handler(self, cap, action, fn):
            handlers[(cap, action)] = fn

    D.register(Runtime())
    assert set(handlers) == {("data", a) for a in (
        "catalog", "series", "metrics", "evidence", "valuation", "inventory", "moneyflow")}


class _DataClient:
    """模拟 Hub：把 data/evidence 的派活转成真函数。"""

    def __init__(self, params):
        self.params = params
        self.calls = []

    def submit(self, capability, action, params=None, **kw):
        self.calls.append((capability, action, params))
        assert capability == "data" and action == "evidence"
        return {"state": "succeeded", "result": D.evidence({**self.params, **(params or {})})}


def test_pick_can_delegate_fetch_to_data_role(tmp_path):
    _write_index(tmp_path, "801010", 300, 0.004)
    _write_index(tmp_path, "801030", 300, -0.004)
    client = _DataClient({"datahub_root": str(tmp_path)})
    out = EP.pick({"data_ref": {"params": {"codes": ["801010", "801030"],
                                           "names": {"801010": "强", "801030": "弱"}}},
                   "top_n": 5, "max_weight": 0.5, "gross": 1.0}, client=client)
    assert out["ok"] and [p["name"] for p in out["picks"]] == ["强"]
    assert client.calls and client.calls[0][0] == "data"


def test_pick_data_ref_without_client_errors():
    out = EP.pick({"data_ref": {"params": {"codes": ["801010"]}}})
    assert out["ok"] is False and "client" in out["error"]


# ───────────────────────────────────────────── 分位与库的连接
def test_single_sample_gives_no_percentile(monkeypatch):
    """库里只有一天估值时，分位必须是 None 而不是 100%。"""
    monkeypatch.setattr(D, "_rows", lambda *a, **k: [{
        "obs_date": "2026-11-20",
        "payload": json.dumps({"code": "C39", "industry": "计算机", "level": 2,
                               "pe_weighted": 47.5, "pe_median": 56.0, "company_count": 700}),
    }])
    out = D.valuation({"industry": "计算机"})
    assert out["ok"] and out["rows"][0]["pe_pct"] is None
    assert "不足" in out["rows"][0]["pct_note"]


def test_valuation_reports_missing_industry(monkeypatch):
    monkeypatch.setattr(D, "_rows", lambda *a, **k: [{
        "obs_date": "2026-11-20",
        "payload": json.dumps({"code": "A", "industry": "农林牧渔", "pe_weighted": 20.0}),
    }])
    out = D.valuation({"industry": "不存在的行业"})
    assert out["ok"] is False and "匹配" in out["error"]


def test_inventory_requires_symbol_and_missing_rows(monkeypatch):
    assert D.inventory({})["ok"] is False
    monkeypatch.setattr(D, "_rows", lambda *a, **k: [])
    out = D.inventory({"symbol": "沪铜"})
    assert out["ok"] is False and "定稿行" in out["error"]


def test_datahub_unavailable_is_explicit(tmp_path, monkeypatch):
    """DataHub 路径指错时，要明确报不可用，不能返回空表装成功。"""
    out = D.valuation({"datahub_path": str(tmp_path / "nope")})
    assert out["ok"] is False and "DataHub" in out["error"]
