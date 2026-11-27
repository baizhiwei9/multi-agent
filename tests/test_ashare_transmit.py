"""甲骨文 CDS → AI → A 股传导链路验收。

五件事分开验：

1. 信用事件能被采到，噪声（游戏皮肤）不入库；
2. 评估把它判成 `credit_stress / bear`，而不是商品供需；
3. 传导按"事件压力 × 环节融资依赖 × 标的敞口"折压力分，档位与顺序可解释；
4. 情景营收影响按"资本开支下修 × 环节弹性 × AI 收入占比"算，线性可复算；
5. 口径写清是压力分 / 收入影响，不是涨跌幅，且带假设声明。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from multi_agent.frameworks.keywords import KeywordSet                   # noqa: E402
from multi_agent.frameworks.news.framework import NewsWatchFramework    # noqa: E402
from multi_agent.integrations.supervisor import tune                     # noqa: E402
from multi_agent.projects.trade.news_assessor import assess              # noqa: E402
from multi_agent.projects.trade.transmit import transmit                 # noqa: E402

HERE = ROOT / "examples" / "oracle_cds_ashare"
NEWS = json.loads((HERE / "fixture" / "news.json").read_text(encoding="utf-8"))


@pytest.fixture()
def collected():
    keywords = KeywordSet.from_file(str(HERE / "keywords.json"))
    return NewsWatchFramework(keyword_set=keywords,
                              judge=lambda hit: False).collect({"items": NEWS})


def test_credit_events_collected_and_noise_dropped(collected):
    groups = [e["group"] for e in collected["events"]]
    assert groups.count("credit_stress") == 3
    assert all("皮肤" not in e["title"] for e in collected["events"])


def test_assess_marks_credit_stress_bear(collected):
    tuned = tune({"keyword_set": "credit-watch",
                  "weights": {"credit_stress": 0.95}, "major_weight": 0.7,
                  "min_level": "warn"})
    res = assess({"events": collected, "tuning": tuned})
    assert res["major"] is True
    assert res["lead"]["tag"] == "credit_stress"
    assert res["lead"]["impact"] == "bear"


def test_transmit_scores_and_bands():
    res = transmit({"severity": 76.7})
    assert res["ok"] and res["counts"]["segments"] == 7 and res["counts"]["tickers"] == 15
    assert res["by_segment"][0]["segment"] == "idc"
    assert res["top"][0]["ticker"] == "300442"
    domestic = next(s for s in res["by_segment"] if s["segment"] == "domestic")
    assert domestic["band"] == "低"
    # 口径：压力分，不是涨跌幅
    assert res["unit"] == "pressure" and res["assumption"] is True


def test_transmit_is_monotonic_in_severity():
    low = transmit({"severity": 30})
    high = transmit({"severity": 90})
    assert high["top"][0]["score"] > low["top"][0]["score"]


def test_transmit_revenue_scenario_linear():
    base = transmit({"severity": 76.7, "capex_cut": 0.10})
    assert base["scenario"]["capex_cut"] == 0.10
    ri = {r["name"]: r["revenue_impact_pct"] for r in base["items"]}
    assert ri["润泽科技"] == -8.5 and ri["海光信息"] == -0.2
    assert all(v <= 0 for v in ri.values())
    # 情景幅度翻倍 → 营收影响翻倍，可复算
    double = transmit({"severity": 76.7, "capex_cut": 0.20})
    assert double["top_revenue"][0]["revenue_impact_pct"] == -17.0
    # 缺字段的链不报错，营收影响落 0
    bare = {"name": "t", "assumption": True,
            "chains": [{"id": "x", "name": "X", "financing_dependence": 1.0,
                        "ashare": [{"ticker": "000001", "name": "测试", "exposure": 1.0}]}]}
    got = transmit({"severity": 50, "chain": bare})
    assert got["items"][0]["revenue_impact_pct"] == 0.0
    assert got["scenario"]["unit"] == "revenue_pct"


def test_transmit_degrades_without_chain():
    res = transmit({"severity": 76.7, "chain": {"chains": []}})
    assert res["degraded"] is True and "没有环节" in res["reason"]


def test_transmit_accepts_custom_chain():
    chain = {"name": "t", "assumption": True,
             "chains": [{"id": "x", "name": "X", "financing_dependence": 1.0,
                         "ashare": [{"ticker": "000001", "name": "测试", "exposure": 1.0}]}]}
    res = transmit({"severity": 50, "chain": chain})
    assert res["top"][0]["score"] == 50.0 and res["top"][0]["band"] == "高"


def test_oracle_demo_passes(capsys):
    from examples.oracle_cds_ashare import run_demo

    assert run_demo.main() == 0
    out = capsys.readouterr().out
    assert "项通过" in out and "[FAIL]" not in out