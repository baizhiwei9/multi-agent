"""审计接入 Coordinator 的测试：级别传递 + degraded 终态。

核心保证：
1. 协调者传 `audit_level` 才会升级，**不传永远是最省的 rule**；
2. 审计判 fail/suspect 时，整轮状态是 `degraded` 而不是 `succeeded`
   （"跑完了" ≠ "结果可信"，这个区分是审计官存在的意义）；
3. 流水线定义里真的挂了 audit 步骤，且它在 report 之后、final 之前。
"""
import pytest

from multi_agent.client import AgentRuntime, HubClient
from multi_agent.coordinator import Coordinator, DEFAULT_AUDIT_LEVEL, TERMINAL
from multi_agent.dbmanager import DBManager, DBClient
from multi_agent.integrations import audit
from multi_agent.pipelines import BALANCE_SHEET, STOCK_DAILY
from multi_agent.server import HubServer

TOKEN = "test-token"

BAL = {
    "commodity": "copper", "period": "2025A", "unit": "万吨",
    "supply": {"mine": 2318.0, "refined_primary": 2322.9,
               "refined_secondary": 534.8, "refined_total": 2857.8},
    "demand": {"refined_usage": 2815.9},
    "balance": {"surplus_deficit": 41.9, "stock_change": 41.0, "stocks_end": 180.9},
    "data_date": "2026-11-21", "as_of": "2026-11-22",
}
GOOD_REPORT = "# 铜平衡表\n\n## 供需\n过剩 41.9 万吨\n\n## 库存\n累库 41.0 万吨"
BAD_REPORT = "# 铜平衡表\n\n## 结论\n本轮去库，库存下降，价格看涨"


@pytest.fixture
def hub():
    s = HubServer(host="127.0.0.1", port=0, db_path=":memory:", token=TOKEN)
    s.start_background()
    yield s
    s.stop()


def _stack(hub, report_md):
    """起一整套最小角色：db / A / B / E / J / auditor。"""
    url = hub.url
    rts = []

    rt_db = AgentRuntime(url, "dbm", [{"name": "db", "actions": list(DBManager.ACTIONS)}],
                         token=TOKEN, concurrency=2, poll_wait=1)
    DBManager(":memory:").attach(rt_db)
    rt_db.start()
    rts.append(rt_db)

    rt_aud = AgentRuntime(url, "aud",
                          [{"name": "auditor", "actions": audit._ROLE_ACTIONS["auditor"]}],
                          token=TOKEN, concurrency=2, poll_wait=1)
    audit.register(rt_aud)
    rt_aud.start()
    rts.append(rt_aud)

    def role(name, action, fn):
        rt = AgentRuntime(url, f"a-{name}", [{"name": name, "actions": [action]}],
                          token=TOKEN, concurrency=1, poll_wait=1)
        rt.add_handler(name, action, fn)
        rt.start()
        rts.append(rt)

    role("A", "build_sheet", lambda p, c: BAL)
    role("B", "scan_news", lambda p, c: {"events": 0, "items": []})
    role("E", "report", lambda p, c: report_md)
    role("J", "final_report", lambda p, c: {"verdict": "ok"})
    return rts, DBClient(url, token=TOKEN)


def _run(hub, report_md, params=None):
    rts, db = _stack(hub, report_md)
    try:
        coord = Coordinator(hub.url, token=TOKEN, db=db)
        p = {"commodity": "copper", "hours": 6}
        p.update(params or {})
        run = coord.start("balance_sheet", p)
        return coord.wait(run["run_id"], timeout=120)
    finally:
        for rt in rts:
            rt.stop()


# ───────────────────────────────────── 流水线定义
def test_balance_pipeline_has_audit_step():
    names = [s["name"] for s in BALANCE_SHEET["steps"]]
    assert "audit" in names
    audit_step = next(s for s in BALANCE_SHEET["steps"] if s["name"] == "audit")
    assert audit_step["capability"] == "auditor"
    assert audit_step["depends_on"] == ["report"]
    # final 必须等 audit
    final = next(s for s in BALANCE_SHEET["steps"] if s["name"] == "final")
    assert "audit" in final["depends_on"]


def test_audit_step_reads_level_from_params():
    audit_step = next(s for s in BALANCE_SHEET["steps"] if s["name"] == "audit")
    assert audit_step["params"]["level"] == "${params.audit_level}"


def test_stock_pipeline_has_audit_step():
    names = [s["name"] for s in STOCK_DAILY["steps"]]
    assert "audit" in names


def test_default_audit_level_is_cheapest():
    assert DEFAULT_AUDIT_LEVEL == "rule"
    assert "degraded" in TERMINAL


# ───────────────────────────────────── 端到端
def test_good_report_succeeds(hub):
    snap = _run(hub, GOOD_REPORT)
    assert snap["state"] == "succeeded"
    assert snap["audit"]["verdict"] == "pass"
    assert snap["audit"]["level"] == "rule"
    assert snap["steps"]["audit"]["state"] == "succeeded"


def test_bad_report_degrades_run(hub):
    """关键语义：报告跑出来了，但审计判 fail → 整轮 degraded，不是 succeeded。"""
    snap = _run(hub, BAD_REPORT)
    assert snap["state"] == "degraded"
    assert snap["audit"]["verdict"] == "fail"
    codes = {f["code"] for f in snap["audit"]["findings"]}
    assert "narrative_conflict" in codes


def test_audit_level_passed_from_coordinator(hub):
    snap = _run(hub, GOOD_REPORT, {"audit_level": "stat"})
    assert snap["audit"]["level"] == "stat"


def test_audit_level_defaults_to_rule_when_omitted(hub):
    snap = _run(hub, GOOD_REPORT)
    assert snap["audit"]["level"] == "rule"


def test_audit_level_from_env(hub, monkeypatch):
    monkeypatch.setenv("AUDIT_LEVEL", "stat")
    snap = _run(hub, GOOD_REPORT)
    assert snap["audit"]["level"] == "stat"


def test_audit_findings_persisted_in_run(hub):
    snap = _run(hub, BAD_REPORT)
    findings = snap["audit"]["findings"]
    assert findings and all("severity" in f and "code" in f for f in findings)


def test_run_audit_summary_has_counts(hub):
    snap = _run(hub, BAD_REPORT)
    counts = snap["audit"]["counts"]
    assert counts["error"] >= 1
