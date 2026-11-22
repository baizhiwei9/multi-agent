"""协调端示例：先有"计划"，再按计划发协议消息。

大模型可以只负责生成 `PLAN`（哪些能力、什么参数、先后顺序），
真正的执行全部走 Hub 的 HTTP 协议，不经过模型。

跑法::

    python examples/coordinator.py --hub http://127.0.0.1:8787
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List

from multi_agent.client import HubClient

# 规则式计划（可替换为 LLM 输出/数据库配置）
PLAN: List[Dict[str, Any]] = [
    {"capability": "macro", "action": "regime",
     "params": {"pmi": 50.2, "cpi_yoy": 2.4, "policy_rate_delta": -10}},
    {"capability": "micro", "action": "dialectic",
     "params": {"revenue_growth": 0.18, "gross_margin_delta": -0.02,
                "ocf_to_ni": 0.72, "accruals_ratio": 0.06}},
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub", required=True)
    ap.add_argument("--token", default=None)
    ap.add_argument("--timeout", type=float, default=30)
    a = ap.parse_args()

    client = HubClient(a.hub, token=a.token)
    results = []
    for step in PLAN:
        task = client.submit(step["capability"], step["action"], step.get("params"),
                             timeout_s=a.timeout, wait=True)
        results.append(task)
        print(f"[{task['state']}] {step['capability']}/{step['action']} -> "
              f"{json.dumps(task.get('result'), ensure_ascii=False)[:200]}")

    ok = all(t["state"] == "succeeded" for t in results)
    print(json.dumps({"ok": ok, "count": len(results)}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())