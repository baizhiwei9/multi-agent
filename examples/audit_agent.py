"""审计官接入示例：一个 Agent 同时绑「审计」+「协调」，演示三级切换。

跑法（本机先起 Hub，或直接用 demo-flow）：

    multi-agent serve --host 0.0.0.0 --port 8787 --db hub.db --token secret &

    python3 examples/audit_agent.py \\
        --hub http://127.0.0.1:8787 --token secret

这个示例做三件事：
1. 把一个 Agent 注册成 capability=auditor（动作 balance/report/series/run/full）；
2. 直接调 `run_action()` 演示 L1 / L2 / L3 三档差异（不经 Hub，看得最清楚）；
3. 在 Hub 上跑一次 auditor 任务，确认"零大模型也能跨进程审计"。

L3 需要你自己提供 llm 回调（本文件演示用本地 Gateway，也可以用任何 OpenAI 兼容端点）：
    export AUDIT_LLM_URL=http://127.0.0.1:18799/v1/chat/completions
    export AUDIT_LLM_MODEL=openclaw/yuanbao-agent
    export AUDIT_LLM_TOKEN=***
"""
from __future__ import annotations

import argparse
import json
import os
import sys


from multi_agent.client import AgentRuntime, HubClient
from multi_agent.integrations import audit
from multi_agent.integrations.audit import Auditor, llm_from_env, run_action

BALANCE = {
    "commodity": "copper", "cn_name": "铜", "period": "2025A", "unit": "万吨",
    "supply": {"mine": 2318.0, "refined_primary": 2322.9,
               "refined_secondary": 534.8, "refined_total": 2857.8},
    "demand": {"refined_usage": 2815.9},
    "balance": {"surplus_deficit": 41.9, "stock_change": 41.0, "stocks_end": 180.9},
    "data_date": "2026-11-21", "as_of": "2026-11-22",
    "source": "ICSG Table1",
}

REPORT_OK = """# 铜平衡表日报

## 供需平衡
精炼总供应 2857.8 万吨，精炼用量 2815.9 万吨，供需过剩 41.9 万吨。

## 库存
期末库存 180.9 万吨，本期累库 41.0 万吨。

## 结论
供给小幅过剩，库存累积，价格上行空间受限。
"""

REPORT_BAD = """# 铜平衡表日报

## 结论
本期明显去库，库存下降，供应偏紧，价格看涨。

## 数据
平衡值 TODO，库存 nan。
"""


def _print_verdict(tag: str, rep) -> None:
    d = rep.as_dict()
    print(f"\n=== {tag} → verdict={d['verdict']} severity={d['severity']} "
          f"(error {d['counts']['error']} / warn {d['counts']['warn']}) "
          f"{d['elapsed_ms']}ms")
    for f in d["findings"][:6]:
        print(f"  [{f['severity']}] {f['code']}: {f['message']}")
    for layer, info in d["layers"].items():
        print(f"  layer {layer}: {json.dumps(info, ensure_ascii=False)}")


def demo_levels() -> None:
    """三档对比：rule / stat / llm —— 直接看成本差在哪。"""
    print("### 1) L1 规则审计（0 token，0 网络）")
    _print_verdict("balance/rule", Auditor(level="rule").audit_balance(BALANCE))
    _print_verdict("report/rule(好报告)",
                   Auditor(level="rule").audit_report(REPORT_OK, BALANCE))
    _print_verdict("report/rule(坏报告)",
                   Auditor(level="rule").audit_report(REPORT_BAD, BALANCE))

    print("\n### 2) L2 统计审计（0 token，本地统计检验）")
    import random
    rnd = random.Random(42)
    walk, v = [], 100.0
    for _ in range(160):
        v += rnd.gauss(0, 1)
        walk.append(v)
    _print_verdict("series/stat(随机游走)", Auditor(level="stat").audit_series(walk, "stock"))

    print("\n### 3) L3 语义审计（需要 llm 回调，走网络烧 token）")
    llm = llm_from_env()
    if llm is None:
        print("  （未配置 AUDIT_LLM_URL，跳过；Auditor 会自动降级并标 llm_unavailable）")
        _print_verdict("report/llm(无回调)",
                       Auditor(level="llm").audit_report(REPORT_BAD, BALANCE))
    else:
        _print_verdict("report/llm", Auditor(level="llm", llm=llm).audit_report(
            REPORT_BAD, BALANCE))


def demo_hub(hub: str, token: str, agent_id: str) -> None:
    """在 Hub 上真跑一次 auditor 任务（跨进程、零大模型）。"""
    rt = AgentRuntime(hub, agent_id,
                      [{"name": "auditor",
                        "actions": audit._ROLE_ACTIONS["auditor"],
                        "description": audit._ROLE_DESCRIPTION}],
                      token=token, concurrency=2, poll_wait=5, ttl_s=60)
    audit.register(rt)
    info = rt.start()
    print(f"\n[agent] {agent_id} registered: {json.dumps(info, ensure_ascii=False)}")

    client = HubClient(hub, token=token)
    task = client.submit("auditor", "full",
                         {"balance": BALANCE, "report": REPORT_BAD, "level": "rule"},
                         timeout_s=60, wait=True)
    print(f"[task] state={task.get('state')}")
    result = task.get("result") or {}
    print(f"[task] verdict={result.get('verdict')} counts={result.get('counts')}")
    for f in (result.get("findings") or [])[:5]:
        print(f"  [{f['severity']}] {f['code']}: {f['message']}")

    # 再跑一次纯函数路径，证明不需要 Hub 也能用
    local = run_action("balance", {"balance": BALANCE, "level": "rule"})
    print(f"[local] 不经 Hub 直接调用：verdict={local['verdict']}")

    rt.stop()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="审计官示例")
    ap.add_argument("--hub", default=None, help="Hub 地址；不给就只跑本地演示")
    ap.add_argument("--token", default=os.environ.get("HUB_TOKEN"))
    ap.add_argument("--id", default="auditor-1")
    ap.add_argument("--local-only", action="store_true")
    a = ap.parse_args(argv)

    print("### 0) 0 token 自检")
    print(" ", json.dumps(audit.assert_no_llm_import(), ensure_ascii=False))
    print("  capabilities:", json.dumps(audit._stats_capabilities(), ensure_ascii=False))

    demo_levels()

    if a.hub and not a.local_only:
        demo_hub(a.hub, a.token, a.id)
    else:
        print("\n（未给 --hub，跳过 Hub 演示）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
