"""平衡表角色项目：只声明 `balance/build` 一个动作。

平衡表的真实计算在 BalanceAI（DataHub + 口径）那边，这个项目只负责
"把请求转过去、把结果按框架形状交回来"。没注入 executor 时就明确 degraded，
不自己估数——这一点和 `projects/futures_balance.py` 的 A 角色同口径。

注入方式（真实部署）：

    from multi_agent.projects.trade import BalanceSheetProject
    BalanceSheetProject(executor=my_balanceai_executor)

`executor(capability, action, params, ctx)` 的签名与 `FuturesBalanceProject` 一致，
所以可以直接复用那边注入的那一个。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from ..base import RoleProject

Executor = Callable[[str, str, Dict[str, Any], Dict[str, Any]], Any]


class BalanceSheetProject(RoleProject):
    name = "balance-sheet"
    title = "平衡表处理"
    description = "官方平衡表 → 固定字段快照（真实计算在 BalanceAI，未注入就 degraded）"
    capability = "balance"
    actions = ("build",)
    role_description = "平衡表：取数建表，口径由 balance_sheet profile 审计"

    def __init__(self, executor: Optional[Executor] = None) -> None:
        super().__init__()
        self._executor = executor

    def handlers(self) -> Dict[str, Dict[str, Any]]:
        def _build(params: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
            if self._executor is None:
                return {"ok": False, "degraded": True,
                        "capability": "balance", "action": "build",
                        "reason": "未注入 executor：平衡表计算在 BalanceAI 侧执行，"
                                  "或用 BalanceSheetProject(executor=...) 接入",
                        "params": params or {}}
            return self._executor("balance", "build", params or {}, ctx or {})

        return {"balance": {"build": _build}}