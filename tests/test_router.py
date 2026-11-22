"""能力路由测试：协调者按 agent 声明派活，而不是靠写死。

验证的核心断言是"**它真的按能力走**"：
1. 只声明了债券能力的 agent，不会收到平衡表任务；
2. 某能力有两台在跑时，轮询会分给两台（不是永远压第一个）；
3. 点名要求某台时，那台不支持就直接报错（**不静默换人**）；
4. 能力没人提供时，报错信息里带上"在线 agent 各自声明了什么"，
   排障不用猜；
5. 一台挂了，submit_with_fallback 会换到另一台。
"""
import threading
import time

import pytest

from multi_agent.client import AgentRuntime, HubClient
from multi_agent.hub import Hub
from multi_agent.router import CapabilityRouter, NoCapability
from multi_agent.server import HubServer
from multi_agent.tcp import TCPClient, TCPHubServer

TOKEN = "***" + "token"


# ───────────────────────────────────────────── 夹具
@pytest.fixture
def hub():
    s = HubServer(host="127.0.0.1", port=0, db_path=":memory:", token=TOKEN)
    s.start_background()
    yield s
    s.stop()


@pytest.fixture
def tcp_hub():
    h = Hub(db_path=":memory:", ttl_s=120)
    s = TCPHubServer(h, host="127.0.0.1", port=0, token=TOKEN)
    s.start_background()
    yield s
    s.stop()


class FakeClient:
    """最小假 client：直接给一份 agent 列表，用来单测路由决策。"""

    def __init__(self, agents, tasks=None):
        self._agents = agents
        self._tasks = tasks or []

    def _request(self, method, path, body=None, timeout=None):
        if path.startswith("/v1/agents"):
            return {"agents": self._agents}
        if path.startswith("/v1/tasks"):
            return {"tasks": self._tasks}
        raise AssertionError(f"未预期请求 {method} {path}")


def _agent(aid, caps, alive=True, labels=None, age=1.0):
    return {"agent_id": aid, "alive": alive, "age_s": age, "ttl_s": 120,
            "labels": labels or {"agent_id": aid},
            "roles": [{"name": c, "actions": a} for c, a in caps]}


# ───────────────────────────────────────────── 能力清单
def test_catalog_lists_capabilities():
    c = FakeClient([_agent("a1", [("auditor", ["balance"]), ("A", ["build_sheet"])]),
                    _agent("a2", [("auditor", ["bond"])])])
    cat = CapabilityRouter(c).catalog()["capabilities"]
    assert "auditor/balance" in cat and "A/build_sheet" in cat
    assert [p["agent_id"] for p in cat["auditor/balance"]] == ["a1"]
    assert cat["auditor/bond"][0]["agent_id"] == "a2"


def test_catalog_marks_dead_agents():
    c = FakeClient([_agent("a1", [("auditor", ["balance"])], alive=False)])
    cat = CapabilityRouter(c).catalog()
    assert cat["alive_count"] == 0
    assert cat["capabilities"]["auditor/balance"][0]["alive"] is False


def test_wildcard_action_supported():
    c = FakeClient([_agent("a1", [("auditor", ["*"])])])
    r = CapabilityRouter(c)
    assert r.providers("auditor", "anything")


def test_providers_excludes_dead():
    c = FakeClient([_agent("a1", [("auditor", ["balance"])], alive=False),
                    _agent("a2", [("auditor", ["balance"])])])
    ids = [a["agent_id"] for a in CapabilityRouter(c).providers("auditor", "balance")]
    assert ids == ["a2"]


# ───────────────────────────────────────────── 只按能力派（不串味）
def test_bond_agent_never_gets_balance_task():
    """只声明债券能力的 agent，绝不能收到平衡表任务。"""
    c = FakeClient([_agent("bond-1", [("auditor", ["bond"])]),
                    _agent("bal-1", [("auditor", ["balance"])])])
    r = CapabilityRouter(c)
    for _ in range(6):
        assert r.route("auditor", "balance")["agent_id"] == "bal-1"
    for _ in range(6):
        assert r.route("auditor", "bond")["agent_id"] == "bond-1"


def test_route_returns_reason_and_candidates():
    c = FakeClient([_agent("a1", [("auditor", ["balance"])]),
                    _agent("a2", [("auditor", ["balance"])])])
    out = CapabilityRouter(c).route("auditor", "balance")
    assert out["agent_id"] in ("a1", "a2")
    assert out["reason"]
    assert sorted(out["candidates"]) == ["a1", "a2"]


# ───────────────────────────────────────────── 策略
def test_round_robin_alternates():
    c = FakeClient([_agent("a1", [("x", ["y"])]), _agent("a2", [("x", ["y"])])])
    r = CapabilityRouter(c, strategy="round_robin")
    picks = [r.route("x", "y")["agent_id"] for _ in range(6)]
    assert picks.count("a1") == 3 and picks.count("a2") == 3
    assert picks[0] != picks[1], "前两次不能都给同一台"


def test_first_strategy_is_stable():
    c = FakeClient([_agent("a1", [("x", ["y"])]), _agent("a2", [("x", ["y"])])])
    r = CapabilityRouter(c, strategy="first")
    picks = {r.route("x", "y")["agent_id"] for _ in range(5)}
    assert picks == {"a1"}


def test_least_loaded_picks_idle():
    c = FakeClient([_agent("a1", [("x", ["y"])]), _agent("a2", [("x", ["y"])])],
                   tasks=[{"state": "assigned", "agent_id": "a1"},
                          {"state": "assigned", "agent_id": "a1"}])
    out = CapabilityRouter(c, strategy="least_loaded").route("x", "y")
    assert out["agent_id"] == "a2"
    assert "负载" in out["reason"]


def test_prefer_label_picks_matching():
    c = FakeClient([_agent("a1", [("x", ["y"])], labels={"site": "sh"}),
                    _agent("a2", [("x", ["y"])], labels={"site": "bj"})])
    out = CapabilityRouter(c).route("x", "y", prefer_labels={"site": "bj"})
    assert out["agent_id"] == "a2"
    assert "标签匹配" in out["reason"]


def test_prefer_label_falls_back_with_note():
    c = FakeClient([_agent("a1", [("x", ["y"])], labels={"site": "sh"})])
    out = CapabilityRouter(c).route("x", "y", prefer_labels={"site": "nowhere"})
    assert out["agent_id"] == "a1"
    assert "无匹配" in out["reason"]


# ───────────────────────────────────────────── 点名 / 缺能力
def test_require_agent_must_support():
    c = FakeClient([_agent("bond-1", [("auditor", ["bond"])])])
    with pytest.raises(NoCapability) as e:
        CapabilityRouter(c).route("auditor", "balance", require_agent="bond-1")
    assert "没有声明" in str(e.value)


def test_require_agent_unknown_lists_available():
    c = FakeClient([_agent("a1", [("x", ["y"])])])
    with pytest.raises(NoCapability) as e:
        CapabilityRouter(c).route("x", "y", require_agent="ghost")
    assert "未注册" in str(e.value) and "a1" in str(e.value)


def test_require_agent_dead_is_error():
    c = FakeClient([_agent("a1", [("x", ["y"])], alive=False)])
    with pytest.raises(NoCapability) as e:
        CapabilityRouter(c).route("x", "y", require_agent="a1")
    assert "失联" in str(e.value)


def test_no_provider_error_is_diagnostic():
    """能力没人提供时，错误里要带上别人声明了什么。"""
    c = FakeClient([_agent("a1", [("auditor", ["bond"])]),
                    _agent("a2", [("A", ["build_sheet"])])])
    with pytest.raises(NoCapability) as e:
        CapabilityRouter(c).route("auditor", "balance")
    msg = str(e.value)
    assert "没有存活 agent" in msg
    assert "auditor" in msg and "build_sheet" in msg


# ───────────────────────────────────────────── 批量可行性
def test_route_plan_all_available():
    c = FakeClient([_agent("a1", [("A", ["build_sheet"])]),
                    _agent("a2", [("auditor", ["balance"])])])
    out = CapabilityRouter(c).route_plan([("A", "build_sheet"),
                                          ("auditor", "balance")])
    assert out["ok"] is True and out["missing"] == []
    assert [s["agent_id"] for s in out["steps"]] == ["a1", "a2"]


def test_route_plan_reports_missing():
    c = FakeClient([_agent("a1", [("A", ["build_sheet"])])])
    out = CapabilityRouter(c).route_plan([("A", "build_sheet"),
                                          ("bond", "audit")])
    assert out["ok"] is False
    assert out["missing"] == ["bond/audit"]
    assert out["steps"][1]["agent_id"] is None


def test_gaps_checks():
    c = FakeClient([_agent("a1", [("auditor", ["balance"])])])
    out = CapabilityRouter(c).gaps(["auditor/balance", "auditor/bond"])
    assert out["ok"] is False
    assert out["missing"] == ["auditor/bond"]
    assert out["checks"][0]["providers"] == ["a1"]


def test_gaps_handles_colon_in_role_name():
    """角色名本身可以带冒号（auditor:bond），缺口检查不能把冒号当分隔符搞错。"""
    c = FakeClient([_agent("a1", [("auditor:bond", ["balance"])]),
                    _agent("a2", [("auditor:stock", ["balance"])])])
    out = CapabilityRouter(c).gaps(["auditor:bond/balance",
                                    "auditor:stock/balance",
                                    "auditor:fund/balance"])
    assert out["ok"] is False
    assert out["missing"] == ["auditor:fund/balance"]
    provs = {x["normalized"]: x["providers"] for x in out["checks"]}
    assert provs["auditor:bond/balance"] == ["a1"]
    assert provs["auditor:stock/balance"] == ["a2"]


def test_role_name_suffix_routes_precisely():
    """auditor:bond 与 auditor:balance_sheet 是两个不同能力，不会互相串。"""
    c = FakeClient([_agent("bond-1", [("auditor:bond", ["balance"])]),
                    _agent("bal-1", [("auditor:balance_sheet", ["balance"])])])
    r = CapabilityRouter(c)
    for _ in range(4):
        assert r.route("auditor:bond", "balance")["agent_id"] == "bond-1"
        assert r.route("auditor:balance_sheet", "balance")["agent_id"] == "bal-1"


# ───────────────────────────────────────────── 端到端（HTTP hub）
def _runtime(hub, agent_id, caps):
    rt = AgentRuntime(hub.url, agent_id,
                      [{"name": c, "actions": a} for c, a in caps],
                      token=TOKEN, concurrency=1, poll_wait=1, ttl_s=60)
    for c, acts in caps:
        for act in acts:
            if act != "*":
                rt.add_handler(c, act, lambda p, ctx, _a=act: {"handled": _a,
                                                                "by": ctx["agent_id"]})
    rt.start()
    return rt


def test_submit_routes_to_right_agent(hub):
    rt_bond = _runtime(hub, "bond-1", [("auditor", ["bond"])])
    rt_bal = _runtime(hub, "bal-1", [("auditor", ["balance"])])
    try:
        r = CapabilityRouter(HubClient(hub.url, token=TOKEN))
        snap = r.submit("auditor", "balance", {}, wait=True, timeout_s=20)
        assert snap["state"] == "succeeded"
        assert snap["routed_to"] == "bal-1"
        assert snap["result"]["by"] == "bal-1"

        snap2 = r.submit("auditor", "bond", {}, wait=True, timeout_s=20)
        assert snap2["routed_to"] == "bond-1"
        assert snap2["result"]["by"] == "bond-1"
    finally:
        rt_bond.stop()
        rt_bal.stop()


def test_submit_round_robin_across_two(hub):
    a = _runtime(hub, "w-1", [("calc", ["add"])])
    b = _runtime(hub, "w-2", [("calc", ["add"])])
    try:
        r = CapabilityRouter(HubClient(hub.url, token=TOKEN))
        seen = set()
        for _ in range(4):
            snap = r.submit("calc", "add", {}, wait=True, timeout_s=20)
            assert snap["state"] == "succeeded"
            seen.add(snap["routed_to"])
        assert seen == {"w-1", "w-2"}, f"轮询没分到两台：{seen}"
    finally:
        a.stop()
        b.stop()


def test_submit_with_fallback_switches_agent(hub):
    """一台 handler 抛异常 → 自动换另一台。"""
    ok = _runtime(hub, "good-1", [("calc", ["add"])])

    bad = AgentRuntime(hub.url, "bad-1", [{"name": "calc", "actions": ["add"]}],
                       token=TOKEN, concurrency=1, poll_wait=1, ttl_s=60)

    def _boom(p, ctx):
        raise RuntimeError("boom")

    bad.add_handler("calc", "add", _boom)
    bad.start()
    try:
        r = CapabilityRouter(HubClient(hub.url, token=TOKEN), strategy="first")
        snap = r.submit_with_fallback("calc", "add", {}, max_attempts=3,
                                      wait=True, timeout_s=20)
        assert snap["state"] == "succeeded"
        assert snap["routed_to"] == "good-1"
        assert "bad-1" in snap["attempts"]
    finally:
        ok.stop()
        bad.stop()


# ───────────────────────────────────────────── TCP 线缆
def test_router_works_over_tcp(tcp_hub):
    """同一套路由逻辑，换 TCP 线缆不改一行业务代码。"""
    stop = threading.Event()

    def _tcp_agent(agent_id, caps):
        c = TCPClient("127.0.0.1", tcp_hub.port, token=TOKEN)
        c.call("hub.register", {"agent_id": agent_id,
                                "roles": [{"name": n, "actions": a} for n, a in caps],
                                "labels": {"agent_id": agent_id}, "ttl_s": 120})

        def loop():
            while not stop.is_set():
                try:
                    msg = c.call("hub.inbox", {"agent_id": agent_id, "wait": 1.0},
                                 timeout=10)
                except Exception:  # noqa: BLE001
                    time.sleep(0.2)
                    continue
                if not msg:
                    continue
                c.call("hub.result", {"task_id": msg["task_id"],
                                      "agent_id": agent_id, "ok": True,
                                      "result": {"by": agent_id}})

        threading.Thread(target=loop, daemon=True).start()

    _tcp_agent("tcp-bond", [("auditor", ["bond"])])
    _tcp_agent("tcp-bal", [("auditor", ["balance"])])
    time.sleep(0.5)
    try:
        client = TCPClient("127.0.0.1", tcp_hub.port, token=TOKEN)
        r = CapabilityRouter(client)
        assert r.route("auditor", "balance")["agent_id"] == "tcp-bal"
        assert r.route("auditor", "bond")["agent_id"] == "tcp-bond"
        snap = r.submit("auditor", "bond", {}, wait=True, timeout_s=20)
        assert snap["state"] == "succeeded"
        assert snap["result"]["by"] == "tcp-bond"
        client.close()
    finally:
        stop.set()
