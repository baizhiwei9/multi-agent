"""印尼矿难：一条新闻进平衡表 —— 采集 → 经理调权重 → 评估 → 平衡表。

    python examples/indonesia_mine_disaster/run_demo.py

两段对照：

1. **程序关键词**（judge=False）：矿难/配额/罢工进链路，需求侧 llm 组不采；
2. **模型复核**（judge=True）：多采到"需求走弱"这条模糊事件。

平衡表也给两段：

- 不注入 executor → 明确 `degraded`，不自己估数；
- 注入演示 executor → 供给下调、缺口扩大，输出标 `source=fixture-demo-executor`。

演示基线不是实盘数据，真实口径在 BalanceAI 侧。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

from multi_agent.frameworks.keywords import KeywordSet                   # noqa: E402
from multi_agent.frameworks.news.framework import NewsWatchFramework    # noqa: E402
from multi_agent.integrations.supervisor import tune                     # noqa: E402
from multi_agent.projects.trade.balance_sheet import BalanceSheetProject  # noqa: E402
from multi_agent.projects.trade.news_assessor import assess              # noqa: E402

HERE = Path(__file__).resolve().parent
NEWS = json.loads((HERE / "fixture" / "news.json").read_text(encoding="utf-8"))
BASE = json.loads((HERE / "fixture" / "base_balance.json").read_text(encoding="utf-8"))
KEYWORDS = HERE / "keywords.json"

results = []
notes = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


def note(name: str, detail: str = "") -> None:
    notes.append((name, detail))
    print(f"[NOTE] {name}" + (f" —— {detail}" if detail else ""))


def collect(judge):
    keywords = KeywordSet.from_file(str(KEYWORDS))
    framework = NewsWatchFramework(keyword_set=keywords, judge=judge)
    return framework.collect({"items": NEWS})


def demo_executor(params, ctx=None):
    """演示 executor：按事件方向调供给，**不是**实盘测算。

    只做一件事：供给冲击按固定比例下调供给，其余不动。目的是验证
    "评估结果 → 平衡表变化"这条形状通不通，不声称任何数量结论。
    """
    events = ((params or {}).get("assess") or {}).get("items") or []
    ratio = {"supply_shock": 0.003, "supply_cut": 0.001}
    supply = float(BASE["supply"])
    changes = []
    for item in events:
        tag = str(item.get("tag") or "")
        if item.get("impact") != "bull" or tag not in ratio:
            continue
        before = supply
        supply = round(supply * (1 - ratio[tag]), 2)
        changes.append({"tag": tag, "event_id": item.get("event_id"),
                        "supply_before": before, "supply_after": supply,
                        "why": item.get("why")})
    demand = float(BASE["demand"])
    return {
        "ok": True, "degraded": False, "commodity": BASE["commodity"],
        "period": BASE["period"], "supply": supply, "demand": demand,
        "balance": round(supply - demand, 2),
        "inventory_days": round(float(BASE["inventory_days"]) * supply / float(BASE["supply"]), 2),
        "changes": changes,
        "source": "fixture-demo-executor",
        "baseline": BASE["source"],
        "assumption": "供给冲击按 0.3% / 配额按 0.1% 下调，仅验证链路形状，非实盘结论",
    }


def main() -> int:
    # ───────────────────────────── 采集：程序模式
    print("\n=== 采集（程序关键词，零 token）===")
    prog = collect(lambda hit: False)
    events = prog.get("events") or []
    print(f"扫 {prog.get('scanned')} 条 / 命中 {prog.get('matched')} 条："
          f"{[(e.get('group'), e.get('title')[:18]) for e in events]}")
    groups = [e.get("group") for e in events]
    check("A1 采到印尼矿难（supply_shock）",
          any(e.get("group") == "supply_shock" and "Grasberg" in e.get("title", "") for e in events))
    check("A2 采到印尼镍矿配额（supply_cut）",
          any(e.get("group") == "supply_cut" for e in events))
    check("A3 天气噪声没有入库",
          all("天气" not in (e.get("title") or "") for e in events))
    check("A4 需求侧（llm 组）程序模式不采",
          "demand_shift" not in groups, str(groups))
    check("A5 采集官不判方向", all(e.get("impact") is None for e in events))

    # ───────────────────────────── 采集：模型复核
    print("\n=== 采集（模型复核 llm 组）===")
    reviewed = collect(lambda hit: True)
    events2 = reviewed.get("events") or []
    print(f"出 {len(events2)} 条：{[(e.get('group'), e.get('title')[:18]) for e in events2]}")
    check("B1 复核后多采到需求走弱",
          any(e.get("group") == "demand_shift" for e in events2))

    # ───────────────────────────── 经理调权重
    print("\n=== 经理调权重（supervisor/tune）===")
    tuned = tune({"keyword_set": "mine-watch",
                  "weights": {"supply_shock": 0.95, "supply_cut": 0.7, "demand_shift": 0.6},
                  "major_weight": 0.8, "min_level": "warn"})
    # `tune` 默认按 news 表给组名；显式 coverage 以参数为准
    print(json.dumps(tuned, ensure_ascii=False))
    check("C1 supply_shock 权重被经理覆盖为 0.95",
          float(tuned["weights"].get("supply_shock") or 0) == 0.95)
    check("C2 大事阈值 0.8", float(tuned["major_weight"]) == 0.8)
    check("C3 最低级别 warn", tuned["min_level"] == "warn")

    # ───────────────────────────── 评估
    print("\n=== 评估（assess/classify）===")
    judged = assess({"events": prog, "tuning": tuned})
    print(f"major={judged['major']} lead={json.dumps(judged.get('lead'), ensure_ascii=False)}")
    lead = judged.get("lead") or {}
    check("D1 印尼矿难判为大事（major=True）", judged.get("major") is True)
    check("D2 方向判为供给收缩/偏多（bull）", lead.get("impact") == "bull")
    check("D3 权重用了经理给的 0.95", float(lead.get("weight") or 0) == 0.95)
    check("D4 min_level=warn 把 info 级需求事件滤掉",
          all(float(i.get("weight") or 0) != 0.6 or i.get("tag") != "demand_down"
              for i in judged.get("items") or []),
          str([i.get("tag") for i in judged.get("items") or []]))

    # 规则表的已知缺陷：只看词，不看语义
    # 反向终止词：罢工结束、恢复生产 → 方向必须翻转
    strike = next((i for i in judged.get("items") or []
                   if "罢工" in str(i.get("why") or "")), None)
    check("E2 智利『罢工结束、恢复生产』已翻转为偏空（bear）",
          strike is not None and strike.get("impact") == "bear"
          and float(strike.get("weight") or 0) <= 0.5,
          json.dumps(strike, ensure_ascii=False))

    # ───────────────────────────── 平衡表：未注入
    print("\n=== 平衡表（未注入 executor）===")
    bare = BalanceSheetProject().handlers()["balance"]["build"](
        {"commodity": BASE["commodity"]}, {})
    print(json.dumps(bare, ensure_ascii=False))
    check("E1 未注入 executor 时明确 degraded，不自己估数",
          bare.get("degraded") is True and "BalanceAI" in str(bare.get("reason") or ""))

    # ───────────────────────────── 平衡表：注入演示 executor
    print("\n=== 平衡表（注入演示 executor）===")
    project = BalanceSheetProject(executor=lambda cap, action, params, ctx:
                                  demo_executor(params, ctx))
    built = project.handlers()["balance"]["build"](
        {"commodity": BASE["commodity"], "assess": judged}, {})
    print(json.dumps(built, ensure_ascii=False, indent=2))
    check("F1 供给下调", float(built["supply"]) < float(BASE["supply"]),
          f"{BASE['supply']} → {built['supply']}")
    check("F2 缺口扩大（balance 更负）", float(built["balance"]) < float(BASE["balance"]),
          f"{BASE['balance']} → {built['balance']}")
    check("F3 库存天数下降", float(built["inventory_days"]) < float(BASE["inventory_days"]),
          f"{BASE['inventory_days']} → {built['inventory_days']}")
    check("F4 产出标注演示来源，不冒充实盘",
          built.get("source") == "fixture-demo-executor"
          and "非实盘" in str(built.get("assumption") or ""))
    check("F5 平衡表变化方向与评估方向一致（bull → 供给收缩）",
          all(c.get("supply_after", 0) <= c.get("supply_before", 0)
              for c in built.get("changes") or [])
          and bool(built.get("changes")))

    # ───────────────────────────── 汇总
    failed = [r for r in results if not r[1]]
    print(f"\n{'=' * 60}\n{len(results) - len(failed)}/{len(results)} 项通过，{len(notes)} 条已知问题")
    for name, detail in notes:
        print(f"  NOTE {name} {detail}")
    for name, _, detail in failed:
        print(f"  FAIL {name} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())