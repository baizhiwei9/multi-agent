"""bug 采集项目：只干一件事——把日志里的报错变成去重后的 bug 列表。

它不知道谁是协调官、谁负责修，也不知道后面有没有审计；它只对 Hub 报一个
capability `bug` + action `collect`。派单是编排的事。
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from ..base import RoleProject
from ...frameworks.netdev.collectors import (BugCollector, LogBugCollector,
                                             NullBugCollector)

_FIXTURE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                                        "examples", "game_server", "fixture"))


class BugCollectProject(RoleProject):
    name = "bug-collector"
    title = "bug 采集官"
    description = "服务器日志 / JSONL 崩溃上报 → 去重后的 bug 列表（不编造、不判根因）"
    capability = "bug"
    actions = ("collect",)
    role_description = "bug 采集官：日志 → bug 列表（取不到就 degraded，不编造）"
    default_params = {"log_path": os.path.join(_FIXTURE, "logs", "server.log")}

    def __init__(self, collector: Optional[BugCollector] = None,
                 log_path: Optional[str] = None) -> None:
        super().__init__()
        if collector is not None:
            self._collector = collector
        elif log_path or self.default_params.get("log_path"):
            self._collector = LogBugCollector(
                path=log_path or self.default_params["log_path"])
        else:
            self._collector = NullBugCollector()

    def collector(self) -> BugCollector:
        return self._collector

    def handlers(self) -> Dict[str, Dict[str, Any]]:
        return {"bug": {"collect": lambda p, c: self._collector.collect(p or {})}}