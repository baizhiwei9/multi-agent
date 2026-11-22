"""示例：把 BalanceAI 的 A/B/T/F/Q/E/J 挂到一个 Agent 上。

启动（在 multi-agent 项目根目录）::

    export BALANCEAI_REPO=/path/to/commodities_strategies
    export BALANCE_AI_ROOT=/path/to/run_root
    python -m multi_agent.cli agent --hub http://127.0.0.1:8787 --id balanceai-1 \
        --role "A:build_sheet" --role "T:check_period" --role "F:check_inventory" \
        --role "Q:verdict" --role "J:final_report" \
        --handlers examples.balanceai_agent:register

协调端（另一个进程/机器）::

    python -m multi_agent.cli submit --hub http://127.0.0.1:8787 \
        --capability A --action build_sheet --params '{"category":"有色"}' --wait
"""

from __future__ import annotations

ROLES = [
    {"name": "A", "actions": ["build_sheet"], "description": "建表"},
    {"name": "T", "actions": ["check_period"], "description": "期次/新鲜度"},
    {"name": "F", "actions": ["check_inventory"], "description": "库存校验"},
    {"name": "Q", "actions": ["verdict"], "description": "品种终审"},
    {"name": "J", "actions": ["final_report"], "description": "整轮终审/汇总"},
]


def register(runtime) -> None:
    from multi_agent.integrations.balanceai import register as register_balanceai
    register_balanceai(runtime)