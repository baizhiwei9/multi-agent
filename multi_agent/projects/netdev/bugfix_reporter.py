"""修复报告项目：只声明 `report` 一个角色。

它消费上游结果（bug/分诊/定位/补丁/审计/闸门），出报告 + manifest + 证据，
不重算、不补数。
"""
from __future__ import annotations

from typing import Any, Dict

from ..base import RoleProject


class BugfixReportProject(RoleProject):
    name = "bugfix-reporter"
    title = "bug 修复报告官"
    description = "修复报告 + 补丁清单 + 证据落盘（消费上游结果，不重算）"
    capability = "report"
    actions = ("build",)
    role_description = "报告官：bug 清单 / 定位 / 修复方案 / 审计 / 闸门 → Markdown + manifest"

    def handlers(self) -> Dict[str, Dict[str, Any]]:
        from ...integrations.game_server_bugfix import build_bugfix_delivery

        return {"report": {"build": lambda p, c: build_bugfix_delivery(p or {}, c)}}