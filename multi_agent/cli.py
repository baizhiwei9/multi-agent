"""命令行入口：serve / agent / submit / agents / tasks / task / cancel / demo。

示例
----
    # 1) 起 Hub（跨机器时把 --host 设成 0.0.0.0，并配 token）
    multi-agent serve --host 0.0.0.0 --port 8787 --db hub.db --token secret

    # 2) 起一个绑定了三个角色的 Agent
    multi-agent agent --hub http://10.0.0.5:8787 --id stock-a --token secret \
        --role "macro:regime,rates" --role "micro:dialectic" \
        --handlers examples.stock_agent:register

    # 3) 协调端按动作表发任务（不经过大模型）
    multi-agent submit --hub http://10.0.0.5:8787 --token secret \
        --capability macro --action regime --params '{"pmi": 50.2}' --wait
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

from .client import AgentRuntime, HubClient, HubError
from .dbmanager import DBClient, DBManager
from .coordinator import CoordinatorAgent
from .mcp import MCPAgent, MCPServer
from .server import HubServer


def _token(args: argparse.Namespace) -> Optional[str]:
    return getattr(args, "token", None) or os.environ.get("MULTI_AGENT_TOKEN") or None


def parse_roles(items: List[str]) -> List[Dict[str, Any]]:
    """`NAME:action1,action2[:描述]` → role 结构。"""
    roles = []
    for raw in items:
        parts = raw.split(":", 2)
        if len(parts) < 2:
            raise SystemExit(f"角色格式不对：{raw!r}，应为 NAME:action1,action2[:描述]")
        name, actions = parts[0].strip(), [a.strip() for a in parts[1].split(",") if a.strip()]
        if not name or not actions:
            raise SystemExit(f"角色格式不对：{raw!r}")
        roles.append({"name": name, "actions": actions,
                      "description": parts[2].strip() if len(parts) > 2 else ""})
    return roles


def parse_labels(items: Optional[List[str]]) -> Dict[str, str]:
    out = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"label 格式不对：{item!r}，应为 k=v")
        k, v = item.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def load_handlers(spec: Optional[str]) -> Optional[str]:
    """`module:register` → 校验可导入并返回模块名。"""
    if not spec:
        return None
    if ":" not in spec:
        raise SystemExit("--handlers 格式应为 module:register")
    module_name, attr = spec.split(":", 1)
    try:
        mod = importlib.import_module(module_name)
    except ImportError as e:
        raise SystemExit(f"无法导入 {module_name}：{e}")
    if not hasattr(mod, attr):
        raise SystemExit(f"{module_name} 里没有 {attr}()")
    return spec


def _call_register(rt: AgentRuntime, spec: str) -> None:
    module_name, attr = spec.split(":", 1)
    mod = importlib.import_module(module_name)
    getattr(mod, attr)(rt)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="multi-agent", description="跨 Agent 通讯平台（HTTP/JSON）")
    ap.add_argument("--version", action="version", version="multi-agent 1.0")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # ---- serve ----
    p = sub.add_parser("serve", help="启动 Hub 服务端")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--db", default="hub.db", help="任务库（SQLite），默认 hub.db")
    p.add_argument("--token", default=None, help="共享 token；留空表示不鉴权（仅限本机调试）")
    p.add_argument("--ttl", type=float, default=90, help="Agent 心跳超时秒数")
    p.add_argument("--registry", default=None, help="注册表快照 JSON（可选）")
    p.add_argument("--beacon", action="store_true", help="开启 UDP 局域网广播")
    p.add_argument("--beacon-port", type=int, default=8788)
    p.add_argument("--tcp-host", default=None, help="TCP 监听地址，默认跟随 --host")
    p.add_argument("--tcp-port", type=int, default=8790, help="TCP 端口；0 表示关闭")
    p.add_argument("--udp-host", default=None, help="rendezvous 监听地址，默认 0.0.0.0")
    p.add_argument("--udp-port", type=int, default=8791, help="UDP rendezvous 端口；0 表示关闭")

    # ---- agent ----
    p = sub.add_parser("agent", help="启动一个 Agent（可绑多个角色）")
    p.add_argument("--hub", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--role", action="append", required=True,
                   help="NAME:action1,action2[:描述]，可重复")
    p.add_argument("--handlers", default=None, help="module:register，注册本地 handler")
    p.add_argument("--concurrency", type=int, default=2, help="并发执行线程数")
    p.add_argument("--poll-wait", type=float, default=20)
    p.add_argument("--ttl", type=float, default=60)
    p.add_argument("--label", action="append", default=[], help="k=v，可选")
    p.add_argument("--token", default=None)

    # ---- submit ----
    p = sub.add_parser("submit", help="按能力清单发任务")
    p.add_argument("--hub", required=True)
    p.add_argument("--capability", required=True)
    p.add_argument("--action", required=True)
    p.add_argument("--params", default="{}", help="JSON 字符串")
    p.add_argument("--params-file", default=None)
    p.add_argument("--timeout", type=float, default=60)
    p.add_argument("--max-retries", type=int, default=0)
    p.add_argument("--idem", default=None, help="幂等键")
    p.add_argument("--label", action="append", default=[])
    p.add_argument("--wait", action="store_true")
    p.add_argument("--token", default=None)

    # ---- 查询 ----
    for name, helptext in (("agents", "列出 Agent"), ("tasks", "列出任务")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--hub", required=True)
        p.add_argument("--token", default=None)
        p.add_argument("--limit", type=int, default=50)

    for name in ("task", "cancel"):
        p = sub.add_parser(name, help="查看/取消任务")
        p.add_argument("task_id")
        p.add_argument("--hub", required=True)
        p.add_argument("--token", default=None)

    # ---- demo ----
    p = sub.add_parser("demo", help="本机起 Hub + 两个 Agent，跑一次闭环")
    p.add_argument("--token", default=None)

    # ---- db ----
    p = sub.add_parser("db", help="启动 DBManager（全平台唯一 DB 读写入口）")
    p.add_argument("--hub", required=True)
    p.add_argument("--id", default="db-manager")
    p.add_argument("--db-path", default="data.db")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--token", default=None)

    # ---- coordinator ----
    p = sub.add_parser("coordinator", help="启动 Coordinator（DAG 编排）")
    p.add_argument("--hub", required=True)
    p.add_argument("--id", default="coordinator")
    p.add_argument("--db-path", default=None, help="可选：把运行状态落到 DBManager")
    p.add_argument("--token", default=None)

    # ---- pipeline ----
    p = sub.add_parser("pipeline", help="启动一条流水线（balance_sheet / stock_daily / demo）")
    p.add_argument("--hub", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--params", default="{}")
    p.add_argument("--resume", default=None, help="续跑已有 run：给 run_id（已成功步骤不重做）")
    p.add_argument("--wait", action="store_true")
    p.add_argument("--token", default=None)

    # ---- demo-flow ----
    p = sub.add_parser("demo-flow", help="本机起 Hub + DBManager + Coordinator + 演示 Agent，跑完整流水线")
    p.add_argument("--token", default=None)

    # ---- MCP ----
    p = sub.add_parser("mcp-server", help="stdio MCP server：把 Hub 能力暴露成 MCP tools")
    p.add_argument("--hub", required=True)
    p.add_argument("--name", default="multi-agent-hub")
    p.add_argument("--token", default=None)

    p = sub.add_parser("mcp-agent", help="把外部 MCP server 的工具挂到 Hub 上当能力")
    p.add_argument("--hub", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--command", required=True, help="启动 MCP server 的命令（stdio）")
    p.add_argument("--prefix", default="mcp", help="注册到 Hub 的 capability 名")
    p.add_argument("--token", default=None)

    p = sub.add_parser("call", help="用指定传输调一次方法（http:// 走 /rpc，tcp:// 走 TCP）")
    p.add_argument("--target", required=True, help="http://host:8787 或 tcp://host:8790")
    p.add_argument("--method", required=True, help="hub.agents / hub.submit / db.write ...")
    p.add_argument("--params", default="{}")
    p.add_argument("--token", default=None)

    # ---- 调试 / NAT ----
    p = sub.add_parser("trace", help="查看任务时间线：提交/派发/重试/完成卡在哪一步")
    p.add_argument("task_id")
    p.add_argument("--hub", required=True)
    p.add_argument("--token", default=None)

    p = sub.add_parser("diagnose", help="分步体检：HTTP/TCP/rendezvous/DB/打洞逐项检查")
    p.add_argument("--hub", default=None)
    p.add_argument("--tcp", default=None, help="tcp://host:8790")
    p.add_argument("--rendezvous", default=None, help="udp://host:8791 或 host:8791")
    p.add_argument("--peer", default=None, help="要打洞的对端 agent_id")
    p.add_argument("--no-db", action="store_true", help="跳过 DB 往返检查")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.add_argument("--token", default=None)

    p = sub.add_parser("rendezvous", help="只启动公网 UDP rendezvous（打洞协调）")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8791)
    p.add_argument("--token", default=None)

    p = sub.add_parser("punch", help="打洞客户端：hello、punch、直连/中继收发")
    p.add_argument("--rendezvous", required=True, help="udp://host:8791 或 host:8791")
    p.add_argument("--id", required=True, help="本 Agent 的 id")
    p.add_argument("--peer", required=True, help="对端 agent_id")
    p.add_argument("--send", default=None, help="连上后发一条消息")
    p.add_argument("--listen", type=float, default=0, help="监听秒数（打印收到的消息）")
    p.add_argument("--force-relay", action="store_true", help="强制走中继（测试回退）")
    p.add_argument("--token", default=None)

    # ---- 项目扩展点 ----
    p = sub.add_parser("project", help="项目扩展点：列出/查看/装配流水线/起项目 Agent")
    p.add_argument("action", choices=["list", "show", "pipelines", "agents", "agent"])
    p.add_argument("name", nargs="?", help="项目名（位置参数形式，show / agents / agent 必填）")
    p.add_argument("--name", dest="name_opt", default=None,
                   help="项目名（--name 形式，与位置参数二选一）")
    p.add_argument("--hub", default=None, help="agent 模式必填")
    p.add_argument("--id", default=None, help="agent 模式必填")
    p.add_argument("--roles", default=None,
                   help="agent 模式：只注册这些 capability（逗号分隔，如 B 或 B,auditor）")
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--poll-wait", type=float, default=20)
    p.add_argument("--json", action="store_true")
    p.add_argument("--token", default=None)

    a = ap.parse_args(argv)

    if a.cmd == "serve":
        srv = HubServer(host=a.host, port=a.port, db_path=a.db,
                        token=_token(a), ttl_s=a.ttl, registry_json=a.registry)
        if a.beacon:
            from .discovery import start_beacon
            start_beacon(srv.port, broadcast_port=a.beacon_port)
        tcp_srv = None
        if a.tcp_port:
            from .tcp import TCPHubServer
            tcp_host = a.tcp_host or ("0.0.0.0" if a.host == "0.0.0.0" else "127.0.0.1")
            tcp_srv = TCPHubServer(srv.hub, host=tcp_host, port=a.tcp_port,
                                   token=_token(a))
            tcp_srv.start_background()
            print(f"[hub] tcp {tcp_srv.address}")
        rv_srv = None
        if a.udp_port:
            from .nat import RendezvousServer
            rv_host = a.udp_host or "0.0.0.0"
            rv_srv = RendezvousServer(host=rv_host, port=a.udp_port, token=_token(a))
            rv_srv.start_background()
            print(f"[hub] rendezvous {rv_srv.address}")
        print(f"[hub] listening on {srv.url}  db={a.db}  token={'yes' if _token(a) else 'no'}")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if rv_srv:
                rv_srv.stop()
            if tcp_srv:
                tcp_srv.stop()
            srv.stop()
        return 0

    if a.cmd == "agent":
        roles = parse_roles(a.role)
        rt = AgentRuntime(a.hub, a.id, roles, token=_token(a),
                          concurrency=a.concurrency, poll_wait=a.poll_wait,
                          ttl_s=a.ttl, labels=parse_labels(a.label))
        if a.handlers:
            _call_register(rt, load_handlers(a.handlers))
        info = rt.start()
        print(f"[agent] {a.id} registered on {a.hub} roles="
              f"{[r['name'] + ':' + ','.join(r['actions']) for r in rt.manifest()]}")
        print(json.dumps(info, ensure_ascii=False))
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            rt.stop()
        return 0

    if a.cmd == "submit":
        params = json.loads(open(a.params_file, encoding="utf-8").read()) if a.params_file else json.loads(a.params)
        c = HubClient(a.hub, token=_token(a))
        task = c.submit(a.capability, a.action, params, timeout_s=a.timeout,
                        wait=a.wait, max_retries=a.max_retries, idempotency_key=a.idem,
                        labels=parse_labels(a.label))
        print(json.dumps(task, ensure_ascii=False, indent=2))
        return 0 if task.get("state") in (None, "succeeded", "queued", "assigned") else 1

    if a.cmd == "agents":
        data = HubClient(a.hub, token=_token(a)).agents()
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if a.cmd == "tasks":
        data = HubClient(a.hub, token=_token(a)).tasks(limit=a.limit)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if a.cmd == "task":
        print(json.dumps(HubClient(a.hub, token=_token(a)).task(a.task_id),
                         ensure_ascii=False, indent=2))
        return 0

    if a.cmd == "cancel":
        print(json.dumps(HubClient(a.hub, token=_token(a)).cancel(a.task_id),
                         ensure_ascii=False, indent=2))
        return 0

    if a.cmd == "db":
        rt = AgentRuntime(a.hub, a.id,
                          [{"name": "db", "actions": list(DBManager.ACTIONS)}],
                          token=_token(a), concurrency=a.concurrency, poll_wait=5)
        DBManager(a.db_path).attach(rt)
        info = rt.start()
        print(f"[db] {a.id} on {a.hub} path={a.db_path}")
        print(json.dumps(info, ensure_ascii=False))
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            rt.stop()
        return 0

    if a.cmd == "coordinator":
        from .projects import install_pipelines
        install_pipelines()   # 项目流水线必须在 Coordinator 进程里可见
        db = DBClient(a.hub, token=_token(a)) if a.db_path else None
        ca = CoordinatorAgent(a.hub, token=_token(a), db=db)
        rt = AgentRuntime(a.hub, a.id,
                          [{"name": "coordinator",
                            "actions": ["start", "status", "runs", "notify"]}],
                          token=_token(a), concurrency=2, poll_wait=5)
        ca.attach(rt)
        info = rt.start()
        print(f"[coordinator] {a.id} on {a.hub} db={'yes' if db else 'no'}")
        print(json.dumps(info, ensure_ascii=False))
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            rt.stop()
        return 0

    if a.cmd == "project":
        from .projects import REGISTRY, install_pipelines

        if a.action == "list":
            data = REGISTRY.describe_all()
            if a.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                for d in data:
                    print(f"{d['name']:<16} {d['title']}  "
                          f"角色 {len(d['roles'])} · Agent {d.get('agents', 0)} · "
                          f"流水线 {d['pipeline'] or '-'} · 审计 {d['audit_project'] or '无'}")
            return 0

        if a.action == "pipelines":
            added = install_pipelines()
            print(json.dumps(added, ensure_ascii=False) if a.json
                  else "已注册流水线：" + (", ".join(added) or "（无）"))
            return 0

        if not a.name and not a.name_opt:
            raise SystemExit("project show / agent 需要项目名")
        proj = REGISTRY.get(a.name or a.name_opt)

        if a.action == "show":
            d = proj.describe()
            if a.json:
                print(json.dumps(d, ensure_ascii=False, indent=2))
            else:
                print(f"{d['name']} — {d['title']}")
                if d["description"]:
                    print(d["description"])
                print("\n角色（×N = 要起几个 Agent）：")
                for r in d["roles"]:
                    print(f"  {r['capability']:<8} ×{r.get('instances', 1)}  "
                          f"{','.join(r['actions']):<34} {r['description']}")
                print(f"\n共需 {d.get('agents', 0)} 个 Agent 进程（project agents 看清单）")
                print(f"流水线：{d['pipeline']} 步骤 {d['pipeline_steps']}")
                print(f"审计：{d['audit_project'] or '无'} · 新闻：{d['news'] or '无'} · "
                      f"报告：{d['reporter'] or '无'}")
                print("已实现：" + (", ".join(d["implemented"]) or "（仅声明）"))
            return 0

        if a.action == "agents":
            plan = proj.agent_plan()
            if a.json:
                print(json.dumps(plan, ensure_ascii=False, indent=2))
            else:
                print(f"{plan['project']} 共需 {plan['total']} 个 Agent：")
                for role in plan["roles"]:
                    ids = ", ".join(role["ids"]) or "（不起）"
                    print(f"  {role['capability']:<10} ×{role['instances']:<3} {ids:<24} "
                          f"{role['role_arg']}")
                print("\n每个实例（按 id 起进程；+ 表示多个角色合在一个 Agent）：")
                for agent in plan["agents"]:
                    caps_arg = ",".join(agent.get("capabilities") or [agent["capability"]])
                    print(f"  multi-agent project agent {plan['project']} --hub $HUB "
                          f"--id {agent['id']} --roles {caps_arg}")
            return 0

        if not a.hub or not a.id:
            raise SystemExit("project agent 需要 --hub 与 --id")
        caps = [s.strip() for s in (a.roles or "").split(",") if s.strip()] or None
        unknown = [c for c in (caps or []) if c not in proj.capabilities()]
        if unknown:
            raise SystemExit(f"项目 {proj.name} 没有这些 capability：{unknown}"
                             f"（有：{proj.capabilities()}）")
        rt = AgentRuntime(a.hub, a.id, proj.manifests(caps), token=_token(a),
                          concurrency=a.concurrency, poll_wait=a.poll_wait)
        proj.register(rt)
        info = rt.start()
        print(f"[project] {proj.name} as {a.id} on {a.hub}")
        print(json.dumps(info, ensure_ascii=False))
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            rt.stop()
        return 0

    if a.cmd == "pipeline":
        c = HubClient(a.hub, token=_token(a))
        if a.resume:
            t = c.submit("coordinator", "resume", {"run_id": a.resume},
                         wait=True, timeout_s=30)
            if t["state"] != "succeeded":
                print(json.dumps(t, ensure_ascii=False, indent=2))
                return 1
            run_id = t["result"]["run_id"]
            print(f"[pipeline] resume run_id={run_id}")
        else:
            params = json.loads(a.params)
            t = c.submit("coordinator", "start", {"pipeline": a.name, "params": params},
                         wait=True, timeout_s=30)
            if t["state"] != "succeeded":
                print(json.dumps(t, ensure_ascii=False, indent=2))
                return 1
            run_id = t["result"]["run_id"]
            print(f"[pipeline] {a.name} run_id={run_id}")
        if not a.wait:
            return 0
        while True:
            st = c.submit("coordinator", "status", {"run_id": run_id},
                          wait=True, timeout_s=30)
            if st["state"] != "succeeded":
                print(json.dumps(st, ensure_ascii=False, indent=2))
                return 1
            snap = st["result"]
            if snap["state"] in ("succeeded", "failed", "canceled"):
                print(json.dumps(snap, ensure_ascii=False, indent=2))
                return 0 if snap["state"] == "succeeded" else 1
            time.sleep(1)

    if a.cmd == "mcp-server":
        srv_mcp = MCPServer(hub_url=a.hub, token=_token(a), name=a.name)
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                print(json.dumps({"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32700, "message": "parse error"}}),
                      flush=True)
                continue
            resp = srv_mcp.handle(req)
            if resp is not None:
                print(json.dumps(resp, ensure_ascii=False), flush=True)
        return 0

    if a.cmd == "mcp-agent":
        mcp_agent = MCPAgent(a.hub, a.id, a.command, token=_token(a), prefix=a.prefix)
        info = mcp_agent.start()
        print(json.dumps(info, ensure_ascii=False))
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            mcp_agent.stop()
        return 0

    if a.cmd == "call":
        params = json.loads(a.params)
        if a.target.startswith("tcp://"):
            from .tcp import TCPClient
            conn = TCPClient.from_url(a.target, token=_token(a))
            try:
                result = conn.call(a.method, params)
            finally:
                conn.close()
        else:
            from .transports.jsonrpc import JsonRpcClient
            result = JsonRpcClient(a.target, token=_token(a)).call(a.method, params)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if a.cmd == "trace":
        data = HubClient(a.hub, token=_token(a)).trace(a.task_id)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if a.cmd == "diagnose":
        from .diagnose import diagnose
        steps = diagnose(hub=a.hub, tcp=a.tcp, rendezvous=a.rendezvous, peer=a.peer,
                         token=_token(a), db=not a.no_db)
        if a.json:
            print(json.dumps(steps, ensure_ascii=False, indent=2))
        else:
            for s in steps:
                if not s["ok"]:
                    mark = "FAIL"
                elif s.get("warn"):
                    mark = "WARN"
                else:
                    mark = "OK  "
                print(f"[{mark}] {s['step']:<18} {s['ms']:>5}ms  {s['detail']}")
        return 0 if all(s["ok"] for s in steps) else 1

    if a.cmd == "rendezvous":
        from .nat import RendezvousServer
        rv = RendezvousServer(host=a.host, port=a.port, token=_token(a))
        print(f"[rendezvous] {rv.address}")
        try:
            rv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            rv.stop()
        return 0

    if a.cmd == "punch":
        from .nat import HolePuncher
        rest = a.rendezvous.replace("udp://", "", 1)
        host, _, port = rest.partition(":")
        p = HolePuncher(a.id, (host, int(port or 8791)), token=_token(a),
                        force_relay=a.force_relay)
        try:
            info = p.start()
            print(json.dumps({"observed": list(p.observed or ()),
                              "peers": info.get("peers")}, ensure_ascii=False))
            res = p.connect(a.peer)
            print(json.dumps(res, ensure_ascii=False))
            if a.send:
                p.send(a.send.encode("utf-8"))
            deadline = time.time() + max(0.0, a.listen)
            while time.time() < deadline:
                msg = p.recv(timeout=max(0.1, deadline - time.time()))
                if msg is None:
                    continue
                print(f"[recv] {msg.decode('utf-8', 'replace')}")
        finally:
            p.close()
        return 0

    if a.cmd == "demo-flow":
        return _demo_flow(_token(a))

    if a.cmd == "demo":
        return _demo(_token(a))

    return 2


def _demo_flow(token: Optional[str]) -> int:
    """完整栈冒烟：Hub + DBManager + Coordinator + 演示 Agent + DEMO 流水线。"""
    srv = HubServer(host="127.0.0.1", port=0, db_path=":memory:", token=token)
    srv.start_background()

    dbm = DBManager(":memory:")
    rt_db = AgentRuntime(srv.url, "db-manager",
                         [{"name": "db", "actions": list(DBManager.ACTIONS)}],
                         token=token, concurrency=4, poll_wait=1)
    dbm.attach(rt_db)
    rt_db.start()

    rt_demo = AgentRuntime(srv.url, "demo-agent",
                           [{"name": "demo", "actions": ["produce", "consume"]}],
                           token=token, concurrency=2, poll_wait=1)
    rt_demo.add_handler("demo", "produce",
                        lambda p, c: {"n": p["n"], "items": list(range(int(p["n"])))})
    rt_demo.add_handler("demo", "consume", lambda p, c: {"input": p.get("input")})
    rt_demo.start()

    ca = CoordinatorAgent(srv.url, token=token, db=DBClient(srv.url, token=token))
    rt_co = AgentRuntime(srv.url, "coordinator",
                         [{"name": "coordinator",
                           "actions": ["start", "status", "runs", "notify"]}],
                         token=token, concurrency=2, poll_wait=1)
    ca.attach(rt_co)
    rt_co.start()

    run = ca.coord.start("demo", {"n": 3, "key": "latest"})
    snap = ca.coord.wait(run["run_id"], timeout=30)
    print(json.dumps(snap, ensure_ascii=False, indent=2))

    ok = snap["state"] == "succeeded"
    rt_co.stop(); rt_demo.stop(); rt_db.stop(); srv.stop()
    return 0 if ok else 1


def _demo(token: Optional[str]) -> int:
    srv = HubServer(host="127.0.0.1", port=0, db_path=":memory:", token=token)
    srv.start_background()
    base = srv.url

    a = AgentRuntime(base, "agent-macro", [{"name": "macro", "actions": ["regime"]}], token=token)
    a.add_handler("macro", "regime", lambda p, c: {"regime": "risk_on" if p.get("pmi", 50) > 50 else "risk_off",
                                                   "pmi": p.get("pmi")})
    b = AgentRuntime(base, "agent-micro", [{"name": "micro", "actions": ["dialectic"]}], token=token)
    b.add_handler("micro", "dialectic", lambda p, c: {"verdict": "pass", "flags": []})
    a.start(); b.start()

    c = HubClient(base, token=token)
    t1 = c.submit("macro", "regime", {"pmi": 50.2}, wait=True, timeout_s=10)
    t2 = c.submit("micro", "dialectic", {"revenue_growth": 0.12}, wait=True, timeout_s=10)
    print(json.dumps({"hub": base, "macro": t1, "micro": t2}, ensure_ascii=False, indent=2))

    ok = t1["state"] == "succeeded" and t2["state"] == "succeeded"
    a.stop(); b.stop(); srv.stop()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())