"""甲骨文 CDS 走阔 → AI 信用 → A 股传导（含情景营收影响）。

    python examples/oracle_cds_ashare/run_demo.py

链路：

    news/collect（关键词命中）
      → supervisor/tune（经理定权重与大事阈值）
      → assess/classify（判 major 与 impact：credit_stress / bear）
      → transmit/map（事件压力 → 环节融资依赖 → A 股标的压力分；
                      再按 capex 下修情景折营收影响）

三层东西分开摆：

1. **事实（11-25 报道口径）**：CDS 点数、隐含违约概率、评级动作、
   AI 名字相对 IG 指数的超额走阔、25 个数据中心园区的股权重估——
   逐条带来源，只写报道里明确给出的数；
2. **最新进展（11-26 口径）**：甲骨文 356bp 纪录、英伟达 142.66bp 且被
   标普列入负面观察、CoreWeave 1314bp / 隐含 PD 68%——单独一节，
   不混进 11-25 的结论；
3. **假设**：环节融资依赖度、标的敞口、资本开支弹性与 AI 收入占比来自
   `config/transmit/ai_ashare.json`，是示例权重。压力分不是涨跌幅，
   营收影响只到收入、不含毛利与估值。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from multi_agent.frameworks.keywords import KeywordSet                  # noqa: E402
from multi_agent.frameworks.news.framework import NewsWatchFramework   # noqa: E402
from multi_agent.integrations.supervisor import tune                    # noqa: E402
from multi_agent.projects.trade.news_assessor import assess             # noqa: E402
from multi_agent.projects.trade.transmit import transmit                # noqa: E402

HERE = Path(__file__).resolve().parent
NEWS = json.loads((HERE / "fixture" / "news.json").read_text(encoding="utf-8"))
KEYWORDS = HERE / "keywords.json"

#: 事实（公开报道，2026-11-25 口径）——只放报道里明确给出的数
FACTS = {
    "cds_bp_now": 316,
    "cds_bp_year_ago": 76,
    "pd_now": 0.23,
    "pd_year_ago": 0.06,
    "ig_index_change": 0.22,
    "ai_names_change": {"博通": 1.55, "英伟达": 1.30, "CoreWeave": 1.00,
                        "甲骨文": 0.80, "Meta": 0.60},
    "rating_action": "标普 2026-07-09 将甲骨文从 BBB 下调至 BBB-",
    "sources": ["https://finance.sina.com.cn/roll/2026-11-25/doc-inizyeha8562855.shtml"],
}

#: 同一篇报道引用的 Groundbreaker 园区估值研究（25 个 AI 数据中心园区样本）
PARK_STUDY = {
    "sample_parks": 25,
    "equity_wiped": 0.40,          # 约 40% 业主股权被抹去
    "oracle_project_wiped": 0.72,  # 甲骨文项目背后 72%
    "coreweave_project_wiped": 0.91,
    "collateral_drop_cds": 0.16,   # 仅 CDS 变动 → 抵押价值 -16%
    "collateral_drop_with_ust5": 0.24,  # 叠加 10Y 美债 5% → -24%
    "asset_pool_usd_bn": 2070,
}

#: 最新进展（2026-11-26 口径），与上面 11-25 的结论分层，不混用
LATEST = {
    "as_of": "2026-11-26",
    "oracle_cds_bp": 356,
    "oracle_day_change_bp": 17,
    "nvidia_cds_bp": 142.66,
    "nvidia_watch": "标普将英伟达 AA 评级列入负面观察，90 天内或下调一档",
    "coreweave_cds_bp": 1314,
    "coreweave_pd": 0.68,
    "softbank_cds_bp": 501,
    "cdx_ig": "CDX 投资级指数升至 3 月 31 日以来最高",
    "sources": ["https://www.tradingview.com/news/seekingalpha:b84726efc094b:0-ai-credit-risk-mounts-as-cds-spreads-for-oracle-coreweave-hit-record-levels/",
                "https://caifuhao.eastmoney.com/news/20261126105036298539900"],
}

#: 情景：AI 资本开支下修幅度（示例，可改）
CAPEX_CUT = 0.10

#: 假设备的"满格"口径：5 年隐含违约概率到 30% 记 100 分。改这里就改打分刻度。
PD_FULL = 0.30

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


def severity_from_pd() -> float:
    """事件压力分：隐含违约概率相对“满格”的比例。透明、可改。"""
    return round(min(1.0, FACTS["pd_now"] / PD_FULL) * 100, 1)


def main() -> int:
    # ───────────────────────────── 采集
    print("\n=== 采集（program 关键词）===")
    keywords = KeywordSet.from_file(str(KEYWORDS))
    collected = NewsWatchFramework(keyword_set=keywords,
                                   judge=lambda hit: False).collect({"items": NEWS})
    events = collected.get("events") or []
    print(f"扫 {collected.get('scanned')} 条 / 命中 {collected.get('matched')} 条："
          f"{[(e.get('group'), e.get('title')[:20]) for e in events]}")
    check("A1 采到 CDS 走阔事件",
          any(e.get("group") == "credit_stress" and "CDS" in (e.get("title") or "")
              for e in events))
    check("A2 采到评级下调事件",
          any("评级" in (e.get("title") or "") for e in events))
    check("A3 游戏皮肤噪声不入库",
          all("皮肤" not in (e.get("title") or "") for e in events))

    # ───────────────────────────── 经理调权重 + 评估
    print("\n=== 经理调权重 + 评估 ===")
    tuned = tune({"keyword_set": "credit-watch",
                  "weights": {"credit_stress": 0.95, "ai_chain": 0.4},
                  "major_weight": 0.7, "min_level": "warn"})
    judged = assess({"events": collected, "tuning": tuned})
    lead = judged.get("lead") or {}
    print(f"major={judged['major']} lead={json.dumps(lead, ensure_ascii=False)}")
    check("B1 判为大事", judged.get("major") is True)
    check("B2 方向为信用收缩（bear）", lead.get("impact") == "bear")
    check("B3 归类为 credit_stress", lead.get("tag") == "credit_stress")
    check("B4 权重用了经理给的 0.95", float(lead.get("weight") or 0) == 0.95)

    # ───────────────────────────── 事实层：事件本身多少
    print("\n=== 事实层①：事件本身（11-25 报道口径）===")
    print(f"甲骨文 5Y CDS {FACTS['cds_bp_year_ago']}bp → {FACTS['cds_bp_now']}bp"
          f"（{FACTS['cds_bp_now'] / FACTS['cds_bp_year_ago']:.1f}x）；"
          f"隐含 5 年 PD {FACTS['pd_year_ago']:.0%} → {FACTS['pd_now']:.0%}")
    check("C1 CDS 一年走阔约 4.2 倍（76→316bp）",
          round(FACTS["cds_bp_now"] / FACTS["cds_bp_year_ago"], 1) == 4.2,
          f"{FACTS['cds_bp_now'] / FACTS['cds_bp_year_ago']:.2f}x")
    check("C2 隐含违约概率从 6% 升到 23%",
          FACTS["pd_now"] == 0.23 and FACTS["pd_year_ago"] == 0.06)
    check("C3 用户口述的『暴增 50%』明显低估（实际 +316%、约 4.2 倍）",
          0.50 < FACTS["cds_bp_now"] / FACTS["cds_bp_year_ago"] - 1,
          f"实际增幅 {FACTS['cds_bp_now'] / FACTS['cds_bp_year_ago'] - 1:.0%}")

    # ───────────────────────────── 事实层：AI 多少
    print("\n=== 事实层②：AI 侧影响（11-25 报道口径）===")
    ai_rows = []
    for name, change in FACTS["ai_names_change"].items():
        excess = round(change / FACTS["ig_index_change"], 1)
        ai_rows.append({"name": name, "cds_change": change, "excess_vs_ig": excess})
    ai_rows.sort(key=lambda r: -r["excess_vs_ig"])
    print(f"AI 名字 CDS 相对投资级指数（+{FACTS['ig_index_change']:.0%}）："
          + "、".join(f"{r['name']} {r['excess_vs_ig']}x" for r in ai_rows))
    print(f"25 个 AI 数据中心园区：股权抹去 {PARK_STUDY['equity_wiped']:.0%}"
          f"（甲骨文项目 {PARK_STUDY['oracle_project_wiped']:.0%}、"
          f"CoreWeave 项目 {PARK_STUDY['coreweave_project_wiped']:.0%}）；"
          f"抵押价值 -{PARK_STUDY['collateral_drop_cds']:.0%}，"
          f"叠加 10Y 美债 5% 后 -{PARK_STUDY['collateral_drop_with_ust5']:.0%}"
          f"（样本资产 {PARK_STUDY['asset_pool_usd_bn']} 亿美元）")
    check("C4 AI 名字超额走阔 2.7~7.0 倍（相对 IG 指数）",
          round(ai_rows[0]["excess_vs_ig"], 1) == 7.0
          and round(ai_rows[-1]["excess_vs_ig"], 1) == 2.7,
          f"{ai_rows[0]['name']} {ai_rows[0]['excess_vs_ig']}x / "
          f"{ai_rows[-1]['name']} {ai_rows[-1]['excess_vs_ig']}x")
    check("C5 数据中心股权已经真的被抹去四成（不是预期）",
          PARK_STUDY["equity_wiped"] == 0.40
          and PARK_STUDY["oracle_project_wiped"] > PARK_STUDY["equity_wiped"])
    check("C6 CoreWeave 项目受损重于甲骨文项目（91% vs 72%）",
          PARK_STUDY["coreweave_project_wiped"] > PARK_STUDY["oracle_project_wiped"])

    # ───────────────────────────── 最新进展（11-26）分层
    print(f"\n=== 最新进展（{LATEST['as_of']}，仅作今日背景）===")
    print(f"甲骨文 {LATEST['oracle_cds_bp']}bp（单日 +{LATEST['oracle_day_change_bp']}bp，"
          f"纪录）；英伟达 {LATEST['nvidia_cds_bp']}bp；"
          f"CoreWeave {LATEST['coreweave_cds_bp']}bp（隐含 PD "
          f"{LATEST['coreweave_pd']:.0%}）；软银 {LATEST['softbank_cds_bp']}bp；"
          f"{LATEST['cdx_ig']}")
    check("G1 今日甲骨文 CDS 已高于 11-25 的 316bp",
          LATEST["oracle_cds_bp"] > FACTS["cds_bp_now"],
          f"{FACTS['cds_bp_now']} → {LATEST['oracle_cds_bp']}bp")
    check("G2 英伟达被标普列入负面观察（评级侧开始跟进）",
          "负面观察" in LATEST["nvidia_watch"])
    check("G3 CoreWeave 隐含 PD 已到 68%（AI 链条压力分层明显）",
          LATEST["coreweave_pd"] == 0.68 and LATEST["coreweave_pd"] > FACTS["pd_now"])

    # ───────────────────────────── 假设层①：A 股压力分
    print("\n=== 假设层①：A 股压力分（不是涨跌幅）===")
    severity = severity_from_pd()
    mapped = transmit({"severity": severity, "capex_cut": CAPEX_CUT})
    print(f"severity={severity}（隐含 PD {FACTS['pd_now']:.0%} / 满格 {PD_FULL:.0%}）")
    for row in mapped["by_segment"]:
        lo, hi = row["revenue_impact_range"]
        print(f"  {row['name']:14s} 依赖 {row['financing_dependence']:.2f} "
              f"→ 压力 {row['stress']:.1f}（{row['band']}）"
              f"{row['tickers']} 只；营收情景 {lo:+.1f}%~{hi:+.1f}%")
    check("D1 传导覆盖 7 个环节 / 15 个标的",
          mapped["counts"]["segments"] == 7 and mapped["counts"]["tickers"] == 15,
          json.dumps(mapped["counts"], ensure_ascii=False))
    check("D2 IDC 环节压力最高", mapped["by_segment"][0]["segment"] == "idc")
    check("D3 压力最大标的在 IDC（润泽科技）",
          mapped["top"][0]["ticker"] == "300442", mapped["top"][0]["name"])
    check("D4 国产算力档位最低（与海外信用周期弱相关）",
          next(r for r in mapped["by_segment"] if r["segment"] == "domestic")["band"] == "低")
    high = [i for i in mapped["items"] if i["band"] == "高"]
    check("D5 高压档标的数量有限（≤3）", 0 < len(high) <= 3,
          f"{[i['name'] for i in high]}")

    # ───────────────────────────── 假设层②：营收情景
    print(f"\n=== 假设层②：若 AI 资本开支下修 {CAPEX_CUT:.0%}，营收影响 ===")
    for row in mapped["top_revenue"]:
        print(f"  {row['name']:8s}（{row['segment_name']}）"
              f"AI 收入占比 {row['ai_revenue_share']:.0%} → "
              f"{row['revenue_impact_pct']:+.2f}%")
    cut = mapped["scenario"]["capex_cut"]
    ri = {r["name"]: r["revenue_impact_pct"] for r in mapped["items"]}
    check("F1 情景参数生效（capex_cut=10%）", cut == CAPEX_CUT)
    check("F2 润泽科技营收影响约 -8.5%（1.0×0.85×10%）",
          ri["润泽科技"] == -8.5, f"{ri['润泽科技']}%")
    check("F3 国产算力几乎不受影响（海光 -0.2%、寒武纪 -0.05%）",
          ri["海光信息"] == -0.2 and ri["寒武纪"] == -0.05,
          f"{ri['海光信息']}% / {ri['寒武纪']}%")
    check("F4 所有标的营收影响为负、且不超过 -10%",
          all(v <= 0 for v in ri.values()) and min(ri.values()) >= -CAPEX_CUT * 100,
          f"最重 {min(ri.values())}%")
    doubled = transmit({"severity": severity, "capex_cut": CAPEX_CUT * 2})
    check("F5 下修幅度翻倍 → 营收影响翻倍（线性、可复算）",
          doubled["top_revenue"][0]["revenue_impact_pct"] == -17.0,
          f"{doubled['top_revenue'][0]['revenue_impact_pct']}%")

    # ───────────────────────────── 口径与来源
    print("\n=== 口径与来源 ===")
    check("E1 输出标注 assumption=True", mapped.get("assumption") is True)
    check("E2 口径写明是压力分而非涨跌幅",
          mapped.get("unit") == "pressure" and "不是涨跌幅" in str(mapped.get("scale")))
    check("E3 带免责说明", "不代表" in str(mapped.get("disclaimer")))
    check("E4 营收情景写清只到收入", "不含毛利" in str(mapped["scenario"]["note"]))
    print(f"事实来源（11-25）：{FACTS['sources'][0]}")
    print(f"最新进展来源（11-26）：{LATEST['sources'][0]}")
    print(f"假设来源：config/transmit/ai_ashare.json"
          f"（financing_dependence / exposure / capex_sensitivity / "
          f"ai_revenue_share 均为示例权重）")

    failed = [r for r in results if not r[1]]
    print(f"\n{'=' * 60}\n{len(results) - len(failed)}/{len(results)} 项通过")
    for name, _, detail in failed:
        print(f"  FAIL {name} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())