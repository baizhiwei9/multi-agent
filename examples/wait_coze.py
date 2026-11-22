#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wait_coze.py —— 常驻协调端：等 coze 出票 → 打洞/中继 → 派活 → 收结果。

这就是大佬要的"我当协调者"的形态：
  · 先由大模型 @ 一下 coze，让它跑一条命令（那是唯一的一次 token）
  · coze 起了之后把票 post 到 ntfy
  · 我这个进程一直 poll，拿到票就自动连上去
  · 连上后派活（跑脚本 / 审计 / 构建平衡表），全程零 token

用法：
    python3 wait_coze.py --topic mergen-7f3a91 --peer coze \
        --run /srv/jobs/build_sheet.py --wait 900
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from multi_agent.signal import Signal, SignalError


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--id", default="openclaw")
    ap.add_argument("--peer", required=True)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--run", action="append", default=[],
                    help="要对方执行的脚本，可多次给")
    ap.add_argument("--wait", type=float, default=900, help="等对端出现在线多久")
    ap.add_argument("--loop", action="store_true", help="连上后持续服务（不退出）")
    a = ap.parse_args(argv)

    sig = Signal(a.topic, a.id, a.port, verbose=True)
    try:
        info = sig.discover()
        print(f"[wait] 我的公网 {sig.link.public}  本地 {sig.link.local_port}",
              flush=True)
        print(f"[wait] 我的票: {sig.ticket()}", flush=True)
        sig.announce()
        print(f"[wait] 等 {a.peer} 出现（最多 {a.wait:.0f}s）…", flush=True)

        peer = sig.wait_peer_ticket(a.peer, timeout=a.wait)
        print(f"[wait] 拿到 {a.peer} 的票: {peer['ticket']}", flush=True)

        res = sig.connect(peer["ticket"])
        print(f"[wait] 连接成功 mode={res['mode']}", flush=True)
        print("[wait] status:", json.dumps(sig.status(), ensure_ascii=False),
              flush=True)

        for script in a.run:
            print(f"[wait] 派活 → {script}", flush=True)
            sig.send({"action": "run", "script": script})
            got = sig.recv(timeout=300,
                           filter_fn=lambda m: m.get("action") == "result")
            if got:
                r = got.get("result") or {}
                print(f"[wait] 结果 rc={r.get('returncode')} "
                      f"{r.get('duration_ms')}ms", flush=True)
                if r.get("stdout"):
                    print("[wait] stdout:", r["stdout"].strip()[:500], flush=True)
            else:
                print("[wait] ✗ 等结果超时", flush=True)

        if a.loop:
            print("[wait] 保持连接中（Ctrl-C 退出）", flush=True)
            while True:
                time.sleep(5)
        return 0
    except SignalError as e:
        print(f"[wait] {e}", file=sys.stderr, flush=True)
        return 2
    finally:
        sig.close()


if __name__ == "__main__":
    sys.exit(main())
