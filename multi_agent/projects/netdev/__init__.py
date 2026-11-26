"""互联网开发域（netdev）：日志 → bug → 分诊 → 定位 → 补丁 → 审计 → 报告。

    bug_collector    bug/collect       继承 `frameworks/netdev` 的关键词采集框架
    bug_triage       triage/classify   按接口聚簇（零 token）
    bugfix_dev       dev/locate|propose|implement
    bugfix_reporter  report/build
    game_server      game_server_release（发布链聚合示例）

"游戏服务端"只是这个域的一个例子；同一条链也能接 Web 服务、App 后端、任何有日志的系统，
所以域名叫 netdev 而不是 game。
"""
from __future__ import annotations

from .bug_collector import BugCollectProject
from .bug_triage import BugTriageProject
from .bugfix_dev import BugfixDevProject
from .bugfix_reporter import BugfixReportProject
from .game_server import GAME_SERVER_PIPELINE, GameServerProject

__all__ = ["BugCollectProject", "BugTriageProject", "BugfixDevProject",
           "BugfixReportProject", "GameServerProject", "GAME_SERVER_PIPELINE"]