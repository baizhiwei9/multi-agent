"""外部复现报告（16 条）的回归：每条至少一个断言，防止再退化。

对应 issue：audit-limit 分支上"审计参数名不匹配 / 评议无法 APPROVE /
moderator-debate 未注册 / pack 压不出初筛结论 / ledger 读错字段 /
foreach 配置错误打死线程 / resume 幂等键 / handoff 不等回执 / 限额与文档漂移"。
"""
from __future__ import annotations

import pytest

from multi_agent import limits as L
from multi_agent.integrations import audit as A
from multi_agent.integrations import debate as D
from multi_agent.integrations import supervisor as S
from multi_agent.projects import (FuturesBalanceProject, Project, ProjectRegistry,
                                  RoleSpec)
from multi_agent.projects.news import DataHubNewsCollector


# 1. 审计官参数名：debate 发 `report`，audit 必须吃
def test_review_brief_accepts_report_field():
    out = A.run_action("review_brief", {"report": "RISK_BRIEF v2.0\nregime: normal"})
    assert isinstance(out, dict) and "verdict" in out


def test_review_brief_accepts_dict_brief():
    out = A.run_action("review_brief", {"brief": {"brief": "regime: normal"}})
    assert isinstance(out, dict) and "verdict" in out


# 2. 评议结论：真实回执的三种形态都要能判
@pytest.mark.parametrize("payload,expected", [
    ("pass", "APPROVE"),
    ("suspect", "MODIFY"),
    ("fail", "REJECT"),
    ({"verdict": {"decision": "pass"}}, "APPROVE"),
    ({"verdict": {"decision": "REJECT"}}, "REJECT"),
    ("decision: MODIFY", "MODIFY"),
    ("驳回", "REJECT"),
])
def test_decision_maps_real_shapes(payload, expected):
    assert D._decision(D._coerce_text(payload)) == expected


def test_coerce_text_reads_topics():
    out = D.screen({"event_news": {"events": [{"title": "铜矿停产", "impact": "high"}]}})
    assert "铜矿停产" in D._coerce_text(out)


# 3. moderator / debate 不再落到 executor 桩
def test_project_registers_builtin_handlers():
    class RT:
        client = object()

        def __init__(self):
            self.handlers = {}

        def add_handler(self, cap, action, fn):
            self.handlers[(cap, action)] = fn

    rt = RT()
    FuturesBalanceProject().register(rt)
    assert ("moderator", "screen") in rt.handlers
    assert ("debate", "run") in rt.handlers
    assert ("supervisor", "pack") in rt.handlers
    # executor 桩不再接管这三个角色
    handlers = FuturesBalanceProject().handlers()
    assert "moderator" not in handlers and "debate" not in handlers
    assert "supervisor" not in handlers


# 4. supervisor.pack 能吃协调官初筛回包
def test_pack_reads_screen_result():
    screen = D.screen({"event_news": {"events": [{"title": "铜矿停产", "impact": "high"}]}})
    out = S.pack({"payload": screen})
    assert out["chars"] > 0 and "铜矿停产" in out["payload"]


# 5. ledger 读 transcript；approved 不是阻塞
def test_ledger_reads_transcript_and_approved_not_blocked():
    out = S.ledger({"entries": {
        "rounds": 2, "decision": "APPROVE", "stopped_reason": "approved",
        "transcript": [{"round": 1, "decision": "MODIFY", "chars": 10},
                       {"round": 2, "decision": "APPROVE", "chars": 12}]}})
    assert out["count"] == 2 and out["blocked"] == 0 and out["delivered"] == 2
    assert [e["state"] for e in out["entries"]] == ["MODIFY", "APPROVE"]


def test_ledger_marks_max_rounds_blocked():
    out = S.ledger({"entries": {"rounds": 10, "decision": "MODIFY",
                                "stopped_reason": "max_rounds"}})
    assert out["blocked"] == 1


# 6. handoff 必须等回执，并把审计结论带回来
class _WaitClient:
    def __init__(self):
        self.kw = None

    def submit(self, capability, action, params=None, **kw):
        self.kw = kw
        return {"id": "t1", "state": "succeeded",
                "result": {"verdict": {"decision": "APPROVE"}}}


def test_handoff_waits_and_returns_verdict():
    client = _WaitClient()
    out = S.handoff({"payload": "报告正文", "round": 1}, client=client)
    assert client.kw.get("wait") is True
    assert client.kw.get("timeout_s", 0) > 0
    assert out["delivered"] is True and out["verdict"] == {"decision": "APPROVE"}


# 7. 限额：instances 超上限、显式文件不存在都要报错
def test_instances_over_max_raise():
    lim = L.load_limits()
    with pytest.raises(L.LimitsError):
        lim.instances_for("X", declared=lim.roles.max_instances_per_role + 1)


def test_explicit_missing_limits_file_raises(tmp_path):
    with pytest.raises(L.LimitsError):
        L.load_limits(str(tmp_path / "nope.json"), reload=True)


# 8. extra_pipelines 也要校验 capability
def test_validate_checks_extra_pipelines():
    class Bad(Project):
        name = "bad-extra"
        roles = [RoleSpec("R", ["do"], "")]
        extra_pipelines = ({"name": "x", "steps": [
            {"name": "s", "capability": "不存在", "action": "do", "params": {}}]},)

    problems = Bad().validate()
    assert any("未声明的 capability" in p for p in problems)
    with pytest.raises(ValueError):
        ProjectRegistry().register(Bad())


# 9. news.max_items_per_collector 不再是死键
def test_news_is_capped_by_limits():
    raw = [{"title": f"铜矿事件{i}"} for i in range(60)]
    out = DataHubNewsCollector(fetch=lambda hours: raw).collect({"commodities": ["铜"]})
    limit = L.load_limits().news.max_items_per_collector
    assert len(out["events"]) == limit
    assert out["truncated"] == 60 - limit