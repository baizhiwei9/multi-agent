"""bug 修复开发项目：只声明 `dev` 一个角色，三个动作。

    定位 locate  → 不猜位置（栈帧 → 路由表 → 关键词）
    方案 propose → 规则模板优先，可选模型补丁（只产 JSON）
    落地 implement → find/replace + 语法检查 + 出 diff（框架实现）

它不持有流水线，也不管审计怎么判；审计是 auditor 那个项目的事。
"""
from __future__ import annotations

from typing import Any, Dict

from ..base import RoleProject


class BugfixDevProject(RoleProject):
    name = "bugfix-dev"
    title = "bug 修复开发"
    description = "定位文件行 → 出补丁方案（规则/可选模型）→ 应用到工作副本"
    capability = "dev"
    actions = ("locate", "propose", "implement")
    role_description = "开发：定位 / 补丁方案 / 落地（implement 由框架实现，零 token）"

    def handlers(self) -> Dict[str, Dict[str, Any]]:
        from ...integrations.game_server_bugfix import locate, propose
        from ...integrations.game_server_dev import implement

        return {"dev": {
            "locate": lambda p, c: locate(p or {}, c),
            "propose": lambda p, c: propose(p or {}, c),
            "implement": lambda p, c: implement(p or {}, c),
        }}