"""分诊项目：只干一件事——把 bug 按接口聚成可派单的簇。

零 token。它不定位、不改代码，只回答"这一批 bug 该分成几个活、先干哪个"。
"""
from __future__ import annotations

from typing import Any, Dict

from ..base import RoleProject


class BugTriageProject(RoleProject):
    name = "bug-triage"
    title = "bug 分诊官"
    description = "按接口把去重后的 bug 聚成簇、排优先级（零 token）"
    capability = "triage"
    actions = ("classify",)
    role_description = "分诊：按接口聚簇，严重度×出现次数排序（零 token）"

    def handlers(self) -> Dict[str, Dict[str, Any]]:
        from ...integrations.game_server_bugfix import triage

        return {"triage": {"classify": lambda p, c: triage(p or {}, c)}}