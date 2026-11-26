"""项目包：**按领域分目录**，两种粒度。

领域（一个业务域一个文件夹，不再堆在一起）：

    trade/     交易域    新闻采集（继承 frameworks/news）→ 评估 → 平衡表 / 股票日报
    netdev/    互联网开发域  bug 采集（继承 frameworks/netdev）→ 分诊 → 开发 → 报告
    platform/  平台角色    审计 / 经理 / 风控 / 模型通道（跨域通用）
    flows/     编排       只写 capability + action，不属于任何角色

粒度：

- **业务聚合项目**（`futures-balance` / `stock-matt` / `game-server` / `min-report`）：
  一条链的完整示例，适合跑 demo；
- **单角色项目**（`bug-collector` / `bugfix-dev` / `news-collector` / `auditor` …）：
  一个项目一个 capability，真实部署时一角色一进程。
"""
from __future__ import annotations

from .base import (REGISTRY, Project, ProjectRegistry, RoleProject, RoleSpec,
                   install_pipelines, register)
from .example_min import MinimalReportProject
from .flows import check_capabilities, register_flow, required_capabilities
from .netdev import (BugCollectProject, BugTriageProject, BugfixDevProject,
                     BugfixReportProject, GAME_SERVER_PIPELINE, GameServerProject)
from .platform import (AuditorProject, LLMGatewayProject, ServerRiskProject,
                       SupervisorProject)
from .reporter import (BalanceSheetReporter, MarkdownReporter, MattReporter,
                       Reporter)
from .trade import (BALANCE_PIPELINE, MATT_PIPELINE, BalanceSheetProject,
                    FuturesBalanceProject, NewsAssessProject, NewsCollectProject,
                    StockMattProject)
from ..frameworks.news.collectors import (DataHubNewsCollector, NewsCollector,
                                          NullNewsCollector, RuleNewsCollector,
                                          StaticNewsCollector)

#: 框架自带项目（导入即登记；重复导入不会报错）
BUILTIN = (
    # 业务聚合项目（demo 用）
    MinimalReportProject, FuturesBalanceProject, StockMattProject, GameServerProject,
    # 交易域单角色
    NewsCollectProject, NewsAssessProject, BalanceSheetProject,
    # 互联网开发域单角色
    BugCollectProject, BugTriageProject, BugfixDevProject, BugfixReportProject,
    # 平台角色
    AuditorProject, SupervisorProject, ServerRiskProject, LLMGatewayProject,
)


def register_builtins(registry: ProjectRegistry = REGISTRY,
                      replace: bool = True) -> ProjectRegistry:
    for cls in BUILTIN:
        registry.register(cls(), replace=replace)
    return registry


register_builtins()

__all__ = [
    "Project", "ProjectRegistry", "RoleProject", "RoleSpec", "REGISTRY", "register",
    "install_pipelines", "register_builtins",
    "NewsCollector", "NullNewsCollector", "DataHubNewsCollector",
    "RuleNewsCollector", "StaticNewsCollector",
    "Reporter", "MarkdownReporter", "BalanceSheetReporter", "MattReporter",
    # 交易域
    "FuturesBalanceProject", "StockMattProject", "NewsCollectProject",
    "NewsAssessProject", "BalanceSheetProject", "BALANCE_PIPELINE", "MATT_PIPELINE",
    # 互联网开发域
    "GameServerProject", "BugCollectProject", "BugTriageProject",
    "BugfixDevProject", "BugfixReportProject", "GAME_SERVER_PIPELINE",
    # 平台角色
    "AuditorProject", "SupervisorProject", "ServerRiskProject", "LLMGatewayProject",
    # 编排
    "register_flow", "required_capabilities", "check_capabilities",
    # 最小示例
    "MinimalReportProject",
]