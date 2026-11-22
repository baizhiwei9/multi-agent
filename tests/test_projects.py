"""项目扩展点测试：三个实现、五类接口、继承与装配。

核心断言：
1. 框架自带三个项目都登记成功，角色表/流水线/审计口径互不串味；
2. 新产品只继承 Project 就能接入（不写 handlers 也能注册，但调用会是 degraded）；
3. 新闻采集官与报告器的契约固定（形状、去重、不造假、无占位符）；
4. install_pipelines 能把项目流水线塞进 PIPELINES，审计步骤落在末尾。
"""
from __future__ import annotations

import pytest

from multi_agent import pipelines as P
from multi_agent.projects import (REGISTRY, BalanceSheetReporter,
                                  DataHubNewsCollector, FuturesBalanceProject,
                                  MattReporter, MinimalReportProject,
                                  Project, ProjectRegistry, RoleSpec,
                                  StaticNewsCollector, StockMattProject,
                                  install_pipelines, register)


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