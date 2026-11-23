"""端到端演示：开发报告 → 拆包 → 并行开发 → 经理送审 → 审计 → 风控 → 发布包。

    python examples/game_server/run_demo.py

全程本地内存 Hub，不连任何外部服务；开发补丁写进 `out/work/repo`（fixture 的副本），
不会改动示例仓库本身。跑完看：

    out/release/gs-1042/release_report.md
    out/release/gs-1042/manifest.json
    out/release/gs-1042/checksums.sha256
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from multi_agent.client import AgentRuntime                 # noqa: E402
from multi_agent.coordinator import CoordinatorAgent         # noqa: E402
from multi_agent.dbmanager import DBManager                  # noqa: E402
from multi_agent.server import HubServer                     # noqa: E402

from multi_agent.projects.game_server import GameServerProject, install  # noqa: E402

TOKEN = "game-server-demo"
EXAMPLE_DIR = Path(__file__).resolve().parent
FIXTURE = EXAMPLE_DIR / "fixture"
OUT = EXAMPLE_DIR / "out"
WORK = OUT / "work"


def _prepare_worktree() -> Path:
    """复制 fixture 到工作副本（dev 会在这上面改代码，不能污染示例仓库）。"""
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True, exist_ok=True)
    work_repo = WORK / "repo"
    shutil.copytree(FIXTURE / "repo", work_repo,
                    ignore=shutil.ignore_patterns(".git", "__pycache__"))
    return work_repo


def params_for(work_repo: Path) -> Dict[str, Any]:
    project = GameServerProject()
    params = dict(project.default_params)
    params.update({
        "ticket": str(FIXTURE / "ticket.json"),
        "repo_root": str(work_repo),
        "doc_root": str(work_repo / "docs"),
        "out_dir": str(OUT / "release" / "gs-1042"),
        "ticket_id": "gs-1042",
        "capacity": {"expected_ccu": 5200, "ccu_limit": 5000,
                     "tick_budget_ms": 50, "p99_tick_ms": 62},
        "replay": {"engine": str(work_repo / "server" / "sim.py"),
                   "scenario": str(FIXTURE / "scenario.json"),
                   "allow_exec": True, "runs": 2},
        "rollback": {"plan": "回滚镜像并反向执行 004 变更",
                     "script": str(work_repo / "ops" / "rollback.sh"),
                     "tested": False},
        "canary_pct": 5,
        "economy": {"faucet_daily_cap": 0, "inflation_daily_pct": 1.2},
        "strict": True,
    })
    return params


def run(quiet: bool = False) -> Dict[str, Any]:
    work_repo = _prepare_worktree()
    project = install()

    srv = HubServer(host="127.0.0.1", port=0, db_path=":memory:", token=TOKEN)
    srv.start_background()
    runtime = []
    try:
        # DBManager：唯一写入口，发布记录入库
        rt_db = AgentRuntime(srv.url, "gs-db",
                             [{"name": "db", "actions": list(DBManager.ACTIONS)}],
                             token=TOKEN, concurrency=2, poll_wait=1)
        DBManager(":memory:").attach(rt_db)
        rt_db.start()
        runtime.append(rt_db)

        # 业务 + 框架角色一个进程（dev 并发 3 以吃下扇出）
        caps = ["producer", "dev", "report", "supervisor", "auditor", "server_risk"]
        worker = AgentRuntime(srv.url, "gs-worker", project.manifests(capabilities=caps),
                              token=TOKEN, concurrency=4, poll_wait=1)
        project.register(worker)
        worker.start()
        runtime.append(worker)

        coord_agent = CoordinatorAgent(srv.url, token=TOKEN)
        rt_coord = AgentRuntime(srv.url, "gs-coord",
                                [{"name": "coordinator",
                                  "actions": ["start", "status", "runs", "resume"]}],
                                token=TOKEN, concurrency=2, poll_wait=1)
        coord_agent.attach(rt_coord)
        rt_coord.start()
        runtime.append(rt_coord)

        run_rec = coord_agent.coord.start("game_server_release", params_for(work_repo))
        snap = coord_agent.coord.wait(run_rec["run_id"], timeout=60)
    finally:
        for rt in runtime:
            try:
                rt.stop()
            except Exception:  # noqa: BLE001 - 关停失败不影响结果输出
                pass
        srv.stop()

    if not quiet:
        print(f"run {snap['run_id']} → {snap['state']}")
        for name, step in snap["steps"].items():
            print(f"  · {name:<8} {step.get('state'):<10} {step.get('error') or ''}")
        gate = (snap["steps"].get("gate") or {}).get("result") or {}
        print(f"\n闸门：{gate.get('gate')}，拦截项：{gate.get('blocked_by')}")
        report = (snap["steps"].get("report") or {}).get("result") or {}
        print(f"发布包：{report.get('out_dir')}")
        print("\n" + (report.get("markdown") or "")[:1500])
    return snap


if __name__ == "__main__":
    out = run()
    gate = (out.get("steps", {}).get("gate") or {}).get("result") or {}
    # 演示的 fixture 故意有缺陷：跑通即成功，闸门结论由报告体现
    sys.exit(0 if out.get("state") == "succeeded" and gate.get("gate") == "block" else 1)