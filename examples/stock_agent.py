"""示例：一个 Agent 同时绑「宏观」和「微观辩证」两个角色。

启动::

    python -m multi_agent.cli agent --hub http://127.0.0.1:8787 --id stock-a \
        --role "macro:regime,rates" --role "micro:dialectic" \
        --handlers examples.stock_agent:register

协调端可以只发宏观，也可以只发微观；Hub 按 capability+action 投递。
"""

from __future__ import annotations

ROLES = [
    {"name": "macro", "actions": ["regime", "rates"], "description": "宏观状态"},
    {"name": "micro", "actions": ["dialectic"], "description": "微观辩证"},
]


def register(runtime) -> None:
    from multi_agent.integrations.stock import register as register_stock
    register_stock(runtime)