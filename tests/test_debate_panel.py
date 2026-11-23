"""多方辩论（participants[] + judge）：顺序发言、上下文、早停、闸门、与老两方兼容。"""
from __future__ import annotations

import json

import pytest

from multi_agent import limits as L
from multi_agent.integrations import debate


class _PanelClient:
    """按脚本回话的假 Hub：每个 capability 依次吐一条发言，judge 单独走脚本。"""

    def __init__(self, speeches=None, judge=None, raise_on=None):
        self.speeches = list(speeches or [])
        self.judge = list(judge or [])
        self.raise_on = raise_on
        self.calls = []

    def submit(self, capability, action, params=None, **kw):
        params = params or {}
        self.calls.append({"capability": capability, "action": action, "params": params})
        if self.raise_on and capability == self.raise_on:
            raise RuntimeError("agent 挂了")
        if capability == "judge":
            reply = self.judge.pop(0) if self.judge else {"decision": "MODIFY"}
            return {"state": "succeeded", "result": reply}
        reply = self.speeches.pop(0) if self.speeches else {"text": "我看多"}
        return {"state": "succeeded", "result": reply}


def _panel(judge=None, **kw):
    params = {
        "topic": "跨服商城折扣券上线",
        "participants": [
            {"capability": "bull", "action": "argue", "role": "bull"},
            {"capability": "bear", "action": "argue", "role": "bear"},
            {"capability": "risk", "action": "argue", "role": "risk"},
        ],
        "max_rounds": 1,
        **kw,
    }
    if judge is not None:
        params["judge"] = {"capability": "judge", "action": "decide"}
    return params


def test_panel_speaks_in_order_and_shares_transcript():
    client = _PanelClient(speeches=[{"text": "多头说：需求强"},
                                    {"text": "空头说：补贴过高"},
                                    {"text": "风控说：预算可控"}])
    out = debate.DebateRunner(client=client).run(_panel())
    order = [c["capability"] for c in client.calls]
    assert order == ["bull", "bear", "risk"]                  # 顺序发言，不并行
    assert client.calls[1]["params"]["transcript"].find("多头说") >= 0
    assert client.calls[1]["params"]["prior"].startswith("多头说")
    assert client.calls[2]["params"]["participants"] == ["bull", "bear", "risk"]
    assert out["mode"] == "panel" and out["speeches"] == 3
    assert out["participants"] == ["bull", "bear", "risk"]
    assert out["stopped_reason"] == "max_rounds"


def test_panel_judge_converged_stops_early():
    client = _PanelClient(speeches=[{"text": "多头"}, {"text": "空头"}, {"text": "风控"}],
                          judge=[{"decision": "APPROVE", "converged": True}])
    out = debate.DebateRunner(client=client).run(_panel(judge=True, max_rounds=5))
    assert out["stopped_reason"] == "judge_converged"
    assert out["decision"] == "APPROVE" and out["rounds"] == 1
    assert out["judge"]["decision"] == "APPROVE"
    assert [c["capability"] for c in client.calls][-1] == "judge"


def test_panel_consensus_stops_without_judge():
    client = _PanelClient(speeches=[{"text": "同意", "stance": "APPROVE"},
                                    {"text": "同意", "stance": "APPROVE"},
                                    {"text": "同意", "stance": "APPROVE"}])
    out = debate.DebateRunner(client=client).run(_panel(max_rounds=5))
    assert out["stopped_reason"] == "consensus" and out["decision"] == "APPROVE"
    assert out["rounds"] == 1


def test_panel_respects_max_speeches(tmp_path, monkeypatch):
    p = tmp_path / "limits.json"
    p.write_text(json.dumps({"debate": {"max_participants": 3, "max_speeches": 4,
                                        "max_rounds": 10}}), encoding="utf-8")
    monkeypatch.setenv("MULTI_AGENT_LIMITS", str(p))
    client = _PanelClient(speeches=[{"text": f"发言{i}"} for i in range(20)])
    out = debate.DebateRunner(client=client,
                              limits=L.load_limits(reload=True)).run(
        _panel(max_rounds=10))
    assert out["stopped_reason"] == "max_speeches" and out["speeches"] == 4


def test_panel_requires_complete_targets():
    out = debate.DebateRunner(client=_PanelClient()).run(
        {"topic": "t", "participants": [{"capability": "B"}]})
    assert out["ok"] is False and "capability" in out["error"]


def test_panel_reports_judge_error():
    client = _PanelClient(speeches=[{"text": "多头"}, {"text": "空头"}, {"text": "风控"}],
                          raise_on="judge")
    out = debate.DebateRunner(client=client).run(_panel(judge=True))
    assert out["ok"] is False and out["stopped_reason"] == "judge_error"
    assert "agent 挂了" in out["error"]


def test_judge_only_at_the_end_saves_tokens():
    client = _PanelClient(speeches=[{"text": f"发言{i}"} for i in range(10)],
                          judge=[{"decision": "MODIFY"}])
    out = debate.DebateRunner(client=client).run(
        _panel(judge=True, max_rounds=2, judge_every_round=False))
    judge_calls = [c for c in client.calls if c["capability"] == "judge"]
    assert len(judge_calls) == 1
    assert out["rounds"] == 2 and out["stopped_reason"] == "max_rounds"


def test_pair_path_unchanged_and_mode_tagged():
    class _Old(_PanelClient):
        def submit(self, capability, action, params=None, **kw):
            task = super().submit(capability, action, params, **kw)
            if capability == "auditor":
                task["result"] = {"verdict": {"decision": "APPROVE"}}
            else:
                task["result"] = {"text": f"draft r{(params or {}).get('round')}"}
            return task

    client = _Old()
    out = debate.DebateRunner(client=client).run({
        "topic": "t",
        "collector": {"capability": "B", "action": "scan_news"},
        "auditor": {"capability": "auditor", "action": "review_brief"},
    })
    # 合并后两方走统一执行器的 audit 形态（不再是独立 pair 分支），行为不变：
    # 审计说 APPROVE 即收敛，turns 记两次发言。
    assert out["mode"] == "audit" and out["decision"] == "APPROVE"
    assert out["rounds"] == 1 and out["turns"] == 2


def test_participants_win_over_pair_params():
    client = _PanelClient(speeches=[{"text": "多头"}, {"text": "空头"}, {"text": "风控"}])
    out = debate.DebateRunner(client=client).run(_panel(
        collector={"capability": "B", "action": "scan_news"},
        auditor={"capability": "auditor", "action": "review_brief"}))
    assert out["mode"] == "panel"


def test_limits_reject_speeches_below_participants(tmp_path, monkeypatch):
    p = tmp_path / "limits.json"
    p.write_text(json.dumps({"debate": {"max_participants": 6, "max_speeches": 3}}),
                 encoding="utf-8")
    monkeypatch.setenv("MULTI_AGENT_LIMITS", str(p))
    with pytest.raises(L.LimitsError):
        L.load_limits(reload=True)