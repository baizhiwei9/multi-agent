"""交易域（trade）：消息面 → 评估 → 平衡表 / 日报。

    news_collector   collector/scan   继承 `frameworks/news` 的关键词采集框架
    news_assessor    assess/classify  规则表判"是否大事、往哪边压"
    balance_sheet    balance/build    平衡表（真实计算在 BalanceAI）
    futures_balance  BalanceAI 聚合示例（A/B/T/F/Q/C/D/E/J）
    stock_matt       股票日报聚合示例（M1..M5 + 新闻采集官）

前三个是"一角色一项目"，后两个是"业务聚合项目"，给 demo 用。
"""
from __future__ import annotations

from .balance_sheet import BalanceSheetProject
from .futures_balance import BALANCE_PIPELINE, FuturesBalanceProject
from .news_assessor import NewsAssessProject
from .news_collector import NewsCollectProject
from .stock_matt import MATT_PIPELINE, StockMattProject

__all__ = ["NewsCollectProject", "NewsAssessProject", "BalanceSheetProject",
           "FuturesBalanceProject", "StockMattProject",
           "BALANCE_PIPELINE", "MATT_PIPELINE"]