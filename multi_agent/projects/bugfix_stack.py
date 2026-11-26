"""bugfix 栈：**把四个角色项目摆在一起**，不是一个项目装四个角色。

    bug-collector      bug/collect
    bug-triage         triage/classify
    bugfix-dev         dev/locate|propose|implement
    bugfix-reporter    report/build
    game_server_bugfix （编排，见 projects/flows/bugfix.py）

`stack()` 只是给 demo / 部署脚本用的清单：想全量起，就按清单起四个进程；
只想换采集官，就只换 `bug-collector` 那个项目，其余不动。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .base import REGISTRY, Project, install_pipelines
from .flows import register_flow
from .flows.bugfix import BUGFIX_FLOW
from .netdev.bug_collector import BugCollectProject
from .netdev.bug_triage import BugTriageProject
from .netdev.bugfix_dev import BugfixDevProject
from .netdev.bugfix_reporter import BugfixReportProject

_FIXTURE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                        "examples", "game_server", "fixture"))

#: 一条 bugfix 链需要的角色项目（一角色一项目）
ROLE_PROJECTS = (BugCollectProject, BugTriageProject, BugfixDevProject,
                 BugfixReportProject)

#: 编排级默认参数：属于"这条链"而不是"某个角色项目"，所以放在编排旁边
DEFAULT_PARAMS: Dict[str, Any] = {
    "log_path": os.path.join(_FIXTURE, "logs", "server.log"),
    "repo_root": os.path.join(_FIXTURE, "repo"),
    "doc_root": os.path.join(_FIXTURE, "repo", "docs"),
    "ticket_id": "bugfix-demo",
    "out_dir": os.path.join(os.getcwd(), "release", "bugfix"),
    "audit_level": "rule",
    "use_llm": False,
    "max_clusters": 8,
    "capacity": {"expected_ccu": 4200, "ccu_limit": 5000,
                 "tick_budget_ms": 50, "p99_tick_ms": 38},
    "replay": {"engine": os.path.join(_FIXTURE, "repo", "server", "sim.py"),
               "scenario": os.path.join(_FIXTURE, "scenario.json"),
               "allow_exec": True, "runs": 2},
    "rollback": {"plan": "回滚镜像并反向执行 004 迁移", "script": None, "tested": True},
    "canary_pct": 5,
    "economy": {"faucet_daily_cap": 0, "inflation_daily_pct": 0.6},
    "strict": True,
}


def stack(log_path: Optional[str] = None) -> List[Project]:
    """返回这条链上的角色项目实例（每个只声明一个 capability）。"""
    return [
        BugCollectProject(log_path=log_path),
        BugTriageProject(),
        BugfixDevProject(),
        BugfixReportProject(),
    ]


def install(registry: Optional[Any] = None, log_path: Optional[str] = None) -> Dict[str, Any]:
    """登记四个角色项目 + bugfix 编排；返回起进程需要的清单。"""
    reg = registry or REGISTRY
    projects = stack(log_path=log_path)
    for project in projects:
        reg.register(project, replace=True)
    install_pipelines(reg)
    register_flow(BUGFIX_FLOW)
    return {
        "projects": [p.name for p in projects],
        "capabilities": [c for p in projects for c in p.capabilities()],
        "flow": BUGFIX_FLOW["name"],
    }