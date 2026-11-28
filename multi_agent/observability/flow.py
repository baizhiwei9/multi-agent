"""消息流追踪 + 角色序列图（JPG）。

为什么单开一层
--------------
辩论跑完只留一份 Markdown，看不出“谁把什么交给了谁”。这里把一次运行拆成
`FlowTrace`（角色 + 消息 + 载荷摘要），再画成序列图：

    Trade Agent → Hub → EventScout → 数据源
    Trade Agent → Hub → Debate → 4 席 → Judge → Reporter

用法
----
    from multi_agent.observability.flow import FlowTrace

    trace = FlowTrace.from_bundle(scan, market, debate, report_chars=len(md),
                                  topic="国常会后 A 股是否开始构筑底部")
    trace.save_json("flow_trace.json")
    trace.render("flow.jpg")

命令行（从一次运行的 trade_result.json 回放）：

    python -m multi_agent.observability.flow \
        --run-dir trade_out --out flow.jpg
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:                                    # 画图是可选能力：没装 Pillow 也能跑辩论
    from PIL import Image, ImageDraw, ImageFont
except ImportError:                     # pragma: no cover
    Image = ImageDraw = ImageFont = None  # type: ignore

HERE = Path(__file__).resolve().parent

#: 中文字体：仓库不再自带 2.5MB 字体，按以下顺序找；也可用 FLOW_FONT 指定
_FONT_CACHE: Path | None = None


def _font_path() -> Path:
    global _FONT_CACHE
    if _FONT_CACHE is not None:
        return _FONT_CACHE
    candidates = [
        os.environ.get("FLOW_FONT"),
        str(HERE / "NotoSansSC-Regular.ttf"),
        str(Path.home() / ".fonts" / "NotoSansSC-Regular.ttf"),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ]
    for cand in candidates:
        if cand and Path(cand).exists():
            _FONT_CACHE = Path(cand)
            return _FONT_CACHE
    raise FileNotFoundError(
        "找不到中文字体：export FLOW_FONT=/path/to/NotoSansSC-Regular.ttf，"
        "或安装 Noto Sans CJK / 文泉驿微米黑")

#: 画布尺寸
W, H = 2400, 1660

#: 颜色
C_BG = "#ffffff"
C_TITLE = "#0f172a"
C_SUB = "#64748b"
C_LINE = "#cbd5e1"
C_ACTOR = "#1e3a8a"
C_REQ = "#2563eb"
C_RET = "#16a34a"
C_SELF = "#9333ea"
C_NOTE = "#f59e0b"
C_BOX = "#f8fafc"
C_BORDER = "#e2e8f0"


@dataclass
class Msg:
    src: str
    dst: str
    label: str
    payload: str = ""
    kind: str = "request"          # request / return / self / note
    seq: int = 0
    loop: str = ""                 # 非空表示属于某个 loop 框（如 "round"）


@dataclass
class FlowTrace:
    title: str
    subtitle: str
    actors: List[Tuple[str, str]]
    messages: List[Msg]
    notes: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    # ---------- 构建 ----------
    @classmethod
    def from_bundle(cls, scan: Dict[str, Any], market: Dict[str, Any],
                    debate: Dict[str, Any], report_chars: int = 0,
                    topic: str = "", brief_chars: int = 0,
                    brief_truncated: bool = False) -> "FlowTrace":
        meta = scan.get("meta") or {}
        src = meta.get("sources") or {}
        n_events = len(scan.get("events") or [])
        n_sched = len(scan.get("scheduled") or [])
        n_cal = len(scan.get("calendar") or [])
        keywords = meta.get("keywords") or []
        date = scan.get("date") or meta.get("date") or ""
        rounds = debate.get("rounds")
        speeches = len(debate.get("transcript") or [])
        decision = debate.get("decision")
        judge_obj = debate.get("judge") or {}
        judge = judge_obj.get("text") or ""
        confidence = judge_obj.get("confidence") or ""
        stopped = debate.get("stopped_reason") or ""
        chars = debate.get("chars_used") or 0
        m = re.search(r"(?:裁决|结论)\s*[】\]]?\s*[:：]?\s*(BULL|BEAR|NEUTRAL)",
                      judge, re.I)
        judge_stance = m.group(1).upper() if m else ""
        judge_label = judge_stance or decision
        spot = market.get("spot") or {}
        spot_txt = "、".join(f"{k}{v.get('chg_pct', 0):+.2f}%"
                            for k, v in list(spot.items())[:4]) or "—"
        src_txt = " / ".join(f"{k} {v}" for k, v in src.items() if v) or "—"

        actors = [
            ("trade", "Trade Agent\n/ Coordinator"),
            ("hub", "Hub\n(8787)"),
            ("events", "EventScout\n(事件官)"),
            ("sources", "数据源\n(6 源)"),
            ("handoff", "Handoff\n(送审/台账)"),
            ("debate", "Debate\n(评议器)"),
            ("supervisor", "Supervisor\n(经理/终审)"),
            ("report", "Reporter\n(报告)"),
        ]
        # 席位数：优先读引擎回包的 participants，其次数 transcript 里的发言席，最后才退回 4
        n_people = len(debate.get("participants") or [])
        if not n_people:
            n_people = len({str(t.get("speaker") or "")
                            for t in (debate.get("transcript") or [])}) or 4
        has_scan = bool(scan.get("date") or n_events or n_sched or keywords)
        has_brief = bool(brief_chars)

        msgs: List[Msg] = []
        if has_scan:
            msgs += [
                Msg("trade", "hub", "submit events/scan",
                    f"date={date}；keywords={len(keywords)}；horizon=5", "request"),
                Msg("hub", "events", "派发 task",
                    "capability=events / action=scan", "request"),
                Msg("events", "sources", "crawl(keyword, date)",
                    "6 源并行；只收 published=当天", "request"),
                Msg("sources", "events", "原始新闻",
                    f"{src_txt}；合计 {sum(src.values()) if src else '—'} 条", "return"),
                Msg("events", "events", "EventOfficer",
                    "规则 + 事实 → 未来日历（MSCI/PMI/月末）", "self"),
                Msg("events", "hub", "scan result",
                    f"events={n_events}；scheduled={n_sched}；calendar={n_cal}", "return"),
                Msg("hub", "trade", "events result",
                    "digest + calendar + meta.sources", "return"),
            ]
        if has_brief:
            msgs += [
                Msg("trade", "handoff", "handoff/pack",
                    "events_digest + 行情 → 送审件", "request"),
                Msg("handoff", "trade", "brief",
                    f"{brief_chars} 字；truncated={brief_truncated}", "return"),
            ]
        msgs += [
            Msg("trade", "hub", "submit debate/run",
                f"{n_people} 席 × {rounds} 轮；calendar={n_cal}", "request"),
            Msg("hub", "debate", "派发 panel",
                "participants / judge / context.evidence", "request"),
            Msg("debate", "debate", "speak(round, transcript)",
                f"每轮每席一次；共 {speeches} 次", "self"),
            Msg("debate", "supervisor", "review(round r)",
                f"r=1..{rounds}；每轮 {n_people} 席后复核", "request", loop="round"),
            Msg("supervisor", "debate", "verdict + critique",
                (f"原文 {judge_stance or decision}；引擎 {decision}；"
                 f"每轮 1 次，前 {max(0, (rounds or 1) - 1)} 次回灌下一轮" if judge
                 else f"未产出裁决（{stopped or '无 supervisor'}）"), "return",
                loop="round"),
            Msg("debate", "hub", "panel result",
                f"{speeches} 条发言；decision={decision}", "return"),
            Msg("hub", "trade", "debate result",
                "transcript + judge + 预算", "return"),
            Msg("trade", "hub", "submit report/build",
                "topic + panel", "request"),
            Msg("hub", "report", "派发 report/build",
                "panel result", "request"),
            Msg("report", "trade", "Markdown",
                f"{report_chars:,} 字", "return"),
            Msg("trade", "trade", "落盘 + 渲染",
                "trade_report.md / trade_result.json / flow.jpg", "self"),
        ]
        for i, m in enumerate(msgs, start=1):
            m.seq = i
        # 只保留真正出现在消息里的角色，避免画一排空泳道
        used = {m.src for m in msgs} | {m.dst for m in msgs}
        actors = [a for a in actors if a[0] in used]
        head = f"{date}｜" if date else ""
        return cls(
            title=topic or "多智能体消息传递图",
            subtitle=f"{head}{n_people} 席 × {rounds} 轮｜{speeches} 条发言｜"
                     f"裁决 {judge_label}｜行情 {spot_txt}",
            actors=actors, messages=msgs,
            notes=[
                "所有 Agent 不点对点直连：注册 / 取任务 / 回结果都走 Hub（HTTP+长轮询）。",
                "辩论席之间的“对话”不是 socket：引擎把 transcript 压缩后拼进下一席 prompt。",
                "本 panel 的收口是 Supervisor；风控官与双审计在子图 B（trade 项目已有，panel 未启用）。",
            ],
            meta={"date": date, "sources": src, "events": n_events,
                  "scheduled": n_sched, "calendar": n_cal,
                  "rounds": rounds, "speeches": speeches,
                  "decision": decision, "chars": chars,
                  "participants": n_people, "has_scan": has_scan,
                  "has_brief": has_brief,
                  "report_chars": report_chars},
        )

    @classmethod
    def from_run_dir(cls, run_dir: str | Path) -> "FlowTrace":
        d = Path(run_dir)
        bundle = json.loads((d / "trade_result.json").read_text(encoding="utf-8"))
        report = d / "trade_report.md"
        chars = len(report.read_text(encoding="utf-8")) if report.exists() else 0
        sup = bundle.get("handoff") or {}
        brief = sup.get("payload") or ""
        subject = ""
        if report.exists():
            for line in report.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("- subject："):
                    subject = line.split("：", 1)[1].strip()
                    break
        return cls.from_bundle(bundle.get("scan") or {}, bundle.get("market") or {},
                               bundle.get("debate") or {}, chars, topic=subject,
                               brief_chars=len(brief),
                               brief_truncated=bool(sup.get("truncated")))

    # ---------- 输出 ----------
    def save_json(self, path: str | Path) -> None:
        data = {
            "title": self.title, "subtitle": self.subtitle,
            "actors": self.actors,
            "messages": [m.__dict__ for m in self.messages],
            "notes": self.notes, "meta": self.meta,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1),
                             encoding="utf-8")

    def render(self, out_path: str | Path) -> None:
        render_sequence(self, out_path)


# ───────────────────────────────────────────── 绘图工具
def _font(size: int):
    if ImageFont is None:
        raise RuntimeError("需要 Pillow 才能画图：pip install pillow")
    return ImageFont.truetype(str(_font_path()), size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont,
          max_w: float) -> List[str]:
    lines, cur = [], ""
    for ch in str(text):
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if draw.textlength(cur + ch, font=f) <= max_w:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines or [""]


def _arrow(draw: ImageDraw.ImageDraw, x1: float, y1: float, x2: float, y2: float,
           color: str, width: int = 2, dashed: bool = False) -> None:
    if dashed:
        total = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        n = max(1, int(total / 10))
        for i in range(n):
            if i % 2:
                continue
            a = i / n
            b = min(1, (i + 1) / n)
            draw.line([(x1 + (x2 - x1) * a, y1 + (y2 - y1) * a),
                       (x1 + (x2 - x1) * b, y1 + (y2 - y1) * b)],
                      fill=color, width=width)
    else:
        draw.line([(x1, y1), (x2, y2)], fill=color, width=width)
    # 箭头三角
    import math
    ang = math.atan2(y2 - y1, x2 - x1)
    size = 11
    p1 = (x2, y2)
    p2 = (x2 - size * math.cos(ang - 0.42), y2 - size * math.sin(ang - 0.42))
    p3 = (x2 - size * math.cos(ang + 0.42), y2 - size * math.sin(ang + 0.42))
    draw.polygon([p1, p2, p3], fill=color)


def _box(draw: ImageDraw.ImageDraw, x: float, y: float, w: float, h: float,
         fill: str, border: str, radius: int = 8) -> None:
    draw.rounded_rectangle([x, y, x + w, y + h], radius=radius,
                           fill=fill, outline=border, width=1)


def _center_text(draw: ImageDraw.ImageDraw, box: Tuple[float, float, float, float],
                 text: str, f: ImageFont.FreeTypeFont, fill: str,
                 line_gap: int = 2) -> None:
    x1, y1, x2, y2 = box
    lines = str(text).split("\n")
    heights = [draw.textbbox((0, 0), ln, font=f)[3] for ln in lines]
    total = sum(heights) + line_gap * (len(lines) - 1)
    y = y1 + (y2 - y1 - total) / 2
    for ln, h in zip(lines, heights):
        w = draw.textlength(ln, font=f)
        draw.text((x1 + (x2 - x1 - w) / 2, y), ln, font=f, fill=fill)
        y += h + line_gap


# ───────────────────────────────────────────── 主渲染
def render_sequence(trace: FlowTrace, out_path: str | Path) -> None:
    if Image is None:
        raise RuntimeError("需要 Pillow 才能画图：pip install pillow")
    img = Image.new("RGB", (W, H), C_BG)
    d = ImageDraw.Draw(img)
    f_title, f_sub = _font(42), _font(21)
    f_actor, f_msg = _font(19), _font(15)
    f_small, f_tiny = _font(14), _font(13)

    # 标题
    d.text((80, 42), trace.title, font=f_title, fill=C_TITLE)
    d.text((82, 102), trace.subtitle, font=f_sub, fill=C_SUB)
    d.text((W - 360, 48), datetime.now().strftime("%Y-%m-%d %H:%M"),
           font=f_small, fill=C_SUB)

    # 角色位置
    actors = trace.actors
    n = len(actors)
    x0, x1 = 135, 1505
    xs = [x0 + (x1 - x0) * i / (n - 1) for i in range(n)]
    x_of = {aid: xs[i] for i, (aid, _) in enumerate(actors)}
    actor_w, actor_h = 168, 58
    y_actor, y_line0, y_line1 = 170, 228, 1170

    # 生命线
    for i, (aid, label) in enumerate(actors):
        x = xs[i]
        d.line([(x, y_line0), (x, y_line1)], fill=C_LINE, width=2)
        _box(d, x - actor_w / 2, y_actor, actor_w, actor_h, C_ACTOR, C_ACTOR, 10)
        _center_text(d, (x - actor_w / 2, y_actor, x + actor_w / 2,
                         y_actor + actor_h), label, f_actor, "#ffffff")

    # 消息：y 从 300 起，步长 42
    y_start, step = 300, 42
    loop_msgs = [m for m in trace.messages if m.loop]
    if loop_msgs:
        ys = [y_start + (m.seq - 1) * step for m in loop_msgs]
        lx1 = x_of.get("debate", 1000) - 110
        lx2 = x_of.get("supervisor", 1275) + 110
        d.rounded_rectangle([lx1, min(ys) - 24, lx2, max(ys) + 18],
                            radius=10, outline="#93c5fd", width=2, fill="#eff6ff")
        rounds_n = trace.meta.get("rounds") or 1
        d.text((lx2 + 8, min(ys) - 8),
               f"loop 1..{rounds_n}（复核×{rounds_n}，回灌×{max(0, rounds_n - 1)}）",
               font=f_tiny, fill="#1d4ed8")
    for m in trace.messages:
        y = y_start + (m.seq - 1) * step
        if y > y_line1 - 20:
            break
        color = {"request": C_REQ, "return": C_RET,
                 "self": C_SELF, "note": C_NOTE}.get(m.kind, C_REQ)
        sx, tx = x_of[m.src], x_of[m.dst]
        if m.kind == "self" or m.src == m.dst:
            # 自消息：右侧小环
            loop = 34
            d.line([(sx, y), (sx + loop, y)], fill=color, width=2)
            d.line([(sx + loop, y), (sx + loop, y + 16)], fill=color, width=2)
            d.line([(sx + loop, y + 16), (sx + 4, y + 16)], fill=color, width=2)
            _arrow(d, sx + 4, y + 16, sx, y + 16, color, 2)
            label = f"{m.seq}. {m.label}"
            lines = _wrap(d, label, f_msg, 210)
            for i, ln in enumerate(lines[:2]):
                d.text((sx + loop + 8, y - 16 + i * 17), ln, font=f_msg, fill=color)
        else:
            _arrow(d, sx, y, tx, y, color, 2)
            # 序号圆点
            d.ellipse([sx - 8, y - 8, sx + 8, y + 8], fill=color)
            d.text((sx - 5 if m.seq < 10 else sx - 7, y - 6), str(m.seq),
                   font=f_tiny, fill="#ffffff")
            label = m.label
            maxw = max(150, abs(tx - sx) - 24)
            lines = _wrap(d, label, f_msg, min(maxw, 300))
            cx = (sx + tx) / 2
            for i, ln in enumerate(lines[:2]):
                lw = d.textlength(ln, font=f_msg)
                d.text((cx - lw / 2, y - 16 - (len(lines[:2]) - 1 - i) * 17),
                       ln, font=f_msg, fill=color)

    # 图例 / 消息清单
    tx0, ty0, tx1, ty1 = 1610, 165, 2340, 1170
    _box(d, tx0, ty0, tx1 - tx0, ty1 - ty0, "#ffffff", C_BORDER, 10)
    d.text((tx0 + 18, ty0 + 14), "消息清单（传递内容）", font=_font(20), fill=C_TITLE)
    d.text((tx0 + 18, ty0 + 46), "seq  从 → 到 / 载荷摘要", font=f_tiny, fill=C_SUB)
    y = ty0 + 76
    row_h = 45
    for m in trace.messages:
        if y + row_h > ty1 - 10:
            break
        bg = "#f8fafc" if m.seq % 2 else "#ffffff"
        d.rectangle([tx0 + 10, y, tx1 - 10, y + row_h - 2], fill=bg)
        d.text((tx0 + 16, y + 6), f"{m.seq:02d}", font=f_tiny, fill=m_color(m.kind))
        short = {"trade": "TA", "hub": "Hub", "events": "ES", "sources": "源",
                 "handoff": "HO", "debate": "Debate", "supervisor": "Sup",
                 "report": "Rep"}
        d.text((tx0 + 48, y + 6),
               f"{short.get(m.src, m.src)} → {short.get(m.dst, m.dst)}",
               font=f_tiny, fill=C_TITLE)
        lines = _wrap(d, m.payload or m.label, f_tiny, tx1 - tx0 - 70)
        for i, ln in enumerate(lines[:2]):
            d.text((tx0 + 48, y + 24 + i * 15), ln, font=f_tiny, fill=C_SUB)
        y += row_h

    # 底部子图 A：Agent 间通讯
    _subgraph_agent_hub(d, (80, 1200, 1150, 1560), f_small, f_tiny)
    # 底部子图 B：风控/双审计
    _subgraph_risk_audit(d, (1180, 1200, 2330, 1560), f_small, f_tiny)

    # 注释
    yy = 1578
    for note in trace.notes:
        d.text((82, yy), "· " + note, font=f_tiny, fill=C_SUB)
        yy += 18

    img.save(out_path, "JPEG", quality=92)


def m_color(kind: str) -> str:
    return {"request": C_REQ, "return": C_RET, "self": C_SELF,
            "note": C_NOTE}.get(kind, C_REQ)


def _subgraph_agent_hub(d: ImageDraw.ImageDraw, box: Tuple[float, float, float, float],
                        f: ImageFont.FreeTypeFont, ft: ImageFont.FreeTypeFont) -> None:
    x1, y1, x2, y2 = box
    _box(d, x1, y1, x2 - x1, y2 - y1, C_BOX, C_BORDER, 12)
    d.text((x1 + 18, y1 + 12), "子图 A｜Agent 间通讯：全部经 Hub，无点对点", font=f, fill=C_TITLE)
    # 三个框
    cy = y1 + 100
    bw, bh = 190, 56
    ax, hx, bx = x1 + 110, x1 + 470, x1 + 830
    for cx, label in ((ax, "Agent A\n(worker)"), (hx, "Hub\n(任务队列)"),
                      (bx, "Agent B\n(worker)")):
        _box(d, cx - bw / 2, cy, bw, bh, "#ffffff", C_ACTOR, 10)
        _center_text(d, (cx - bw / 2, cy, cx + bw / 2, cy + bh), label, ft, C_ACTOR)
    _arrow(d, ax + bw / 2, cy + 18, hx - bw / 2, cy + 18, C_REQ, 2)
    d.text((ax + 105, cy - 2), "register / poll / submit", font=ft, fill=C_REQ)
    _arrow(d, hx + bw / 2, cy + 38, bx - bw / 2, cy + 38, C_RET, 2)
    d.text((hx + 105, cy + 18), "task(capability,action)", font=ft, fill=C_RET)
    # 禁止直连
    d.line([(ax, cy + bh + 40), (bx, cy + bh + 40)], fill="#dc2626", width=2)
    d.text(((ax + bx) / 2 - 70, cy + bh + 46), "✕ 无直连（禁止 P2P）", font=ft, fill="#dc2626")


def _subgraph_risk_audit(d: ImageDraw.ImageDraw, box: Tuple[float, float, float, float],
                         f: ImageFont.FreeTypeFont, ft: ImageFont.FreeTypeFont) -> None:
    x1, y1, x2, y2 = box
    _box(d, x1, y1, x2 - x1, y2 - y1, C_BOX, C_BORDER, 12)
    d.text((x1 + 18, y1 + 12), "子图 B｜风控官与双审计（trade 项目已有；panel 未启用）",
           font=f, fill=C_TITLE)
    cy = y1 + 92
    bw, bh = 160, 54
    xs = [x1 + 120, x1 + 400, x1 + 680, x1 + 960]
    labels = ["采集 B\n(events)", "审计 A\n(过程复核)", "审计 B\n(终审 J)",
              "报告\n(report)"]
    for cx, label in zip(xs, labels):
        _box(d, cx - bw / 2, cy, bw, bh, "#ffffff", C_ACTOR, 10)
        _center_text(d, (cx - bw / 2, cy, cx + bw / 2, cy + bh), label, ft, C_ACTOR)
    def _mid_label(xa, xb, yy, text, color):
        w = d.textlength(text, font=ft)
        d.text(((xa + xb) / 2 - w / 2, yy), text, font=ft, fill=color)
    # 采集 ⇄ 审计 A
    _arrow(d, xs[0] + bw / 2, cy + 16, xs[1] - bw / 2, cy + 16, C_REQ, 2)
    _arrow(d, xs[1] - bw / 2, cy + 40, xs[0] + bw / 2, cy + 40, C_RET, 2)
    _mid_label(xs[0] + bw / 2, xs[1] - bw / 2, cy - 18, "draft", C_REQ)
    _mid_label(xs[0] + bw / 2, xs[1] - bw / 2, cy + 44, "review", C_RET)
    # 审计 A → 审计 B
    _arrow(d, xs[1] + bw / 2, cy + 16, xs[2] - bw / 2, cy + 16, C_REQ, 2)
    _mid_label(xs[1] + bw / 2, xs[2] - bw / 2, cy - 18, "audit verdict", C_REQ)
    # 审计 B → 报告
    _arrow(d, xs[2] + bw / 2, cy + 16, xs[3] - bw / 2, cy + 16, C_RET, 2)
    _mid_label(xs[2] + bw / 2, xs[3] - bw / 2, cy - 18, "final ok/block", C_RET)
    # 报告 ⇢ 审计 A（虚线回传）
    _arrow(d, xs[3], cy + bh, xs[1], cy + bh + 34, "#94a3b8", 2, dashed=True)
    d.text(((xs[1] + xs[3]) / 2 - 50, cy + bh + 40), "report for audit",
           font=ft, fill="#64748b")
    # 风控官在审计 A 下方，向上提交 risk brief
    rx, ry = xs[1], cy + 128
    _box(d, rx - bw / 2, ry, bw, bh, "#fffbeb", C_NOTE, 10)
    _center_text(d, (rx - bw / 2, ry, rx + bw / 2, ry + bh), "风控官\n(risk/brief)",
                 ft, "#b45309")
    _arrow(d, rx, ry, rx, cy + bh + 4, C_NOTE, 2)
    d.text((rx + 8, ry - 18), "risk brief", font=ft, fill=C_NOTE)


# ───────────────────────────────────────────── CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="消息流序列图：从一次运行回放 JPG")
    ap.add_argument("--run-dir", required=True, help="含 trade_result.json 的目录")
    ap.add_argument("--out", default=None, help="输出 JPG（默认 <run-dir>/flow.jpg）")
    args = ap.parse_args(argv)
    trace = FlowTrace.from_run_dir(args.run_dir)
    out = Path(args.out) if args.out else Path(args.run_dir) / "flow.jpg"
    trace.save_json(Path(args.run_dir) / "flow_trace.json")
    trace.render(out)
    print(f"已生成 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
