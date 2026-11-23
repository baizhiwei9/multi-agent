"""端到端演示：开发报告 → 接口规范门 → 拆包 → 并行开发 → 审计 → 风控 → 发布包。

    python examples/game_server/run_demo.py                     # 正常报告（规范通过，闸门拦截）
    python examples/game_server/run_demo.py --ticket examples/game_server/fixture/ticket_bad_spec.md
                                                                # 规范不合格：不拆包、不下发开发

全程本地内存 Hub，不连任何外部服务；开发补丁写进 `out/work/repo`（fixture 的副本），
不会改动示例仓库本身。跑完看：

    out/release/<ticket_id>/release_report.md
    out/release/<ticket_id>/manifest.json
    out/release/<ticket_id>/checksums.sha256
    out/release/<ticket_id>/artifacts/test_plan.md
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional

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


def params_for(work_repo: Path, ticket: Optional[str] = None,
               ticket_id: Optional[str] = None,
               out_dir: Optional[str] = None) -> Dict[str, Any]:
    project = GameServerProject()
    params = dict(project.default_params)
    ticket_path = ticket or str(FIXTURE / "ticket.json")
    ticket_id = ticket_id or Path(ticket_path).stem.replace("ticket_", "gs-") or "gs-1042"
    if ticket_id in ("ticket", "gs-"):
        ticket_id = "gs-1042"
    params.update({
        "ticket": ticket_path,
        "repo_root": str(work_repo),
        "doc_root": str(work_repo / "docs"),
        "out_dir": out_dir or str(OUT / "release" / ticket_id),
        "ticket_id": ticket_id,
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
        "llm_test_report": False,
    })
    return params


def run(quiet: bool = False, ticket: Optional[str] = None,
        ticket_id: Optional[str] = None,
        out_dir: Optional[str] = None) -> Dict[str, Any]:
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

        params = params_for(work_repo, ticket, ticket_id, out_dir)
        run_rec = coord_agent.coord.start("game_server_release", params)
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
            print(f"  · {name:<10} {step.get('state'):<10} {step.get('error') or ''}")
        spec = (snap["steps"].get("spec_gate") or {}).get("result") or {}
        print(f"\n接口规范：{'通过' if spec.get('ok') else '不合格'} "
              f"（error {((spec.get('counts') or {}).get('error', 0))}）")
        gate = (snap["steps"].get("gate") or {}).get("result") or {}
        print(f"闸门：{gate.get('gate')}，拦截项：{gate.get('blocked_by')}")
        plan = (snap["steps"].get("test_plan") or {}).get("result") or {}
        print(f"测试用例：{plan.get('count')} 条（{'模型' if plan.get('llm_used') else '模板'}）")
        report = (snap["steps"].get("report") or {}).get("result") or {}
        print(f"发布包：{report.get('out_dir')}")
        print("\n" + (report.get("markdown") or "")[:1500])
    return snap


def main(argv: Optional[list] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="game-server 端到端演示")
    ap.add_argument("--ticket", default=None,
                    help="开发报告路径（默认 fixture/ticket.json；可传 ticket_bad_spec.md）")
    ap.add_argument("--ticket-id", default=None, help="工单号（决定 out 目录名）")
    ap.add_argument("--out", default=None, help="发布包落点")
    args = ap.parse_args(argv)

    snap = run(ticket=args.ticket, ticket_id=args.ticket_id, out_dir=args.out)
    steps = snap.get("steps", {})
    spec_ok = (steps.get("spec_gate", {}).get("result") or {}).get("ok")
    gate = (steps.get("gate", {}).get("result") or {}).get("gate")
    plan_state = (steps.get("plan") or {}).get("state")
    ok = snap.get("state") in ("succeeded", "degraded")
    if spec_ok is False:
        # 规范不合格的演示：必须停在报告阶段（plan 未执行）
        ok = ok and plan_state == "skipped"
    else:
        # 正常演示：fixture 故意有缺陷，闸门必须拦
        ok = ok and gate == "block"
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())