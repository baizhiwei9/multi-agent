"""框架角色项目：审计官 / 经理 / 风控官 / 模型通道，各自一个项目。

它们的实现本来就在 `integrations/` 里（任何业务都能用），这里只是给每个角色
配一个"只有一个 capability 的项目壳"，好让它们和业务角色一样：

    · 单独起进程、单独扩实例、单独下线；
    · 出现在 `multi-agent project show` 里，知道谁在线；
    · 被编排按 capability 名字引用，不需要知道是框架实现还是业务实现。

所以这些项目的 `handlers()` 是空的，动作全部由 `register_builtin()` 挂。
"""
from __future__ import annotations

from typing import Any, Dict

from ..base import RoleProject


class AuditorProject(RoleProject):
    name = "auditor"
    title = "审计官"
    description = "文档/协议/代码/迁移/配置审计（规则级 0 token；需要时才升级到统计/语义）"
    capability = "auditor"
    actions = ("code", "test_plan")
    role_description = "审计官：只读、不改代码、不联网（规则级）；test_plan 可选接模型"

    def handlers(self) -> Dict[str, Dict[str, Any]]:
        return {}   # 全部由框架注册

    def register_builtin(self, runtime: Any) -> None:
        from ...integrations.game_server_audit import register as register_code
        from ...integrations.game_server_spec import register_test_plan

        register_code(runtime)
        register_test_plan(runtime)


class SupervisorProject(RoleProject):
    name = "supervisor"
    title = "经理/监理"
    description = "送审件打包 + 送审台账（框架实现，0 token）"
    capability = "supervisor"
    actions = ("pack", "handoff", "ledger", "assign", "tune")
    role_description = "经理：把上游结果压成送审件、记台账、必要时转交审计"

    def handlers(self) -> Dict[str, Dict[str, Any]]:
        return {}

    def register_builtin(self, runtime: Any) -> None:
        from ...integrations.supervisor import register

        register(runtime, client=getattr(runtime, "client", None))


class ServerRiskProject(RoleProject):
    name = "server-risk"
    title = "服务端风控官"
    description = "容量 / 确定性回放 / 回滚 / 灰度 / 经济闸门（0 token，回放需 allow_exec）"
    capability = "server_risk"
    actions = ("gate", "capacity", "replay")
    role_description = "风控官：计算与闸门，回放执行被审代码时必须显式 allow_exec=True"

    def handlers(self) -> Dict[str, Dict[str, Any]]:
        return {}

    def register_builtin(self, runtime: Any) -> None:
        from ...integrations.game_server_risk import register

        register(runtime)


class LLMGatewayProject(RoleProject):
    name = "llm-gateway"
    title = "模型通道"
    description = "llm/chat：http（OpenAI 兼容）或 mcp 后端，未配置就 degraded，不烧 token"
    capability = "llm"
    actions = ("chat",)
    role_description = "模型通道：调用方只写 llm/chat，换后端不改调用方"

    def handlers(self) -> Dict[str, Dict[str, Any]]:
        return {}

    def register_builtin(self, runtime: Any) -> None:
        from ...integrations.llm import register

        register(runtime)