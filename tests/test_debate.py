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