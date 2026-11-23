"""项目扩展点测试：三个实现、五类接口、继承与装配。

核心断言：
1. 框架自带三个项目都登记成功，角色表/流水线/审计口径互不串味；
2. 新产品只继承 Project 就能接入（不写 handlers 也能注册，但调用会是 degraded）；
3. 新闻采集官与报告器的契约固定（形状、去重、不造假、无占位符）；
4. install_pipelines 能把项目流水线塞进 PIPELINES，审计步骤落在末尾。
"""
from __future__ import annotations

import json

import pytest

from multi_agent import limits as LIM
from multi_agent import pipelines as P
from multi_agent.projects import (REGISTRY, BalanceSheetReporter,
                                  DataHubNewsCollector, FuturesBalanceProject,
                                  MattReporter, MinimalReportProject,
                                  Project, ProjectRegistry, RoleSpec,
                                  StaticNewsCollector, StockMattProject,
                                  install_pipelines)


# ───────────────────────────────────────────── 三个实现
def test_builtin_projects_registered():
    names = REGISTRY.names()
    assert {"futures-balance", "stock-matt", "min-report"} <= set(names)


def test_futures_project_declaration():
    p = REGISTRY.get("futures-balance")
    assert isinstance(p, FuturesBalanceProject)
    assert {"A", "B", "T", "F", "Q", "C", "D", "E", "J"} <= set(p.capabilities())
    assert p.audit_project == "balance_sheet"
    steps = [s["name"] for s in p.pipeline["steps"]]
    assert steps[0] == "build" and steps[-2:] == ["audit", "final"]
    assert p.validate() == []


def test_stock_project_declaration():
    p = REGISTRY.get("stock-matt")
    assert isinstance(p, StockMattProject)
    assert {"M1", "M2", "M3", "M4", "M5", "news"} <= set(p.capabilities())
    # 审计口径与期货不同：这是"审计官按项目分"的落点
    assert p.audit_project == "stock"
    assert p.reporter().name == "matt-stock-md"


def test_minimal_project_runs():
    p = REGISTRY.get("min-report")
    h = p.handlers()
    assert h["collect"]["run"]({"topic": "t"}, {})["topic"] == "t"
    md = h["render"]["md"]({"topic": "t"}, {})
    assert "最小报告" in md


# ───────────────────────────────────────────── 继承
def test_subclass_can_add_role_without_touching_framework():
    class Extended(FuturesBalanceProject):
        name = "futures-ext"
        roles = FuturesBalanceProject.roles + [RoleSpec("X", ["extra"], "新增角色")]

    p = Extended()
    assert "X" in p.capabilities()
    assert p.handlers()["X"]["extra"]({}, {})["degraded"] is True
    assert p.validate() == []


def test_project_without_handlers_is_registrable_but_degraded():
    class Bare(Project):
        name = "bare"
        roles = [RoleSpec("R", ["do"], "只有声明")]
        pipeline = {"name": "bare", "steps": [
            {"name": "do", "capability": "R", "action": "do", "params": {}}]}

    reg = ProjectRegistry()
    reg.register(Bare())
    got = reg.get("bare").handlers()
    assert got == {}          # 没实现
    assert reg.get("bare").validate() == []


def test_registry_rejects_unknown_capability_in_pipeline():
    class Bad(Project):
        name = "bad"
        roles = [RoleSpec("R", ["do"], "")]
        pipeline = {"name": "bad", "steps": [
            {"name": "x", "capability": "不存在", "action": "do", "params": {}}]}

    reg = ProjectRegistry()
    with pytest.raises(ValueError, match="未声明的 capability"):
        reg.register(Bad())


def test_registry_rejects_duplicate_name():
    reg = ProjectRegistry()
    reg.register(MinimalReportProject())
    with pytest.raises(ValueError, match="已登记"):
        reg.register(MinimalReportProject())


# ───────────────────────────────────────────── 新闻采集官
def test_static_news_collector_dedups():
    c = StaticNewsCollector([{"title": "A", "published": "2026-11-22"},
                             {"title": "A", "published": "2026-11-22"},
                             {"title": "B"}])
    out = c.collect()
    assert len(out["events"]) == 2 and out["deduped"] == 1
    assert all(e["impact"] is None for e in out["events"])   # 采集官不判方向


def test_datahub_news_without_fetcher_is_degraded_not_fake():
    out = DataHubNewsCollector().collect({"hours": 24})
    assert out["degraded"] is True and out["events"] == []
    assert "未注入" in out["reason"]


def test_datahub_news_filters_and_normalizes():
    raw = [{"title": "铜矿罢工", "published": "2026-11-22", "site": "x"},
           {"title": "无关新闻", "published": "2026-11-22"},
           {"title": "铜矿罢工", "published": "2026-11-22"}]
    out = DataHubNewsCollector(fetch=lambda hours: raw).collect(
        {"hours": 24, "commodities": ["铜"]})
    assert len(out["events"]) == 1 and out["scanned"] == 3
    assert out["events"][0]["event_id"]


# ───────────────────────────────────────────── 报告器
def test_balance_reporter_has_sections_and_no_placeholder():
    md = BalanceSheetReporter().build({
        "commodity": "copper", "unit": "万吨",
        "balance": {"supply": {"refined_total": 2857.8},
                    "demand": {"refined_usage": 2815.9},
                    "balance": {"surplus_deficit": 41.9, "stocks_end": 180.9}},
        "audit": {"verdict": "pass", "findings": []}})
    assert "## 供需平衡" in md and "## 审计结论" in md
    assert "TODO" not in md and "占位" not in md


def test_matt_reporter_handles_missing_data():
    md = MattReporter().build({"data_ok": False, "gap": "上游未给指数"})
    assert "数据缺口" in md and "（无数据）" in md


def test_reporter_save(tmp_path):
    path = tmp_path / "r" / "a.md"
    out = MattReporter().save("# x\n", str(path))
    assert path.exists() and out == str(path)


# ───────────────────────────────────────────── 装配
def test_install_pipelines_registers_project_dags():
    added = install_pipelines()
    assert {"futures_balance", "stock_matt", "min_report"} <= set(added)
    spec = P.PIPELINES["futures_balance"]
    assert spec["project"] == "futures-balance"
    assert spec["audit_project"] == "balance_sheet"
    assert any(s["capability"] == "auditor" for s in spec["steps"])


def test_describe_exposes_extension_points():
    d = REGISTRY.get("stock-matt").describe()
    for key in ("roles", "pipeline", "audit_project", "news", "reporter", "implemented"):
        assert key in d
    assert d["news"] == "rule-news"


# ───────────────────────────────────────────── 流水线依赖：风控解耦，审计不脱报告
def test_futures_pipeline_risk_audit_deps():
    """risk 只依赖 build（不因无新闻而沉默）；audit 必须等 report 且真拿到报告。"""
    from multi_agent.projects.futures_balance import BALANCE_PIPELINE
    steps = {s["name"]: s for s in BALANCE_PIPELINE["steps"]}

    # 风控官：只与建表链绑定，不受新闻/DB/报告链故障牵连
    assert steps["risk"]["depends_on"] == ["build"]
    assert steps["risk"]["params"]["data"] == "${params.market_data}"

    # 审计师：action=report，必须等报告落盘，否则审的是空报告；
    # 且 final 只依赖 audit —— audit 脱开 report 会让终审早于报告
    assert set(steps["audit"]["depends_on"]) == {"report", "risk_review"}
    assert steps["audit"]["params"]["report"] == "${steps.report.result}"
    assert steps["audit"]["params"]["balance"] == "${steps.build.result}"
    assert steps["final"]["depends_on"] == ["audit"]


# ───────────────────────────────────────────── 多轮评议流水线与角色
def test_debate_pipeline_and_roles():
    from multi_agent.projects.futures_balance import DEBATE_PIPELINE
    from multi_agent.projects import install_pipelines

    p = REGISTRY.get("futures-balance")
    assert {"moderator", "debate"} <= set(p.capabilities())
    assert "scan_events" in p.actions_for("B")
    assert p.describe()["pipelines"] == ["futures_balance", "futures_balance_debate"]

    installed = install_pipelines()
    assert {"futures_balance", "futures_balance_debate"} <= set(installed)
    assert P.PIPELINES["futures_balance_debate"]["project"] == "futures-balance"
    assert P.PIPELINES["futures_balance"]["name"] == "futures_balance"   # 简版仍在

    steps = {s["name"]: s for s in DEBATE_PIPELINE["steps"]}
    assert steps["moderator_screen"]["depends_on"] == ["news_balance", "news_event"]
    assert steps["debate"]["depends_on"] == ["moderator_screen"]
    assert steps["debate"]["params"]["no_news"] == "${steps.moderator_screen.result.no_news}"
    assert steps["report"]["depends_on"] == ["debate"]
    assert steps["risk"]["depends_on"] == ["build"]
    assert set(steps["audit"]["depends_on"]) == {"report", "risk_review"}
    assert steps["final"]["depends_on"] == ["audit"]


# ───────────────────────────────────────────── 角色数量：要几个 Agent
def test_role_plan_expands_instances():
    p = REGISTRY.get("futures-balance")
    plan = p.agent_plan()
    by_cap = {r["capability"]: r for r in plan["roles"]}
    assert by_cap["B"]["declared_instances"] == 2
    assert by_cap["B"]["ids"] == ["B-1", "B-2"]
    assert by_cap["B"]["role_arg"] == "B:scan_news,scan_events"
    assert by_cap["E"]["instances"] == 1
    ids = [x["id"] for x in plan["agents"]]
    assert len(ids) == plan["total"] and {"B-1", "B-2"} <= set(ids)
    # describe 也要能看出规模
    d = p.describe()
    assert d["agents"] == plan["total"]
    assert {r["capability"]: r["instances"] for r in d["roles"]}["B"] == 2


def test_limits_override_role_instances(tmp_path):
    f = tmp_path / "limits.json"
    f.write_text(json.dumps({"roles": {"overrides": {"B": 3, "E": 2}}}), encoding="utf-8")
    lim = LIM.load_limits(str(f), reload=True)
    by_cap = {r["capability"]: r for r in
              REGISTRY.get("futures-balance").role_plan(lim)}
    assert by_cap["B"]["instances"] == 3 and len(by_cap["B"]["ids"]) == 3
    assert by_cap["E"]["instances"] == 2
    assert by_cap["A"]["instances"] == 1          # 未覆盖仍按角色表/兜底


def test_manifests_filter_by_capability():
    p = REGISTRY.get("futures-balance")
    only = p.manifests(["B"])
    assert [m["name"] for m in only] == ["B"] and only[0]["instances"] == 2
    assert len(p.manifests()) > len(only)


def test_role_instances_must_be_positive_integer():
    class Bad(Project):
        name = "bad-inst"
        roles = [RoleSpec("R", ["do"], "x", instances=0)]

    problems = Bad().validate()
    assert any("instances" in x for x in problems)
    with pytest.raises(ValueError, match="instances"):
        ProjectRegistry().register(Bad())
