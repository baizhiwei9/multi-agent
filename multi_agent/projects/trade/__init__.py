"""交易域（trade）：新闻 → 调权重 → 评估 → 传导（本分支只带这条链）。

    news_collector   collector/scan   继承 `frameworks/news` 的关键词采集框架
    news_assessor    assess/classify  规则表判"是否大事、往哪边压"
    transmit         transmit/map     事件压力 → 产业链环节 → 标的压力分 / 营收情景

三个都是"一角色一项目"：一个 capability + 一组 action + 一份 handlers()。
业务侧（期货平衡表、A 股日报）在各自仓库里继承这些能力，不复制实现。
"""
from __future__ import annotations

from .news_assessor import NewsAssessProject
from .news_collector import NewsCollectProject
from .transmit import TransmitProject

__all__ = ["NewsCollectProject", "NewsAssessProject", "TransmitProject"]