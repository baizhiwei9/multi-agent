"""BalanceAI 接入：把 A/B/T/F/Q/C/D/E/J 变成 Hub 上的动作。

原则：Hub 只负责"把活派给谁"，真正的平衡表逻辑仍由各自的仓库执行；
这里用 `subprocess` 调 `python -m BalanceAI.hub_entry exec`，所以执行链路
不经过大模型，也不会把业务代码复制进平台。

入口选型（别用错）
------------------
BalanceAI 侧有两个 CLI：

· `BalanceAI.hub_entry` —— **项目适配器，本模块用的就是它**。参数是
  `--capability <角色> --action <动作> --params <JSON>`，覆盖
  A/B/T/F/Q/C/D/E/J 共 12 个动作，返回动作的原始 dict，
  所以 `result.by_commodity.<品种>.major` 这类路径直接可读。

· `BalanceAI.multi_agent` —— BalanceAI 自己的多角色**批量**运行时
  （`--role A --commodities gold,copper`），只支持 A/T/F/Q/J，
  **不是 Hub 入口**：`--role B` 会打印"未支持的角色"并 exit 2，
  且它的返回是 run 级 summary，没有 `by_commodity` 这些字段。

常驻模式更推荐：BalanceAI 自己实现了 `BalanceAIProject`（继承框架的
`FuturesBalanceProject`，只换 executor），
`python -m BalanceAI.hub_entry agent --hub ... --id ...` 就能把全部角色
注册到 Hub，省掉每步起进程的开销，C/D/E 还能拿到 DBClient。

动作表
------
    A  build_sheet / inventory   建表 / 库存交叉校验
    B  scan_news                 新闻扫描 → 按品种分流
    T  check_period              期次/新鲜度
    F  check_inventory           库存交叉校验
    Q  verdict                   品种终审
    C  shock                     事件冲击测算
    D  review                    事件 × 冲击复核
    E  report                    品种报告
    J  final_report              整轮终审 + 汇总
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Dict, Optional

#: 与 `python -m BalanceAI.hub_entry describe` 的角色表对齐。
#: 注意：辩论流水线用的 moderator / supervisor / debate 不在 BalanceAI 的
#: DISPATCH 里（它只实现 A/B/T/F/Q/C/D/E/J + risk），这里不挂。
ROLE_ACTIONS = {
    "A": ["build_sheet", "fetch_official", "inventory"],
    "B": ["scan_news", "scan_events"],
    "T": ["check_period"],
    "F": ["check_inventory"],
    "Q": ["verdict"],
    "C": ["shock"],
    "D": ["review"],
    "E": ["report"],
    "J": ["final_report"],
}

ROLE_DESCRIPTIONS = {
    "A": "数据建表：官方平衡表 → JSON + 新鲜度 + diff",
    "B": "新闻扫描：DataHub futures_news → 按品种分流的事件",
    "T": "期次与新鲜度校验",
    "F": "库存 ↔ 平衡表交叉校验",
    "Q": "品种级终审（accept/correct/question/reject）",
    "C": "事件冲击测算",
    "D": "事件 × 冲击复核",
    "E": "品种报告（7 段式）",
    "J": "整轮终审与汇总（离线）",
}

#: 只给 Hub 侧用、不该透给 BalanceAI CLI 的键
_LOCAL_KEYS = ("repo", "root", "action", "timeout_s")


def register(runtime, repo: Optional[str] = None, root: Optional[str] = None) -> None:
    """把一个 Agent 注册成 BalanceAI 执行端。repo/root 也可用环境变量给。"""
    repo = repo or os.environ.get("BALANCEAI_REPO")
    root = root or os.environ.get("BALANCE_AI_ROOT")
    for role, actions in ROLE_ACTIONS.items():
        for action in actions:
            runtime.add_handler(role, action, _make_handler(role, action, repo, root))


def _make_handler(role: str, action: str,
                  repo: Optional[str], root: Optional[str]):
    def _handler(params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        return run_role(role, params, action=action, repo=repo, root=root)
    return _handler


def run_role(role: str, params: Dict[str, Any], *,
             repo: Optional[str] = None, root: Optional[str] = None,
             action: Optional[str] = None) -> Dict[str, Any]:
    """调 `BalanceAI.hub_entry exec` 跑一个动作，返回动作自己的 dict。

    `params` 原样作为 `--params` 的 JSON 传过去（BalanceAI 侧用
    `params["commodity"]` 取品种，单数；也兼容 `category`），
    所以 `{"commodity": "gold"}` → `_commodity()` 拿到 gold。
    """
    repo = repo or params.get("repo") or os.environ.get("BALANCEAI_REPO") or "."
    root = root or params.get("root") or os.environ.get("BALANCE_AI_ROOT")
    action = action or params.get("action") or (ROLE_ACTIONS.get(role) or [None])[0]
    if not action:
        raise ValueError(f"未知角色 {role}：无法确定 action")

    payload = {k: v for k, v in params.items() if k not in _LOCAL_KEYS}
    cmd = [sys.executable, "-m", "BalanceAI.hub_entry", "exec",
           "--capability", role, "--action", action,
           "--params", json.dumps(payload, ensure_ascii=False)]

    env = os.environ.copy()
    if root:
        env["BALANCE_AI_ROOT"] = str(root)
    # 追加而不是覆盖：hub_entry 要同时 import BalanceAI 和 multi_agent 框架，
    # 把 PYTHONPATH 整个换成 repo 会让框架找不到（No module named 'multi_agent'）。
    if repo:
        prev = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(repo) + (os.pathsep + prev if prev else "")

    proc = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True,
                          timeout=float(params.get("timeout_s") or 1200))
    out, err = proc.stdout or "", proc.stderr or ""
    if proc.returncode != 0:
        raise RuntimeError(
            f"BalanceAI {role}/{action} 退出码 {proc.returncode}\n"
            f"{err[-800:] or out[-800:]}")

    try:
        result = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        result = None
    if not isinstance(result, dict):
        raise RuntimeError(
            f"BalanceAI {role}/{action} 输出不是 JSON dict\n{out[-800:] or err[-800:]}")
    # 动作自己报错时 hub_entry 已经 exit 1，这里再兜一层（防止有人改了它）
    if result.get("ok") is False:
        raise RuntimeError(f"BalanceAI {role}/{action} 失败：{result.get('error')}")
    return result
