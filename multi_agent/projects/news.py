"""新闻采集官接口：所有项目的"消息面"都实现同一份契约。

为什么要抽象
------------
期货要的是"供应链硬事件"（矿难/停产/配额，带数量级），股票要的是"公告/政策/业绩预告"，
两边规则完全不同，但**对流水线的形状是一样的**：

    collect(params) -> {"events": [...], "scanned": int, "degraded": bool, ...}

流水线只认这个形状，所以新闻源可以换：DataHub 的 `futures_news`、MATT 的正则规则、
你自己的爬虫，都只要继承 `NewsCollector`。

三条硬规矩
----------
1. **不编造**：取不到就 `degraded=True` + `reason`，`events` 留空，不许造事件；
2. **不判方向**：采集官只说"发生了什么"，涨跌/影响留给测算与审计；
3. **去重可复现**：同一条新闻同一 event_id 只出一次（由实现自己保证并回传 `deduped`）。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Dict, Iterable, List, Optional


class NewsCollector:
    """新闻采集官基类：`collect()` 是唯一必须实现的方法。"""

    name: str = "news"
    description: str = ""
    # 采集口径（写进报告，便于复现）
    source: str = ""

    def collect(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        raise NotImplementedError

    # ---------------- 给实现用的工具 ----------------
    @staticmethod
    def event_id(title: str, published: str = "") -> str:
        raw = f"{title}|{published}".encode("utf-8")
        return hashlib.sha1(raw).hexdigest()[:16]

    @staticmethod
    def cap_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按 `news.max_items_per_collector` 截断（以前这个限额是死键，没人读）。"""
        from ..limits import load_limits

        limit = int(load_limits().news.max_items_per_collector)
        return events[:limit]

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "source": self.source,
                "description": self.description}


class NullNewsCollector(NewsCollector):
    """没有新闻源时的明确占位：不假装成功，也不阻断流水线。"""

    name = "none"
    description = "未配置新闻源（返回空事件 + degraded）"

    def collect(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {"events": [], "scanned": 0, "deduped": 0,
                "collector": self.name, "degraded": True,
                "reason": "本项目未配置新闻采集官（news() 返回 None）"}


class DataHubNewsCollector(NewsCollector):
    """期货：经 DataHub 的 `futures_news` 取多站点新闻池。

    取数在 DataHub（多线程、去重都在那边），这里只做**分流与形状归一**。
    调用方注入 `fetch(hours) -> list[dict]`（一般是 DataHubClient 的薄包装），
    没注入就返回 degraded —— 不猜、不造。
    """

    name = "datahub-futures-news"
    description = "DataHub futures_news：多站点新闻池 → 品种分流"
    source = "datahub:futures_news"

    def __init__(self, fetch: Optional[Callable[[float], List[Dict[str, Any]]]] = None,
                 commodities: Optional[Iterable[str]] = None) -> None:
        self._fetch = fetch
        self.commodities = [c for c in (commodities or [])]

    def collect(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        p = params or {}
        hours = float(p.get("hours") or 24)
        if self._fetch is None:
            return {"events": [], "scanned": 0, "deduped": 0,
                    "collector": self.name, "degraded": True,
                    "reason": "未注入 DataHub 取数函数（fetch=...）；"
                              "在有 DataHub 的机器上注册 DataHubNewsCollector(fetch=...) 即可"}

        raw = list(self._fetch(hours))
        wanted = [c for c in (p.get("commodities") or self.commodities)]
        events, seen = [], set()
        for item in raw:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            if wanted and not any(c in title or c in str(item.get("summary") or "")
                                  for c in wanted):
                continue
            eid = self.event_id(title, str(item.get("published") or ""))
            if eid in seen:
                continue
            seen.add(eid)
            events.append({
                "event_id": eid,
                "title": title,
                "published": item.get("published"),
                "source": item.get("source") or item.get("site"),
                "url": item.get("url"),
                "summary": item.get("summary"),
                # 采集官不判方向，只标"未判定"
                "impact": None,
            })
        deduped = len(raw) - len(events)
        kept = self.cap_events(events)
        return {"events": kept, "scanned": len(raw), "deduped": deduped,
                "truncated": len(events) - len(kept),
                "collector": self.name, "degraded": False, "source": self.source}


class RuleNewsCollector(NewsCollector):
    """股票：按正则规则过滤（零 token，等价 MATT 的 `news_rules.yaml`）。

    `rules` 形如 `{"硬事件": ["回购", "中标"], "噪音": ["传闻"]}`：
    命中 `hard` 的留，命中 `noise` 且没命中 `hard` 的丢。
    """

    name = "rule-news"
    description = "正则规则过滤（零 token）：硬事件保留、纯噪音丢弃"
    source = "rules"

    def __init__(self, rules: Optional[Dict[str, List[str]]] = None,
                 fetch: Optional[Callable[[float], List[Dict[str, Any]]]] = None) -> None:
        self.rules = rules or {"hard": ["回购", "中标", "业绩预告", "减持", "立案",
                                        "重组", "分红", "增持", "股权激励"],
                               "noise": ["传闻", "据悉", "或", "有望"]}
        self._fetch = fetch
        self._compiled = {k: [re.compile(v) for v in vs] for k, vs in self.rules.items()}

    def collect(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        p = params or {}
        if self._fetch is None:
            return {"events": [], "scanned": 0, "deduped": 0,
                    "collector": self.name, "degraded": True,
                    "reason": "未注入取数函数（fetch=...）；规则已就绪，接上新闻源即可"}
        raw = list(self._fetch(float(p.get("hours") or 24)))
        events, seen = [], set()
        for item in raw:
            text = f"{item.get('title') or ''} {item.get('summary') or ''}"
            hard = [k for k, pats in self._compiled.items() if k != "noise"
                    if any(x.search(text) for x in pats)]
            noise = any(x.search(text) for x in self._compiled.get("noise", []))
            if not hard or (noise and not hard):
                continue
            eid = self.event_id(str(item.get("title") or ""), str(item.get("published") or ""))
            if eid in seen:
                continue
            seen.add(eid)
            events.append({"event_id": eid, "title": item.get("title"),
                           "published": item.get("published"),
                           "source": item.get("source"), "url": item.get("url"),
                           "tags": hard, "impact": None})
        deduped = len(raw) - len(events)
        kept = self.cap_events(events)
        return {"events": kept, "scanned": len(raw), "deduped": deduped,
                "truncated": len(events) - len(kept),
                "collector": self.name, "degraded": False, "source": self.source}


class StaticNewsCollector(NewsCollector):
    """测试/演示用：给定固定列表，仍走去重与形状归一。"""

    name = "static"
    description = "固定列表（测试/演示）"

    def __init__(self, items: Optional[List[Dict[str, Any]]] = None) -> None:
        self.items = list(items or [])

    def collect(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        seen, events = set(), []
        for item in self.items:
            eid = self.event_id(str(item.get("title") or ""), str(item.get("published") or ""))
            if eid in seen:
                continue
            seen.add(eid)
            # 契约：impact 字段恒在（采集官不判方向），event_id 以去重结果为准
            events.append({"impact": None, **item, "event_id": eid})
        kept = self.cap_events(events)
        return {"events": kept, "scanned": len(self.items),
                "deduped": len(self.items) - len(events),
                "truncated": len(events) - len(kept),
                "collector": self.name, "degraded": False}


def as_json(result: Dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False)