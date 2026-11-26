"""新闻采集框架的实现：关键词表 + 取数 → 命中事件（带 group / target）。

一条新闻只有命中了关键词组才会成为事件。这就是用户说的
"在采集里添加关键词，过滤出来，往上传"——没命中的不往上送。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional

from ..keywords import KeywordSet, judge_via_llm
from .collectors import NewsCollector


class NewsWatchFramework(NewsCollector):
    """关键词驱动的新闻采集官。

    三个注入点（都可以缺，缺了明确 degraded，不编造）：
    · `keyword_set`：关键词表；缺省按名字去 `config/keywords/<name>.json` 找；
    · `fetch`：取数函数 `fetch(hours) -> list[dict]`；
    · `judge`：`mode="llm"` 的复核函数，缺省走 `llm/chat`；端点没配就不否决。
    """

    name = "news-watch"
    description = "关键词驱动：标题/摘要命中关键词才出事件，并给出该交给谁"
    source = "keywords"

    def __init__(self, keyword_set: Optional[KeywordSet] = None,
                 fetch: Optional[Callable[[float], List[Dict[str, Any]]]] = None,
                 judge: Optional[Callable[[Dict[str, Any]], bool]] = None,
                 keyword_name: str = "news") -> None:
        self.keywords = keyword_set if keyword_set is not None \
            else KeywordSet.load(keyword_name)
        self._fetch = fetch
        self._judge = judge
        self._judge_default = False

    def judge(self) -> Optional[Callable[[Dict[str, Any]], bool]]:
        if self._judge is not None:
            return self._judge
        if not self._judge_default:
            self._judge = judge_via_llm("你在筛商品/股票新闻。")
            self._judge_default = True
        return self._judge

    def collect(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        p = params or {}
        if self.keywords is None:
            return {"events": [], "scanned": 0, "matched": 0, "by_group": {},
                    "collector": self.name, "degraded": True,
                    "reason": "没找到关键词表（config/keywords/news.json）"}

        items = p.get("items")
        if items is None and self._fetch is not None:
            try:
                items = list(self._fetch(float(p.get("hours") or 24)))
            except Exception as e:  # noqa: BLE001 - 取数失败走 degraded
                return {"events": [], "scanned": 0, "matched": 0, "by_group": {},
                        "collector": self.name, "degraded": True,
                        "reason": f"取数失败：{type(e).__name__}: {e}"}
        if items is None:
            return {"events": [], "scanned": 0, "matched": 0, "by_group": {},
                    "collector": self.name, "degraded": True,
                    "reason": "未注入取数函数（fetch=...）或 params.items"}

        judge = self.judge()
        events: List[Dict[str, Any]] = []
        seen = set()
        by_group: Dict[str, int] = {}
        scanned = 0

        for item in items:
            if not isinstance(item, dict):
                continue
            scanned += 1
            title = str(item.get("title") or "").strip()
            summary = str(item.get("summary") or item.get("text") or "")
            text = f"{title} {summary}".strip()
            if not text:
                continue
            hits = self.keywords.match(text)
            for h in hits:
                if h["mode"] == "llm" and judge is not None:
                    try:
                        if not bool(judge({**h, "line": text})):
                            continue
                    except Exception:  # noqa: BLE001 - 判定失败按程序命中
                        pass
                eid = self.event_id(title, str(item.get("published") or ""))
                if (eid, h["group"]) in seen:
                    continue
                seen.add((eid, h["group"]))
                by_group[h["group"]] = by_group.get(h["group"], 0) + 1
                events.append({
                    "event_id": eid, "title": title,
                    "published": item.get("published"),
                    "source": item.get("source") or item.get("site"),
                    "url": item.get("url"), "summary": item.get("summary"),
                    "impact": None,           # 采集官不判方向
                    "line": text[:300],       # 过滤出来的那一行（原样保留）
                    "group": h["group"], "keyword": h["keyword"],
                    "level": h["level"], "target": dict(h["target"]),
                })

        kept = self.cap_events(events)
        return {"events": kept, "scanned": scanned, "matched": len(events),
                "by_group": by_group, "truncated": len(events) - len(kept),
                "collector": self.name, "degraded": False, "reason": "",
                "source": self.source, "keyword_set": self.keywords.name}