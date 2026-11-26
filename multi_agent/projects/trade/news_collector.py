"""新闻采集项目：只干一件事——把消息面变成去重后的 events。

项目名不叫"期货"也不叫"股票"：采集口径由注入的 `NewsCollector` 决定
（DataHub 新闻池 / 正则规则 / 你自己的爬虫），项目本身只声明 `collector/scan`。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..base import RoleProject
from ...frameworks.news.collectors import NewsCollector, NullNewsCollector


class NewsCollectProject(RoleProject):
    name = "news-collector"
    title = "新闻采集官"
    description = "消息面 → events（不编造、不判方向、去重可复现）"
    capability = "collector"
    actions = ("scan",)
    role_description = "新闻采集官：抓取 → 去重 → events；取不到就 degraded，不造事件"

    def __init__(self, collector: Optional[NewsCollector] = None) -> None:
        super().__init__()
        # 默认空采集官：明确"没配新闻源"，而不是假装成功
        self._collector = collector or NullNewsCollector()

    def collector(self) -> NewsCollector:
        return self._collector

    def handlers(self) -> Dict[str, Dict[str, Any]]:
        return {"collector": {"scan": lambda p, c: self._collector.collect(p or {})}}