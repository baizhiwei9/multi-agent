#!/usr/bin/env python3
"""复现"跑一次任务到底花多少"：`python3 scripts/measure_cost.py`

只做计量，不修任何业务；输出说明见 docs/COST.md。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from multi_agent.cost import format_table, measure  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="计量一次任务在 HTTP / UDP 直连 / 中继上的帧数与字节数")
    ap.add_argument("--json", action="store_true", help="输出 JSON（默认打印表格）")
    ap.add_argument("--skip-relay", action="store_true", help="跳过中继场景（更快）")
    a = ap.parse_args()

    rep = measure(skip_relay=a.skip_relay)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(format_table(rep))
    return 0 if rep["llm"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())