"""端到端演示：Hub + 业务 Agent + 协调官全在本地内存里跑，不连任何外部服务。

    python examples/my_project/run_demo.py

它会打印每一步的状态和最终报告。把 `run()` 当模板：接真环境时只改
`HubServer(...)` 的地址与 `AgentRuntime(...)` 的 hub_url 即可。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from multi_agent.client import AgentRuntime            # noqa: E402
from multi_agent.coordinator import CoordinatorAgent    # noqa: E402
from multi_agent.integrations.audit import (AUDIT_ACTIONS,  # noqa: E402
                                            register as register_auditor)
from multi_agent.server import HubServer                # noqa: E402

from examples.my_project.project import CommodityMonitorProject, install  # noqa: E402

TOKEN = "demo-token"


def run(commodities: List[str] | None = None, quiet: bool = False) -> Dict[str, Any]:
    """起一个临时 Hub，跑完 commodity_monitor 流水线，返回最终 run 快照。"""
    project = install()                       # 登记项目 + 装上它的流水线
    srv = HubServer(host="127.0.0.1", port=0, db_path=":memory:", token=TOKEN)
    srv.start_background()

    # 业务 Agent：一条进程绑 5 个角色（采集官 / 风控官 / 报告官 / 监理 / 评议器）
    worker = AgentRuntime(srv.url, "monitor-worker",
                          project.manifests() + [{"name": "auditor",
                                                  "actions": list(AUDIT_ACTIONS)}],
                          token=TOKEN, concurrency=4, poll_wait=1)
    project.register(worker)                  # 业务 handler + supervisor
    register_auditor(worker)                  # 审核官（框架实现）

    # 协调官
    coord_agent = CoordinatorAgent(srv.url, token=TOKEN)
    coord_rt = AgentRuntime(srv.url, "monitor-coord",
                            [{"name": "coordinator",
                              "actions": ["start", "status", "runs", "resume"]}],
                            token=TOKEN, concurrency=2, poll_wait=1)
    coord_agent.attach(coord_rt)

    worker.start()
    coord_rt.start()
    try:
        run = coord_agent.coord.start("commodity_monitor",
                                      {"commodities": commodities or ["copper"]})
        snap = coord_agent.coord.wait(run["run_id"], timeout=30)
    finally:
        coord_rt.stop()
        worker.stop()
        srv.stop()

    if not quiet:
        print(f"run {snap['run_id']} → {snap['state']}")
        for name, step in snap["steps"].items():
            print(f"  · {name:<8} {step.get('state'):<10} {step.get('error') or ''}")
        md = (snap["steps"]["report"].get("result") or {}).get("markdown") or ""
        print("\n" + md)
    return snap


if __name__ == "__main__":
    out = run()
    sys.exit(0 if out["state"] == "succeeded" else 1)