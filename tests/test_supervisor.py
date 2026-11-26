"""管理员 / 监理（supervisor）：送审件打包、交给审计官、送审台账。"""
from __future__ import annotations

from multi_agent import limits as L
from multi_agent.integrations import supervisor as S


class _Client:
    """假 Hub 客户端：记录派活，按脚本回审计结论。"""

    def __init__(self, state: str = "succeeded", verdict: str = "ok"):
        self.calls = []
        self.state = state
        self.verdict = verdict

    def submit(self, capability, action, params=None, **kw):
        self.calls.append({"capability": capability, "action": action,
                           "params": params or {}, "kw": kw})
        return {"id": "t_1", "state": self.state,
                "result": {"verdict": self.verdict}}


# ───────────────────────────────────────────── pack
def test_pack_truncates_to_audit_limit():
    lim = L.load_limits().audit
    out = S.pack({"payload": "x" * (lim.max_output_chars + 120),
                  "project": "balance_sheet"})
    assert out["truncated"] is True
    assert out["chars"] == lim.max_output_chars
    assert out["delivered"] is False
    assert any("截断" in n for n in out["notes"])


def test_pack_counts_over_citations_and_names_extra():
    lim = L.load_limits().audit
    out = S.pack({"payload": {"report": "审计稿"},
                  "citations": lim.max_citations + 2})
    assert out["over_cited"] is True and out["extra_citations"] == 2
    assert any("多出 2 处" in n for n in out["notes"])


def test_pack_flattens_nested_result():
    out = S.pack({"payload": {"result": {"draft": "初筛观点：铜矿停产"}}})
    assert "铜矿停产" in out["payload"]


def test_pack_reads_citation_list_from_payload():
    out = S.pack({"payload": {"report": "稿", "issues": [{"id": 1}, {"id": 2}]}})
    assert out["citations"] == 2


# ───────────────────────────────────────────── handoff
def test_handoff_without_client_is_degraded_not_fake_success():
    out = S.handoff({"payload": "稿"})
    assert out["delivered"] is False and out["degraded"] is True
    assert "client" in out["reason"]


def test_handoff_sends_to_auditor_and_brings_verdict_back():
    client = _Client(verdict="pass")
    out = S.handoff({"payload": "报告正文", "project": "balance_sheet", "round": 2},
                    client=client)
    assert out["delivered"] is True and out["degraded"] is False
    assert out["verdict"] == "pass" and out["task_id"] == "t_1"
    call = client.calls[0]
    assert call["capability"] == "auditor" and call["action"] == "report"
    assert call["params"]["report"] == "报告正文"
    assert call["params"]["round"] == 2
    assert call["params"]["project"] == "balance_sheet"


def test_handoff_accepts_pre_packed_brief():
    client = _Client()
    brief = S.pack({"payload": "已经压好", "project": "stock"})
    out = S.handoff({"brief": brief, "round": 1}, client=client)
    assert out["delivered"] is True and out["chars"] == brief["chars"]


# ───────────────────────────────────────────── ledger
def test_ledger_summarizes_rounds_and_pack():
    out = S.ledger({"entries": {"rounds": 2, "decision": "APPROVE"},
                    "pack": {"payload": "稿", "chars": 10}})
    assert out["entries"][0]["state"] == "packed"
    assert out["count"] >= 2 and out["delivered"] >= 1


def test_ledger_marks_stopped_reason():
    out = S.ledger({"entries": {"rounds": 0, "decision": "SKIPPED",
                                "stopped_reason": "no_major_news"}})
    assert out["blocked"] == 0          # no_major_news 是跳过，不算阻塞
    states = [e["state"] for e in out["entries"]]
    assert "no_major_news" in states


# ───────────────────────────────────────────── 接入
def test_register_exposes_three_actions():
    class RT:
        client = None

        def __init__(self):
            self.handlers = {}

        def add_handler(self, cap, action, fn):
            self.handlers[(cap, action)] = fn

    rt = RT()
    S.register(rt)
    assert set(rt.handlers) == {("supervisor", "pack"), ("supervisor", "handoff"),
                                ("supervisor", "ledger"), ("supervisor", "tune")}
    assert rt.handlers[("supervisor", "pack")]({"payload": "x"}, {})["chars"] == 1
    assert rt.handlers[("supervisor", "handoff")]({"payload": "x"}, {})["degraded"] is True
    tuned = rt.handlers[("supervisor", "tune")]({"weights": {"supply_shock": 0.9}}, {})
    assert tuned["weights"]["supply_shock"] == 0.9