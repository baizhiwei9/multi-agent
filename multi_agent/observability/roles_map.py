"""角色职责图：按“Coordinator 接活 / Developer 干活 / Supervisor 终审”的语义画。

和 flow.py 的区别：
- flow.py 画的是**一次运行的消息序列**（谁在什么时候把什么交给谁）；
- 本图画的**角色职责与汇报线**，并标注代码现状（哪些名字与语义不一致）。

生成：
    python -m multi_agent.observability.roles_map --out roles.jpg
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:                     # pragma: no cover
    Image = ImageDraw = None  # type: ignore

from .flow import (_arrow, _box, _center_text, _font, C_ACTOR, C_BORDER,
                   C_BOX, C_REQ, C_RET, C_SUB, C_TITLE)

W, H = 2000, 1250
GREEN = "#16a34a"
ORANGE = "#d97706"
GRAY = "#94a3b8"


def render(out_path: str | Path) -> None:
    if Image is None:
        raise RuntimeError("需要 Pillow 才能画图：pip install pillow")
    img = Image.new("RGB", (W, H), "#ffffff")
    d = ImageDraw.Draw(img)
    f_title, f_sub = _font(38), _font(18)
    f_node, f_small, f_tiny = _font(20), _font(15), _font(13)

    d.text((60, 36), "角色职责图：Coordinator 接活 / Developer 干活 / Supervisor 终审",
           font=f_title, fill=C_TITLE)
    d.text((62, 88), "绿=panel 已启用；橙=代码名与语义不一致；灰=futures 已有、panel 未启用",
           font=f_sub, fill=C_SUB)

    # ── 1. Coordinator
    cx, cy, cw, ch = 820, 140, 360, 74
    _box(d, cx, cy, cw, ch, "#ecfdf5", GREEN, 12)
    _center_text(d, (cx, cy, cx + cw, cy + ch),
                 "Coordinator（接活 / 拆 DAG / 往下派）", f_node, "#065f46")

    # ── 2. Developer / Worker 容器
    dx, dy, dw, dh = 100, 270, 1800, 215
    _box(d, dx, dy, dw, dh, C_BOX, C_BORDER, 14)
    d.text((dx + 18, dy + 12), "Developer / Worker（干活）", font=f_node, fill=C_TITLE)
    boxes = [
        (180, "EventScout\n多源爬虫 + 事件官"),
        (700, "4 席辩论\nbull / bear"),
        (1220, "行情快照\n东财日线"),
    ]
    for bx, label in boxes:
        _box(d, bx, dy + 70, 400, 110, "#ffffff", C_ACTOR, 10)
        _center_text(d, (bx, dy + 70, bx + 400, dy + 180), label, f_small, C_ACTOR)

    # ── 3. Supervisor（语义上的经理/终审）
    sx, sy, sw, sh = 820, 550, 360, 82
    _box(d, sx, sy, sw, sh, "#ecfdf5", GREEN, 12)
    _center_text(d, (sx, sy, sx + sw, sy + sh),
                 "Supervisor（经理 / 终审 / 签发）", f_node, "#9a3412")


    # ── 4. Handoff（代码里叫 supervisor，实际是送审/台账）
    hx, hy, hw, hh = 1450, 548, 400, 84
    _box(d, hx, hy, hw, hh, "#ecfdf5", GREEN, 12)
    _center_text(d, (hx, hy, hx + hw, hy + hh),
                 "Handoff（送审件 / 台账）", f_small, "#9a3412")


    # ── 5. 风控 / 双审计（futures 已有，panel 未启用）
    rx, ry, rw, rh = 140, 720, 340, 84
    _box(d, rx, ry, rw, rh, "#f8fafc", GRAY, 12)
    _center_text(d, (rx, ry, rx + rw, ry + rh), "风控官 risk/brief", f_small, "#475569")

    ax, ay, aw, ah = 600, 720, 300, 84
    _box(d, ax, ay, aw, ah, "#f8fafc", GRAY, 12)
    _center_text(d, (ax, ay, ax + aw, ay + ah), "审计 A（过程复核）", f_small, "#475569")

    bx, by, bw, bh = 1010, 720, 300, 84
    _box(d, bx, by, bw, bh, "#f8fafc", GRAY, 12)
    _center_text(d, (bx, by, bx + bw, by + bh), "审计 B（终审 J）", f_small, "#475569")

    # ── 6. Reporter
    px, py, pw, ph = 820, 910, 360, 74
    _box(d, px, py, pw, ph, "#ecfdf5", GREEN, 12)
    _center_text(d, (px, py, px + pw, py + ph), "Reporter（报告）", f_node, "#065f46")

    # ── 箭头：接活 → 干活
    _arrow(d, 1000, 214, 1000, 268, C_REQ, 2)
    d.text((1010, 228), "派活 / 拆步骤", font=f_small, fill=C_REQ)
    # 干活 → 终审
    _arrow(d, 1000, 486, 1000, 548, C_REQ, 2)
    d.text((1010, 500), "交付：事件+行情+发言", font=f_small, fill=C_REQ)
    # 终审 → 报告
    _arrow(d, 1000, 632, 1000, 908, C_RET, 2)
    d.text((1008, 664), "签发", font=f_small, fill=C_RET)
    # 终审 → 审计 A（送审）
    _arrow(d, 900, 632, 830, 718, C_REQ, 2)
    d.text((790, 650), "送审", font=f_small, fill=C_REQ)
    # 审计 A ⇄ 审计 B
    _arrow(d, 900, 748, 1010, 748, C_REQ, 2)
    _arrow(d, 1010, 776, 900, 776, C_RET, 2)
    d.text((905, 732), "复核", font=f_tiny, fill=C_REQ)
    d.text((905, 780), "终审回执", font=f_tiny, fill=C_RET)
    # 审计 B → 终审
    _arrow(d, 1150, 718, 1080, 635, C_RET, 2)
    d.text((1110, 655), "audit verdict", font=f_tiny, fill=C_RET)
    # 终审 → 风控（虚线，futures 未启用）
    _arrow(d, 820, 610, 480, 718, GRAY, 2, dashed=True)
    d.text((520, 660), "risk brief（未启用）", font=f_tiny, fill=GRAY)
    # Handoff ↔ 终审（代码现状）
    _arrow(d, 1448, 590, 1184, 590, GRAY, 2, dashed=True)
    d.text((1210, 566), "送审 / 台账（staff）", font=f_tiny, fill=GRAY)
    # 报告 → Coordinator（左侧回执）
    d.line([(820, 948), (60, 948)], fill=GRAY, width=2)
    d.line([(60, 948), (60, 178)], fill=GRAY, width=2)
    _arrow(d, 60, 178, 818, 178, GRAY, 2, dashed=True)
    d.text((70, 930), "回执 / 报告", font=f_tiny, fill=GRAY)

    # ── 图例 / 说明
    ly = 1040
    d.rectangle([60, ly, 78, ly + 18], fill="#ecfdf5", outline=GREEN)
    d.text((86, ly - 2), "panel 已启用", font=f_tiny, fill=C_SUB)
    d.rectangle([260, ly, 278, ly + 18], fill="#f8fafc", outline=GRAY)
    d.text((286, ly - 2), "futures 已有、panel 未启用", font=f_tiny, fill=C_SUB)

    d.text((60, 1090),
           "代码已对齐：capability=supervisor 是经理/终审；capability=handoff 是送审/台账。",
           font=f_small, fill=C_TITLE)
    d.text((60, 1122),
           "Debate 每轮后把 4 席发言交给 Supervisor 复核；前 N-1 次复核回灌下一轮（默认 3 轮 → 回灌 2 次）。",
           font=f_small, fill=C_TITLE)
    d.text((60, 1154),
           "风控官 / 审计 A / 审计 B(J) 仍在 futures_balance，panel 未启用；Coordinator 由 Trade Agent 承担。",
           font=f_small, fill=C_SUB)

    img.save(out_path, "JPEG", quality=93)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="角色职责图（Coordinator/Developer/Supervisor 语义）")
    ap.add_argument("--out", default="roles.jpg")
    args = ap.parse_args(argv)
    render(args.out)
    print(f"已生成 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
