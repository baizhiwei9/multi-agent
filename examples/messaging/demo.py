"""三种消息语义的实跑演示（不连任何外部服务）。

1. **单播 / 竞争消费**：多个 Agent 注册同一个 `capability+action`，Hub 按 round-robin
   挑一个派活——是"服务发现 + 任务队列"，不是广播。
2. **共享通道 / run 内广播**：两个生产者 `emit` 到同一个通道（`append` 合并），
   两个订阅者各自用 `${channels.events}` 读全量——发布者不知道有谁订阅，订阅者之间也不认识。
3. **请求体落库 + 幂等重放**：任务行（params / idempotency_key / state / attempts / result）
   真写在 SQLite 的 `tasks` 表里；同幂等键重复提交返回同一条任务，handler 不会被再调一次。
4. 顺带演示**没有在线订阅者**时的行为：直接 `failed / no_agent`，不是"等上线再投"。

跑法：

    PYTHONPATH=. python examples/messaging/demo.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List


from multi_agent.client import AgentRuntime, HubClient            # noqa: E402
from multi_agent.coordinator import CoordinatorAgent              # noqa: E402
from multi_agent.server import HubServer                          # noqa: E402

TOKEN = "msg-demo"


def _hub(db_path: str = ":memory:") -> HubServer:
    srv = HubServer(host="127.0.0.1", port=0, db_path=db_path, token=TOKEN)
    srv.start_background()
    return srv


# ───────────────────────────────────────────── 1. 单播：能力路由 + 竞争消费
def scenario_unicast(n_tasks: int = 4) -> Dict[str, Any]:
    srv = _hub()
    seen: Dict[str, List[str]] = {"ocr-1": [], "ocr-2": []}
    agents: List[AgentRuntime] = []
    for aid in ("ocr-1", "ocr-2"):
        rt = AgentRuntime(srv.url, aid,
                          [{"name": "ocr", "actions": ["run"]}],
                          token=TOKEN, concurrency=2, poll_wait=1)

        def make(name: str):
            def _run(p: Dict[str, Any], c: Dict[str, Any]) -> Dict[str, Any]:
                seen[name].append(p.get("doc"))
                return {"agent": name, "doc": p.get("doc")}

            return _run

        rt.add_handler("ocr", "run", make(aid))
        rt.start()
        agents.append(rt)

    client = HubClient(srv.url, token=TOKEN)
    try:
        out = [client.submit("ocr", "run", {"doc": f"d{i}"},
                             wait=True, timeout_s=10) for i in range(n_tasks)]
        return {"per_agent": {k: len(v) for k, v in seen.items()},
                "by_agent": {k: list(v) for k, v in seen.items()},
                "distinct_docs": len({r["result"]["doc"] for r in out}),
                "states": [r["state"] for r in out]}
    finally:
        for rt in agents:
            rt.stop()
        srv.stop()


# ───────────────────────────────────────────── 2. 共享通道：run 内广播
def scenario_broadcast() -> Dict[str, Any]:
    srv = _hub()
    worker = AgentRuntime(srv.url, "broadcast-worker",
                          [{"name": "producer", "actions": ["run"]},
                           {"name": "subscriber", "actions": ["read"]}],
                          token=TOKEN, concurrency=4, poll_wait=1)
    worker.add_handler("producer", "run",
                       lambda p, c: {"items": p.get("items") or []})
    worker.add_handler("subscriber", "read",
                       lambda p, c: {"got": p.get("input")})
    coord_agent = CoordinatorAgent(srv.url, token=TOKEN)
    coord_rt = AgentRuntime(srv.url, "broadcast-coord",
                            [{"name": "coordinator",
                              "actions": ["start", "status", "runs", "resume"]}],
                            token=TOKEN, concurrency=2, poll_wait=1)
    coord_agent.attach(coord_rt)
    worker.start()
    coord_rt.start()

    spec = {"name": "broadcast_demo", "steps": [
        {"name": "p1", "capability": "producer", "action": "run",
         "params": {"items": ["铜矿停产", "库存连降三周"]},
         "emit": {"channel": "events", "path": "items", "merge": "append"}},
        {"name": "p2", "capability": "producer", "action": "run",
         "params": {"items": ["美元走强"]},
         "emit": {"channel": "events", "path": "items", "merge": "append"}},
        {"name": "sub_a", "capability": "subscriber", "action": "read",
         "params": {"input": "${channels.events}"}, "depends_on": ["p1", "p2"]},
        {"name": "sub_b", "capability": "subscriber", "action": "read",
         "params": {"input": "${channels.events}"}, "depends_on": ["p1", "p2"]},
    ]}
    try:
        run = coord_agent.coord.start(spec, {})
        snap = coord_agent.coord.wait(run["run_id"], timeout=20)
        return {"state": snap["state"],
                "channel": snap.get("channels", {}).get("events"),
                "sub_a": (snap["steps"]["sub_a"].get("result") or {}).get("got"),
                "sub_b": (snap["steps"]["sub_b"].get("result") or {}).get("got")}
    finally:
        coord_rt.stop()
        worker.stop()
        srv.stop()


# ───────────────────────────────────────────── 3. 请求体落库 + 幂等
def _task_row(db_path: str, task_id: str) -> Dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, capability, action, params, idempotency_key, state, attempts "
            "FROM tasks WHERE id=?", (task_id,)).fetchone()
    return dict(row) if row else {}


def scenario_durable() -> Dict[str, Any]:
    tmp = tempfile.mkdtemp(prefix="msg-demo-")
    db = os.path.join(tmp, "hub.sqlite")
    srv = _hub(db)
    calls: List[str] = []
    worker = AgentRuntime(srv.url, "durable-ocr",
                          [{"name": "ocr", "actions": ["run"]}],
                          token=TOKEN, concurrency=1, poll_wait=1)
    worker.add_handler("ocr", "run",
                       lambda p, c: calls.append(p.get("doc")) or {"doc": p.get("doc")})
    worker.start()
    client = HubClient(srv.url, token=TOKEN)
    try:
        first = client.submit("ocr", "run", {"doc": "订单#42"},
                              wait=False, idempotency_key="order:42")
        row = _task_row(db, first["id"])            # 体还没被执行，先看库里存了什么
        second = client.submit("ocr", "run", {"doc": "订单#42"},
                               wait=False, idempotency_key="order:42")
        deadline = time.time() + 10
        state = ""
        while time.time() < deadline:
            state = client.task(first["id"]).get("state") or ""
            if state in ("succeeded", "failed", "canceled", "timeout"):
                break
            time.sleep(0.2)
        time.sleep(0.3)
        return {"row": row,
                "same_id": first["id"] == second["id"],
                "calls": len(calls),
                "final_state": state}
    finally:
        worker.stop()
        srv.stop()


# ───────────────────────────────────────────── 4. 没有在线订阅者
def scenario_no_subscriber() -> Dict[str, Any]:
    srv = _hub()
    client = HubClient(srv.url, token=TOKEN)
    try:
        task = client.submit("ocr", "run", {"doc": "没人接"}, wait=True, timeout_s=5)
        return {"state": task["state"], "error": task.get("error") or {}}
    finally:
        srv.stop()


def main() -> None:
    uni = scenario_unicast()
    print("① 单播（能力路由，round-robin 派活）")
    print(f"   每个 Agent 接到：{uni['per_agent']}；不同文档 {uni['distinct_docs']} 份；"
          f"状态 {set(uni['states'])}")

    bc = scenario_broadcast()
    print("\n② 共享通道（两个生产者 append 合并，两个订阅者各读全量）")
    print(f"   通道 events = {bc['channel']}")
    print(f"   sub_a 收到 {bc['sub_a']}")
    print(f"   sub_b 收到 {bc['sub_b']}（不用认识 sub_a，也不用认识发布者）")

    du = scenario_durable()
    print("\n③ 请求体落库 + 幂等重放")
    print(f"   第一条任务行：{json.dumps(du['row'], ensure_ascii=False)}")
    print(f"   同幂等键再提交 → 同一个 task_id：{du['same_id']}；"
          f"handler 实际执行次数：{du['calls']}；最终状态：{du['final_state']}")

    ns = scenario_no_subscriber()
    print("\n④ 没有在线订阅者")
    print(f"   状态 {ns['state']}，错误 {ns['error'].get('code')}：" 
          f"{ns['error'].get('message')}")
    print("   → 它不攒 backlog：没人接就当场失败，不是'等订阅者上线再投'。")


if __name__ == "__main__":
    main()