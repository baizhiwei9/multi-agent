"""示例项目（examples/my_project）的验收：能跑通 = "照抄即可继承框架"。

这里刻意只测"框架契约"，不测业务数值：
· 声明自洽（validate 空）、角色/流水线能展开成 Agent 计划；
· 内置角色没被项目覆盖（否则 `project agent` 起出来是空壳）；
· 端到端：采集 → 风控 → 送审 → 审核 → 报告，全链 succeeded 且报告有内容。
"""
from __future__ import annotations

from multi_agent import pipelines as P

from examples.my_project import project as MP
from examples.my_project import run_demo


def test_project_declaration_is_self_consistent():
    p = MP.CommodityMonitorProject()
    assert p.validate() == []
    d = p.describe()
    caps = {r["capability"] for r in d["roles"]}
    assert {"collector", "risk", "report", "supervisor", "debate"} <= caps
    # 内置角色不算"项目实现的"
    assert set(d["implemented"]) == {"collector", "risk", "report"}
    assert d["pipeline"] == "commodity_monitor"
    assert [s["name"] for s in p.pipeline["steps"]] == [
        "collect", "risk", "pack", "review", "report"]


def test_install_registers_pipeline_and_agent_plan():
    p = MP.install()
    try:
        assert "commodity_monitor" in P.PIPELINES
        plan = p.agent_plan()
        assert plan["total"] == len(plan["agents"]) == 5     # 5 个角色各 1 个实例
        roles = {r["capability"]: r for r in plan["roles"]}
        assert roles["collector"]["ids"] == ["collector-1"]
    finally:
        P.PIPELINES.pop("commodity_monitor", None)


def test_builtin_roles_are_not_stubbed_by_project():
    """监理/评议/审核由框架注册：项目 handlers 里不能出现，否则覆盖真实现。"""
    h = MP.CommodityMonitorProject().handlers()
    assert set(h) == {"collector", "risk", "report"}
    for cap in ("supervisor", "debate", "moderator", "auditor"):
        assert cap not in h


def test_example_pipeline_runs_end_to_end():
    snap = run_demo.run(quiet=True)
    assert snap["state"] == "succeeded"
    for name in ("collect", "risk", "pack", "review", "report"):
        assert snap["steps"][name]["state"] == "succeeded", snap["steps"][name]
    md = snap["steps"]["report"]["result"]["markdown"]
    assert "铜矿" in md                       # 采集官的事件进了报告
    assert "审核结论" in md                    # 审核官的回执进了报告
    assert snap["steps"]["risk"]["result"]["aggregate_signal"] == "danger"