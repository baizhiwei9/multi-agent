"""任务生命周期回归：消息作废 / 超时竞态 / wait 空等 / 入口校验。

issue #28 的 5 条里有 4 条落在 Hub 侧，这里逐条钉住：

1. 已收尾（成功 / 取消 / 重派）的任务消息不能再被执行第二遍；
2. 超时分支不能用过期快照把已写好的 `succeeded` 覆盖回 `queued`；
3. `wait()` 对「已终态 failed、不会再重试」不能空等整段重试预算；
4. 非法 labels 与 RPC 入口必须被校验拦住；调度线程异常必须落 failed。
"""
from __future__ import annotations

import time

import pytest

from multi_agent import protocol
from multi_agent.client import HubClient, HubError
from multi_agent.hub import Hub
from multi_agent.server import HubServer
from multi_agent.store import TaskStore
from multi_agent.transports.jsonrpc import JsonRpcClient


# ───────────────────────────────────────────── 夹具
def _hub_with_agents(*agent_ids: str) -> Hub:
    """只注册能力、不起 Agent 进程：消息会推给 Agent 的收件箱但没人取。"""
    hub = Hub(":memory:")
    for aid in agent_ids:
        hub.registry.register({"agent_id": aid,
                               "roles": [{"name": "demo", "actions": ["echo"]}]})
    return hub


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ───────────────────────────────────────────── 1. 已收尾任务的旧消息作废
def test_message_keeps_ownership_fields():
    """投递消息必须带 agent_id / attempt，出队才有依据校验归属。"""
    hub = _hub_with_agents("a1")
    task = hub.submit(capability="demo", action="echo", params={}, timeout_s=5)
    assert _wait_until(lambda: bool(hub._inbox.get("a1")))
    msg = hub.next_message("a1", 1)
    assert msg["task_id"] == task["id"]
    assert msg["agent_id"] == "a1"
    assert msg["attempt"] == 1


def test_stale_message_dropped_after_reassign_and_success():
    """a1 超时未取 → 重派 a2 成功 → a1 恢复轮询不能再执行同一个任务。"""
    hub = _hub_with_agents("a1", "a2")
    task = hub.submit(capability="demo", action="echo", params={}, timeout_s=1,
                      max_retries=1)
    tid = task["id"]
    # 第一轮派给 a1：消息进队，但 a1 的 worker 全忙，没来取
    assert _wait_until(lambda: hub.store.get(tid)["agent_id"] == "a1")
    assert hub._inbox.get("a1"), "a1 的消息应该已经进收件箱"

    msg_a2 = hub.next_message("a2", 5)
    assert msg_a2 and msg_a2["task_id"] == tid
    assert msg_a2["attempt"] == 2
    done = hub.complete(tid, "a2", True, result={"by": "a2"})
    assert done["state"] == "succeeded"

    assert hub.next_message("a1", 0.3) is None, "已成功任务的旧消息不能被执行第二遍"
    snap = hub.store.get(tid)
    assert snap["result"] == {"by": "a2"}
    assert snap["agent_id"] == "a2"


def test_message_dropped_after_cancel():
    """取消后收件箱里的消息同样作废。"""
    hub = _hub_with_agents("a1")
    task = hub.submit(capability="demo", action="echo", params={}, timeout_s=5)
    tid = task["id"]
    assert _wait_until(lambda: bool(hub._inbox.get("a1")))

    assert hub.cancel(tid)["state"] == "canceled"
    assert hub.next_message("a1", 0.3) is None
    assert hub.store.get(tid)["state"] == "canceled"


def test_stale_attempt_message_is_dropped_but_current_attempt_passes():
    """同一 task 的旧 attempt 消息要丢，当前 attempt 的照常投递。"""
    hub = _hub_with_agents("a1")
    task, _ = hub.store.create(capability="demo", action="echo", params={})
    tid = task["id"]
    hub.store.update(tid, state="assigned", agent_id="a1", attempts=2)

    stale = protocol.envelope("task", to="a1", agent_id="a1", attempt=1,
                              task_id=tid, capability="demo", action="echo", params={})
    hub._push("a1", stale)
    assert hub.next_message("a1", 0.3) is None

    fresh = protocol.envelope("task", to="a1", agent_id="a1", attempt=2,
                              task_id=tid, capability="demo", action="echo", params={})
    hub._push("a1", fresh)
    got = hub.next_message("a1", 1)
    assert got and got["task_id"] == tid and got["attempt"] == 2


# ───────────────────────────────────────────── 2. 超时分支的条件更新
class _RacyStore(TaskStore):
    """在超时分支做条件更新之前，插入 Agent 的迟到成功结果。

    复现 issue #28 第 2 条的时序窗口：`ev.wait` 超时那一刻 a1 刚把结果写完
    （state=succeeded），旧实现用过期快照把它无条件覆盖回 queued 再派给 a2。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_timeout_probe = None

    def update_if(self, task_id, expect, **fields):
        callback, self.on_timeout_probe = self.on_timeout_probe, None
        if callback and expect.get("state") == "assigned" \
                and fields.get("state") == "queued":
            callback()
        return super().update_if(task_id, expect, **fields)


def test_timeout_branch_does_not_overwrite_succeeded_result():
    store = _RacyStore(":memory:")
    hub = _hub_with_agents("a1", "a2")
    hub.store.close()
    hub.store = store
    holder = {}
    store.on_timeout_probe = lambda: hub.complete(
        holder["tid"], "a1", True, result={"by": "a1"})

    task = hub.submit(capability="demo", action="echo", params={}, timeout_s=0.2,
                      max_retries=1)
    holder["tid"] = task["id"]
    snap = hub.wait(task["id"], timeout=5)

    assert snap["state"] == "succeeded"
    assert snap["result"] == {"by": "a1"}, "先写好的成功结果不能被超时分支覆盖"
    assert snap["agent_id"] == "a1"
    assigned = [e for e in store.events(task["id"]) if e["event"] == "assigned"]
    assert len(assigned) == 1, "同一个任务不能被派给两个 Agent"
    assert not hub._inbox.get("a2"), "a2 不该再收到重派消息"


def test_complete_is_conditional_on_own_attempt():
    """任务被改回 queued（换人重试）后，上一轮的迟到结果不能写成 succeeded。"""
    hub = _hub_with_agents("a1")
    task, _ = hub.store.create(capability="demo", action="echo", params={})
    tid = task["id"]
    hub.store.update(tid, state="assigned", agent_id="a1", attempts=1)
    hub.store.update_if(tid, {"state": "assigned", "agent_id": "a1", "attempts": 1},
                        state="queued", agent_id=None, error={"code": "timeout",
                                                             "message": "超时"})

    snap = hub.complete(tid, "a1", True, result={"late": True})
    assert snap["state"] == "queued"
    assert hub.store.get(tid)["result"] is None


# ───────────────────────────────────────────── 3. wait() 不空等重试预算
def test_wait_returns_immediately_when_no_agent_available():
    hub = Hub(":memory:")          # 一个 Agent 都没有
    started = time.time()
    task = hub.submit(capability="nope", action="nothing", params={},
                      timeout_s=2, max_retries=0, wait=True)
    elapsed = time.time() - started
    assert task["state"] == "failed"
    assert task["error"]["code"] == "no_agent"
    assert elapsed < 1.0, f"已终态 failed 却白等 {elapsed:.2f}s（预算 7s）"


def test_wait_does_not_burn_retry_budget_after_final_failure():
    """候选比 max_retries 少、attempts 仍 <= max_retries 的收尾，也要立刻返回。

    只有一个 Agent 时第二轮 `registry.next(exclude=tried)` 直接没候选，
    `_execute` 提前收尾为 failed（attempts=1 / max_retries=2）——旧实现
    还按 attempts>max_retries 判断"会重试"，于是白等 0.2*3+5 = 5.6s。
    """
    hub = _hub_with_agents("a1")            # 有 Agent，但它永远不回结果
    started = time.time()
    task = hub.submit(capability="demo", action="echo", params={},
                      timeout_s=0.2, max_retries=2, wait=True)
    elapsed = time.time() - started
    assert task["state"] == "failed"
    assert task["attempts"] == 1
    assert elapsed < 3.0, f"已终态 failed 却白等 {elapsed:.2f}s（预算 5.6s）"


# ───────────────────────────────────────────── 4. 入口校验 + 调度兜底
def test_hub_submit_rejects_non_dict_labels():
    hub = Hub(":memory:")
    with pytest.raises(protocol.ProtocolError):
        hub.submit(capability="demo", action="echo", params={}, labels=["a1"])
    assert hub.store.list() == [], "非法请求不能建任务"


def test_dispatch_exception_marks_task_failed():
    """调度线程里抛异常，任务也必须落 failed，不能永远停在 queued。"""
    hub = _hub_with_agents("a1")

    def exploding_next(*args, **kwargs):
        raise RuntimeError("boom")

    hub.registry.next = exploding_next
    task = hub.submit(capability="demo", action="echo", params={}, timeout_s=1)
    snap = hub.wait(task["id"], timeout=5)
    assert snap["state"] == "failed"
    assert snap["error"]["code"] == "internal"
    assert "boom" in snap["error"]["message"]


@pytest.fixture
def server():
    srv = HubServer(host="127.0.0.1", port=0, db_path=":memory:")
    srv.start_background()
    yield srv
    srv.stop()


def test_http_submit_rejects_non_dict_labels(server):
    client = HubClient(server.url)
    with pytest.raises(HubError) as e:
        client.submit("demo", "echo", {"x": 1}, labels=["a1"])
    assert e.value.status == 400
    assert server.hub.store.list() == []


def test_rpc_register_validates_roles(server):
    """roles 传字符串不能让注册表存进去（否则之后所有任务的 _supports 都崩）。"""
    rpc = JsonRpcClient(server.url)
    with pytest.raises(HubError) as e:
        rpc.call("hub.register", {"agent_id": "bad", "roles": "cap"})
    assert e.value.code == -32602
    assert server.hub.registry.get("bad") is None

    ok = rpc.call("hub.register", {"agent_id": "good",
                                   "roles": [{"name": "demo", "actions": ["echo"]}]})
    assert ok["agent_id"] == "good"


def test_rpc_result_requires_boolean_ok(server):
    """ok 传字符串 "false" 不能把失败任务记成 succeeded。"""
    hub = server.hub
    hub.registry.register({"agent_id": "a1",
                           "roles": [{"name": "demo", "actions": ["echo"]}]})
    task, _ = hub.store.create(capability="demo", action="echo", params={})
    tid = task["id"]
    hub.store.update(tid, state="assigned", agent_id="a1", attempts=1)

    rpc = JsonRpcClient(server.url)
    with pytest.raises(HubError) as e:
        rpc.call("hub.result", {"task_id": tid, "agent_id": "a1", "ok": "false"})
    assert e.value.code == -32602
    assert hub.store.get(tid)["state"] == "assigned"

    failed = rpc.call("hub.result", {"task_id": tid, "agent_id": "a1", "ok": False,
                                     "error": {"code": "handler_error",
                                               "message": "boom"}})
    assert failed["state"] == "failed"