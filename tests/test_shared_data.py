"""共享数据协议测试 —— 所有 agent 读写同一份数据。

核心断言：
1. A 写的平衡表，Q/E 读得到（同一份数据，不是各自的副本）
2. 命名规范机器可校验（写错集合名立刻报错）
3. 越权写会 warn（strict 模式抛错）—— 防止 B 去改平衡表
4. 审计结论既落 docs 又落 audit_log（可查 + 可追溯）
5. 库里出现未登记集合要能发现（"有人绕过 SharedData 直接写"）
"""
import pytest

from multi_agent.shared_data import (
    COLLECTIONS, STREAMS, SchemaError, SharedData, balance_key, demo,
    validate_collection, validate_stream,
)
from multi_agent.dbmanager import DBManager


@pytest.fixture
def db():
    return DBManager(":memory:")


BALANCE = {
    "commodity": "copper", "period": "2025A", "unit": "万吨",
    "supply": {"refined_total": 2857.8},
    "demand": {"refined_usage": 2815.9},
    "balance": {"surplus_deficit": 41.9},
}


# ───────────────────────────────────────────── 命名规范
def test_validate_known_collections():
    for name in ("balance", "verdicts", "reports", "audit"):
        assert validate_collection(name) == name


def test_unknown_collection_raises():
    with pytest.raises(SchemaError) as e:
        validate_collection("balance_stuff")
    assert "未登记" in str(e.value)


def test_validate_streams():
    assert validate_stream("news") == "news"
    assert validate_stream("news:commodity") == "news:commodity"
    with pytest.raises(SchemaError):
        validate_stream("random_stream")


def test_balance_key_normalises():
    assert balance_key("Copper") == "copper"
    assert balance_key("  Nickel  ") == "nickel"
    with pytest.raises(SchemaError):
        balance_key("")


def test_every_collection_has_owner():
    for name, spec in COLLECTIONS.items():
        assert spec.get("owner"), f"{name} 没声明 owner"
    for name, spec in STREAMS.items():
        assert spec.get("owner"), f"stream {name} 没声明 owner"


# ───────────────────────────────────────────── 跨角色读同一份数据
def test_balance_written_by_a_read_by_q(db):
    """A 写 → Q/E 读，必须是同一份数据（不是各自副本）。"""
    a = SharedData(db, role="A")
    a.put_balance("copper", BALANCE)

    for reader in ("Q", "E", "J", "auditor"):
        got = SharedData(db, role=reader).get_balance("copper")
        assert got is not None, f"{reader} 读不到平衡表"
        assert got["supply"]["refined_total"] == 2857.8
        assert got["balance"]["surplus_deficit"] == 41.9


def test_news_written_by_b_read_by_q(db):
    b = SharedData(db, role="B")
    b.append_news({"title": "矿山减产", "impact": "减产", "commodity": "copper"})

    q = SharedData(db, role="Q")
    news = q.tail_news(limit=10)
    assert len(news) == 1
    assert news[0]["payload"]["impact"] == "减产"
    assert news[0]["payload"]["by"] == "B"


def test_verdict_flow_q_to_e(db):
    q = SharedData(db, role="Q")
    q.put_verdict("copper", {"verdict": "accept", "score": 0.8})
    e = SharedData(db, role="E")
    assert e.get_verdict("copper")["verdict"] == "accept"


def test_full_chain_demo(db):
    """完整链路：A→B→Q→E→auditor，全部读同一份数据。"""
    steps = demo(SharedData(db, role="any"))
    assert steps["Q_read"]["has_balance"] is True
    assert steps["Q_read"]["news"] >= 1
    assert steps["E_read_verdict"] is True
    assert steps["protocol"]["ok"] is True


# ───────────────────────────────────────────── 越权检查
def test_wrong_role_write_warns(db):
    b = SharedData(db, role="B")           # B 不该写 balance（owner=A）
    out = b.put_balance("nickel", {"commodity": "nickel"})
    assert "warn" in out and "owner=A" in out["warn"]
    # 但数据还是要写进去（warn 不是拒绝）—— 否则排查时更难
    assert SharedData(db, role="Q").get_balance("nickel") is not None


def test_strict_mode_raises_on_wrong_role(db):
    b = SharedData(db, role="B", strict=True)
    with pytest.raises(SchemaError) as e:
        b.put_balance("nickel", {"commodity": "nickel"})
    assert "违反共享协议" in str(e.value)


def test_right_role_no_warn(db):
    a = SharedData(db, role="A")
    out = a.put_balance("copper", BALANCE)
    assert "warn" not in out


def test_any_role_allowed(db):
    s = SharedData(db, role="someone")
    out = s._put("demo", "k", {"v": 1})     # demo owner=any
    assert "warn" not in out


# ───────────────────────────────────────────── 审计双写
def test_audit_goes_both_docs_and_stream(db):
    aud = SharedData(db, role="auditor")
    aud.put_audit("copper", {"verdict": "pass", "severity": "info",
                             "counts": {"error": 0, "warn": 0}})
    # docs 侧可查
    got = aud.get_audit("copper")
    assert got["verdict"] == "pass"
    # stream 侧可追溯
    log = db.tail("audit_log", limit=10)
    assert len(log["items"]) == 1
    assert log["items"][0]["payload"]["verdict"] == "pass"
    assert log["items"][0]["payload"]["by"] == "auditor"


# ───────────────────────────────────────────── 平衡表历史
def test_balance_history_kept_for_auditor(db):
    a = SharedData(db, role="A")
    a.put_balance("copper", BALANCE)
    a.put_balance("copper", {**BALANCE, "period": "2026E"})
    hist = db.list("balance_history", limit=50)
    assert len(hist["items"]) == 2, "两个期次都要留（auditor 要看口径变化）"


def test_put_balance_merge_keeps_other_fields(db):
    a = SharedData(db, role="A")
    a.put_balance("copper", BALANCE)
    a.put_balance("copper", {"period": "2026E"}, merge=True)
    got = a.get_balance("copper")
    assert got["period"] == "2026E"
    assert got["supply"]["refined_total"] == 2857.8   # 没被覆盖掉


# ───────────────────────────────────────────── 协议自检
def test_audit_protocol_detects_unknown(db):
    SharedData(db, role="A").put_balance("copper", BALANCE)
    db.write("rogue_collection", "k", {"x": 1})     # 绕过 SharedData
    r = SharedData(db).audit_protocol()
    assert r["ok"] is False
    assert "rogue_collection" in r["unknown"]


def test_audit_protocol_clean(db):
    SharedData(db, role="A").put_balance("copper", BALANCE)
    assert SharedData(db).audit_protocol()["ok"] is True


# ───────────────────────────────────────────── 多 agent 并发写同一份
def test_two_agents_see_same_state(db):
    """A 写平衡表、B 塞新闻、Q 读两者 —— 三方共享同一份库。"""
    SharedData(db, role="A").put_balance("copper", BALANCE)
    SharedData(db, role="B").append_news({"title": "t1", "impact": "减产"})
    SharedData(db, role="B").append_news({"title": "t2", "impact": "增产"})

    q = SharedData(db, role="Q")
    assert q.get_balance("copper") is not None
    assert len(q.tail_news(limit=10)) == 2
    # Q 看到的是 A 和 B 都写过之后的状态
    assert db.stats()["docs"] >= 2
    assert db.stats()["streams"] == 2
