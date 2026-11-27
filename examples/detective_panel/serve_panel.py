"""常驻侦探团服务：起 Hub（127.0.0.1:8787）+ N 个 worker，供 DSH 通过 MCP 调用。

    bash examples/detective_panel/start_panel.sh            # 一键（推荐）
    python examples/detective_panel/serve_panel.py --instances 8

启动后会打印一段可直接贴进 DSH 的 MCP 配置：

    {"mcpServers": {"multi-agent-hub": {
        "command": "multi-agent",
        "args": ["mcp-server", "--hub", "http://127.0.0.1:8787", "--token", "<token>"]}}}

DSH 那边能看到 `debate__run`、`report__build`、`policy__read`… 每个能力一个 tool。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from multi_agent.client import AgentRuntime      # noqa: E402
from multi_agent.server import HubServer          # noqa: E402

from examples.detective_panel.project import install   # noqa: E402

HERE = Path(__file__).resolve().parent


def _mcp_config(url: str, token: str) -> Dict[str, Any]:
    return {"mcpServers": {"multi-agent-hub": {
        "command": "multi-agent",
        "args": ["mcp-server", "--hub", url, "--token", token],
    }}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="侦探团常驻服务")
    ap.add_argument("--host", default="127.0.0.1",
                    help="只监听回环；要跨机自己上隧道，别直接绑 0.0.0.0")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--token", default=os.environ.get("PANEL_TOKEN") or "")
    ap.add_argument("--instances", type=int, default=1,
                    help="每个侦探席位起几个 worker（并发能力，不是讨论人数）")
    ap.add_argument("--db", default=str(HERE / "panel_hub.db"))
    args = ap.parse_args(argv)

    token = args.token or secrets.token_hex(16)
    instances = max(1, min(int(args.instances), 300))
    if instances > 32:
        print(f"注意：单进程起 {instances} 个 worker 会吃满线程与连接；"
              "生产建议每进程 ≤32，多进程/多机横向加。", file=sys.stderr)

    project = install(instances=1)     # 席位本身按 1 起；并发靠多 worker 实例
    srv = HubServer(host=args.host, port=args.port, db_path=args.db, token=token)
    srv.start_background()

    workers: List[AgentRuntime] = []
    for i in range(instances):
        rt = AgentRuntime(srv.url, f"panel-worker-{i}", project.manifests(),
                          token=token, concurrency=4, poll_wait=1)
        project.register(rt)
        rt.start()
        workers.append(rt)

    url = srv.url
    print(f"Hub 已起：{url}（token 已注入，不打印全文）")
    print(f"worker：{len(workers)} 个，席位：{len(project.manifests())} 个")
    print("把下面这段贴进 DSH 的 MCP 配置：")
    print(json.dumps(_mcp_config(url, token), ensure_ascii=False, indent=2))
    print("DSH 里可用 tool：debate__run（发起一局）、report__build（出报告）、"
          "policy__read / capital__probe / technical__structure / sentiment__track / "
          "contrarian__attack / judge__verdict（单席调用）")

    stop = {"flag": False}

    def _sig(_s, _f):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    try:
        while not stop["flag"]:
            time.sleep(0.5)
    finally:
        for rt in workers:
            rt.stop()
        srv.stop()
        print("已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())