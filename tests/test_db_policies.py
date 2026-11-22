"""DB 清理策略测试：不同项目不同规则。

核心断言：
1. 不同项目真的用不同规则（平衡表清新闻、股票保财报）
2. dry-run 算的数量和真删的**必须一致**（不然计划就是骗人的）
3. keep_days 按时间、keep_count 按条数，两种语义不能混
4. 未登记的集合要报警（"有人建了表从没清理"）
5. 单次删除有上限，不会一次锁表
6. schemas 永远不动（清了下次写入会重登，白丢历史）
"""
import time

import pytest

from multi_agent.db_policies import (
    BalanceSheetPolicy, BondPolicy, GlobalPolicy, MaintenanceScheduler,
    PROJECT_POLICIES, ProjectPolicy, Rule, StockPolicy, attach_policies,
    get_policy, plan_prune, register_policy,
)
from multi_agent.dbmanager import DBManager


# ───────────────────────────────────────────── 夹具
def _db_with_data(days_old_docs=None, days_old_streams=None):
    """建一个带数据的库：docs 与 streams 的时间戳可以拨到过去。"""
    db = DBManager(":memory:")
    for i in range(10):
        db.write("balance", f"copper:{i}", {"v": i})
    for i in range(300):
        db.append("news", {"i": i})
    if days_old_docs:
        for coll, days in days_old_docs.items():
            db._conn.execute("UPDATE docs SET updated=? WHERE collection=?",
                             (time.time() - days * 86400, coll))
    if days_old_streams:
        for st, days in days_old_streams.items():
            db._conn.execute("UPDATE streams SET created=? WHERE stream=?",
                             (time.time() - days * 86400, st))
    db._conn.commit()
    return db


# ───────────────────────────────────────────── 策略定义
def test_three_project_policies_exist():
    assert {"balance_sheet", "stock", "bond", "global"} <= set(PROJECT_POLICIES)


def test_policies_differ_by_project():
    bs = get_policy("balance_sheet")
    st = get_policy("stock")
    bond = get_policy("bond")
    # 平衡表要清新闻，股票不清 balance
    assert bs.rule_for("news", "stream") is not None
    assert bs.rule_for("financials") is None
    # 股票财报不清理（rule 存在但无 keep_days/count）
    fin = st.rule_for("financials")
    assert fin is not None and fin.keep_days is None and fin.keep_count is None
    # 债券要素永久
    b = bond.rule_for("bond")
    assert b is not None and b.keep_days is None


def test_policy_describe():
    r = Rule("news", kind="stream", keep_days=30, keep_count=50000)
    assert "30天" in r.describe() and "留50000条" in r.describe()


def test_unknown_project_falls_back_to_global():
    p = get_policy("does_not_exist")
    assert p.name == "global"


def test_register_custom_policy():
    class MyPolicy(ProjectPolicy):
        def __init__(self):
            super().__init__(name="mytest", description="测试",
                             rules=[Rule("mydata", keep_days=1)])

    register_policy(MyPolicy())
    try:
        assert "mytest" in PROJECT_POLICIES
        assert get_policy("mytest").name == "mytest"
    finally:
        PROJECT_POLICIES.pop("mytest", None)


def test_register_rejects_non_policy():
    with pytest.raises(TypeError):
        register_policy({"name": "x"})


# ───────────────────────────────────────────── 计划 vs 实删一致
def test_dry_run_matches_actual_delete():
    """最重要的断言：计划说删 N 条，真删就必须正好删 N 条。"""
    db = _db_with_data(days_old_docs={"balance": 100},
                       days_old_streams={"news": 60})
    plan = plan_prune(db, "balance_sheet", dry_run=True)
    assert plan["total_would_delete"] > 0

    actual = plan_prune(db, "balance_sheet", dry_run=False)
    assert actual["total_would_delete"] == plan["total_would_delete"], \
        f"计划 {plan['total_would_delete']} ≠ 实删 {actual['total_would_delete']}"


def test_dry_run_does_not_modify_data():
    db = _db_with_data(days_old_docs={"balance": 100})
    before = db.stats()
    plan_prune(db, "balance_sheet", dry_run=True)
    assert db.stats() == before


def test_keep_count_semantics():
    """keep_count=3 → 10 条里删 7 条。"""
    db = _db_with_data()
    out = plan_prune(db, "balance_sheet", dry_run=True)
    action = next(a for a in out["actions"] if a["target"] == "balance")
    assert action["would_delete"] == 7
    assert action["matched"] == 7


def test_keep_days_semantics():
    """keep_days=30，300 条全在 60 天前 → 全删。"""
    db = _db_with_data(days_old_streams={"news": 60})
    out = plan_prune(db, "balance_sheet", dry_run=True)
    action = next(a for a in out["actions"] if a["target"] == "news")
    assert action["would_delete"] == 300


def test_recent_data_not_deleted():
    """新数据一条都不能删。"""
    db = _db_with_data()
    out = plan_prune(db, "balance_sheet", dry_run=True)
    stream_actions = [a for a in out["actions"] if a["kind"] == "stream"]
    assert all(a["would_delete"] == 0 for a in stream_actions) or not stream_actions


# ───────────────────────────────────────────── 未登记集合
def test_unregistered_collection_reported():
    db = _db_with_data()
    db.write("weird_table", "k1", {"x": 1})
    out = plan_prune(db, "balance_sheet", dry_run=True)
    assert any("weird_table" in u for u in out["unregistered"])


def test_known_name_on_either_side_not_reported():
    """news 登记在 streams 侧，docs 里同名不该被报成未登记（会淹没真问题）。"""
    db = _db_with_data()
    db.write("news", "n1", {"x": 1})
    out = plan_prune(db, "balance_sheet", dry_run=True)
    assert not any("news" in u for u in out["unregistered"]), out["unregistered"]


# ───────────────────────────────────────────── 安全约束
def test_schemas_never_pruned():
    db = _db_with_data()
    before = len(db.collections()["schemas"])
    assert before > 0
    plan_prune(db, "balance_sheet", dry_run=False)
    assert len(db.collections()["schemas"]) == before


def test_max_delete_per_run_caps():
    """单次删除有上限，不会一次锁表。"""
    pol = ProjectPolicy(name="cap_test", description="", max_delete_per_run=50,
                        rules=[Rule("news", kind="stream", keep_days=1)])
    register_policy(pol)
    try:
        db = _db_with_data(days_old_streams={"news": 60})
        out = plan_prune(db, "cap_test", dry_run=True)
        total = sum(a["would_delete"] for a in out["actions"])
        assert total <= 50, f"超出上限：{total}"
        # 被截断要标出来
        assert all(a.get("truncated", False) or a["would_delete"] <= 50
                   for a in out["actions"])
    finally:
        PROJECT_POLICIES.pop("cap_test", None)


def test_prune_on_non_dbmanager_returns_error():
    """传错对象要明确报错，不能静默什么都不做。"""
    out = plan_prune(object(), "balance_sheet")
    assert out["ok"] is False and "DBManager" in out["error"]


# ───────────────────────────────────────────── 股票 / 债券口径
def test_stock_policy_keeps_financials():
    db = DBManager(":memory:")
    for i in range(100):
        db.write("financials", f"000001:{i}", {"v": i})
    db._conn.execute("UPDATE docs SET updated=?", (time.time() - 3000 * 86400,))
    db._conn.commit()
    out = plan_prune(db, "stock", dry_run=True)
    fin = [a for a in out["actions"] if a["target"] == "financials"]
    assert not fin, "财报不该出现在清理计划里"


def test_bond_policy_keeps_bond_elements():
    db = DBManager(":memory:")
    for i in range(50):
        db.write("bond", f"{i:06d}", {"coupon": 2.5})
    db._conn.execute("UPDATE docs SET updated=?", (time.time() - 5000 * 86400,))
    db._conn.commit()
    out = plan_prune(db, "bond", dry_run=True)
    assert not [a for a in out["actions"] if a["target"] == "bond"]


# ───────────────────────────────────────────── 定时器
def test_scheduler_run_once():
    db = _db_with_data(days_old_docs={"balance": 100})
    sched = MaintenanceScheduler(db, project="balance_sheet", dry_run=True)
    results = sched.run_once()
    assert len(results) == 1
    assert results[0]["project"] == "balance_sheet"
    assert results[0]["dry_run"] is True
    assert sched.status()["runs"] == 1


def test_scheduler_multiple_projects():
    db = _db_with_data()
    sched = MaintenanceScheduler(db, project=["balance_sheet", "stock"],
                                 dry_run=True)
    results = sched.run_once()
    assert {r["project"] for r in results} == {"balance_sheet", "stock"}


def test_scheduler_start_stop():
    db = _db_with_data()
    sched = MaintenanceScheduler(db, project="balance_sheet", dry_run=True,
                                 interval_s=60)
    assert sched.status()["running"] is False
    sched.start()
    try:
        assert sched.status()["running"] is True
    finally:
        sched.stop()
    assert sched.status()["running"] is False


def test_scheduler_on_result_callback():
    seen = []
    db = _db_with_data()
    sched = MaintenanceScheduler(db, project="balance_sheet", dry_run=True,
                                 on_result=lambda r: seen.append(r))
    sched.run_once()
    assert len(seen) == 1


# ───────────────────────────────────────────── Hub 接入
def test_attach_policies_registers_actions():
    class FakeRuntime:
        def __init__(self):
            self.handlers = {}

        def add_handler(self, cap, act, fn):
            self.handlers[(cap, act)] = fn

    rt = FakeRuntime()
    db = _db_with_data(days_old_docs={"balance": 100})
    sched = attach_policies(db, rt, default_project="balance_sheet")
    try:
        assert ("db", "prune_plan") in rt.handlers
        assert ("db", "prune") in rt.handlers
        assert ("db", "policies") in rt.handlers
        assert ("db", "maintenance_status") in rt.handlers

        plan = rt.handlers[("db", "prune_plan")]({}, {})
        assert plan["dry_run"] is True and plan["ok"] is True

        policies = rt.handlers[("db", "policies")]({}, {})
        names = {p["name"] for p in policies["policies"]}
        assert {"balance_sheet", "stock", "bond"} <= names

        st = rt.handlers[("db", "maintenance_status")]({}, {})
        assert st["projects"] == ["balance_sheet"]
    finally:
        sched.stop()
