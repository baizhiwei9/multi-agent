"""端到端演示：三个 Agent 跑在**不同主机**（靠 TCP），协调者点名派角色 + 跑 Python 程序。

本脚本可以在**一台机器上启动多个独立进程**来模拟三台主机 —— 因为它们之间
只通过 TCP socket 说话，没有任何共享内存/文件，和真跨主机等价：

    ┌─────────────┐  TCP :8790   ┌────────────────────────────┐
    │ coordinator │ ───────────► │ Hub（注册表 + 任务队列）      │
    └─────────────┘              └───────┬────────────────────┘
                                  TCP    │    TCP
                        ┌────────────────┼────────────────┐
                        ▼                ▼                ▼
                   agent-openclaw    agent-hermes     agent-coze
                   (A/F/E 角色)      (B/C 角色)       (D/Q/J 角色)

跑法：

    # 一体化演示（自己起 Hub + 三个 agent + 协调者，全部走 TCP）
    python3 examples/tcp_roles_demo.py

    # 只起 Hub（给真跨主机用）
    python3 examples/tcp_roles_demo.py --mode hub --port 8790

    # 在另一台机器上起一个 agent（角色由协调者稍后分配）
    python3 examples/tcp_roles_demo.py --mode agent --hub 10.0.0.5:8790 \\
        --id hermes-1 --allow /srv/jobs

    # 在一台机器上跑协调者：分配角色 + 派 Python 程序
    python3 examples/tcp_roles_demo.py --mode coord --hub 10.0.0.5:8790 \\
        --plan '{"openclaw-1":["A","F","E"],"hermes-1":["B","C"],"coze-1":["D","Q","J"]}'
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from multi_agent.client import AgentRuntime
from multi_agent.hub import Hub
from multi_agent.integrations import audit, runner
from multi_agent.remote import RemoteCoordinator, RoleBinding, plan_roles
from multi_agent.tcp import TCPClient, TCPHubServer

TOKEN = "demo-" + "token"   # 演示用，生产请改环境变量

# 演示用的三个"作业脚本"（真实场景里是 BalanceAI 的各个角色入口）
JOB_SOURCES = {
    "build_sheet.py": '''
import json, sys
argv = sys.argv[1:]
print(json.dumps({"role": "A", "action": "build_sheet",
                  "commodity": argv[0] if argv else "copper",
                  "supply": {"refined_total": 2857.8},
                  "demand": {"refined_usage": 2815.9},
                  "balance": {"surplus_deficit": 41.9}}, ensure_ascii=False))
''',
    "scan_news.py": '''
import json
print(json.dumps({"role": "B", "action": "scan_news", "events": 3}, ensure_ascii=False))
''',
    "report.py": '''
import json
print(json.dumps({"role": "E", "action": "report",
                  "markdown": "# 铜平衡表\\n\\n## 供需\\n过剩 41.9 万吨"}, ensure_ascii=False))
''',
    "final.py": '''
import json
print(json.dumps({"role": "J", "action": "final_report", "verdict": "ok"}, ensure_ascii=False))
''',
}


def _write_jobs(directory: str) -> str:
    os.makedirs(directory, exist_ok=True)
    for name, src in JOB_SOURCES.items():
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
    return directory


# ───────────────────────────────────────────── Hub
def start_hub(port: int = 8790, host: str = "0.0.0.0") -> TCPHubServer:
    hub = Hub(db_path=":memory:", ttl_s=180)
    srv = TCPHubServer(hub, host=host, port=port, token=TOKEN)
    srv.start_background()
    return srv


# ───────────────────────────────────────────── Agent
def start_agent(hub_host: str, hub_port: int, agent_id: str, allow_dir: str,
                roles=None) -> AgentRuntime:
    """一个 agent = 独立进程视角：只通过 TCP 连 Hub。"""
    os.environ["AGENT_RUN_ALLOW"] = allow_dir
    os.environ.setdefault("PYTHONPATH", os.getcwd())

    # AgentRuntime 走 HTTP；这里给它一个假的 base_url，只是为了拿 client.create
    # 真正跑任务时用 TCP 长轮询（见下面 _tcp_worker）
    rt = AgentRuntime(f"http://{hub_host}:{hub_port}",
                      agent_id,
                      roles or [{"name": "runner", "actions": runner.ROLE_ACTIONS["runner"],
                                 "description": runner.ROLE_DESCRIPTION}],
                      token=TOKEN, concurrency=1, poll_wait=1, ttl_s=120)
    runner.register(rt)
    audit.register(rt)
    return rt


def _tcp_loop(hub_host: str, hub_port: int, agent_id: str, roles, stop: threading.Event,
              allow_dir: str) -> None:
    """最小 TCP 版 agent 循环：auth → register → 长轮询 inbox → 执行 → result。

    这条循环就是"跨主机 agent"的真实形态：只需要一条出站 TCP 连接。
    """
    os.environ["AGENT_RUN_ALLOW"] = allow_dir
    client = TCPClient(hub_host, hub_port, token=TOKEN, timeout=60)

    manifest = []
    for r in roles:
        manifest.append({"name": r["name"],
                         "actions": r.get("actions") or ["*"],
                         "description": r.get("description", "")})
    client.call("hub.register", {"agent_id": agent_id, "roles": manifest,
                                 "labels": {"agent_id": agent_id}, "ttl_s": 120})

    from multi_agent.integrations import runner as R
    local_runner = R._runner_for(agent_id)

    last_hb = 0.0
    while not stop.is_set():
        now = time.time()
        if now - last_hb > 30:
            try:
                client.call("hub.heartbeat", {"agent_id": agent_id})
            except Exception:  # noqa: BLE001
                pass
            last_hb = now
        try:
            msg = client.call("hub.inbox", {"agent_id": agent_id, "wait": 1.0},
                              timeout=10)
        except Exception:  # noqa: BLE001
            time.sleep(1)
            continue
        if not msg:
            continue
        task_id = msg.get("task_id")
        cap, act = msg.get("capability"), msg.get("action")
        params = msg.get("params") or {}
        started = time.time()
        try:
            if cap == "runner":
                result = R.run_action(act, params, agent_id=agent_id)
                # ★ 语义：「被白名单拒绝」是 agent **正常工作**的结果，不是任务失败。
                #   必须走 ok=True + result.denied=True 回传，否则 Hub 会把
                #   result 丢成 error，调用方看不到拒绝理由（审计就没意义了）。
                ok = True
                if result.get("denied"):
                    pass
            elif cap == "auditor":
                result = audit.run_action(act, params)
                ok = True
            else:
                raise RuntimeError(f"未实现 {cap}/{act}")
        except Exception as e:  # noqa: BLE001
            try:
                client.call("hub.result", {"task_id": task_id, "agent_id": agent_id,
                                           "ok": False,
                                           "error": {"code": "handler_error",
                                                     "message": f"{type(e).__name__}: {e}"},
                                           "duration_ms": int((time.time() - started) * 1000)})
            except Exception:  # noqa: BLE001
                pass
            continue
        try:
            client.call("hub.result", {"task_id": task_id, "agent_id": agent_id,
                                       "ok": ok, "result": result,
                                       "duration_ms": int((time.time() - started) * 1000)})
        except Exception:  # noqa: BLE001
            pass
    try:
        client.close()
    except Exception:  # noqa: BLE001
        pass


# ───────────────────────────────────────────── 协调者
def run_coordinator(hub_host: str, hub_port: int, plan: dict, jobs_dir: str) -> int:
    from multi_agent.remote import plan_roles
    check = plan_roles(plan)
    print("### 1) 角色分配计划体检")
    print("   ", json.dumps(check, ensure_ascii=False))
    if not check["ok"]:
        print("   ✗ 有重复角色，中止（同一角色双写会互相覆盖）")
        return 2

    print("\n### 2) 用 TCP 连 Hub")
    c = TCPClient(hub_host, hub_port, token=TOKEN)
    agents = c.call("hub.agents")
    print("   注册表:", sorted(a["agent_id"] for a in (agents.get("agents") or [])))
    c.close()

    print("\n### 3) 给每个 agent 分配角色（经 runner/assign）")
    ok_all = True
    for agent_id, roles in plan.items():
        params = {"roles": [{"name": r} for r in roles]}
        task = _submit_wait(hub_host, hub_port, "runner", "assign", params,
                           labels={"agent_id": agent_id})
        good = task.get("state") == "succeeded"
        ok_all &= good
        print(f"   {agent_id:<16} {roles} → {task.get('state')}"
              f" {json.dumps((task.get('result') or {}).get('assigned'), ensure_ascii=False)}")

    print("\n### 4) 点名让指定 agent 跑指定 Python 程序（TCP）")
    jobs = [
        ("openclaw-1", "build_sheet.py", ["copper"]),
        ("hermes-1", "scan_news.py", []),
        ("openclaw-1", "report.py", []),
        ("coze-1", "final.py", []),
    ]
    for agent_id, script, argv in jobs:
        task = _submit_wait(hub_host, hub_port, "runner", "py",
                            {"script": os.path.join(jobs_dir, script),
                             "argv": argv, "timeout_s": 30},
                            labels={"agent_id": agent_id})
        res = task.get("result") or {}
        print(f"   {agent_id:<16} {script:<15} → state={task.get('state')}"
              f" rc={res.get('returncode')} {res.get('duration_ms')}ms")
        if res.get("stdout"):
            print(f"      stdout: {res['stdout'].strip()[:120]}")
        if res.get("denied"):
            print(f"      DENIED: {res.get('reason')}")
            ok_all = False

    print("\n### 5) 安全边界验证（不在白名单的脚本必须被拒）")
    outside = os.path.join(tempfile.gettempdir(), "evil_script.py")
    with open(outside, "w", encoding="utf-8") as fh:
        fh.write("print('should never run')")
    task = _submit_wait(hub_host, hub_port, "runner", "py",
                        {"script": outside, "timeout_s": 10},
                        labels={"agent_id": "openclaw-1"})
    res = task.get("result") or {}
    denied = bool(res.get("denied"))
    print(f"   白名单外脚本 → denied={denied}")
    if not denied:
        ok_all = False
        print("   ✗ 安全边界失效！")
    else:
        print(f"      理由: {res.get('reason')}")

    print("\n### 6) 超时保护验证")
    task = _submit_wait(hub_host, hub_port, "runner", "py",
                        {"script": os.path.join(jobs_dir, "scan_news.py"),
                         "timeout_s": 0.001}, labels={"agent_id": "hermes-1"})
    res = task.get("result") or {}
    print(f"   timeout_s=0.001 → timed_out={res.get('timed_out')} "
          f"rc={res.get('returncode')}")

    print(f"\n=== 总结: {'✅ 全部通过' if ok_all else '❌ 有失败项'}")
    return 0 if ok_all else 1


def _submit_wait(hub_host, hub_port, capability, action, params, labels=None,
                 timeout_s=40.0) -> dict:
    c = TCPClient(hub_host, hub_port, token=TOKEN)
    try:
        task = c.call("hub.submit", {"capability": capability, "action": action,
                                     "params": params, "timeout_s": timeout_s,
                                     "wait": False, "labels": labels or {}})
        deadline = time.time() + timeout_s
        task_id = task["id"]
        while time.time() < deadline:
            snap = c.call("hub.task", {"task_id": task_id})
            if snap.get("state") in ("succeeded", "failed", "timeout", "canceled"):
                return snap
            time.sleep(0.15)
        return c.call("hub.task", {"task_id": task_id})
    finally:
        c.close()


# ───────────────────────────────────────────── main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="TCP 多 Agent 角色分配 + 远程执行演示")
    ap.add_argument("--mode", default="all",
                    choices=["all", "hub", "agent", "coord"])
    ap.add_argument("--hub", default="127.0.0.1:8790")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--id", default="agent")
    ap.add_argument("--allow", default=None, help="白名单目录（agent 模式必填）")
    ap.add_argument("--plan", default=None, help="JSON: {agent_id: [roles]}")
    ap.add_argument("--jobs", default=None, help="脚本目录")
    a = ap.parse_args(argv)

    hub_host, _, hub_port = a.hub.partition(":")
    hub_port = int(hub_port or 8790)

    if a.mode == "hub":
        srv = start_hub(a.port)
        print(f"TCP Hub: {srv.address}", flush=True)
        try:
            while True:
                time.sleep(10)
        except KeyboardInterrupt:
            srv.stop()
        return 0

    if a.mode == "agent":
        if not a.allow:
            print("agent 模式必须给 --allow <白名单目录>", file=sys.stderr)
            return 2
        stop = threading.Event()
        threading.Thread(target=_tcp_loop,
                         args=(hub_host, hub_port, a.id,
                               [{"name": "runner", "actions": runner.ROLE_ACTIONS["runner"],
                                 "description": runner.ROLE_DESCRIPTION},
                                {"name": "auditor", "actions": audit._ROLE_ACTIONS["auditor"],
                                 "description": audit._ROLE_DESCRIPTION}],
                               stop, a.allow),
                         daemon=True).start()
        print(f"agent {a.id} 已接入 {hub_host}:{hub_port}，白名单 {a.allow}", flush=True)
        try:
            while True:
                time.sleep(10)
        except KeyboardInterrupt:
            stop.set()
        return 0

    if a.mode == "coord":
        if not a.plan:
            print("coord 模式必须给 --plan", file=sys.stderr)
            return 2
        jobs = a.jobs or _write_jobs(os.path.join(tempfile.gettempdir(), "agent_jobs"))
        return run_coordinator(hub_host, hub_port, json.loads(a.plan), jobs)

    # ---- all：一体化 ----
    print("### 0) 起 TCP Hub（0.0.0.0:8790）")
    srv = start_hub(a.port)
    jobs_dir = _write_jobs(os.path.join(tempfile.gettempdir(), "agent_jobs"))
    print(f"    作业目录: {jobs_dir}")
    print(f"    Hub: {srv.address}")

    print("\n### 0.5) 起三个独立 agent（各自独立 TCP 连接，等价于三台主机）")
    stop = threading.Event()
    plan = {"openclaw-1": ["A", "F", "E"],
            "hermes-1": ["B", "C"],
            "coze-1": ["D", "Q", "J"]}
    roles_for_agent = [{"name": "runner", "actions": runner.ROLE_ACTIONS["runner"],
                        "description": runner.ROLE_DESCRIPTION},
                       {"name": "auditor", "actions": audit._ROLE_ACTIONS["auditor"],
                        "description": audit._ROLE_DESCRIPTION}]
    for agent_id in plan:
        threading.Thread(target=_tcp_loop,
                         args=("127.0.0.1", a.port, agent_id, roles_for_agent,
                               stop, jobs_dir),
                         daemon=True, name=f"agent-{agent_id}").start()
        print(f"    started {agent_id}")
    time.sleep(1.5)

    try:
        rc = run_coordinator("127.0.0.1", a.port, plan, jobs_dir)
    finally:
        stop.set()
        time.sleep(0.5)
        srv.stop()
    return rc


if __name__ == "__main__":
    sys.exit(main())
