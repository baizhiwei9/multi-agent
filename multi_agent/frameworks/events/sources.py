"""多源事件爬虫：国家统计局 / 东财 / 新浪 / 腾讯 / CNBC / MarketWatch。

设计原则
--------
- **每个源一个函数**，签名统一：`fetch_x(day, keywords, pages=...) -> list[dict]`；
- 返回统一形状：`{title, content, published, url, source, region, priority}`；
- 单源失败只记 `meta.errors`，不影响其他源；
- **只收 `published` 落在 day 当天的条目**，不拿“最近”冒充“当天”；
- 源优先级：官方（统计局）> 东财 > 新浪 > 腾讯 > 海外（CNBC/MarketWatch）。

海外源说明
----------
美联储/BLS 官网在这个环境被拦（403/连接重置），所以美国侧用 CNBC 与
MarketWatch 的公开 RSS；RSS 只覆盖“最近”，回看历史日期会返回 0 条，
这是已知边界，不是编造。
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Optional

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120 Safari/537.36"}
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
CST = timezone(timedelta(hours=8))


def _clean(text: str) -> str:
    text = _TAG_RE.sub("", text or "")
    text = _WS_RE.sub(" ", text)
    return " ".join(x.strip() for x in text.split("\n") if x.strip())


def _get(url: str, timeout: float = 25,
         headers: Optional[Dict[str, str]] = None) -> str:
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _day_of(published: str) -> str:
    """统一成北京时间的 YYYY-MM-DD；认 ISO / 'YYYY-MM-DD HH:MM:SS' / RFC822。"""
    text = str(published or "").strip()
    if not text:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CST).date().isoformat()
    except (TypeError, ValueError):
        return ""


def _prio(value: Any) -> int:
    """priority=0 是合法值，不能用 `or 9`（0 or 9 == 9）。"""
    return int(value) if value is not None else 9


def _item(title: str, content: str, published: str, url: str,
          source: str, priority: int, region: str = "cn") -> Dict[str, Any]:
    return {"title": _clean(title), "content": _clean(content),
            "published": str(published or ""), "url": str(url or ""),
            "source": source, "priority": int(priority), "region": region}


# ───────────────────────────────────────────── 1. 国家统计局（宏观，官方）
def fetch_stats(day: str, keywords: Iterable[str] = (), pages: int = 1,
                timeout: float = 25) -> List[Dict[str, Any]]:
    """统计局“最新发布”列表：标题即事实，日期从 t20261127 里取。"""
    html = _get("https://www.stats.gov.cn/sj/zxfb/", timeout=timeout)
    pat = re.compile(
        r'<a[^>]+href="(\./\d{6}/t(\d{8})_\d+\.html)"[^>]*>(.*?)</a>', re.S)
    out: List[Dict[str, Any]] = []
    seen = set()
    for href, ymd, title in pat.findall(html):
        if len(ymd) != 8:
            continue
        pub = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
        if pub != day:
            continue
        name = _clean(title)
        if len(name) < 6 or name in seen:
            continue
        seen.add(name)
        out.append(_item(name, "", pub,
                         "https://www.stats.gov.cn/sj/zxfb/" + href[2:],
                         "国家统计局", priority=0))
    return out


# ───────────────────────────────────────────── 2. 东方财富（关键词搜索）
def fetch_eastmoney(day: str, keywords: Iterable[str], pages: int = 2,
                    size: int = 50, timeout: float = 25) -> List[Dict[str, Any]]:
    api = "https://search-api-web.eastmoney.com/search/jsonp"

    def _one(kw: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for page in range(1, max(1, pages) + 1):
            param = {
                "uid": "", "keyword": kw, "type": ["cmsArticleWebOld"],
                "client": "web", "clientType": "web", "clientVersion": "curr",
                "param": {"cmsArticleWebOld": {
                    "searchScope": "default", "sort": "time",
                    "pageIndex": page, "pageSize": max(1, min(size, 100)),
                    "preTag": "", "postTag": "",
                }},
            }
            url = api + "?cb=cb&param=" + urllib.parse.quote(
                json.dumps(param, ensure_ascii=False))
            raw = _get(url, timeout=timeout,
                       headers={"Referer": "https://so.eastmoney.com/"})
            try:
                payload = json.loads(raw[raw.find("(") + 1:raw.rfind(")")])
            except ValueError:
                break
            rows = ((payload.get("result") or {}).get("cmsArticleWebOld") or [])
            if not rows:
                break
            last_day = ""
            for row in rows:
                published = str(row.get("date") or "").strip()
                last_day = published[:10]
                if last_day != day:
                    continue
                out.append(_item(str(row.get("title") or ""),
                                 str(row.get("content") or ""),
                                 published, str(row.get("url") or ""),
                                 str(row.get("media") or "东方财富"),
                                 priority=1))
            if last_day and last_day < day:
                break
        return out

    keywords = [str(k).strip() for k in keywords if str(k).strip()]
    if not keywords:
        return []
    with ThreadPoolExecutor(max_workers=4) as pool:
        chunks = list(pool.map(_one, keywords))
    return [x for chunk in chunks for x in chunk]


# ───────────────────────────────────────────── 3. 新浪财经 7x24
def fetch_sina(day: str, keywords: Iterable[str] = (), pages: int = 4,
               size: int = 100, timeout: float = 25) -> List[Dict[str, Any]]:
    base = ("https://zhibo.sina.com.cn/api/zhibo/feed"
            "?zhibo_id=152&tag_id=0&dire=f&dpc=1&page={page}&page_size={size}")
    out: List[Dict[str, Any]] = []
    # 新浪 7x24 一页约 100 条；回看昨天至少要 5 页，别被调用方的 pages=2 卡住
    for page in range(1, max(5, int(pages)) + 1):
        raw = _get(base.format(page=page, size=min(size, 100)), timeout=timeout)
        try:
            data = json.loads(raw)
        except ValueError:
            break
        feed = (((data or {}).get("result") or {}).get("data") or {}).get("feed") or {}
        rows = feed.get("list") or []
        if not rows:
            break
        for row in rows:
            created = str(row.get("create_time") or "").strip()
            if created[:10] != day:
                continue
            text = _clean(str(row.get("rich_text") or ""))
            if not text:
                continue
            title, body = text, ""
            if text.startswith("【") and "】" in text:
                head, rest = text.split("】", 1)
                title, body = head + "】", rest.strip()
            out.append(_item(title[:160], body or text, created,
                             str(row.get("docurl") or ""),
                             "新浪财经7x24", priority=2))
    return out


# ───────────────────────────────────────────── 4. 腾讯财经（JSON-LD 列表）
def fetch_tencent(day: str, keywords: Iterable[str] = (), pages: int = 1,
                  timeout: float = 25) -> List[Dict[str, Any]]:
    html = _get("https://new.qq.com/ch/finance/", timeout=timeout)
    out: List[Dict[str, Any]] = []
    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                         html, re.S):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        entity = data.get("mainEntity") or {}
        for row in entity.get("itemListElement") or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "")
            url = str(row.get("url") or "")
            published = str((row.get("item") or {}).get("datePublished") or "")
            if _day_of(published) != day:
                continue
            out.append(_item(name, name, published, url, "腾讯财经", priority=3))
    return out


# ───────────────────────────────────────────── 5. 海外 RSS（CNBC / MarketWatch）
def _fetch_rss(url: str, source: str, day: str, priority: int = 5,
               timeout: float = 25) -> List[Dict[str, Any]]:
    raw = _get(url, timeout=timeout)
    out: List[Dict[str, Any]] = []
    for block in re.findall(r"<item>(.*?)</item>", raw, re.S):
        def pick(tag: str) -> str:
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.S)
            return _clean(m.group(1)) if m else ""
        published = pick("pubDate")
        if _day_of(published) != day:
            continue
        out.append(_item(pick("title"), pick("description"), published,
                         pick("link"), source, priority=priority, region="us"))
    return out


def fetch_cnbc(day: str, keywords: Iterable[str] = (), pages: int = 1,
               timeout: float = 25) -> List[Dict[str, Any]]:
    return _fetch_rss("https://www.cnbc.com/id/100003114/device/rss/rss.html",
                      "CNBC", day, timeout=timeout)


def fetch_marketwatch(day: str, keywords: Iterable[str] = (), pages: int = 1,
                      timeout: float = 25) -> List[Dict[str, Any]]:
    return _fetch_rss("https://www.marketwatch.com/rss/topstories",
                      "MarketWatch", day, timeout=timeout)


# ───────────────────────────────────────────── 6. 汇总
#: 源顺序即优先级（数字越小越优先）
SOURCES = (
    ("stats", fetch_stats),
    ("eastmoney", fetch_eastmoney),
    ("sina", fetch_sina),
    ("tencent", fetch_tencent),
    ("cnbc", fetch_cnbc),
    ("marketwatch", fetch_marketwatch),
)


def fetch_all(day: str, keywords: Iterable[str], pages: int = 2,
              timeout: float = 25) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """跑所有源；单源失败只记 errors。返回 (items, meta)。"""
    keywords = [str(k) for k in keywords if str(k).strip()]
    items: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {"sources": {}, "errors": [], "day": day}

    def _one(name: str):
        if name == "eastmoney" and not keywords:
            return name, [], None
        try:
            return name, fn_map[name](day, keywords, pages=pages, timeout=timeout), None
        except Exception as exc:        # noqa: BLE001 - 单源失败不影响其他
            return name, [], f"{name}: {type(exc).__name__}: {exc}"

    fn_map = dict(SOURCES)
    with ThreadPoolExecutor(max_workers=6) as pool:
        for name, got, err in pool.map(_one, [n for n, _ in SOURCES]):
            meta["sources"][name] = len(got)
            items.extend(got)
            if err:
                meta["errors"].append(err)

    # 跨源去重：标题前 60 字；同标题留优先级最高的那条
    dedup: Dict[str, Dict[str, Any]] = {}
    for item in items:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        key = re.sub(r"\s+", "", title)[:60]
        old = dedup.get(key)
        if old is None or _prio(item.get("priority")) < _prio(old.get("priority")):
            dedup[key] = item
    merged = sorted(dedup.values(),
                    key=lambda x: (_prio(x.get("priority")),
                                   str(x.get("published") or "")))
    meta["fetched"] = len(items)
    meta["kept"] = len(merged)
    return merged, meta
