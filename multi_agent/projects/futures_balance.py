"""兼容旧导入路径：项目已移到 `multi_agent/projects/trade/futures_balance.py`。

外部接入层（如 BalanceAI 的 `hub_entry`）仍按 `multi_agent.projects.futures_balance`
导入，升级框架后会直接 ImportError。这里只做转发，不复制实现——新代码请走
`multi_agent.projects.trade.futures_balance`。
"""
from __future__ import annotations

from .trade.futures_balance import (  # noqa: F401  (re-export)
    BALANCE_PIPELINE,
    DEBATE_PIPELINE,
    FuturesBalanceProject,
)

__all__ = ["BALANCE_PIPELINE", "DEBATE_PIPELINE", "FuturesBalanceProject"]
