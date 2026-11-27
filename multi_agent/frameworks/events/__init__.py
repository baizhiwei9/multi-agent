"""事件官：把「预定要发生的事」从日期规则里推出来，标 fact / inferred。

    from multi_agent.frameworks.events import EventOfficer

    EventOfficer(today="2026-11-28").scan(facts=news_items, horizon_days=7)
    # → {"events": [{date, event, kind, basis, verify, source}, ...], "meta": {...}}

和新闻官的分工
--------------
news   已发生：文本里有什么
events 将发生：指数调仓、期权到期、季末结算、发布冻结……
       这些在新闻流里通常一条都没有，得靠规则与日历。
"""
from __future__ import annotations

from .officer import (EventOfficer, from_facts, from_inference, from_rules,
                      last_business_day, load_rules, nth_weekday, parse_day)

__all__ = ["EventOfficer", "from_rules", "from_facts", "from_inference",
           "last_business_day", "nth_weekday", "parse_day", "load_rules"]