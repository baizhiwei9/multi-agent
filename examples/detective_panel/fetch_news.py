"""取数脚本：宏观 + 股票（新浪）+ 行情快照 → 一份 JSON，喂给 run_once --news-cmd。

    python examples/detective_panel/fetch_news.py --hours 24 --pages 2

输出（stdout，JSON）：

    {"items":  [ {title, text, source, published_at, url}, ... ],
     "market": {"spot": {...}, "history": {...}, "as_of": "...", "note": "..."},
     "meta":   {"sources": {...}, "errors": [...]}}

为什么要这个脚本
----------------
框架的 `news/collect` 只做关键词过滤，取数得有人干。这里把「宏观」与「股票」两类
最常用的公开源打包好，全是新浪/央视，无需 Key：

    · 新浪财经 7x24 快讯   zhibo.sina.com.cn 公开接口（宏观 + 市场，主力源）
    · 央视新闻联播文字稿   akshare `news_cctv`（政策口径最硬）
    · 新浪指数快照         hq.sinajs.cn（6 个宽基的点位/涨跌幅/成交额）
    · 新浪指数日线         akshare `stock_zh_index_daily`（算 MA40、近 5/10/20 日）

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

#: 宽基指数：新浪代码 → 展示名
INDEXES = {
    "sh000001": "上证指数",
    "sh000300": "沪深300",
    "sz399006": "创业板指",
    "sh000688": "科创50",
    "sh000905": "中证500",
    "sh000852": "中证1000",
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def clean(text: str) -> str:
    """去 HTML 标签、压空白；不删正文。"""
    out = _TAG_RE.sub("", text or "")
    out = _WS_RE.sub(" ", out)
    return "\n".join(line.strip() for line in out.split("\n") if line.strip())


# ───────────────────────────────────────────── 1. 新浪 7x24
def fetch_sina(pages: int = 2, page_size: int = 100,
               hours: float = 24) -> List[Dict[str, Any]]:
    """新浪财经 7x24 快讯（公开接口，无需 Key）。按时间倒序翻页。"""
    base = ("https://zhibo.sina.com.cn/api/zhibo/feed"
            "?zhibo_id=152&tag_id=0&dire=f&dpc=1"
            "&page={page}&page_size={size}")
    since = datetime.now() - timedelta(hours=hours)
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
            title = text
            body = ""
            for sep in ("】", "："):
                if sep in text:
                    head, rest = text.split(sep, 1)
                    if sep == "】" and head.startswith("【"):
                        title, body = head + "】", rest.strip()
                        break
            items.append({
                "title": title[:120],
                "text": (body or text)[:600],
                "source": "新浪财经7x24",
                "published_at": created,
                "url": str(row.get("docurl") or ""),
            })
    return items


# ───────────────────────────────────────────── 2. 央视（宏观政策口径）
def fetch_cctv(days: int = 3) -> List[Dict[str, Any]]:
    """央视新闻联播文字稿（akshare news_cctv）。日期按天回溯，拿不到就跳过。"""
    try:
        import akshare as ak        # 延迟导入：没装 akshare 也不影响其他源
    except Exception as exc:        # noqa: BLE001
        raise RuntimeError(f"akshare 不可用：{type(exc).__name__}: {exc}") from exc

    items: List[Dict[str, Any]] = []
    for offset in range(max(1, days)):
        day = (datetime.now() - timedelta(days=offset)).strftime("%Y%m%d")
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


# ───────────────────────────────────────────── 3. 新浪指数快照
def fetch_spot() -> Dict[str, Dict[str, Any]]:
    """hq.sinajs.cn 快照：点位 / 涨跌幅 / 成交额（万元 → 亿元）。"""
    url = "https://hq.sinajs.cn/list=" + ",".join(f"s_{code}" for code in INDEXES)
    req = urllib.request.Request(url, headers={**UA,
                                               "Referer": "https://finance.sina.com.cn"})
    raw = urllib.request.urlopen(req, timeout=15).read().decode("gbk", "replace")
    spot: Dict[str, Dict[str, Any]] = {}
    for line in raw.strip().split("\n"):
        if "=" not in line:
            continue
        code = line.split("=", 1)[0].split("_")[-1].strip()
        name = INDEXES.get(code)
        payload = line.split("=", 1)[1].strip().strip(";").strip('"')
        parts = payload.split(",")
        if not name or len(parts) < 6:
            continue
        try:
            amount_yi = float(parts[5]) / 1e4      # 万元 → 亿元
        except ValueError:
            amount_yi = None
        spot[name] = {
            "close": _to_float(parts[1]),
            "chg": _to_float(parts[2]),
            "chg_pct": _to_float(parts[3]),
            "volume": _to_float(parts[4]),
            "amount_yi": round(amount_yi, 2) if amount_yi is not None else None,
        }
    return spot


def _to_float(value: Any) -> Optional[float]:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


# ───────────────────────────────────────────── 4. 新浪指数日线 → MA40 / 近 N 日
def fetch_history(indices: Optional[List[str]] = None, ma: int = 40) -> Dict[str, Dict[str, Any]]:
    """新浪指数日线：算 MA40、是否站上、近 5/10/20 日涨跌幅。"""
    try:
        import akshare as ak
    except Exception as exc:        # noqa: BLE001
        raise RuntimeError(f"akshare 不可用：{type(exc).__name__}: {exc}") from exc

    codes = indices or list(INDEXES)
    out: Dict[str, Dict[str, Any]] = {}
    for code in codes:
        name = INDEXES.get(code, code)
        try:
            df = ak.stock_zh_index_daily(symbol=code)
        except Exception:           # noqa: BLE001 - 单指数失败不影响其他
            continue
        if df is None or getattr(df, "empty", True) or len(df) < ma + 1:
            continue
        close = [float(v) for v in df["close"].tolist()]
        last = close[-1]
        ma_val = round(sum(close[-ma:]) / ma, 4)

        def ret(n: int) -> Optional[float]:
            if len(close) <= n:
                return None
            base = close[-(n + 1)]
            return round((last / base - 1) * 100, 2) if base else None

        out[name] = {
            "close": round(last, 4),
            "ma40": ma_val,
            "above_ma40": last > ma_val,
            "dev_ma40_pct": round((last / ma_val - 1) * 100, 2) if ma_val else None,
            "ret5": ret(5), "ret10": ret(10), "ret20": ret(20),
            "as_of": str(df["date"].iloc[-1]),
        }
    return out


# ───────────────────────────────────────────── 5. 外部追加源（商品等）
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
def build(args: argparse.Namespace) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    errors: List[str] = []
    sources: Dict[str, int] = {}

    if args.sina:
        try:
            got = fetch_sina(args.pages, args.page_size, args.hours)
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

    market: Dict[str, Any] = {}
    keep = {n.strip() for n in
            str(getattr(args, "indexes", None) or "沪深300,科创50,创业板指").split(",")
            if n.strip()}
    if args.quote:
        try:
            spot = fetch_spot()
            # 只留关键字段：证据包每次发言都会被重读，字段越少越省 token
            market["spot"] = {
                name: {k: v for k, v in row.items()
                       if k in ("close", "chg_pct", "amount_yi")}
                for name, row in spot.items() if name in keep
            }
            sources["quote"] = len(market["spot"])
        except Exception as exc:    # noqa: BLE001
            errors.append(f"新浪行情快照失败：{type(exc).__name__}: {exc}")
    if args.history:
        try:
            codes = [c for c, n in INDEXES.items() if n in keep]
            hist = fetch_history(codes or args.history_codes, args.ma)
            market["history"] = {
                name: {k: v for k, v in row.items()
                       if k in ("close", "ma40", "above_ma40", "dev_ma40_pct",
                                "ret5", "ret20")}
                for name, row in hist.items()
            }
            sources["history"] = len(market["history"])
        except Exception as exc:    # noqa: BLE001
            errors.append(f"新浪指数日线失败：{type(exc).__name__}: {exc}")
    if market:
        market["as_of"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        market["note"] = (f"spot 来自新浪快照（成交额单位亿元）；history 为新浪指数日线，"
                          f"MA{args.ma} 为 {args.ma} 日简单均线；缺失字段视为待补")

    # 去重：同 title 前 60 字只留一条，保持原顺序
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

    return {"items": dedup[: args.limit], "market": market,
            "meta": {"sources": sources, "errors": errors,
                     "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     "count": len(dedup)}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="宏观 + 股票（新浪）素材聚合")
    ap.add_argument("--hours", type=float, default=24, help="时间窗（小时）")
    ap.add_argument("--pages", type=int, default=2, help="新浪7x24 翻几页（每页≤100）")
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--limit", type=int, default=120, help="最多输出多少条")
    ap.add_argument("--no-sina", dest="sina", action="store_false", help="不抓新浪7x24")
    ap.add_argument("--no-cctv", dest="cctv", action="store_false", help="不抓央视")
    ap.add_argument("--cctv-days", type=int, default=3)
    ap.add_argument("--no-quote", dest="quote", action="store_false", help="不取行情快照")
    ap.add_argument("--no-history", dest="history", action="store_false", help="不算MA40")
    ap.add_argument("--history-codes", default=None,
                    help="只对指定指数算日线，逗号分隔，如 sh000300,sh000688")
    ap.add_argument("--indexes", default=None,
                    help="行情保留哪几个指数（默认 沪深300,科创50,创业板指）；"
                         "多留指数 = 每次发言都多读几行，token 会涨")
    ap.add_argument("--ma", type=int, default=40, help="均线窗口，默认 40")
    ap.add_argument("--extra-cmd", default=None, help="追加源命令（输出 JSON 列表）")
    args = ap.parse_args(argv)

    if args.history_codes:
        args.history_codes = [c.strip() for c in args.history_codes.split(",") if c.strip()]

    try:
        out = build(args)
    except Exception as exc:        # noqa: BLE001
        print(json.dumps({"items": [], "market": {}, "meta": {
            "errors": [f"整体失败：{type(exc).__name__}: {exc}"]}},
            ensure_ascii=False))
        print(f"整体失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(out, ensure_ascii=False))
    meta = out["meta"]
    print(f"取数完成：{meta['count']} 条 / 源 {meta['sources']} / "
          f"行情 {'有' if out['market'] else '无'}", file=sys.stderr)
    for err in meta["errors"]:
        print(f"  ! {err}", file=sys.stderr)
    return 0 if (out["items"] or out["market"]) else 1


if __name__ == "__main__":
    sys.exit(main())