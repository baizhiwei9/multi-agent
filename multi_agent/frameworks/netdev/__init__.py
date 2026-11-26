"""互联网开发采集框架：服务端日志 / 崩溃 / 超时 → bug 行 → 上报。

    from multi_agent.frameworks.netdev import BugWatchFramework, examples

    BugWatchFramework()                         # 用 config/keywords/netdev.json
    BugWatchFramework(examples.CRASH_WATCH)     # 只盯崩溃
    BugWatchFramework(examples.PERF_WATCH)      # 只盯超时，交风控

三个示例（`examples.py`）演示"关键词多少、怎么触发、调哪个"：
`ERROR_WATCH`（错误行 → dev/locate）、`CRASH_WATCH`（崩溃 → dev/locate）、
`PERF_WATCH`（超时/5xx → server_risk/capacity）。
"""
from __future__ import annotations

from . import examples
from .collectors import (BugCollector, LogBugCollector, NullBugCollector,
                         StaticBugCollector)
from .framework import BugWatchFramework

__all__ = ["BugCollector", "NullBugCollector", "StaticBugCollector",
           "LogBugCollector", "BugWatchFramework", "examples"]