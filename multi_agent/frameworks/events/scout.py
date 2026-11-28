"""事件侦察官（EventScout）：爬虫按关键词搜「这一天」的新闻 → 事件官 → 统一事件包。

和 EventOfficer 的分工
----------------------
EventOfficer  只做「日期规则 + 文本日期抽取」，不联网；
EventScout    负责联网：按关键词搜指定日期的新闻，做关键词命中、分类、去重、排序，
              再把结果交给 EventOfficer 补「将发生」的日历（MSCI/月末/期权到期/PMI）。

输出形状
--------
    {
      "date": "2026-11-27",
      "events":    [{date, event, category, kind, source, url, published,
                     summary, impact_hint, score, quality, keywords}],
      "scheduled": [{date, event, kind, basis, verify, source}],
      "digest":    "给辩论/报告用的事件摘要（Markdown）",
      "calendar":  [{date, event, note}],     # 直接喂 evidence.calendar
      "meta":      {queries, fetched, kept, errors, ...}
    }

三条硬规矩
----------
1. **只搜指定日期**：文章发布时间必须落在 `date` 当天；不拿“最近”冒充“当天”；
2. **不编造**：搜不到就 degraded + 原因，events 留空；
3. **方向只给 hint**：政策/流动性类给 bull/bear 提示，其余 neutral，最终判断仍归辩论席。

用法
----
    from multi_agent.frameworks.events.scout import EventScout

    out = EventScout(day="2026-11-27", keywords=["国常会", "A股", "MSCI"]).scan()
    out["calendar"]   # 喂给 detective_panel 的 evidence.calendar

命令行：

    python -m multi_agent.frameworks.events.scout \
        --date 2026-11-27 --keywords "国常会,资本市场,A股,MSCI,韩国央行" --horizon 5
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import sources
from .officer import EventOfficer, parse_day

_DEFAULT_RULES = str(Path(__file__).resolve().parents[3] / "config/events/rules.json")

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120 Safari/537.36",
      "Referer": "https://so.eastmoney.com/"}

SEARCH_API = "https://search-api-web.eastmoney.com/search/jsonp"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")

#: 事件分类：第一个命中的规则生效；顺序即优先级。
#: index 必须带“调仓/审议/生效/纳入/剔除”等动作词，避免把“MSCI 亚太指数涨 1%”误判成指数事件。
_CATEGORIES = (
    ("policy", ("国常会", "国务院常务会议", "政治局会议", "中央经济工作会议",
                "证监会", "金融委", "国务院")),
    ("macro", ("统计局", "PMI", "CPI", "PPI", "社融", "工业企业利润",
               "GDP", "进出口", "海关", "非农")),
    ("index", ("指数审议", "半年度审议", "季度审议", "指数调整", "样本调整",
               "调仓", "纳入", "剔除", "生效")),
    ("derivatives", ("期权", "期货", "交割", "行权", "到期")),
    ("central_bank", ("韩国央行", "美联储", "欧洲央行", "日本央行", "加息",
                      "降息", "降准", "MLF", "逆回购")),
    ("flow", ("两融", "ETF", "北向", "南向", "回购", "增持", "分红",
              "国家队", "汇金", "平准基金", "净买入")),
    ("market", ("涨停", "跌停", "成交额", "A股", "港股", "指数")),
    ("company", ("业绩预告", "重组", "减持", "立案", "退市", "IPO")),
)

#: 方向提示只覆盖最明确的词；不确定一律 neutral
_BEAR_WORDS = ("加息", "收紧", "制裁", "关税", "禁运", "下调评级", "违约")
_BULL_WORDS = ("降息", "降准", "稳市", "国家队", "平准基金", "净买入",
               "增持", "回购", "注入流动性", "支持入市")

#: 排序用的“像不像一条真事件”
_ACTION_WORDS = ("召开", "研究", "部署", "发布", "公布", "加息", "降息",
                 "上市", "挂牌", "生效", "调整", "纳入", "剔除", "回购",
                 "增持", "签约", "定调", "出手", "回应", "发声")
_COMMENTARY_WORDS = ("解读", "点评", "首席", "券商", "机构", "基金", "研报",
                     "如何看", "后市", "火速", "重磅发声", "展望")

#: 明显不是“事件”的稿子：ESG 评级、指数涨跌、盘前/早报、时评、商品短线
_NOISE_WORDS = ("ESG", "评级", "指数上涨", "指数涨", "主力合约", "短线拉升",
                "早报", "晚报", "早知道", "盘前必读", "时评", "十大券商",
                "研报", "点评", "解读", "图解", "快讯", "周报", "最新数据",
                "为何", "如何", "展望")


def _clean(text: str) -> str:
    text = _TAG_RE.sub("", text or "")
    text = _WS_RE.sub(" ", text)
    return " ".join(line.strip() for line in text.split("\n") if line.strip())


_INDEX_ENTITIES = ("MSCI", "富时罗素", "标普道琼斯", "中证指数",
                   "上证指数", "科创50", "沪深300", "创业板指")
_INDEX_ACTIONS = ("审议", "调仓", "调整", "纳入成分", "剔除成分",
                  "权重", "生效", "样本")
_CB_ENTITIES = ("央行", "美联储", "韩国央行", "欧洲央行", "日本央行", "人民银行")


def _category(text: str) -> str:
    """分类要看“实体 + 动作”，不能因为文中顺带出现“加息/纳入”就归类。"""
    if any(w in text for w in ("国常会", "国务院常务会议", "政治局会议",
                               "中央经济工作会议", "证监会", "金融委", "国务院")):
        return "policy"
    if (any(e in text for e in _INDEX_ENTITIES)
            and any(a in text for a in _INDEX_ACTIONS)):
        return "index"
    if any(e in text for e in _CB_ENTITIES):
        return "central_bank"
    for name, words in _CATEGORIES:
        if any(w in text for w in words):
            return name
    return "other"


def _hint(text: str) -> str:
    if any(w in text for w in _BEAR_WORDS):
        return "bear"
    if any(w in text for w in _BULL_WORDS):
        return "bull"
    return "neutral"


def _quality(title: str, published: str, base: int) -> int:
    """真事件 > 解读稿；早盘/盘中发布的“动作”优先。"""
    q = int(base)
    q += 2 * sum(1 for w in _ACTION_WORDS if w in title)
    q -= 4 * sum(1 for w in _COMMENTARY_WORDS if w in title)
    try:
        hour = int(str(published)[11:13])
        if hour < 15:
            q += 1
    except (ValueError, IndexError):
        pass
    return q


_ENTITIES = ("国常会", "证监会", "工业企业利润", "规模以上工业", "工业利润",
             "韩国央行", "美联储", "MSCI", "PMI", "氢氧化锂", "四大行",
             "科创50", "中央经济工作会议")


def _event_key(fact: str, category: str) -> str:
    """同一事件的多篇报道合并：实体 + 关键数字。"""
    ents: List[str] = []
    # 工业利润有几种写法，先归一到同一个实体，避免“规模以上”把同一条新闻拆开
    if any(w in fact for w in ("工业企业利润", "规模以上工业", "工业利润")):
        ents.append("工业企业利润")
    for e in _ENTITIES:
        if e in fact and e not in ents and e != "规模以上工业":
            ents.append(e)
    nums = re.findall(r"\d+(?:\.\d+)?%", fact)[:1]
    if not nums:
        nums = re.findall(r"\d+(?:\.\d+)?", fact)[:1]
    if ents or nums:
        return f"{category}|{'/'.join(ents)}|{'/'.join(nums)}"
    return f"{category}|" + re.sub(r"\s+", "", fact)[:50]


def _snippet(text: str, kw: str, limit: int = 220) -> str:
    """取“包含关键词的完整一句”：从上一个句号/分号起，到下一个句号/分号止。"""
    low, key = (text or "").lower(), (kw or "").lower()
    i = low.find(key)
    if i < 0:
        return (text or "")[:limit]
    delims = "。；！？\n"
    start = 0
    for d in delims:
        pos = text.rfind(d, 0, i)
        if pos + 1 > start:
            start = pos + 1
    end = len(text)
    for d in delims:
        pos = text.find(d, i + len(key))
        if pos != -1 and pos + 1 < end:
            end = pos + 1
    return text[start:end].strip()[:limit]


def _fact(snippet: str, kw: str = "", limit: int = 150) -> str:
    """把句子清成一条事实：去掉【栏目】前缀和空白。"""
    s = (snippet or "").strip()
    s = re.sub(r"^【[^】]*】\s*", "", s)
    s = s.strip("　 ·…")
    return s[:limit]


def _snippet_score(s: str, kw: str) -> int:
    """挑“像事实”的句子：带数字、带动作、不是观点。"""
    score = 0
    if kw.lower() in s.lower():
        score += 3
    if re.search(r"\d", s or ""):
        score += 2
    if any(w in s for w in ("宣布", "公布", "发布", "上调", "下调", "生效",
                            "实施", "签订", "同比", "环比", "增至", "降至",
                            "超过", "不足", "首次")):
        score += 2
    if any(w in s for w in ("表示", "认为", "指出", "研报", "评级", "预计")):
        score -= 1
    return score


def crawl_day(day: str, keywords: Iterable[str], pages: int = 2,
              errors: Optional[List[str]] = None
              ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """多源抓 day 当天新闻（统计局/东财/新浪/腾讯/CNBC/MarketWatch）。

    源层只负责“抓 + 日期过滤 + 去重”；关键词命中、附近事实句、分类都在这里做。
    """
    keywords = [str(k).strip() for k in keywords if str(k).strip()]
    rows, meta = sources.fetch_all(day, keywords, pages=pages)
    if errors is not None:
        errors.extend(meta.get("errors") or [])
    kept: List[Dict[str, Any]] = []
    seen: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        text = f"{row.get('title') or ''} {row.get('content') or ''}"
        hits = [k for k in keywords if k.lower() in text.lower()]
        if not hits:
            continue
        key = re.sub(r"\s+", "", str(row.get("title") or ""))[:60]
        snippets = [_snippet(text, k) for k in hits]
        if key and key in seen:
            item = seen[key]
            item["hits"].extend([h for h in hits if h not in item["hits"]])
            item["snippets"].extend(snippets)
        else:
            item = dict(row)
            item["hits"] = hits
            item["snippets"] = snippets
            if key:
                seen[key] = item
            kept.append(item)
    return kept, meta


# ───────────────────────────────────────────── LLM 兜底（最后一步，不抢爬虫）
_LLM_SYSTEM = ("你是事件官，只负责把给定新闻标题里“明确写了日期”的预定事件抽出来。"
               "没有日期、没有可核验来源的一律不要。只输出 JSON 数组。")


def llm_infer(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """LLM 兜底：爬虫 + 规则都补不齐未来日历时才调用；失败返回 []。"""
    import os
    import urllib.request as _req

    token = os.environ.get("LLM_TOKEN") or os.environ.get("AUDIT_LLM_TOKEN") or ""
    if not token:
        return []
    url = os.environ.get("LLM_URL") or "https://api.deepseek.com/v1/chat/completions"
    model = os.environ.get("LLM_MODEL") or "deepseek-chat"
    prompt = json.dumps(payload, ensure_ascii=False)
    body = json.dumps({
        "model": model, "stream": False,
        "messages": [{"role": "system", "content": _LLM_SYSTEM},
                     {"role": "user", "content": prompt}],
    }, ensure_ascii=False).encode("utf-8")
    req = _req.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    })
    try:
        with _req.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        text = str((((data.get("choices") or [{}])[0].get("message") or {})
                    .get("content") or ""))
        m = re.search(r"\[.*\]", text, re.S)
        parsed = json.loads(m.group(0) if m else text)
        return parsed if isinstance(parsed, list) else []
    except Exception:                   # noqa: BLE001 - 兜底失败就当没有
        return []


class EventScout:
    """事件侦察官：关键词 → 当日新闻 → 事件包 + 未来日历。"""

    name = "event-scout"
    description = "爬虫按关键词搜指定日期的新闻，交事件官补日历"

    def __init__(self, day: Any = None, keywords: Optional[Iterable[str]] = None,
                 rules_path: Optional[str] = None, use_llm: str = "auto") -> None:
        self.day = parse_day(day) if day else date.today()
        self.keywords = [str(k).strip() for k in (keywords or []) if str(k).strip()]
        self.rules_path = rules_path or _DEFAULT_RULES
        self.use_llm = str(use_llm or "auto").lower()

    # ---------- 1. 当日事件 ----------
    def _events(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        day_s = self.day.isoformat()
        events: List[Dict[str, Any]] = []
        seen_facts: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            hits = row.get("hits") or [k for k in self.keywords
                                       if k.lower() in
                                       f"{row['title']} {row['content']}".lower()]
            if not hits:
                continue
            snippets = row.get("snippets") or [row["content"][:150]]
            best = max(snippets,
                       key=lambda x: (_snippet_score(x, hits[0]), len(x)))
            fact = _fact(best, hits[0])
            if not fact or len(fact) < 8:
                continue
            if any(w in fact for w in _NOISE_WORDS):
                continue
            text = f"{row['title']} {fact}"
            cat = _category(text)
            score = 3 if any(k.lower() in row["title"].lower() for k in hits) else 0
            score += max(0, len(hits) - 1)
            if cat in ("policy", "index"):
                score += 2
            elif cat in ("macro", "central_bank", "flow"):
                score += 1
            item = {
                "date": day_s,
                "event": fact[:180],
                "title": row["title"],
                "category": cat,
                "kind": "fact",
                "source": row.get("source") or "",
                "url": row.get("url") or "",
                "published": row.get("published") or "",
                "summary": fact,
                "impact_hint": _hint(text),
                "score": score,
                "quality": _quality(fact, row.get("published") or "", score)
                           + (6 if row.get("source") == "国家统计局" else
                              2 if row.get("source") in ("新浪财经7x24", "腾讯财经") else 0),
                "priority": int(row.get("priority") if row.get("priority") is not None else 9),
                "keywords": hits,
            }
            # 同一事件（实体+数字）只留“源优先级高、质量高”的一条
            key = _event_key(fact, cat)
            old = seen_facts.get(key)
            if old is None or (item["priority"], -item["quality"]) < \
                    (old["priority"], -old["quality"]):
                seen_facts[key] = item
        events = list(seen_facts.values())
        events.sort(key=lambda x: (-int(x["quality"]), -int(x["score"])))
        return events

    # ---------- 2. 未来日历 ----------
    def _scheduled(self, rows: List[Dict[str, Any]], horizon: int,
                   use_llm: bool = False) -> Dict[str, Any]:
        officer = EventOfficer(today=self.day, horizon_days=horizon,
                               rules_path=self.rules_path)
        facts = [{"title": r["title"], "text": r["content"],
                  "source": r.get("source"), "url": r.get("url")} for r in rows]
        out = officer.scan(facts=facts, infer=llm_infer if use_llm else None)
        # 只留未来；当天的“预定”已经在 events 里；评论稿不算预定事件
        events = [e for e in out["events"]
                  if e["date"] > self.day.isoformat()
                  and not any(w in str(e.get("event") or "") for w in _NOISE_WORDS)]
        return {"events": events, "meta": out["meta"]}

    # ---------- 3. 摘要 ----------
    @staticmethod
    def _digest(events: List[Dict[str, Any]], scheduled: List[Dict[str, Any]],
                limit: int = 18) -> str:
        """按类别分组，每类最多 3 条；只给标题，不带可能串行的正文摘要。"""
        lines: List[str] = []
        if events:
            lines.append("### 当日事件（爬虫命中，按类别）")
            grouped: Dict[str, List[Dict[str, Any]]] = {}
            for e in events:
                grouped.setdefault(e["category"], []).append(e)
            order = ["policy", "macro", "central_bank", "index", "flow",
                     "derivatives", "market", "company", "other"]
            shown = 0
            for cat in order + [c for c in grouped if c not in order]:
                rows = grouped.get(cat) or []
                if not rows:
                    continue
                lines.append(f"【{cat}】")
                for e in rows[:3]:
                    if shown >= limit:
                        break
                    lines.append(f"- {e['event']}（{e['source']}，{e['published']}，"
                                 f"hint={e['impact_hint']}）")
                    shown += 1
        if scheduled:
            lines.append("")
            lines.append("### 未来预定事件（事件官规则/原文日期）")
            for e in scheduled:
                lines.append(f"- {e['date']} {e['event']}（{e.get('kind')}，"
                             f"{e.get('source')}）")
        return "\n".join(lines)

    def as_calendar(self, result: Dict[str, Any], limit: int = 8) -> List[Dict[str, Any]]:
        """转成 detective_panel 的 `evidence.calendar`。

        当日事件：按类别各取 1 条（同类不重复），最多 `limit` 条；
        未来预定事件全部保留。宁可少而准，不把 20 条噪音塞进 prompt。
        """
        picked: List[Dict[str, Any]] = []
        seen_cat: Dict[str, int] = {}
        for e in result.get("events", []):
            cat = e["category"]
            # 当日“行情类”噪音不进日历：指数涨跌/商品短线不是预定事件
            if cat in ("market", "index"):
                continue
            if seen_cat.get(cat):
                continue
            seen_cat[cat] = 1
            picked.append({"date": e["date"], "event": e["event"],
                           "note": f"{cat}｜hint={e['impact_hint']}｜"
                                   f"{e['source']}｜{e['published']}"})
            if len(picked) >= limit:
                break
        for e in result.get("scheduled", []):
            picked.append({"date": e["date"], "event": e["event"],
                           "note": f"{e.get('basis') or ''}｜{e.get('verify') or ''}"[:200]})
        return picked

    # ---------- 4. 主流程 ----------
    def scan(self, horizon_days: int = 5, pages: int = 2,
             digest_limit: int = 18) -> Dict[str, Any]:
        errors: List[str] = []
        if not self.keywords:
            return {"date": self.day.isoformat(), "events": [], "scheduled": [],
                    "digest": "", "calendar": [], "degraded": True,
                    "meta": {"date": self.day.isoformat(), "keywords": [],
                             "errors": ["未给关键词（keywords）"], "fetched": 0,
                             "kept": 0, "horizon_days": horizon_days}}
        rows, src_meta = crawl_day(self.day.isoformat(), self.keywords, pages=pages,
                                   errors=errors)
        events = self._events(rows)
        sched = self._scheduled(rows, horizon_days, use_llm=False) if rows else \
            {"events": [], "meta": {}}
        need_llm = (self.use_llm == "always"
                    or (self.use_llm == "auto"
                        and (len(events) < 5 or not sched.get("events"))))
        if rows and need_llm:
            sched_llm = self._scheduled(rows, horizon_days, use_llm=True)
            if sched_llm.get("events"):
                sched = sched_llm
        result = {
            "date": self.day.isoformat(),
            "events": events,
            "scheduled": sched["events"],
            "degraded": not events,
            "meta": {
                "date": self.day.isoformat(),
                "keywords": self.keywords,
                "horizon_days": horizon_days,
                "fetched": len(rows),
                "kept": len(events),
                "errors": errors + (sched.get("meta", {}).get("errors") or []),
                "llm_used": bool(rows and need_llm),
                "sources": src_meta.get("sources") or {},
                "fetched_raw": src_meta.get("fetched", len(rows)),
                "note": "多源抓取；events 只含当天；scheduled 含未来预定事件",
            },
        }
        result["digest"] = self._digest(events, result["scheduled"], digest_limit)
        result["calendar"] = self.as_calendar(result)
        return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="事件侦察官：关键词搜当日新闻 + 事件官日历")
    ap.add_argument("--date", required=True, help="目标日期 YYYY-MM-DD")
    ap.add_argument("--keywords", required=True,
                    help="逗号/顿号/空格分隔，如：国常会,资本市场,A股,MSCI")
    ap.add_argument("--horizon", type=int, default=5, help="未来日历看几天，默认 5")
    ap.add_argument("--pages", type=int, default=2, help="每个关键词搜几页，默认 2")
    ap.add_argument("--llm", default="auto", choices=("auto", "always", "never"),
                    help="大模型兜底：auto=爬虫/规则不足才用，always/never")
    ap.add_argument("--out", default=None, help="把 JSON 写到文件（默认打印）")
    args = ap.parse_args(argv)

    keywords = [x for x in re.split(r"[,，、;；\s]+", args.keywords) if x]
    result = EventScout(day=args.date, keywords=keywords,
                        use_llm=args.llm).scan(
        horizon_days=args.horizon, pages=args.pages)
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"已写入 {args.out}：{len(result['events'])} 条当日事件 / "
              f"{len(result['scheduled'])} 条未来事件", file=sys.stderr)
    else:
        print(text)
    if result["digest"]:
        print(result["digest"], file=sys.stderr)
    return 0 if result["events"] else 1


if __name__ == "__main__":
    sys.exit(main())
