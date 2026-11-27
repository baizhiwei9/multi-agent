"""取数脚本：宏观 + 股票**新闻** → 一份 JSON，喂给 run_once --news-cmd。

    python examples/detective_panel/fetch_news.py --hours 30 --pages 3

输出（stdout，JSON）：

    {"items": [ {title, text, source, published_at, url}, ... ],
     "meta":  {"sources": {...}, "errors": [...], "count": N}}

只抓新闻，**不抓行情**。行情（点位、MA40、成交额、两融、PCR…）由你自己算好后，
以证据包 JSON 的形式传给 `--evidence`，两者互不掺和 —— 新闻里没有、也不该有行情。

数据源（全是公开接口，无需 Key）：

    · 新浪财经 7x24   zhibo.sina.com.cn（宏观 + 市场快讯，主力源，可翻页）
    · 央视新闻联播    akshare `news_cctv`（政策口径最硬的宏观通稿）

单源失败只记 meta.errors，不影响其他源；一条都拿不到才非零退出。

接商品新闻（可选）：`--extra-cmd "<你的命令>"`，命令 stdout 输出 JSON 列表即可，
公开仓不出现任何私有库名 —— 商品源留在你自己的仓里。
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120 Safari/537.36"}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def clean(text: str) -> str:
    """去 HTML 标签、压空白；不删正文。"""
    out = _TAG_RE.sub("", text or "")
    out = _WS_RE.sub(" ", out)
    return "\n".join(line.strip() for line in out.split("\n") if line.strip())


# ───────────────────────────────────────────── 1. 新浪 7x24
def fetch_sina(pages: int = 2, page_size: int = 100,
               hours: float = 24,
               since: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """新浪财经 7x24 快讯（公开接口，无需 Key）。按时间倒序翻页。

    `since` 给了就用绝对起点（指定日期时用），否则按 `hours` 从此刻倒推。
    """
    base = ("https://zhibo.sina.com.cn/api/zhibo/feed"
            "?zhibo_id=152&tag_id=0&dire=f&dpc=1"
            "&page={page}&page_size={size}")
    since = since or (datetime.now() - timedelta(hours=hours))
    items: List[Dict[str, Any]] = []
    seen = set()
    for page in range(1, max(1, pages) + 1):
        url = base.format(page=page, size=max(1, min(page_size, 100)))
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        feed = (((data or {}).get("result") or {}).get("data") or {}).get("feed") or {}
        rows = feed.get("list") or []
        if not rows:
            break
        for row in rows:
            text = clean(str(row.get("rich_text") or ""))
            if not text:
                continue
            created = str(row.get("create_time") or "").strip()
            try:
                ts = datetime.strptime(created, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                ts = None
            if ts is not None and ts < since:
                continue
            key = text[:40]
            if key in seen:
                continue
            seen.add(key)
            title, body = text, ""
            if text.startswith("【") and "】" in text:
                head, rest = text.split("】", 1)
                title, body = head + "】", rest.strip()
            items.append({
                "title": title[:120],
                "text": (body or text)[:600],
                "source": "新浪财经7x24",
                "published_at": created,
                "url": str(row.get("docurl") or ""),
            })
    return items


# ───────────────────────────────────────────── 2. 央视（宏观政策口径）
def fetch_cctv(days: int = 3,
               since: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """央视新闻联播文字稿（akshare news_cctv）。日期按天回溯，拿不到就跳过。

    给了 `since` 就只收不早于那天的通稿（"搜哪天"要能卡住，别把去年的带进来）。
    """
    try:
        import akshare as ak        # 延迟导入：没装 akshare 也不影响其他源
    except Exception as exc:        # noqa: BLE001
        raise RuntimeError(f"akshare 不可用：{type(exc).__name__}: {exc}") from exc

    floor = since.strftime("%Y%m%d") if since is not None else ""
    items: List[Dict[str, Any]] = []
    for offset in range(max(1, days)):
        day = (datetime.now() - timedelta(days=offset)).strftime("%Y%m%d")
        if floor and day < floor:
            continue
        try:
            df = ak.news_cctv(date=day)
        except Exception:           # noqa: BLE001 - 单日失败不影响其它天
            continue
        if df is None or getattr(df, "empty", True):
            continue
        for _, row in df.iterrows():
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            items.append({
                "title": title[:120],
                "text": clean(str(row.get("content") or ""))[:800],
                "source": "央视新闻联播",
                "published_at": str(row.get("date") or day),
                "url": "",
            })
    return items


# ───────────────────────────────────────────── 3. 外部追加源（商品等）
def fetch_extra(cmd: str) -> List[Dict[str, Any]]:
    """再跑一条命令，把它 stdout 的 JSON 列表并进来（私有源走这里）。"""
    proc = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"extra-cmd 退出码 {proc.returncode}：{(proc.stderr or '')[:120]}")
    data = json.loads(proc.stdout)
    if isinstance(data, dict):
        for key in ("items", "results", "data", "events"):
            if isinstance(data.get(key), list):
                return [x for x in data[key] if isinstance(x, dict)]
        return []
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


# ───────────────────────────────────────────── 主流程
def _parse_dt(text: str) -> Optional[datetime]:
    """认 'YYYY-MM-DD' 与 'YYYY-MM-DD HH:MM'；不认就返回 None（绝不猜）。"""
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def build(args: argparse.Namespace) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    errors: List[str] = []
    sources: Dict[str, int] = {}

    # 时间窗：给了 --since 就用绝对起点（"搜哪天"就靠它），否则按 --hours 倒推
    until = datetime.now()
    since: Optional[datetime] = None
    if getattr(args, "since", None):
        since = _parse_dt(args.since)
        if since is None:
            errors.append(f"--since 认不出：{args.since}（要 YYYY-MM-DD 或 YYYY-MM-DD HH:MM）")
    hours = ((until - since).total_seconds() / 3600.0) if since else float(args.hours)
    if hours <= 0:
        errors.append(f"时间窗不合法：since={since} 已在未来")
        hours = float(args.hours)

    if args.sina:
        try:
            got = fetch_sina(args.pages, args.page_size, hours, since=since)
            items.extend(got)
            sources["sina_7x24"] = len(got)
            if not got:
                errors.append("新浪7x24：返回 0 条（可能被限流或时间窗太窄）")
        except Exception as exc:    # noqa: BLE001
            errors.append(f"新浪7x24失败：{type(exc).__name__}: {exc}")

    if args.cctv:
        try:
            got = fetch_cctv(args.cctv_days)
            items.extend(got)
            sources["cctv"] = len(got)
        except Exception as exc:    # noqa: BLE001
            errors.append(f"央视失败：{type(exc).__name__}: {exc}")

    if args.extra_cmd:
        try:
            got = fetch_extra(args.extra_cmd)
            items.extend(got)
            sources["extra"] = len(got)
        except Exception as exc:    # noqa: BLE001
            errors.append(f"追加源失败：{type(exc).__name__}: {exc}")

    dedup: List[Dict[str, Any]] = []
    seen = set()
    for item in items:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        key = title[:60]
        if key in seen:
            continue
        seen.add(key)
        dedup.append(item)

    # 新浪快讯量大、央视/私有源量小但不能被挤掉：先保底收非快讯，再补快讯到 limit
    keep: List[Dict[str, Any]] = [x for x in dedup
                                  if x.get("source") != "新浪财经7x24"]
    if len(keep) < args.limit:
        picked = {id(x) for x in keep}
        for item in dedup:
            if len(keep) >= args.limit:
                break
            if id(item) not in picked:
                keep.append(item)

    win_from = since if since is not None else (until - timedelta(hours=float(args.hours)))
    return {"items": keep[: args.limit],
            "meta": {"sources": sources, "errors": errors,
                     "as_of": until.strftime("%Y-%m-%d %H:%M:%S"),
                     "window": {"from": win_from.strftime("%Y-%m-%d %H:%M"),
                                "to": until.strftime("%Y-%m-%d %H:%M"),
                                "hours": round(hours, 1),
                                "mode": (f"since:{args.since}" if since
                                         else f"last:{args.hours:g}h")},
                     "count": len(dedup)}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="宏观 + 股票新闻素材聚合（不含行情）")
    ap.add_argument("--hours", type=float, default=24, help="时间窗（小时，从此刻倒推）")
    ap.add_argument("--since", default=None,
                    help="绝对起点：YYYY-MM-DD 或 'YYYY-MM-DD HH:MM'（指定「搜哪天」）")
    ap.add_argument("--pages", type=int, default=2, help="新浪7x24 翻几页（每页≤100）")
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--limit", type=int, default=400,
                    help="最多输出多少条（央视/私有源先保底，再用新浪快讯补满）")
    ap.add_argument("--no-sina", dest="sina", action="store_false", help="不抓新浪7x24")
    ap.add_argument("--no-cctv", dest="cctv", action="store_false", help="不抓央视")
    ap.add_argument("--cctv-days", type=int, default=3)
    ap.add_argument("--extra-cmd", default=None, help="追加源命令（输出 JSON 列表）")
    args = ap.parse_args(argv)

    try:
        out = build(args)
    except Exception as exc:        # noqa: BLE001
        print(json.dumps({"items": [], "meta": {
            "errors": [f"整体失败：{type(exc).__name__}: {exc}"]}}, ensure_ascii=False))
        print(f"整体失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(out, ensure_ascii=False))
    meta = out["meta"]
    print(f"取数完成：{meta['count']} 条 / 源 {meta['sources']}", file=sys.stderr)
    for err in meta["errors"]:
        print(f"  ! {err}", file=sys.stderr)
    return 0 if out["items"] else 1


if __name__ == "__main__":
    sys.exit(main())