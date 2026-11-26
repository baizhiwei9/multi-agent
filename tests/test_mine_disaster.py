"""印尼矿难链路验收：采集 → 调权重 → 评估 → 平衡表。

校验四件事：
1. 程序模式只采供给硬事件，llm 组不采，采集官不判方向；
2. 经理的权重 / 阈值 / 最低级别真的生效；
3. 矿难被判为大事且方向为供给收缩（bull）；
4. 平衡表未注入时明确 degraded，注入演示 executor 时供给下调且标注来源。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from multi_agent.frameworks.keywords import KeywordSet                    # noqa: E402
from multi_agent.frameworks.news.framework import NewsWatchFramework     # noqa: E402
from multi_agent.integrations.supervisor import tune                      # noqa: E402
from multi_agent.projects.trade.balance_sheet import BalanceSheetProject  # noqa: E402
from multi_agent.projects.trade.news_assessor import assess               # noqa: E402

HERE = ROOT / "examples" / "indonesia_mine_disaster"
NEWS = json.loads((HERE / "fixture" / "news.json").read_text(encoding="utf-8"))
BASE = json.loads((HERE / "fixture" / "base_balance.json").read_text(encoding="utf-8"))


@pytest.fixture()
def collected():
    keywords = KeywordSet.from_file(str(HERE / "keywords.json"))
    framework = NewsWatchFramework(keyword_set=keywords, judge=lambda hit: False)
    return framework.collect({"items": NEWS})


def test_program_mode_keeps_shocks_and_skips_llm_group(collected):
    groups = [e["group"] for e in collected["events"]]
    assert collected["scanned"] == len(NEWS)
    assert groups.count("supply_shock") == 2 and "supply_cut" in groups
    assert "demand_shift" not in groups
    # 采集官不判方向
    assert all(e["impact"] is None for e in collected["events"])
    # 噪声不入库
    assert all("天气" not in (e["title"] or "") for e in collected["events"])


def test_llm_mode_adds_demand_shift():
    keywords = KeywordSet.from_file(str(HERE / "keywords.json"))
    framework = NewsWatchFramework(keyword_set=keywords, judge=lambda hit: True)
    res = framework.collect({"items": NEWS})
    assert any(e["group"] == "demand_shift" for e in res["events"])


def test_tune_overrides_weights_and_threshold():
    res = tune({"keyword_set": "mine-watch", "weights": {"supply_shock": 0.95},
                "major_weight": 0.8, "min_level": "warn"})
    assert res["weights"]["supply_shock"] == 0.95
    assert res["major_weight"] == 0.8 and res["min_level"] == "warn"


def test_assess_marks_mine_as_major_bull(collected):
    tuned = tune({"keyword_set": "mine-watch",
                  "weights": {"supply_shock": 0.95, "supply_cut": 0.7},
                  "major_weight": 0.8, "min_level": "warn"})
    res = assess({"events": collected, "tuning": tuned})
    assert res["major"] is True
    assert res["lead"]["tag"] == "supply_shock"
    assert res["lead"]["impact"] == "bull"
    assert res["lead"]["weight"] == 0.95
    assert all(i["tag"] != "demand_down" for i in res["items"])


def test_balance_without_executor_is_degraded():
    res = BalanceSheetProject().handlers()["balance"]["build"](
        {"commodity": "铜"}, {})
    assert res["degraded"] is True and "BalanceAI" in res["reason"]


def test_balance_with_demo_executor_moves_supply_down(collected):
    from examples.indonesia_mine_disaster import run_demo

    tuned = tune({"keyword_set": "mine-watch",
                  "weights": {"supply_shock": 0.95, "supply_cut": 0.7},
                  "major_weight": 0.8, "min_level": "warn"})
    judged = assess({"events": collected, "tuning": tuned})
    project = BalanceSheetProject(executor=lambda cap, action, params, ctx:
                                  run_demo.demo_executor(params, ctx))
    res = project.handlers()["balance"]["build"](
        {"commodity": "铜", "assess": judged}, {})
    assert res["supply"] < BASE["supply"]
    assert res["balance"] < BASE["balance"]
    assert res["source"] == "fixture-demo-executor"
    assert "非实盘" in res["assumption"]


def test_indonesia_demo_passes(capsys):
    from examples.indonesia_mine_disaster import run_demo

    assert run_demo.main() == 0
    out = capsys.readouterr().out
    assert "20/20 项通过" in out and "[FAIL]" not in out


def test_assess_flips_reversal_event_to_bear(collected):
    """『罢工结束、恢复生产』是终态，不能被当成供给收缩（bull）。"""
    tuned = tune({"keyword_set": "mine-watch",
                  "weights": {"supply_shock": 0.95, "supply_cut": 0.7},
                  "major_weight": 0.8, "min_level": "warn"})
    res = assess({"events": collected, "tuning": tuned})
    strike = next(i for i in res["items"] if "罢工" in str(i["why"]))
    assert strike["impact"] == "bear"
    assert float(strike["weight"]) <= 0.5
    assert "方向翻转" in strike["why"]
    # 真冲击仍领先且为 bull
    assert res["lead"]["tag"] == "supply_shock" and res["lead"]["impact"] == "bull"