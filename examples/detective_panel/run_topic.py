"""一句话入口：贴一句话，跑完一局，报告落到 examples/detective_panel/out/。

    python examples/detective_panel/run_topic.py "国常会后A股是否开始构筑底部"

不用记 --topic / --keywords / --news-cmd：

    · 关键词从句子里自动切（停用词切段 + 段内 3 字前后缀），也可 --keywords 覆盖
    · 取数默认调 fetch_news.py（新浪 7x24 + 央视），窗口 --hours / --pages 可调
    · 行情不抓——你要技术面就自己算好，--evidence 传 market 字段
    · 报告固定写到 out/<日期-时间>-<句子前若干字>.md，路径最后打印

跑完在 out/ 里就是一份 Markdown 报告，直接看、直接发。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]
PANEL_DIR = Path(__file__).resolve().parent
OUT_DIR = PANEL_DIR / "out"

#: 纯功能词：整段等于它就没意义（如"是否""开始"）
STOPWORDS: List[str] = [
    "会不会", "有没有", "是不是", "怎么样", "为什么", "什么", "怎么", "如何",
    "是否", "能否", "开始", "已经", "现在", "最近", "今天", "明天", "后面",
    "行情", "影响", "东西", "一个", "这个", "那个", "哪些", "多少", "多大",
]
#: 单字功能字：只用来判断候选里还剩几个"实字"，**不参与切分**
#: （把"会"当分隔符会把"国常会/证监会/美联储"拆坏）
FUNC_CHARS = set("的了着吗呢吧啊我你他它们是在有就也都要想让去上下里对从与和及"
                 "把被给到过这那个还会能更最很不没该其而且或之以为于")

_SEG_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9]+")


def _keep(word: str) -> bool:
    """候选能不能当关键词：去掉功能单字后至少还剩 2 个实字。"""
    if len(word) < 2 or word in STOPWORDS:
        return False
    core = "".join(ch for ch in word if ch not in FUNC_CHARS)
    return len(core) >= 2


def split_keywords(sentence: str) -> List[str]:
    """把一句话切成检索关键词（本地过滤，多切几个不加钱）。

    · 按标点/空白切 clause，整段不动，避免把「国常会」拆成「国常」+「后A股」
    · ≤6 字：整段；5-6 字再加前 3 / 后 3（"国常会后A股" → 国常会）
    · >6 字：3-gram 滑窗，逐个过 _keep() 筛掉纯功能组合
    """
    out: List[str] = []
    for seg in _SEG_RE.findall(sentence):
        if len(seg) <= 6:
            out.append(seg)
            if len(seg) in (5, 6):
                out.extend([seg[:3], seg[-3:]])
        else:
            out.extend(seg[i:i + 3] for i in range(len(seg) - 2))

    dedup: List[str] = []
    for kw in out:
        if _keep(kw) and kw not in dedup:
            dedup.append(kw)
    if not dedup:
        dedup = [sentence[:12]]
    return dedup[:16]


def _cmd_json(cmd: str, what: str = "命令") -> Any:
    """跑一条命令取 JSON：起不来/超时/非 JSON 都明说，不猜。"""
    try:
        proc = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print(f"{what}超时（>120s）", file=sys.stderr)
        return None
    except (OSError, ValueError) as exc:
        print(f"{what}起不来：{type(exc).__name__}: {exc}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(f"{what}退出码 {proc.returncode}：{(proc.stderr or '')[:160]}", file=sys.stderr)
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        text = proc.stdout.strip()
        if text:
            print(f"{what}的 stdout 不是 JSON，按纯文本用", file=sys.stderr)
        return text or None


def _slug(sentence: str, limit: int = 20) -> str:
    clean = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]+", "", sentence)
    return (clean[:limit] or "topic")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="一句话跑一局侦探团，报告落到 out/")
    ap.add_argument("sentence", nargs="+", help="一句话议题，例：\"国常会后A股是否开始构筑底部\"")
    ap.add_argument("--keywords", default="", help="可选：手工关键词（逗号分隔），给了就不再自动切")
    ap.add_argument("--rounds", type=int, default=3, help="轮数，默认 3")
    ap.add_argument("--hours", type=float, default=30, help="新闻回溯小时数，默认 30")
    ap.add_argument("--pages", type=int, default=3, help="新浪 7x24 翻几页，默认 3")
    ap.add_argument("--evidence", default=None, help="证据包 JSON（行情自己算好后放这里）")
    ap.add_argument("--market-cmd", default=None,
                    help="行情命令：stdout 回 {\"market\": {...}} 或 market 字典；"
                         "行情由你的 agent/命令出，不经新闻")
    ap.add_argument("--since", default=None,
                    help="新闻起点（YYYY-MM-DD 或 'YYYY-MM-DD HH:MM'）；给了就按它算窗口，"
                         "避免搜到去年的新闻")
    ap.add_argument("--calendar", default=None,
                    help="事件日历：JSON 文件路径，或直接给一段文本"
                         "（指数调仓 / 期权到期 / 重要数据发布）")
    ap.add_argument("--calendar-cmd", default=None,
                    help="日历命令：stdout 回 JSON 列表（date/event/note）或纯文本")
    ap.add_argument("--no-fetch", action="store_true", help="不调 fetch_news（用 --evidence 或 --news）")
    ap.add_argument("--news", default="", help="直接给新闻正文")
    ap.add_argument("--out-dir", default=str(OUT_DIR), help="报告目录，默认 examples/detective_panel/out")
    args = ap.parse_args(argv)

    sentence = " ".join(args.sentence).strip()
    keywords = args.keywords.strip() or ", ".join(split_keywords(sentence))

    news_cmd = None
    if not args.no_fetch:
        fetch = PANEL_DIR / "fetch_news.py"
        news_cmd = (f'"{sys.executable}" "{fetch}" '
                    f"--hours {args.hours:g} --pages {args.pages}")
        if args.since:
            news_cmd += f' --since "{args.since}"'

    sys.path.insert(0, str(ROOT))
    from examples.detective_panel.run_once import run        # noqa: E402

    # 证据包：--evidence 打底，日历（--calendar / --calendar-cmd）并进去
    evidence_obj: Dict[str, Any] = {}
    if args.evidence:
        try:
            loaded = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                evidence_obj = loaded
            else:
                print("证据包不是 JSON 对象，忽略 --evidence", file=sys.stderr)
        except (OSError, ValueError) as exc:
            print(f"证据包读不了（{exc}），忽略 --evidence", file=sys.stderr)
    calendar: Any = None
    if args.calendar_cmd:
        calendar = _cmd_json(args.calendar_cmd, "日历命令")
    if args.calendar:
        maybe_file = Path(args.calendar)
        if maybe_file.is_file():
            raw = maybe_file.read_text(encoding="utf-8")
            try:
                calendar = json.loads(raw)
            except ValueError:
                calendar = raw
        else:
            calendar = args.calendar
    if isinstance(calendar, dict) and isinstance(calendar.get("events"), list):
        calendar = calendar["events"]        # {"events": [...]} / {"calendar": [...]} 包裹也认
    if isinstance(calendar, dict) and isinstance(calendar.get("calendar"), list):
        calendar = calendar["calendar"]
    if calendar:
        evidence_obj["calendar"] = calendar
    ev_path = args.evidence
    if evidence_obj:
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        ev_path = str(out_dir / "_evidence.tmp.json")
        Path(ev_path).write_text(json.dumps(evidence_obj, ensure_ascii=False),
                                 encoding="utf-8")
    cal_raw = evidence_obj.get("calendar")
    cal_count = (len(cal_raw) if isinstance(cal_raw, (list, tuple))
                 else 1 if cal_raw else 0)

    print(f"议题：{sentence}")
    print(f"关键词：{keywords}")
    print(f"取数：{news_cmd or '（跳过，用 --evidence / --news）'}")
    win_from = (args.since or
                (datetime.now() - timedelta(hours=args.hours)).strftime("%Y-%m-%d %H:%M"))
    win_note = f"--since {args.since}" if args.since else f"最近 {args.hours:g} 小时"
    print(f"新闻窗口：{win_from} → 现在（{win_note}）")
    print(f"行情：{('命令注入 ' + args.market_cmd) if args.market_cmd else '未提供（纯新闻判断）'}")
    print(f"日历：{cal_count} 条" if cal_count else "日历：未提供（不凭记忆补日期）")

    result = run(sentence, news=args.news, evidence_path=ev_path,
                 rounds=args.rounds, keywords=keywords, news_cmd=news_cmd,
                 market_cmd=args.market_cmd)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    path = out_dir / f"{stamp}-{_slug(sentence)}.md"
    news_stat = result.get("news") or {}
    hit_line = (f"未命中（{news_stat.get('reason')}）" if news_stat.get("degraded")
                else f"命中 {news_stat.get('kept')} / 扫描 {news_stat.get('scanned')}")
    market_note = (("命令注入 " + args.market_cmd) if args.market_cmd
                   else "未提供（纯新闻判断）")
    calendar_note = f"{cal_count} 条" if cal_count else "未提供（不凭记忆补日期）"
    model_note = os.environ.get("LLM_MODEL") or "按 config/llm.json 的 profiles"
    header = (f"<!-- 议题：{sentence}\n"
              f"     关键词：{keywords}\n"
              f"     新闻窗口：{win_from} → 现在（{win_note}）；{hit_line}\n"
              f"     行情：{market_note}\n"
              f"     日历：{calendar_note}\n"
              f"     模型：{model_note}（三档：detective_fast / detective_deep / judge_pro）\n"
              f"     模拟：{'是（未配端点，结论不作数）' if result['simulated'] else '否'} -->\n\n")
    path.write_text(header + (result["markdown"] or "（未生成报告）"), encoding="utf-8")

    s, p = result["stats"], result["panel"]
    print("=" * 60)
    print(f"发言：{s['calls']} 次模型调用 / {p.get('rounds')} 轮 / "
          f"{len(p.get('transcript') or [])} 条")
    print(f"裁决：{p.get('decision')}（{p.get('stopped_reason')}）"
          + ("  ⚠ 含模拟发言，不作数" if result["simulated"] else ""))
    print(f"token：输入 ≈{s['in']:,} / 输出 ≈{s['out']:,}")
    print(f"花费：空闲约 {result['cost_off']['total_yuan']:.4f} 元 / "
          f"高峰约 {result['cost_peak']['total_yuan']:.4f} 元（估算非账单）")
    print(f"报告：{path}")
    print("=" * 60)
    print(f"out/ 最近的文件：")
    for f in sorted(out_dir.glob("*.md"))[-5:]:
        print(f"  {f.name}  {f.stat().st_size} B")
    return 0


if __name__ == "__main__":
    sys.exit(main())