"""端到端演示：服务器日志 → 分诊 → 定位 → 补丁 → 审计 → 闸门 → 修复报告。

    python examples/game_server_bugfix/run_demo.py                 # 规则补丁（0 token）
    python examples/game_server_bugfix/run_demo.py --llm           # 让模型补模板覆盖不到的
    python examples/game_server_bugfix/run_demo.py --log <日志>     # 换一份日志

全程本地内存 Hub，不连外部服务；补丁写进 `out/work/repo`（fixture 副本），
不改示例仓库本身。跑完看：

    out/bugfix/bugfix_report.md
    out/bugfix/manifest.json
    out/bugfix/checksums.sha256
    out/bugfix/artifacts/*.patch
    out/bugfix/evidence/{bugs,triage,locate,propose,audit,gate}.json
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[2]

from multi_agent.client import AgentRuntime                        # noqa: E402
from multi_agent.coordinator import CoordinatorAgent               # noqa: E402
from multi_agent.dbmanager import DBManager                        # noqa: E402
from multi_agent.integrations.game_server_audit import audit_code  # noqa: E402
from multi_agent.server import HubServer                           # noqa: E402

from multi_agent.projects.bugfix_stack import DEFAULT_PARAMS, install, stack  # noqa: E402
from multi_agent.projects.platform import (                            # noqa: E402
    AuditorProject, ServerRiskProject, SupervisorProject)

TOKEN = "game-server-bugfix-demo"
EXAMPLE_DIR = Path(__file__).resolve().parent
GAME_FIXTURE = EXAMPLE_DIR.parent / "game_server" / "fixture"
OUT = EXAMPLE_DIR / "out"
WORK = OUT / "work"


def _prepare_worktree() -> Path:
    """复制 fixture 到工作副本：补丁只能改副本，不能污染示例仓库。"""
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True, exist_ok=True)
    work_repo = WORK / "repo"
    shutil.copytree(GAME_FIXTURE / "repo", work_repo,
                    ignore=shutil.ignore_patterns(".git", "__pycache__"))
    return work_repo


def params_for(work_repo: Path, log_path: Optional[str] = None,
               out_dir: Optional[str] = None, use_llm: bool = False) -> Dict[str, Any]:
    params = dict(DEFAULT_PARAMS)
    params.update({
        "log_path": log_path or str(GAME_FIXTURE / "logs" / "server.log"),
        "repo_root": str(work_repo),
        "doc_root": str(work_repo / "docs"),
        "out_dir": out_dir or str(OUT / "bugfix"),
        "use_llm": bool(use_llm),
        "replay": {"engine": str(work_repo / "server" / "sim.py"),
                   "scenario": str(GAME_FIXTURE / "scenario.json"),
                   "allow_exec": True, "runs": 2},
        "rollback": {"plan": "回滚镜像并反向执行 004 变更",
                     "script": str(work_repo / "ops" / "rollback.sh"),
                     "tested": True},
    })
    return params


def _audit_counts(repo_root: Path) -> Dict[str, int]:
    """修复前后的审计基线：同一个 profile、同一套规则。"""
    res = audit_code({"repo_root": str(repo_root), "doc_root": str(repo_root / "docs")})
    return dict(res.get("counts") or {})


def run(quiet: bool = False, log_path: Optional[str] = None,
        out_dir: Optional[str] = None, use_llm: bool = False) -> Dict[str, Any]:
    work_repo = _prepare_worktree()
    # 默认落点每次先清空：否则 artifacts 里同名补丁会被加序号，越跑越多
    target_out = Path(out_dir) if out_dir else OUT / "bugfix"
    if target_out.exists():
        shutil.rmtree(target_out)
    before = _audit_counts(work_repo)
    install(log_path=log_path)          # 登记四个业务角色项目 + bugfix 编排

    srv = HubServer(host="127.0.0.1", port=0, db_path=":memory:", token=TOKEN)
    srv.start_background()
    runtime = []
    try:
        rt_db = AgentRuntime(srv.url, "bf-db",
                             [{"name": "db", "actions": list(DBManager.ACTIONS)}],
                             token=TOKEN, concurrency=2, poll_wait=1)
        DBManager(":memory:").attach(rt_db)
        rt_db.start()
        runtime.append(rt_db)

        # 一个角色一个 Agent：每个 AgentRuntime 只挂一个项目（一角色一项目）
        roles = stack(log_path=log_path) + [
            AuditorProject(), SupervisorProject(), ServerRiskProject()]
        for project in roles:
            rt = AgentRuntime(srv.url, f"bf-{project.capability}",
                              project.manifests(), token=TOKEN,
                              concurrency=2, poll_wait=1)
            project.register(rt)
            rt.start()
            runtime.append(rt)

        coord_agent = CoordinatorAgent(srv.url, token=TOKEN)
        rt_coord = AgentRuntime(srv.url, "bf-coord",
                                [{"name": "coordinator",
                                  "actions": ["start", "status", "runs", "resume"]}],
                                token=TOKEN, concurrency=2, poll_wait=1)
        coord_agent.attach(rt_coord)
        rt_coord.start()
        runtime.append(rt_coord)

        params = params_for(work_repo, log_path, out_dir, use_llm)
        run_rec = coord_agent.coord.start("game_server_bugfix", params)
        snap = coord_agent.coord.wait(run_rec["run_id"], timeout=90)
    finally:
        for rt in runtime:
            try:
                rt.stop()
            except Exception:  # noqa: BLE001 - 关停失败不影响结果输出
                pass
        srv.stop()

    # 修复后直接取流水线里审计步骤的口径，保证与报告上的数字一致；
    # 审计步骤被跳过（没有 bug）时才现场复算一次。
    audit_step = ((snap.get("steps") or {}).get("audit") or {}).get("result") or {}
    after = dict(audit_step.get("counts") or {}) or _audit_counts(work_repo)
    if not quiet:
        print(f"run {snap['run_id']} → {snap['state']}")
        for name, step in snap["steps"].items():
            print(f"  · {name:<9} {step.get('state'):<10} {step.get('error') or ''}")
        bugs = (snap["steps"].get("bugs") or {}).get("result") or {}
        tri = (snap["steps"].get("triage") or {}).get("result") or {}
        loc = (snap["steps"].get("locate") or {}).get("result") or {}
        prop = (snap["steps"].get("propose") or {}).get("result") or {}
        audit = (snap["steps"].get("audit") or {}).get("result") or {}
        gate = (snap["steps"].get("gate") or {}).get("result") or {}
        report = (snap["steps"].get("report") or {}).get("result") or {}
        print(f"\n采集：{len(bugs.get('bugs') or [])} 条 bug"
              f"（扫描 {bugs.get('scanned', 0)} 条，去重 {bugs.get('deduped', 0)}，"
              f"忽略 INFO {bugs.get('ignored', 0)}）")
        print(f"分诊：{len(tri.get('clusters') or [])} 簇")
        print(f"定位：{loc.get('counts', {}).get('located', 0)} 条，"
              f"未定位 {loc.get('counts', {}).get('unlocated', 0)} 条")
        print(f"补丁：{prop.get('counts', {}).get('edits', 0)} 条"
              f"（模式 {prop.get('mode')}），待人工 {prop.get('counts', {}).get('deferred', 0)} 簇")
        print(f"审计：error {before.get('error', 0)} → {after.get('error', 0)}，"
              f"warn {before.get('warn', 0)} → {after.get('warn', 0)}")
        print(f"闸门：{gate.get('gate')}，拦截项：{gate.get('blocked_by')}")
        print(f"报告：{report.get('report_path')}")
        print("\n" + (report.get("markdown") or "")[:1600])
    snap["_audit_before"] = before
    snap["_audit_after"] = after
    snap["_work_repo"] = str(work_repo)
    return snap


def main(argv: Optional[list] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="game-server bugfix 端到端演示")
    ap.add_argument("--log", default=None, help="日志文件（默认 fixture/logs/server.log）")
    ap.add_argument("--out", default=None, help="报告落点")
    ap.add_argument("--llm", action="store_true",
                    help="允许模型补丁（需要 LLM_URL/AUDIT_LLM_URL 或 LLM_MCP_COMMAND）")
    args = ap.parse_args(argv)

    snap = run(log_path=args.log, out_dir=args.out, use_llm=args.llm)
    steps = snap.get("steps", {})
    prop = (steps.get("propose") or {}).get("result") or {}
    before, after = snap["_audit_before"], snap["_audit_after"]
    ok = snap.get("state") in ("succeeded", "degraded")
    ok = ok and int(prop.get("counts", {}).get("edits", 0)) > 0
    ok = ok and after.get("error", 0) <= before.get("error", 0)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())