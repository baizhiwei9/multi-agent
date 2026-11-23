"""示例：把 BalanceAI 的 A/B/T/F/Q/C/D/E/J 挂到一个 Agent 上。

两条路，优先用第二条
--------------------
**① 常驻模式（推荐）**：BalanceAI 仓库自己就有适配器，全部角色一次注册，
进程内执行，C/D/E 还能拿到 DBClient——

    cd /path/to/commodities_strategies
    python -m BalanceAI.hub_entry agent --hub http://127.0.0.1:8787 --id balanceai-1

**② 本示例（subprocess 派活）**：Hub 每派一个动作就起一个
`python -m BalanceAI.hub_entry exec` 子进程。适合只想挂部分角色、
或不想让 BalanceAI 常驻的场景，代价是每步多一次进程启动。

启动（在 multi-agent 项目根目录）::

    export BALANCEAI_REPO=/path/to/commodities_strategies
    export BALANCE_AI_ROOT=/path/to/run_root
    python -m multi_agent.cli agent --hub http://127.0.0.1:8787 --id balanceai-1 \
        --role "A:build_sheet" --role "B:scan_news" --role "T:check_period" \
        --role "F:check_inventory" --role "Q:verdict" --role "C:shock" \
        --role "D:review" --role "E:report" --role "J:final_report" \
        --handlers examples.balanceai_agent:register

协调端（另一个进程/机器）::

    python -m multi_agent.cli submit --hub http://127.0.0.1:8787 \
        --capability B --action scan_news --params '{"commodity":"gold","hours":24}' --wait
"""

from __future__ import annotations

from multi_agent.integrations.balanceai import ROLE_ACTIONS, ROLE_DESCRIPTIONS

#: 角色表直接从接入层生成，避免两处各写一份、加角色时漏掉
ROLES = [
    {"name": role, "actions": list(actions),
     "description": ROLE_DESCRIPTIONS.get(role, "")}
    for role, actions in ROLE_ACTIONS.items()
]


def register(runtime) -> None:
    from multi_agent.integrations.balanceai import register as register_balanceai
    register_balanceai(runtime)
