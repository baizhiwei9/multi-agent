"""项目包：框架自带四个参考实现，新增项目照抄任意一个即可。

    futures-balance  期货收支平衡表（BalanceAI：A/B/T/F/Q/C/D/E/J）
    stock-matt       股票 A 股日报（stocks_matt_report：M1..M5 + 新闻采集官）
    min-report       最小示例（只声明角色与流水线，用来验证"不写代码也能接"）
    game-server      游戏服务端：开发报告 → 并行开发 → 审计 → 风控 → 发布包

四个都继承自 `base.Project`，差异只在声明与两三个覆写方法上。
"""
from __future__ import annotations

from .base import (REGISTRY, Project, ProjectRegistry, RoleSpec,
                   install_pipelines, register)
from .example_min import MinimalReportProject
from .futures_balance import BALANCE_PIPELINE, FuturesBalanceProject
from .news import (DataHubNewsCollector, NewsCollector, NullNewsCollector,
                   RuleNewsCollector, StaticNewsCollector)
from .reporter import (BalanceSheetReporter, MarkdownReporter, MattReporter,
                       Reporter)
from .stock_matt import MATT_PIPELINE, StockMattProject
from .game_server import GAME_SERVER_PIPELINE, GameServerProject

#: 框架自带项目（导入即登记；重复导入不会报错）
BUILTIN = (MinimalReportProject, FuturesBalanceProject, StockMattProject,
           GameServerProject)


def register_builtins(registry: ProjectRegistry = REGISTRY,
                      replace: bool = True) -> ProjectRegistry:
    for cls in BUILTIN:
        registry.register(cls(), replace=replace)
    return registry


register_builtins()

__all__ = [
    "Project", "ProjectRegistry", "RoleSpec", "REGISTRY", "register",
    "install_pipelines", "register_builtins",
    "NewsCollector", "NullNewsCollector", "DataHubNewsCollector",
    "RuleNewsCollector", "StaticNewsCollector",
    "Reporter", "MarkdownReporter", "BalanceSheetReporter", "MattReporter",
    "FuturesBalanceProject", "StockMattProject", "MinimalReportProject",
    "GameServerProject",
    "BALANCE_PIPELINE", "MATT_PIPELINE", "GAME_SERVER_PIPELINE",
]