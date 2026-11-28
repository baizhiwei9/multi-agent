"""Trade Agent 行情注入：命令 / JSON 两种来源，坏数据要明确报错。

不碰网络：只测 `load_market` 的契约与错误路径；抓取兜底（东财→新浪）由
`market_snapshot` 负责，失败只记 `missing` 不抛异常。
"""
from __future__ import annotations

import json
import sys

import pytest

import trade_agent as T

MARKET = {"as_of": "2026-11-27",
          "spot": {"上证指数": {"close": 3606.49, "chg_pct": 3.73, "amount_yi": None}},
          "history": {"上证指数": {"ma40": 3500.0, "above_ma40": True}}}


def test_market_from_json(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps(MARKET), encoding="utf-8")
    out = T.load_market(market_json=str(path))
    assert out["source"] == "json"
    assert out["spot"]["上证指数"]["close"] == 3606.49
    assert out["history"]["上证指数"]["above_ma40"] is True


def test_market_from_cmd(tmp_path):
    script = tmp_path / "mk.py"
    script.write_text("import json,sys;print(json.dumps(%r))" % MARKET, encoding="utf-8")
    out = T.load_market(market_cmd=f"{sys.executable} {script}")
    assert out["source"] == "cmd" and out["spot"]["上证指数"]["close"] == 3606.49


def test_market_cmd_failure_is_loud():
    with pytest.raises(RuntimeError, match="行情命令失败"):
        T.load_market(market_cmd="exit 3")


def test_market_json_without_spot_is_loud(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"as_of": "2026-11-27"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="spot"):
        T.load_market(market_json=str(path))
