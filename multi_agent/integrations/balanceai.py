"""BalanceAI 接入：把 A/B/T/F/Q/E/J 变成 Hub 上的动作。

原则：Hub 只负责"把活派给谁"，真正的平衡表逻辑仍由各自的仓库执行；
这里用 `subprocess` 调 `python -m BalanceAI.multi_agent --role X`，
所以执行链路不经过大模型，也不会把业务代码复制进平台。

动作表
------
    A  build_sheet       建表（可带 category / commodities / workers）
    B  scan_news         新闻扫描 → 事件
    T  check_period      期次/新鲜度
    F  check_inventory   库存交叉校验
    Q  verdict           品种终审
    E  report            品种报告
    J  final_report      整轮终审 + 汇总
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Dict, Optional

ROLE_ACTIONS = {
    "A": ["build_sheet"],
    "B": ["scan_news"],
    "T": ["check_period"],
    "F": ["check_inventory"],
    "Q": ["verdict"],
    "E": ["report"],
    "J": ["final_report"],
}

ROLE_DESCRIPTIONS = {
    "A": "数据建表：官方平衡表 → JSON + 新鲜度 + diff",
    "B": "新闻扫描：DataHub futures_news → 事件 JSON",
    "T": "期次与新鲜度校验",
    "F": "库存 ↔ 平衡表交叉校验",
    "Q": "品种级终审（accept/correct/question/reject）",
    "E": "品种报告（7 段式）",
    "J": "整轮终审与汇总（离线）",
}


def register(runtime, repo: Optional[str] = None, root: Optional[str] = None) -> None:
    """把一个 Agent 注册成 BalanceAI 执行端。repo/root 也可用环境变量给。"""
    repo = repo or os.environ.get("BALANCEAI_REPO")
    root = root or os.environ.get("BALANCE_AI_ROOT")
    for role, actions in ROLE_ACTIONS.items():
        for action in actions:
            runtime.add_handler(role, action, _make_handler(role, repo, root))


def _make_handler(role: str, repo: Optional[str], root: Optional[str]):
    def _handler(params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        return run_role(role, params, repo=repo, root=root)
    return _handler


def run_role(role: str, params: Dict[str, Any], *,
             repo: Optional[str] = None, root: Optional[str] = None) -> Dict[str, Any]:
    repo = repo or params.get("repo") or os.environ.get("BALANCEAI_REPO") or "."
    root = root or params.get("root") or os.environ.get("BALANCE_AI_ROOT")
    cmd = [sys.executable, "-m", "BalanceAI.multi_agent", "--role", role]
    if role == "J":
        cmd.append("--report")
    if params.get("category"):
        cmd += ["--category", str(params["category"])]
    if params.get("commodities"):
        cmd += ["--commodities", str(params["commodities"])]
    if params.get("workers") and role == "A":
        cmd += ["--workers", str(int(params["workers"]))]

    env = os.environ.copy()
    if root:
        env["BALANCE_AI_ROOT"] = str(root)
    env.setdefault("PYTHONPATH", repo)

    proc = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True,
                          timeout=float(params.get("timeout_s") or 1200))
    out, err = proc.stdout or "", proc.stderr or ""
    if proc.returncode != 0:
        raise RuntimeError(f"BalanceAI {role} 退出码 {proc.returncode}\n{err[-800:] or out[-800:]}")

    report = None
    for line in out.splitlines():
        if line.startswith("REPORT="):
            report = line.split("=", 1)[1].strip()
    return {
        "role": role,
        "returncode": proc.returncode,
        "stdout_tail": out[-2000:],
        "report": report,
        "hint": "报告/产物路径由 BalanceAI 落盘；这里只回执行摘要",
    }