"""多空辩论的参与方实现：**规则对手 + LLM 裁决**（一个角色，三个动作）。

为什么不放进 debate.py
----------------------
`debate` 只管编排：谁先说话、几轮、花多少钱。多空双方"说什么"、裁决"怎么定"
是业务层的事。这里给一个不绑定任何业务的默认实现：

· `argue`（零 token）：按 profile 的词表把证据分成多头论据 / 空头论据，
  算权重得分，并点名"对手最强的一条"要求本方回应。同一份证据永远得到同一份发言，
  可复现、可审计——比让模型每轮自由发挥更便宜，也不会自己编数据；
· `decide`：优先走 LLM（复用 `AUDIT_LLM_*` 那个 OpenAI 兼容端点），出
  BULL/BEAR/NEUTRAL + 置信度；没配端点或解析失败就退回规则判定，
  返回里用 `judge_mode` 写清这次是 `llm` 还是 `rule`，不假装模型给过意见。

三种口径（`profile`）
--------------------
· `commodity`：库存/供需/开工/事件（平衡表口径）
· `equity`：动量/景气/估值/拥挤/资金（行业与个股口径）
· `generic`：不预设词表，全靠证据项自带 `signal`

证据格式（两种都吃）
------------------
    {"evidence": {"库存": {"value": "-8%", "signal": "bullish", "weight": 1.2},
                  "需求": "+5%"}}
    {"evidence": [{"name": "库存", "pct": -0.08, "signal": "bullish", "weight": 1.2}]}

`signal` 写 `bullish/bearish/neutral`（或「利多/利空/中性」），不写就按 profile
词表在 `name/note` 里找关键词；两处都没有就算中性，不猜。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .audit import llm_from_env

#: 证据词表：库里只放"指标名里出现就算"的词，不做同义词推理
PROFILES: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "commodity": {
        "bull": ("去库", "库存下降", "库存低位", "库存去化", "需求走强", "需求回暖",
                 "需求旺盛", "减产", "限产", "供给收紧", "供给中断", "缺口", "现货升水"),
        "bear": ("累库", "库存上升", "库存高位", "需求转弱", "需求疲软", "需求走弱",
                 "增产", "复产", "供给宽松", "供给过剩", "过剩", "现货贴水"),
    },
    "equity": {
        "bull": ("动量强", "景气上行", "盈利上修", "业绩超预期", "资金流入", "订单增长",
                 "提价", "产能利用率提升", "北向流入", "融资余额上升"),
        "bear": ("估值高", "估值分位", "拥挤", "动量转弱", "盈利下修", "业绩不及预期",
                 "资金流出", "订单下滑", "价格战", "减持", "融资余额下降"),
    },
    "generic": {"bull": (), "bear": ()},
}

SIDE_BULL = ("bull", "bullish", "long", "pro", "+", "利多", "看多", "做多", "多")
SIDE_BEAR = ("bear", "bearish", "short", "con", "-", "利空", "看空", "做空", "空")
SIDE_FLAT = ("neutral", "flat", "0", "中性", "观望")

#: 单方发言最多列几条论据（再多也读不完，且撑大 transcript）
MAX_POINTS = 8


def _profile(name: Any) -> Tuple[str, Dict[str, Tuple[str, ...]]]:
    key = str(name or "generic").strip().lower()
    if key not in PROFILES:
        raise ValueError(f"未知 profile：{key!r}（可选 {tuple(PROFILES)}）")
    return key, PROFILES[key]


def _weight(value: Any) -> float:
    try:
        w = float(value)
    except (TypeError, ValueError):
        return 1.0
    return w if w > 0 else 1.0


def _evidence(raw: Any) -> List[Dict[str, Any]]:
    """把 dict / list 两种证据写法统一成条目列表。"""
    items: List[Dict[str, Any]] = []
    if isinstance(raw, dict):
        for name, val in raw.items():
            if isinstance(val, dict):
                items.append({**val, "name": val.get("name") or name})
            elif (isinstance(val, str)
                  and val.strip().lower() in SIDE_BULL + SIDE_BEAR + SIDE_FLAT):
                # {"复产": "bearish"} 这种简写按信号读，不要当成数值
                items.append({"name": name, "signal": val})
            else:
                items.append({"name": name, "value": val})
    elif isinstance(raw, (list, tuple)):
        for entry in raw:
            if isinstance(entry, dict):
                items.append(dict(entry))
            elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                items.append({"name": entry[0], "value": entry[1]})
    out = []
    for it in items:
        name = str(it.get("name") or it.get("key") or "指标")
        out.append({
            "name": name,
            "value": it.get("value"),
            "pct": it.get("pct"),
            "signal": str(it.get("signal") or "").strip(),
            "weight": _weight(it.get("weight", 1.0)),
            "text": " ".join(str(it.get(k) or "") for k in ("name", "note", "desc")),
        })
    return out


def _side(item: Dict[str, Any], profile: Dict[str, Tuple[str, ...]]) -> str:
    """一条证据站哪边：显式 signal 优先，其次 profile 词表，都没有算中性。"""
    sig = (item.get("signal") or "").lower()
    if sig:
        if sig in SIDE_FLAT:
            return "neutral"
        if sig in SIDE_BULL:
            return "bull"
        if sig in SIDE_BEAR:
            return "bear"
    text = item.get("text", "").lower()
    if text:
        if any(kw.lower() in text for kw in profile["bull"]):
            return "bull"
        if any(kw.lower() in text for kw in profile["bear"]):
            return "bear"
    return "neutral"


def _describe(item: Dict[str, Any]) -> str:
    """一条论据的人话描述：指标名 + 数值 + 幅度（有什么写什么，不补）。"""
    text = str(item["name"])
    if item.get("value") is not None:
        text += f"={item['value']}"
    pct = item.get("pct")
    if isinstance(pct, (int, float)):
        text += f"（{pct:+.1%}）"
    elif pct:
        text += f"（{pct}）"
    return f"{text}｜权重 {item['weight']:.1f}"


def _split(evidence: List[Dict[str, Any]], profile: Dict[str, Tuple[str, ...]]
           ) -> Dict[str, List[Dict[str, Any]]]:
    buckets: Dict[str, List[Dict[str, Any]]] = {"bull": [], "bear": [], "neutral": []}
    for item in evidence:
        buckets[_side(item, profile)].append(item)
    for key in buckets:
        buckets[key].sort(key=lambda it: (-it["weight"], it["name"]))
    return buckets


def _score(items: Sequence[Dict[str, Any]]) -> float:
    return round(sum(it["weight"] for it in items), 2)


def argue(params: Dict[str, Any]) -> Dict[str, Any]:
    """一方发言（纯规则，零 token）。`side` 由 debate 传 pro/con，也认 bull/bear。"""
    raw_side = str(params.get("side") or params.get("role") or "bull").strip().lower()
    side = "bear" if raw_side in SIDE_BEAR else "bull"
    name, profile = _profile(params.get("profile"))
    buckets = _split(_evidence(params.get("evidence") or params.get("metrics")), profile)
    mine, theirs = buckets[side], buckets["bear" if side == "bull" else "bull"]
    score = _score(mine)

    label = "多头" if side == "bull" else "空头"
    lines = [f"【{label}·{name}】得分 {score:.2f}，命中 {len(mine)} 条证据"]
    lines += [f"· {_describe(it)}" for it in mine[:MAX_POINTS]]
    if not mine:
        lines.append("· 本方无可用证据（规则未命中，不编造）")
    if theirs:
        lines.append(f"【需回应的对手强证据】{_describe(theirs[0])}")
    lines.append("（规则生成，零 token；裁决由 judge 决定）")
    text = "\n".join(lines)
    return {"ok": True, "model": "rule", "profile": name, "side": side, "score": score,
            "points": [_describe(it) for it in mine[:MAX_POINTS]],
            "opponent_strongest": _describe(theirs[0]) if theirs else None,
            "n_evidence": len(buckets["bull"]) + len(buckets["bear"]) + len(buckets["neutral"]),
            "text": text}


def _judge_prompt(topic: str, transcript: List[Dict[str, Any]],
                  evidence: List[Dict[str, Any]], profile_name: str) -> str:
    rounds = []
    for item in transcript or []:
        if not isinstance(item, dict):
            continue
        rounds.append(f"R{item.get('round')} 多头：{str(item.get('pro') or '')[:800]}\n"
                      f"R{item.get('round')} 空头：{str(item.get('con') or '')[:800]}")
    ev = "\n".join(f"- {_describe(it)}" for it in evidence[:20]) or "（无结构化证据）"
    return f"""你是多空辩论的裁决官。只输出一行 JSON，不要解释、不要代码块。

辩题：{topic or "（未给辩题）"}
口径：{profile_name}

【辩论纪要（每轮双方首段）】
{chr(10).join(rounds) or "（无）"}

【结构化证据】
{ev}

要求：
1. 立场只能取 BULL / BEAR / NEUTRAL 三者之一；
2. 不要因为"两边都有道理"就一律 NEUTRAL——只有证据势均力敌或都不足时才中性；
3. confidence ∈ [0,1]，是这一判定的把握度，不是观点强度；
4. 输出格式：{{"stance":"BULL","confidence":0.6,"rationale":"一句话依据"}}"""


def _parse_json(text: Any) -> Optional[Dict[str, Any]]:
    """从模型回包里抠出第一个 JSON 对象（容忍 ```json 围栏与前后废话）。"""
    import json
    import re
    raw = str(text or "").strip()
    if not raw:
        return None
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    m = re.search(r"\{.*\}", raw, flags=re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _norm_stance(value: Any) -> Optional[str]:
    token = str(value or "").strip().upper()
    if token in ("BULL", "BUY", "OVERWEIGHT", "LONG"):
        return "BULL"
    if token in ("BEAR", "SELL", "UNDERWEIGHT", "SHORT"):
        return "BEAR"
    if token in ("NEUTRAL", "HOLD", "FLAT"):
        return "NEUTRAL"
    return None


def _rule_verdict(evidence: List[Dict[str, Any]], profile: Dict[str, Tuple[str, ...]],
                  profile_name: str) -> Dict[str, Any]:
    """没有模型时的裁决：按证据权重比大小，差距太小就中性（不硬凑方向）。"""
    buckets = _split(evidence, profile)
    bull, bear = _score(buckets["bull"]), _score(buckets["bear"])
    total = bull + bear
    if total <= 0:
        return {"ok": True, "stance": "NEUTRAL", "confidence": 0.0,
                "rationale": "没有可用的多空证据，无法定方向", "judge_mode": "rule",
                "profile": profile_name, "bull_score": bull, "bear_score": bear}
    margin = abs(bull - bear) / total
    stance = "NEUTRAL" if margin < 0.15 else ("BULL" if bull > bear else "BEAR")
    rationale = (f"多头证据权重 {bull:.2f} vs 空头 {bear:.2f}，"
                 f"差距 {margin:.0%}" + ("（势均力敌）" if stance == "NEUTRAL" else ""))
    return {"ok": True, "stance": stance, "confidence": round(margin, 2),
            "rationale": rationale, "judge_mode": "rule", "profile": profile_name,
            "bull_score": bull, "bear_score": bear}


def decide(params: Dict[str, Any], llm: Optional[Callable[[str], str]] = None) -> Dict[str, Any]:
    """裁决（LLM 优先，规则兜底）。返回里的 `judge_mode` 说明这次到底是谁判的。"""
    topic = str(params.get("topic") or "")
    profile_name, profile = _profile(params.get("profile"))
    evidence = _evidence(params.get("evidence") or params.get("metrics"))
    transcript = params.get("transcript") or []
    caller = llm if callable(llm) else llm_from_env()

    if caller is not None:
        note = ""
        try:
            parsed = _parse_json(caller(_judge_prompt(topic, transcript, evidence, profile_name)))
        except Exception as e:  # noqa: BLE001 - 端点不通就降级，不把整场辩论判死
            parsed, note = None, f"LLM 调用失败：{type(e).__name__}: {e}"
        stance = _norm_stance((parsed or {}).get("stance"))
        if stance:
            try:
                conf = float((parsed or {}).get("confidence"))
            except (TypeError, ValueError):
                conf = 0.0
            conf = min(1.0, max(0.0, conf))
            return {"ok": True, "stance": stance, "confidence": round(conf, 2),
                    "rationale": str((parsed or {}).get("rationale") or "")[:500],
                    "judge_mode": "llm", "profile": profile_name,
                    "text": f"裁决 {stance}（置信度 {conf:.2f}）"}
        note = note or "LLM 回包里没有可用 stance"
        out = _rule_verdict(evidence, profile, profile_name)
        out["degraded"] = note
        out["text"] = f"裁决 {out['stance']}（规则兜底：{note}）"
        return out

    out = _rule_verdict(evidence, profile, profile_name)
    out["text"] = f"裁决 {out['stance']}（未配置 LLM，规则判定）"
    return out


# ───────────────────────────────────────────── 流水线模板
def build_pipeline(name: str, profile: str, description: str) -> Dict[str, Any]:
    """多空辩论流水线：debate（adversarial）→ 送审台账。

    参与者全是 capability，不写 agent id：`debate` 起几个实例、`adversary`
    落在哪个 Agent 上，由角色表和 Hub 轮询决定。轮的轮数与 token 预算走
    `${params.max_rounds}` / `${params.max_tokens}`，不写死。
    """
    return {
        "name": name,
        "description": description,
        "steps": [
            {"name": "debate", "capability": "debate", "action": "run",
             "params": {"mode": "adversarial",
                        "topic": "${params.topic}",
                        "bull": {"capability": "adversary", "action": "argue"},
                        "bear": {"capability": "adversary", "action": "argue"},
                        "judge": {"capability": "adversary", "action": "decide"},
                        "max_rounds": "${params.max_rounds}",
                        "max_tokens": "${params.max_tokens}",
                        "context": {"profile": profile,
                                    "evidence": "${params.evidence}"}},
             "timeout_s": 1800},
            {"name": "ledger", "capability": "supervisor", "action": "ledger",
             "params": {"entries": "${steps.debate.result}"},
             "depends_on": ["debate"]},
        ],
    }


PIPELINES: Dict[str, Dict[str, Any]] = {
    "adversarial_debate": build_pipeline(
        "adversarial_debate", "generic",
        "通用多空辩论模板：证据进、裁决出；对手规则、裁决可接 LLM，不绑定业务"),
    "commodity_debate": build_pipeline(
        "commodity_debate", "commodity",
        "商品多空辩论：库存/供需/开工口径的证据对抗，裁决出方向"),
    "equity_debate": build_pipeline(
        "equity_debate", "equity",
        "股票多空辩论：动量/景气/估值/拥挤口径的证据对抗，裁决出方向"),
}


# ───────────────────────────────────────────── Hub 接入
def run_action(action: str, params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None,
               llm: Optional[Callable[[str], str]] = None) -> Dict[str, Any]:
    if action == "argue":
        return argue(params or {})
    if action == "decide":
        return decide(params or {}, llm=llm)
    raise ValueError(f"adversary 不支持的动作：{action}")


def register(runtime, llm: Optional[Callable[[str], str]] = None) -> None:
    """把一个 Agent 注册成多空双方的发言者与裁决官。

    ```python
    from multi_agent.integrations.adversary import register
    register(runtime)                      # 动作：adversary/argue、adversary/decide
    ```

    `argue` 零 token；`decide` 默认从 `AUDIT_LLM_*` 环境变量取端点，没配就规则判定。
    """
    runtime.add_handler("adversary", "argue", lambda p, c: run_action("argue", p, c))
    runtime.add_handler("adversary", "decide",
                        lambda p, c: run_action("decide", p, c, llm=llm))