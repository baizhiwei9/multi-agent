"""采集框架：**关键词驱动的"挑行 → 分组 → 上报"**，按域分两个。

    news    新闻采集框架（交易域：期货 / 股票 / 任何消息面）
    netdev  互联网开发采集框架（服务端日志 / 崩溃 / 超时）

两者共用 `keywords.py`：加一行关键词，就多一条触发；命中行带 `target`，
告诉 coordinator"这条该交给谁"。判定两条路径（`mode`）：程序规则（0 token）
或大模型复核（`llm/chat`）。
"""
from __future__ import annotations

from .keywords import KeywordGroup, KeywordSet, judge_via_llm

__all__ = ["KeywordGroup", "KeywordSet", "judge_via_llm"]