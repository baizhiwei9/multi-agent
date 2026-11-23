"""多轮评议（采集官 ⇄ 审计官）与协调官初筛：限额、轮数、无新闻短路。"""
from __future__ import annotations

import json

import pytest

from multi_agent import limits as L
from multi_agent.integrations import debate


class _Client:
    """假 Hub 客户端：采集官回稿、审计官按脚本回结论，并记录每次派活。"""

    def __init__(self, audits=None):
        self.audits = list(audits or [])
        self.calls = []

    def submit(self, capability, action, params=None, **kw):
        params = params or {}
        self.calls.append({"capability": capability, "action": action,
                           "params": params, "kw": kw})
        if capability == "auditor":
            text = self.audits.pop(0) if self.audits else "decision: MODIFY"
            return {"state": "succeeded", "result": {"verdict": text}}
        return {"state": "succeeded",
                "result": {"text": f"draft r{params.get('round')}"}}


class _LongClient(_Client):
    """采集官永远回超长稿，用来验证截断与预算。"""

    def submit(self, capability, action, params=None, **kw):
        task = super().submit(capability, action, params, **kw)
        if capability != "auditor":
            task["result"]["text"] = "x" * 20000
        return task


def _targets():
    return ({"capability": "B", "action": "scan_news"},
            {"capability": "auditor", "action": "review_brief"})


# ───────────────────────────────────────────── 协调官初筛
def test_screen_flags_major_event():
    out = debate.screen({
        "balance_news": {"events": [{"title": "某铜矿发生矿难停产", "impact": "high"}]},
        "event_news": {"events": [{"title": "周度库存例行更新"}]},
    })
    assert out["no_news"] is False and out["major_count"] == 1
    assert out["topics"][0]["title"].startswith("某铜矿")
    assert "重大" in out["verdict"]["summary"]


def test_screen_no_news_path():
    out = debate.screen({"balance_news": {"events": []}, "event_news": {}})
    assert out["no_news"] is True and out["topics"] == []
    assert out["verdict"]["no_news"] is True
    assert "无新闻" in out["verdict"]["summary"]


def test_screen_topics_capped_by_limits():
    items = [{"title": f"事件{i}", "impact": "high"} for i in range(20)]
    out = debate.screen({"event_news": {"events": items}})
    assert len(out["topics"]) == L.load_limits().news.max_topics


def test_screen_keywords_configurable():
    out = debate.screen({"event_news": {"events": [{"title": "某地出台出口配额新规"}]},
                         "major_keywords": ["出口配额"]})
    assert out["major_count"] == 1


# ───────────────────────────────────────────── 多轮评议
def test_run_no_news_skips_without_calling_agents():
    client = _Client()
    out = debate.DebateRunner(client=client).run({"no_news": True})
    assert out == {**out, "rounds": 0, "decision": "SKIPPED",
                   "stopped_reason": "no_major_news"}
    assert client.calls == []


def test_run_loops_until_approve():
    collector, auditor = _targets()
    client = _Client(["decision: MODIFY", "decision: APPROVE"])
    out = debate.DebateRunner(client=client).run(
        {"topic": "铜矿停产", "collector": collector, "auditor": auditor})
    assert out["rounds"] == 2
    assert out["decision"] == "APPROVE"
    assert out["stopped_reason"] == "approved"
    assert len(out["transcript"]) == 2
    # 第二轮采集官必须收到上一轮审计意见
    second = [c for c in client.calls if c["capability"] == "B"][1]
    assert second["params"]["critique"] == "decision: MODIFY"
    assert second["params"]["round"] == 2


def test_run_caps_rounds_by_limits():
    collector, auditor = _targets()
    client = _Client(["decision: MODIFY"] * 50)
    out = debate.DebateRunner(client=client).run(
        {"topic": "t", "collector": collector, "auditor": auditor, "max_rounds": 999})
    assert out["rounds"] == L.load_limits().debate.max_rounds
    assert out["stopped_reason"] == "max_rounds"


def test_run_truncates_each_message(tmp_path, monkeypatch):
    p = tmp_path / "limits.json"
    p.write_text(json.dumps({"debate": {"max_message_chars": 100,
                                       "max_total_chars": 10000,
                                       "max_rounds": 10}}), encoding="utf-8")
    monkeypatch.setenv("MULTI_AGENT_LIMITS", str(p))
    collector, auditor = _targets()
    runner = debate.DebateRunner(client=_LongClient(["decision: MODIFY"] * 5),
                                 limits=L.load_limits(reload=True))
    out = runner.run({"topic": "t", "collector": collector, "auditor": auditor})
    assert out["transcript"]
    assert all(len(t["draft"]) <= 100 for t in out["transcript"])


def test_run_stops_on_char_budget(tmp_path, monkeypatch):
    p = tmp_path / "limits.json"
    p.write_text(json.dumps({"debate": {"max_message_chars": 500,
                                       "max_total_chars": 1200,
                                       "max_rounds": 10}}), encoding="utf-8")
    monkeypatch.setenv("MULTI_AGENT_LIMITS", str(p))
    collector, auditor = _targets()
    runner = debate.DebateRunner(client=_LongClient(["decision: MODIFY"] * 10),
                                 limits=L.load_limits(reload=True))
    out = runner.run({"topic": "t", "collector": collector, "auditor": auditor})
    assert out["stopped_reason"] == "char_budget"
    assert out["chars_used"] >= 1200


def test_run_without_client_or_targets_fails_loudly():
    out = debate.DebateRunner(client=None).run({"topic": "t", "collector": {"capability": "B"},
                                                "auditor": {"capability": "auditor"}})
    assert out["ok"] is False and "client" in out["error"]
    out = debate.DebateRunner(client=_Client()).run({"topic": "t"})
    assert out["ok"] is False and "collector" in out["error"]


def test_register_exposes_debate_and_moderator():
    class RT:
        client = _Client()

        def __init__(self):
            self.h = {}

        def add_handler(self, cap, act, fn):
            self.h[(cap, act)] = fn

    rt = RT()
    debate.register(rt)
    assert ("debate", "run") in rt.h and ("moderator", "screen") in rt.h
    out = rt.h[("moderator", "screen")]({"event_news": {"events": []}}, {})
    assert out["no_news"] is True

# ───────────────────────────────────────────── 多空辩论（同一引擎的另一形态）
class _Sides:
    """多头 / 空头 / 裁决官各回各的话；记录派活顺序与入参。"""

    def __init__(self, judge_text="Recommendation: BUY", bull_text=None, bear_text=None):
        self.calls = []
        self.judge_text = judge_text
        self.bull_text = bull_text or "多头：需求走强，库存低位，看多"
        self.bear_text = bear_text or "空头：高库存叠加需求转弱，看空"

    def submit(self, capability, action, params=None, **kw):
        params = params or {}
        self.calls.append({"capability": capability, "params": params})
        text = {"bull": self.bull_text, "bear": self.bear_text,
                "judge": self.judge_text}.get(capability, "")
        return {"state": "succeeded", "result": {"text": text}}


def _adversarial(judge=True, **extra):
    params = {"mode": "adversarial",
              "bull": {"capability": "bull", "action": "argue"},
              "bear": {"capability": "bear", "action": "argue"}}
    if judge:
        params["judge"] = {"capability": "judge", "action": "decide"}
    params.update(extra)
    return params


def test_adversarial_runs_full_rounds_then_judges():
    client = _Sides()
    out = debate.DebateRunner(client=client).run(_adversarial(max_rounds=3))
    assert out["ok"] and out["mode"] == "adversarial"
    assert out["rounds"] == 3 and out["turns"] == 6
    assert out["decision"] == "BULL" and out["stopped_reason"] == "judged"
    assert out["verdict"] == "Recommendation: BUY"
    # 顺序：多头 → 空头 → 多头 → 空头 … 最后裁决官
    assert [c["capability"] for c in client.calls] == ["bull", "bear"] * 3 + ["judge"]
    # 第二轮回多头要拿到第一轮空头的意见
    second_bull = client.calls[2]["params"]
    assert second_bull["round"] == 2 and "看空" in second_bull["critique"]
    # 裁决官拿到的是一整份 transcript，不是最后一句话
    assert len(client.calls[-1]["params"]["transcript"]) == 3


def test_adversarial_requires_judge():
    out = debate.DebateRunner(client=_Sides()).run(_adversarial(judge=False))
    assert out["ok"] is False and "judge" in out["error"]


def test_adversarial_stance_parsing():
    for text, want in (("Recommendation: Sell", "BEAR"),
                       ("结论：做空", "BEAR"),
                       ("Rating: HOLD", "NEUTRAL"),
                       ("建议：做多", "BULL")):
        out = debate.DebateRunner(client=_Sides(judge_text=text)).run(_adversarial(max_rounds=1))
        assert out["decision"] == want, (text, out["decision"])


def test_adversarial_caps_full_rounds_by_limits():
    client = _Sides()
    out = debate.DebateRunner(client=client).run(_adversarial(max_rounds=999))
    assert out["rounds"] == L.load_limits().debate.max_rounds
    assert out["turns"] == 2 * L.load_limits().debate.max_rounds


class _Repeat:
    """两边每轮都回同一句话，用来验证收敛判据。"""

    def __init__(self):
        self.calls = []

    def submit(self, capability, action, params=None, **kw):
        self.calls.append(capability)
        text = "decision: MODIFY" if capability == "auditor" else "同一段稿件，一字不改"
        return {"state": "succeeded", "result": {"text": text}}


def test_converge_stops_when_both_sides_repeat():
    collector, auditor = _targets()
    client = _Repeat()
    out = debate.DebateRunner(client=client).run(
        {"topic": "t", "collector": collector, "auditor": auditor,
         "max_rounds": 10, "converge": True})
    assert out["stopped_reason"] == "converged"
    assert out["rounds"] == 2
    assert out["transcript"][-1]["novelty"]["pro"] == 0.0


def test_converge_off_by_default_keeps_rounds():
    collector, auditor = _targets()
    client = _Client(["decision: MODIFY"] * 20)
    out = debate.DebateRunner(client=client).run(
        {"topic": "t", "collector": collector, "auditor": auditor, "max_rounds": 3})
    assert out["stopped_reason"] == "max_rounds" and out["rounds"] == 3
    assert "novelty" not in out["transcript"][0]


def test_bad_mode_rejected():
    out = debate.DebateRunner(client=_Client()).run({"mode": "chat", "topic": "t"})
    assert out["ok"] is False and "mode" in out["error"]
