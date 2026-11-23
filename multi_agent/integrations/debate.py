"""协调官初筛（`moderator/screen`）与多轮评议（`debate/run`）。

用户要的流程
-----------
    多路新闻采集官 → 协调官初筛（今日有没有大事 / 初轮观点）
        → 审计官  ⇄  采集官（多轮互相点评，默认最多 10 轮）
        → 报告官 + 风控官 → 终审

两条硬约束（数字都在 `config/limits.json`，见 docs/LIMITS.md）：
· 单条消息最多 `debate.max_message_chars` 字（审计官输入/输出都是 5000）；
· 全流程最多 `debate.max_total_chars` 字、最多 `debate.max_rounds` 轮，超了停并说明原因。

"晚上没有重大新闻"不是异常：协调官给 `no_news=true`，评议器直接把状态记成
`SKIPPED`（`stopped_reason=no_major_news`），让报告官按"今日无重大新闻"出稿，
而不是让审计官对着空观点编点评。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..limits import load_limits, truncate

#: 判定"重大事件"的关键词（可由 step 的 params.major_keywords 覆盖）
DEFAULT_MAJOR_KEYWORDS = (
    "矿难", "罢工", "制裁", "禁运", "地震", "洪水", "战争", "停产", "爆炸",
    "出口禁令", "关税", "收储", "抛储", "减产协议", "限产",
)

_TEXT_KEYS = ("draft", "opinion", "text", "markdown", "md", "content", "report",
              "brief", "verdict", "audit", "critique", "comment", "feedback")


def _items(value: Any) -> List[Dict[str, Any]]:
    """把一路新闻结果摊平成事件列表（容忍 dict/list 两种回包）。"""
    if isinstance(value, dict):
        value = (value.get("events") or value.get("items")
                 or value.get("news") or value.get("data") or [])
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, dict)]


def screen(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """协调官初筛：合并多路新闻 → 判有无大事 → 给出待议观点（零 token）。

    params: `{balance_news?, event_news?, major_keywords?, max_topics?}`
    """
    lim = load_limits()
    balance_news = _items(params.get("balance_news"))
    event_news = _items(params.get("event_news"))
    keywords = tuple(params.get("major_keywords") or DEFAULT_MAJOR_KEYWORDS)
    merged = balance_news + event_news

    def _hit(e: Dict[str, Any]) -> bool:
        impact = str(e.get("impact") or "").lower()
        if impact in ("high", "major"):
            return True
        blob = " ".join(str(e.get(k) or "") for k in ("title", "summary", "content"))
        return any(k in blob for k in keywords)

    major = [e for e in merged if _hit(e)]
    no_news = not merged
    limit_topics = int(params.get("max_topics") or lim.news.max_topics)
    topics = (major or merged)[:limit_topics]
    summary = ("今日无重大新闻，不输出观点" if no_news else
               f"初筛 {len(merged)} 条（重大 {len(major)} 条），待议 {len(topics)} 条")
    return {
        "ok": True,
        "no_news": bool(no_news),
        "major_count": len(major),
        "balance_count": len(balance_news),
        "event_count": len(event_news),
        "topics": topics,
        "verdict": {"no_news": bool(no_news), "summary": summary},
        "limits": {"max_topics": limit_topics},
    }


def _coerce_text(value: Any) -> str:
    """从上游回包里取正文字段；dict 优先取 `_TEXT_KEYS`。"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in _TEXT_KEYS:
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                return text
        return ""
    return "" if value is None else str(value)


_DECISION_RE = re.compile(r"decision:\s*(APPROVE|MODIFY|REJECT)", re.I)


def _decision(text: str) -> str:
    """从审计回执里抽结论：显式 `decision: X` 优先，其次 verdict/关键字。"""
    m = _DECISION_RE.search(text or "")
    if m:
        return m.group(1).upper()
    low = (text or "").lower()
    if re.search(r'"verdict"\s*:\s*"pass"', low) or "通过" in low:
        return "APPROVE"
    if re.search(r'"verdict"\s*:\s*"fail"', low):
        return "REJECT"
    if "驳回" in low or "重做" in low:
        return "REJECT"
    return "MODIFY"


class DebateRunner:
    """多轮评议执行器：采集官出稿 → 审计官点评 → 回采集官改 → …（≤ 轮数上限）。"""

    def __init__(self, client: Any = None, limits: Any = None):
        self.client = client
        self.limits = limits or load_limits()

    # ---------- 派活 ----------
    def _submit(self, target: Dict[str, Any], params: Dict[str, Any],
                timeout_s: float) -> Any:
        if self.client is None:
            raise RuntimeError("debate/run 没有 client（应由 agent 侧 register(runtime) 注入）")
        task = self.client.submit(str(target["capability"]), str(target["action"]),
                                  params, wait=True, timeout_s=timeout_s)
        if not isinstance(task, dict) or task.get("state") != "succeeded":
            raise RuntimeError(f"{target['capability']}/{target['action']} 失败："
                               f"{(task or {}).get('error')}")
        return task.get("result")

    # ---------- 主流程 ----------
    def run(self, params: Dict[str, Any]) -> Dict[str, Any]:
        debate_lim, audit_lim = self.limits.debate, self.limits.audit
        if params.get("no_news"):
            return {"ok": True, "rounds": 0, "decision": "SKIPPED",
                    "stopped_reason": "no_major_news", "transcript": [],
                    "chars_used": 0, "limits": debate_lim.as_dict()}
        collector = params.get("collector") or {}
        auditor = params.get("auditor") or {}
        if not collector.get("capability") or not auditor.get("capability"):
            return {"ok": False, "error": "debate/run 需要 collector 与 auditor 的 "
                                          "capability/action"}

        max_rounds = int(params.get("max_rounds") or debate_lim.max_rounds)
        max_rounds = max(1, min(max_rounds, debate_lim.max_rounds))
        budget = int(debate_lim.max_total_chars)
        timeout_s = float(params.get("timeout_s") or 300)
        topic = _coerce_text(params.get("topic"))
        context = params.get("context") if isinstance(params.get("context"), dict) else {}

        transcript: List[Dict[str, Any]] = []
        chars, critique, decision = 0, "", "MODIFY"
        for round_no in range(1, max_rounds + 1):
            try:
                draft_raw = self._submit(collector, {
                    "topic": topic, "round": round_no, "critique": critique, **context}, timeout_s)
                draft = truncate(_coerce_text(draft_raw), debate_lim.max_message_chars)
                audit_raw = self._submit(auditor, {
                    "report": draft, "round": round_no,
                    "max_citations": audit_lim.max_citations, **context}, timeout_s)
                verdict = truncate(_coerce_text(audit_raw), debate_lim.max_message_chars)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"{type(e).__name__}: {e}",
                        "rounds": round_no, "decision": "ERROR",
                        "stopped_reason": "agent_error", "transcript": transcript,
                        "chars_used": chars}

            chars += len(draft) + len(verdict)
            decision = _decision(verdict)
            transcript.append({"round": round_no, "draft": draft, "audit": verdict,
                              "decision": decision, "chars": len(draft) + len(verdict)})
            if decision == "APPROVE":
                return self._done("APPROVE", "approved", round_no, transcript, chars)
            if chars >= budget:
                return self._done(decision, "char_budget", round_no, transcript, chars)
            critique = verdict
        return self._done(decision, "max_rounds", max_rounds, transcript, chars)

    def _done(self, decision: str, reason: str, rounds: int,
              transcript: List[Dict[str, Any]], chars: int) -> Dict[str, Any]:
        lim = self.limits.debate
        return {"ok": True, "rounds": rounds, "decision": decision,
                "stopped_reason": reason, "transcript": transcript,
                "chars_used": chars,
                "limits": {"max_rounds": lim.max_rounds,
                           "max_message_chars": lim.max_message_chars,
                           "max_total_chars": lim.max_total_chars}}


# ───────────────────────────────────────────── Hub 接入
def run_action(action: str, params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None,
               client: Any = None) -> Dict[str, Any]:
    """动作执行体（单测可直调）：`screen` 初筛 / `run` 多轮评议。"""
    if action == "screen":
        return screen(params, ctx)
    if action == "run":
        return DebateRunner(client=client).run(params)
    raise ValueError(f"debate 不支持的动作：{action}")


def register(runtime, client: Any = None) -> None:
    """把一个 Agent 注册成评议执行器 + 协调官。

    ```python
    from multi_agent.integrations.debate import register
    register(runtime)          # 动作：debate/run、moderator/screen
    ```

    评议器要用 `runtime.client` 给采集官/审计官派活；协调官初筛是纯规则（零 token）。
    """
    hub_client = client if client is not None else getattr(runtime, "client", None)
    runtime.add_handler("debate", "run",
                        lambda p, c: run_action("run", p, c, client=hub_client))
    runtime.add_handler("moderator", "screen", lambda p, c: run_action("screen", p, c))