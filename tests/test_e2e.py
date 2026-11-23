import time

import pytest

from multi_agent.client import AgentRuntime, HubClient, HubError
from multi_agent.server import HubServer

TOKEN = "test-token"


@pytest.fixture
def server():
    s = HubServer(host="127.0.0.1", port=0, db_path=":memory:", token=TOKEN)
    s.start_background()
    yield s
    s.stop()


@pytest.fixture
def client(server):
    return HubClient(server.url, token=TOKEN)


def _runtime(server, agent_id, role, action, fn, concurrency=1):
    rt = AgentRuntime(server.url, agent_id, [{"name": role, "actions": [action]}],
                      token=TOKEN, concurrency=concurrency, poll_wait=1, ttl_s=30)
    rt.add_handler(role, action, fn)
    rt.start()
    return rt


def test_sync_success(server, client):
    rt = _runtime(server, "a1", "demo", "echo", lambda p, c: {"echo": p["x"]})
    try:
        task = client.submit("demo", "echo", {"x": 42}, wait=True, timeout_s=5)
        assert task["state"] == "succeeded"
        assert task["result"] == {"echo": 42}
        assert task["agent_id"] == "a1"
    finally:
        rt.stop()


def test_async_poll(server, client):
    rt = _runtime(server, "a1", "demo", "slow", lambda p, c: {"ok": True})
    try:
        task = client.submit("demo", "slow", {}, wait=False, timeout_s=5)
        assert task["state"] in ("queued", "assigned")
        deadline = time.time() + 10
        while time.time() < deadline:
            snap = client.task(task["id"])
            if snap["state"] == "succeeded":
                break
            time.sleep(0.1)
        assert snap["state"] == "succeeded"
    finally:
        rt.stop()


def test_no_agent(server, client):
    task = client.submit("nobody", "nothing", {}, wait=True, timeout_s=2)
    assert task["state"] == "failed"
    assert task["error"]["code"] == "no_agent"


def test_idempotency(server, client):
    rt = _runtime(server, "a1", "demo", "echo", lambda p, c: p)
    try:
        t1 = client.submit("demo", "echo", {"n": 1}, wait=False, idempotency_key="K1")
        t2 = client.submit("demo", "echo", {"n": 1}, wait=False, idempotency_key="K1")
        assert t1["id"] == t2["id"]
        assert len([x for x in client.tasks() if x["idempotency_key"] == "K1"]) == 1
    finally:
        rt.stop()


def test_retry_next_agent(server, client):
    def boom(p, c):
        raise RuntimeError("first agent fails")

    rt1 = _runtime(server, "a1", "demo", "work", boom)
    rt2 = _runtime(server, "a2", "demo", "work", lambda p, c: {"by": "a2"})
    try:
        task = client.submit("demo", "work", {}, wait=True, timeout_s=5, max_retries=1)
        assert task["state"] == "succeeded"
        assert task["result"] == {"by": "a2"}
        assert task["agent_id"] == "a2"
        assert task["attempts"] == 2
    finally:
        rt1.stop()
        rt2.stop()


def test_auth_required(server):
    bad = HubClient(server.url, token="wrong")
    with pytest.raises(HubError) as e:
        bad.agents()
    assert e.value.status == 401


def test_multiple_roles_one_agent(server, client):
    rt = AgentRuntime(server.url, "stock-a",
                      [{"name": "macro", "actions": ["regime"]},
                       {"name": "micro", "actions": ["dialectic"]}],
                      token=TOKEN, concurrency=2, poll_wait=1)
    rt.add_handler("macro", "regime", lambda p, c: {"regime": "risk_on"})
    rt.add_handler("micro", "dialectic", lambda p, c: {"verdict": "pass"})
    rt.start()
    try:
        t1 = client.submit("macro", "regime", {}, wait=True, timeout_s=5)
        t2 = client.submit("micro", "dialectic", {}, wait=True, timeout_s=5)
        assert t1["state"] == t2["state"] == "succeeded"
        assert t1["agent_id"] == t2["agent_id"] == "stock-a"
    finally:
        rt.stop()


def test_handler_ctx_has_client(server, client):
    """handler 的 ctx 必须带上 client，角色才能在自己的动作里主动调别的角色。"""
    seen = {}

    def ask(p, c):
        seen["ctx_keys"] = sorted(c.keys())
        # 用 ctx 里的 client 主动给 capability=helper 发任务，拿到它的 result
        t = c["client"].submit("helper", "echo", {"x": p["x"]},
                               wait=True, timeout_s=5)
        seen["helper_state"] = t["state"]
        return {"echo": t["result"]["echo"], "using": t["agent_id"]}

    rt_ask = AgentRuntime(server.url, "asker",
                          [{"name": "boss", "actions": ["ask"]}],
                          token=TOKEN, concurrency=1, poll_wait=1)
    rt_ask.add_handler("boss", "ask", ask)
    rt_help = AgentRuntime(server.url, "helper",
                           [{"name": "helper", "actions": ["echo"]}],
                           token=TOKEN, concurrency=1, poll_wait=1)
    rt_help.add_handler("helper", "echo", lambda p, c: {"echo": p["x"]})
    rt_ask.start()
    rt_help.start()
    try:
        t = client.submit("boss", "ask", {"x": 9}, wait=True, timeout_s=6)
        assert t["state"] == "succeeded"
        assert t["result"] == {"echo": 9, "using": "helper"}
        assert "client" in seen["ctx_keys"]
        assert seen["helper_state"] == "succeeded"
    finally:
        rt_ask.stop()
        rt_help.stop()


def test_handler_unknown_action(server, client):
    rt = _runtime(server, "a1", "demo", "echo", lambda p, c: {})
    try:
        # 绕过 manifest：直接往 Agent 收件箱塞一个未注册动作
        from multi_agent import protocol
        msg = protocol.task_message(
            {"id": "t_manual", "capability": "demo", "action": "nope",
             "params": {}, "timeout_s": 5, "attempts": 0}, "a1")
        server.hub._push("a1", msg)
        deadline = time.time() + 5
        while time.time() < deadline:
            if server.hub.store.get("t_manual"):
                break
            time.sleep(0.05)
        # 任务本身不存在时结果会被 404 丢弃，这里只验证进程不崩
        assert server.hub.registry.get("a1") is not None
    finally:
        rt.stop()