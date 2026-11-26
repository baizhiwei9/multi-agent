"""新闻采集框架：任何"消息面 → 事件"的业务都继承它。

    from multi_agent.frameworks.news import NewsWatchFramework

    fw = NewsWatchFramework()                    # 用 config/keywords/news.json
    fw = NewsWatchFramework(keyword_set=my_set)  # 自己一套关键词
    fw.collect({"hours": 24})                    # → events（带 group / target）

和具体业务的边界
--------------
框架只管三件事：**取文本、按关键词挑、按 `target` 说交给谁**。
"这条新闻对铜是多是空"是评估角色（`assess`）的事；
"要不要重算平衡表"是编排（`projects/flows/news_balance.py`）的事。
所以 BalanceAI 继承它时只需要配关键词表与取数函数，不用改框架代码。
"""
from __future__ import annotations

from .collectors import (DataHubNewsCollector, NewsCollector, NullNewsCollector,
                         RuleNewsCollector, StaticNewsCollector)
from .framework import NewsWatchFramework

__all__ = ["NewsCollector", "NullNewsCollector", "DataHubNewsCollector",
           "RuleNewsCollector", "StaticNewsCollector", "NewsWatchFramework"]