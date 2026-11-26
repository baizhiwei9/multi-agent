"""平台角色（platform）：跨域通用，不属于 trade 也不属于 netdev。

    auditor      审计官        code / test_plan
    supervisor   经理/监理     pack / handoff / ledger / assign
    server_risk  风控官        gate / capacity / replay
    llm-gateway  模型通道      chat（http 或 mcp 后端）

它们的实现都在 `integrations/`，这里只是给每个角色配一个"只有一个 capability 的项目壳"，
好单独起进程、单独扩实例、单独下线。
"""
from __future__ import annotations

from .roles import (AuditorProject, LLMGatewayProject, ServerRiskProject,
                    SupervisorProject)

__all__ = ["AuditorProject", "SupervisorProject", "ServerRiskProject",
           "LLMGatewayProject"]