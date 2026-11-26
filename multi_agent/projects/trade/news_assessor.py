"""新闻评估项目：只干一件事——判断"这条新闻算不算大事、往哪个方向压"。

和采集官的分工：采集官只说"发生了什么"，不判方向；评估官才判
`major`（是否够大）与 `impact`（供给收缩偏多 / 供给宽松偏空），并给出置信度。
全程规则，零 token；规则表改这里，不动采集与平衡表。

输出：

    {"ok": true, "major": bool, "items": [{event_id, tag, impact, weight, why}],
     "counts": {"events": n, "major": k}, "degraded": false}
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..base import RoleProject

#: 规则表：命中关键词 → (tag, impact, 权重)。新增品种/事件类型加一行即可。
_RULES = (
    (("矿难", "停产", "罢工", "事故", "不可抗力"), "supply_shock", "bull", 0.9),
    (("配额", "减产", "限产", "收储"), "supply_cut", "bull", 0.7),
    (("制裁", "关税", "禁运"), "trade_shock", "bull", 0.6),
    (("增产", "复产", "抛储", "累库"), "supply_up", "bear", 0.6),
    (("需求走弱", "砍单", "去库放缓"), "demand_down", "bear", 0.6),
    (("旺季", "补库", "订单回暖"), "demand_up", "bull", 0.4),
    # 信用侧：AI 资本开支靠债融资，CDS 走阔 / 评级下调是利空（不是商品供需）
    (("CDS", "信用违约", "违约概率", "评级下调", "降级", "信用利差"),
     "credit_stress", "bear", 0.8),
)

#: 达到这个权重才算"大事"（下游据此决定要不要进测算）
MAJOR_WEIGHT = 0.6

#: 反向终止词：命中"罢工/事故"的同时出现"结束/达成协议/恢复生产"，方向要反过来。
#: 只列真正表示"冲击已终止"的词——"复产时间待评估"这类没复产的不能算。
_REVERSAL = ("结束", "达成协议", "解除", "复工", "恢复生产", "恢复正常")
#: 只翻转供给端利多的规则；需求类规则本来就不该被这些词影响
_FLIP_TAGS = ("supply_shock", "supply_cut")
#: 翻转后的权重上限：方向变了，力度也要降，避免一条"结束"新闻压过真冲击
FLIP_WEIGHT = 0.5


def assess(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    params = params or {}
    payload = params.get("events")
    if isinstance(payload, dict):
        events = payload.get("events") or []
        upstream_degraded = bool(payload.get("degraded"))
        upstream_reason = payload.get("reason") or ""
    elif isinstance(payload, list):
        events, upstream_degraded, upstream_reason = payload, False, ""
    else:
        events, upstream_degraded, upstream_reason = [], True, "上游没有 events"

    if not events:
        return {"ok": False, "degraded": True,
                "reason": upstream_reason or "没有可评估的事件",
                "major": False, "items": [], "counts": {"events": 0, "major": 0}}

    # 经理调参：权重 / 大事阈值 / 最低级别（`supervisor/tune` 的产出）
    tuning = params.get("tuning") if isinstance(params.get("tuning"), dict) else {}
    weights = {str(k): float(v) for k, v in (tuning.get("weights") or {}).items()}
    try:
        major_w = float(tuning.get("major_weight") if tuning.get("major_weight") is not None
                        else MAJOR_WEIGHT)
    except (TypeError, ValueError):
        major_w = MAJOR_WEIGHT
    min_level = str(tuning.get("min_level") or "info")
    _rank = {"info": 0, "warn": 1, "error": 2, "critical": 3}
    min_rank = _rank.get(min_level, 0)

    items: List[Dict[str, Any]] = []
    for e in events:
        if not isinstance(e, dict):
            continue
        if _rank.get(str(e.get("level") or "info"), 0) < min_rank:
            continue
        text = f"{e.get('title') or ''} {e.get('summary') or e.get('text') or ''}"
        best: Optional[Dict[str, Any]] = None
        for words, tag, impact, weight in _RULES:
            hit = [w for w in words if w in text]
            if not hit:
                continue
            w = float(weights.get(tag, weight))
            if best is None or w > float(best["weight"]):
                best = {"tag": tag, "impact": impact, "weight": w,
                        "why": "命中关键词：" + "、".join(hit)}
        if best is None:
            continue
        # 反向终止词：方向翻转、权重降档（"罢工结束、恢复生产"不该算利多）
        if (best["tag"] in _FLIP_TAGS and best["impact"] == "bull"
                and any(k in text for k in _REVERSAL)):
            best = {**best, "impact": "bear",
                    "weight": min(float(best["weight"]), FLIP_WEIGHT),
                    "why": best["why"] + "（含结束/恢复信号，方向翻转）"}
        items.append({"event_id": e.get("event_id") or "", **best})

    items.sort(key=lambda x: -float(x.get("weight") or 0))
    max_w = float(items[0]["weight"]) if items else 0.0
    major = max_w >= major_w
    return {"ok": bool(items), "degraded": not items,
            "reason": "" if items else "规则表没命中任何事件（不硬判）",
            "major": major,
            "lead": items[0] if items else None,
            "items": items,
            "counts": {"events": len(events), "major": sum(
                1 for i in items if float(i.get("weight") or 0) >= major_w)},
            "tuning": {"weights": weights, "major_weight": major_w,
                       "min_level": min_level}}


class NewsAssessProject(RoleProject):
    name = "news-assessor"
    title = "新闻评估官"
    description = "事件 → 是否大事 / 方向倾向（规则表，零 token）"
    capability = "assess"
    actions = ("classify",)
    role_description = "评估：规则表判 major 与 impact，不命中就不硬判"

    def handlers(self) -> Dict[str, Dict[str, Any]]:
        return {"assess": {"classify": lambda p, c: assess(p or {}, c)}}