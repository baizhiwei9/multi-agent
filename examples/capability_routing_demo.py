"""演示：协调者按「能力」自动派活（谁声明了谁接），不用写死机器。

这个 demo 把三件事串起来：
  · 不同 agent 声明不同能力（openclaw 支平衡表审计、hermes 支股票、coze 支债券）
  · 协调者**只看能力**派活，不关心是哪台机器
  · 缺能力时提前报出来，而不是跑到一半挂掉

跑法：

    python3 examples/capability_routing_demo.py

一体化会起 HTTP Hub + 5 个 agent，全在 127.0.0.1 但**只知道能力、不知道地址**。
"""
from __future__ import annotations

import json
import os
import sys
import time


from multi_agent.client import AgentRuntime, HubClient
from multi_agent.integrations import audit
from multi_agent.router import CapabilityRouter, NoCapability
from multi_agent.server import HubServer

TOKEN = "demo-token"


BALANCE = {
    "commodity": "copper", "period": "2025A", "unit": "万吨",
    "supply": {"mine": 2318.0, "refined_primary": 2322.9,
               "refined_secondary": 534.8, "refined_total": 2857.8},
    "demand": {"refined_usage": 2815.9},
    "balance": {"surplus_deficit": 41.9, "stock_change": 41.0, "stocks_end": 180.9},
    "data_date": "2026-11-21", "as_of": "2026-11-22",
}
STOCK = {
    "period": "2025Q3", "revenue": 1000.0, "cost_of_revenue": 600.0,
    "gross_profit": 400.0, "operating_profit": 200.0, "net_income": 160.0,
    "operating_cash_flow": 155.0, "total_assets": 5000.0,
    "total_liabilities": 2000.0, "total_equity": 3000.0,
    "eps": 1.60, "shares_outstanding": 100.0,
}
BOND = {
    "bond_code": "240215", "coupon_rate": 2.55, "ytm": 2.60,
    "maturity_date": "2034-06-15", "coupon_frequency": 2,
    "macaulay_duration": 7.85, "modified_duration": 7.65,
    "convexity": 72.5, "credit_spread_bp": 35,
    "curve": {"1Y": 1.85, "2Y": 2.05, "5Y": 2.35, "10Y": 2.60},
}


def start_hub() -> HubServer:
    s = HubServer(host="127.0.0.1", port=0, db_path=":memory:", token=TOKEN)
    s.start_background()
    return s


def make_agent(hub_url: str, agent_id: str, projects, labels=None) -> AgentRuntime:
    """一个 agent 只声明它能审哪几种 project —— 这就是"能力注册到 Hub"。

    ★ 关键：角色名带口径（`auditor:bond`），而不是统统叫 `auditor`。
      这样协调者 `submit("auditor:bond", ...)` 就能精确落到支持债券的那台，
      不需要"投给所有人，让不支持的自己拒绝"。
    """
    projects = list(projects)
    roles = [{"name": audit.role_name(p),
              "actions": [a for a in audit.AUDIT_ACTIONS if a != "projects"],
              "description": f"{p} 审计"} for p in projects]
    rt = AgentRuntime(hub_url, agent_id, roles, token=TOKEN,
                      concurrency=2, poll_wait=1, ttl_s=120,
                      labels=labels or {"agent_id": agent_id})
    audit.register(rt, projects=projects)
    rt.start()
    return rt


def main() -> int:
    print("### 0) 起 Hub + 5 个 agent（各自声明不同能力）")
    hub = start_hub()
    print(f"    Hub: {hub.url}")

    rt_bal = make_agent(hub.url, "openclaw-1", {"balance_sheet"},
                        labels={"site": "home", "agent_id": "openclaw-1"})
    rt_stk = make_agent(hub.url, "hermes-1", {"stock"},
                        labels={"site": "cloud", "agent_id": "hermes-1"})
    rt_bond = make_agent(hub.url, "coze-1", {"bond"},
                         labels={"site": "cloud", "agent_id": "coze-1"})
    # 第二名平衡表审计（演示轮询与 fallback）
    rt_bal2 = make_agent(hub.url, "openclaw-2", {"balance_sheet"},
                         labels={"site": "home", "agent_id": "openclaw-2"})
    rts = [rt_bal, rt_stk, rt_bond, rt_bal2]
    time.sleep(1.2)

    client = HubClient(hub.url, token=TOKEN)
    router = CapabilityRouter(client)

    try:
        print("\n### 1) 全网能力清单（协调者看到的视图）")
        cat = router.catalog()
        for cap, provs in sorted(cat["capabilities"].items()):
            names = [f"{p['agent_id']}{'' if p['alive'] else '(dead)'}" for p in provs]
            print(f"    {cap:<22} ← {names}")

        print("\n### 2) 按能力派活（协调者只认能力，不知道谁在哪台机器）")
        for label, proj, params in (
                ("平衡表", "balance_sheet", {"balance": BALANCE, "project": "balance_sheet"}),
                ("股票", "stock", {"balance": STOCK, "project": "stock"}),
                ("债券", "bond", {"balance": BOND, "project": "bond"})):
            cap = audit.role_name(proj)
            snap = router.submit(cap, "balance", params, wait=True, timeout_s=30)
            res = snap.get("result") or {}
            print(f"    {label:<4} 需要 {cap:<22} → {snap['routed_to']:<12} "
                  f"verdict={res.get('verdict')}")

        print("\n### 3) 平衡表有两台 → 轮询会分到两台")
        picks = []
        for _ in range(4):
            snap = router.submit(audit.role_name("balance_sheet"), "balance",
                                 {"balance": BALANCE, "project": "balance_sheet"},
                                 wait=True, timeout_s=30)
            picks.append(snap["routed_to"])
        print(f"    4 次派活 → {picks}")
        assert len(set(picks)) == 2, f"轮询失效：{picks}"

        print("\n### 4) 点名要某台（它不支持就报错，不静默换人）")
        try:
            router.route(audit.role_name("balance_sheet"), "balance",
                         require_agent="coze-1")
            print("    ✗ 应该报错却成功了")
        except NoCapability as e:
            print(f"    ✓ 正确拒绝：{str(e)[:100]}")

        print("\n### 5) 缺能力能提前发现")
        need = [f"{audit.role_name('balance_sheet')}/balance",
                f"{audit.role_name('bond')}/balance",
                "runner/py"]
        gap = router.gaps(need)
        print(f"    查 {need}")
        print(f"    缺：{gap['missing']}")
        need2 = need[:2]
        gap2 = router.gaps(need2)
        print(f"    只查两个审计口径 → 缺：{gap2['missing']}  ok={gap2['ok']}")

        print("\n### 6) 一条流水线的可行性预检")
        plan = router.route_plan([(audit.role_name("balance_sheet"), "balance"),
                                  (audit.role_name("bond"), "balance")])
        print(f"    ok={plan['ok']}")
        for s in plan["steps"]:
            print(f"      {s['capability']}/{s['action']} → {s['agent_id']} ({s['reason']})")

        print("\n### 7) 一台挂了 → fallback 自动换另一台（注意看 Attempts）")
        rt_bal.stop()          # 先停掉一台平衡表审计
        time.sleep(1.5)
        snap = router.submit(audit.role_name("balance_sheet"), "balance",
                             {"balance": BALANCE, "project": "balance_sheet"},
                             wait=True, timeout_s=30)
        print(f"    仍成功派到 {snap['routed_to']}，state={snap['state']}")

        print("\n=== ✅ 能力路由全部通过（who declares, who serves）")
        return 0
    finally:
        for rt in rts:
            try:
                rt.stop()
            except Exception:  # noqa: BLE001
                pass
        hub.stop()


if __name__ == "__main__":
    sys.exit(main())
