"""规则对手 + LLM 裁决的测试：论据拆分、信号口径、裁决降级、流水线模板、与 debate 联跑。"""
from __future__ import annotations

import json

import pytest

from multi_agent.integrations import adversary as A
from multi_agent.integrations import debate


EVIDENCE = {
    "库存": {"value": "-8%", "signal": "bullish", "weight": 1.2},
    "需求": {"value": "+5%"},
    "复产": "bearish",
    # 估值高 只在 equity 词表里；这里显式给信号，让同一份证据在任一 profile 下都成立
    "估值高": {"value": 90, "pct": 0.95, "weight": 1.5, "signal": "bearish"},
}


def test_argue_splits_evidence_by_side():
    bull = A.argue({"side": "pro", "profile": "commodity", "evidence": EVIDENCE})
    bear = A.argue({"side": "con", "profile": "commodity", "evidence": EVIDENCE})
    assert bull["side"] == "bull" and bear["side"] == "bear"
    assert bull["score"] == 1.2                       # 只有「库存」站多头
    assert bear["score"] == 2.5                       # 复产 1.0 + 估值高 1.5
    assert "库存=-8%｜权重 1.2" in bull["points"]
    # 每方都要点名对手最强的一条，让本方知道该回应什么
    assert "估值高" in bull["opponent_strongest"]
    assert "库存" in bear["opponent_strongest"]


def test_argue_signal_shorthand_is_not_a_value():
    out = A.argue({"side": "bull", "evidence": {"复产": "bearish"}})
    assert out["score"] == 0 and "本方无可用证据" in out["text"]


def test_profile_keywords_route_by_name():
    assert A.argue({"side": "bear", "profile": "commodity",
                    "evidence": {"累库": {"value": 3}}})["score"] == 1.0
    assert A.argue({"side": "bear", "profile": "equity",
                    "evidence": {"拥挤": {"value": 0.9}}})["score"] == 1.0
    # generic 没有词表：不写 signal 就一律中性，不猜
    assert A.argue({"side": "bear", "profile": "generic",
                    "evidence": {"累库": {"value": 3}}})["score"] == 0


def test_unknown_profile_rejected():
    with pytest.raises(ValueError):
        A.argue({"side": "bull", "profile": "astrology", "evidence": {}})


def test_rule_verdict_picks_bigger_side_and_abstains_when_close():
    strong = {"库存": {"signal": "bullish", "weight": 2.0},
              "累库": {"signal": "bearish", "weight": 0.5}}
    out = A.decide({"profile": "commodity", "evidence": strong}, llm=None)
    assert out["stance"] == "BULL" and out["judge_mode"] == "rule"
    assert out["confidence"] == 0.6

    close = {"库存": {"signal": "bullish", "weight": 1.2},
             "复产": {"signal": "bearish", "weight": 1.0}}
    assert A.decide({"profile": "commodity", "evidence": close}, llm=None)["stance"] \
        == "NEUTRAL"
    assert A.decide({"profile": "commodity", "evidence": {}}, llm=None)["confidence"] == 0.0


def test_llm_judge_is_used_when_endpoint_present():
    seen = {}

    def fake_llm(prompt: str) -> str:
        seen["prompt"] = prompt
        return '```json\n{"stance": "bear", "confidence": 0.71, "rationale": "库存仍高"}\n```'

    out = A.decide({"topic": "铜价方向", "profile": "commodity", "evidence": EVIDENCE},
                   llm=fake_llm)
    assert out["judge_mode"] == "llm" and out["stance"] == "BEAR"
    assert out["confidence"] == 0.71 and out["rationale"] == "库存仍高"
    assert "铜价方向" in seen["prompt"] and "只输出一行 JSON" in seen["prompt"]


def test_llm_garbage_falls_back_to_rule_with_note():
    out = A.decide({"profile": "commodity", "evidence": EVIDENCE},
                   llm=lambda p: "我觉得还行吧")
    assert out["judge_mode"] == "rule" and out.get("degraded")
    assert out["text"].startswith("裁决")


def test_llm_exception_falls_back_to_rule():
    def boom(prompt: str) -> str:
        raise OSError("endpoint down")

    out = A.decide({"profile": "commodity", "evidence": EVIDENCE}, llm=boom)
    assert out["judge_mode"] == "rule" and "endpoint down" in out["degraded"]


def test_register_exposes_both_actions():
    handlers = {}

    class Runtime:
        def add_handler(self, cap, action, fn):
            handlers[(cap, action)] = fn

    A.register(Runtime())
    assert set(handlers) == {("adversary", "argue"), ("adversary", "decide")}
    assert handlers[("adversary", "argue")]({"side": "bull", "evidence": EVIDENCE}, {})["ok"]


def test_pipeline_presets_are_role_based():
    assert set(A.PIPELINES) == {"adversarial_debate", "commodity_debate", "equity_debate"}
    steps = A.PIPELINES["commodity_debate"]["steps"]
    assert [s["name"] for s in steps] == ["debate", "ledger"]
    debate_params = steps[0]["params"]
    assert debate_params["mode"] == "adversarial"
    # 参与者只写 capability/action，没有任何 agent id
    for side in ("bull", "bear", "judge"):
        assert set(debate_params[side]) == {"capability", "action"}
        assert debate_params[side]["capability"] == "adversary"
    assert debate_params["context"]["profile"] == "commodity"
    assert steps[1]["depends_on"] == ["debate"]


class _AdversaryClient:
    """把 debate 的派活直接转成 adversary 的真函数（不经 Hub）。"""

    def __init__(self, llm=None):
        self.llm = llm
        self.calls = []

    def submit(self, capability, action, params=None, **kw):
        params = params or {}
        self.calls.append((capability, action, params))
        if action == "argue":
            return {"state": "succeeded", "result": A.argue(params)}
        if action == "decide":
            return {"state": "succeeded", "result": A.decide(params, llm=self.llm)}
        return {"state": "failed", "error": "unknown"}


def test_debate_engine_with_rule_adversary_end_to_end():
    client = _AdversaryClient()
    out = debate.DebateRunner(client=client).run({
        "mode": "adversarial", "topic": "铜价方向",
        "bull": {"capability": "adversary", "action": "argue"},
        "bear": {"capability": "adversary", "action": "argue"},
        "judge": {"capability": "adversary", "action": "decide"},
        "max_rounds": 2, "max_tokens": 200000,
        "context": {"profile": "commodity", "evidence": EVIDENCE},
    })
    assert out["ok"] and out["rounds"] == 2 and out["turns"] == 4
    assert out["decision"] == "BEAR"          # 空头权重 2.5 > 多头 1.2
    assert out["stopped_reason"] == "judged"
    # 争论双方都拿到了同一份证据，但站的边不同
    bull_call = [c for c in client.calls if c[1] == "argue"][0][2]
    assert bull_call["side"] == "pro" and bull_call["evidence"]["库存"]
    judge_call = client.calls[-1][2]
    assert judge_call["topic"] == "铜价方向" and len(judge_call["transcript"]) == 2


def test_debate_engine_with_llm_judge():
    def llm(prompt: str) -> str:
        return json.dumps({"stance": "NEUTRAL", "confidence": 0.4, "rationale": "证据互抵"})

    client = _AdversaryClient(llm=llm)
    out = debate.DebateRunner(client=client).run({
        "mode": "adversarial", "topic": "t",
        "bull": {"capability": "adversary", "action": "argue"},
        "bear": {"capability": "adversary", "action": "argue"},
        "judge": {"capability": "adversary", "action": "decide"},
        "max_rounds": 1, "max_tokens": 200000,
        "context": {"profile": "equity", "evidence": EVIDENCE},
    })
    assert out["decision"] == "NEUTRAL" and out["tokens_used"] > 0