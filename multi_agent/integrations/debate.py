"""协调官初筛（`moderator/screen`）与多轮评议（`debate/run`）。

用户要的流程
-----------
    多路新闻采集官 → 协调官初筛（今日有没有大事 / 初轮观点）
        → 审计官  ⇄  采集官（多轮互相点评，默认最多 10 轮）
        → 报告官 + 风控官 → 终审

两种评议模式（`debate/run` 自己按参数选）
--------------------------------------
· **两方（pair，老路径，行为不变）**：`collector` 出稿 → `auditor` 点评 → 回稿，直到
  `APPROVE` / 轮数 / 字数预算；审计意见每轮以 `critique` 回灌采集官。
· **多方（panel）**：`participants[]` 按顺序发言，每人能看到本轮之前的发言稿（`transcript`），
  可选 `judge` 在每轮末尾裁决（`decision` + `converged`）；`stop_on_consensus=true` 时
  全员立场一致即提前收敛。用于"多头⇄空头+研究经理裁决""激进/保守/中性+组合经理终审"。

三条硬约束（数字都在 `config/limits.json`，见 docs/LIMITS.md）
----------------------------------------------------------
· 单条消息最多 `debate.max_message_chars` 字（审计官输入/输出都是 5000）；
· 全流程最多 `debate.max_total_chars` 字、最多 `debate.max_rounds` 轮，超了停并说明原因；
· 多方模式另有 `debate.max_speeches`（发言总数）与 `debate.max_participants`（人数上限）——
  成本按"发言条数"而不是"轮数"算，因为三方辩论一轮就是三条。

"晚上没有重大新闻"不是异常：协调官给 `no_news=true`，评议器直接把状态记成
`SKIPPED`（`stopped_reason=no_major_news`），让报告官按"今日无重大新闻"出稿，
而不是让审计官对着空观点编点评。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

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
    # 文档口径：**没有重大事件就不评议**（不是"一条新闻都没有"才跳过）。
    # 有一般新闻但没有矿难/禁令这类硬事件时，评议只会让审计官对着无关稿件空转。
    no_news = not major
    limit_topics = int(params.get("max_topics") or lim.news.max_topics)
    topics = (major or merged)[:limit_topics]
    if no_news:
        summary = ("今日无新闻，不输出观点" if not merged else
                   f"今日 {len(merged)} 条新闻但无重大事件，不输出观点")
    else:
        summary = f"初筛 {len(merged)} 条（重大 {len(major)} 条），待议 {len(topics)} 条"
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


def _topics_text(topics: List[Dict[str, Any]]) -> str:
    """把初筛的 topics 拼成可读要点（标题 + 摘要）。"""
    lines: List[str] = []
    for item in topics[:10]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        summary = str(item.get("summary") or item.get("content") or "").strip()
        if title:
            lines.append(f"- {title}" + (f"：{summary}" if summary else ""))
    return "\n".join(lines)


def _coerce_text(value: Any) -> str:
    """从上游回包里取正文；兼容字符串、嵌套 verdict（dict）、初筛 topics。

    真实回包不是纯字符串：审计官回 `{"verdict": {"decision": "APPROVE", ...}}`，
    协调官回 `{"topics": [...], "verdict": {...}}`。以前这两种都取成空串，
    导致评议对着空稿点评、送审件 payload 为空。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        # 先取 topics（具体事件），再取正文字段，最后才退回 verdict 摘要——
        # 否则送审件只剩"初筛 N 条"这种摘要，审计官看不到具体事件。
        topics = value.get("topics")
        if isinstance(topics, list):
            text = _topics_text(topics)
            if text:
                return text
        for key in _TEXT_KEYS:
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                return text
            if key == "verdict" and isinstance(text, dict):
                for sub in ("decision", "summary", "reason", "text", "status"):
                    s = text.get(sub)
                    if isinstance(s, str) and s.strip():
                        return s
                return json.dumps(text, ensure_ascii=False)
        return ""
    if isinstance(value, list):
        return _topics_text([v for v in value if isinstance(v, dict)])
    return "" if value is None else str(value)


_DECISION_RE = re.compile(
    r"decision[\"']?\s*[:=]\s*[\"']?(APPROVE|MODIFY|REJECT|PASS|SUSPECT|FAIL)", re.I)
_ALIAS = {"APPROVE": "APPROVE", "PASS": "APPROVE",
          "MODIFY": "MODIFY", "SUSPECT": "MODIFY",
          "REJECT": "REJECT", "FAIL": "REJECT"}

_CONVERGED_RE = re.compile(
    r"(converged|consensus|收敛|一致)[\"']?\s*[:=]\s*[\"']?(true|yes|1|是)", re.I)


def _decision(text: str) -> str:
    """从审计回执里抽结论。

    兼容三种真实形态：`decision: APPROVE`、`{"verdict": {"decision": "pass"}}`、
    裸词 `pass / suspect / fail`。以前只认第一种，`pass` 会被当成 MODIFY，
    评议永远跑满轮数、无法 APPROVE。
    """
    blob = text or ""
    m = _DECISION_RE.search(blob)
    if m:
        return _ALIAS.get(m.group(1).upper(), "MODIFY")
    low = blob.lower()
    if re.search(r"\b(reject|fail)\b|驳回|重做|不通过|不可用", low):
        return "REJECT"
    if re.search(r"\b(approve|pass)\b|通过|同意|可用", low):
        return "APPROVE"
    if re.search(r"\b(modify|suspect)\b|存疑|修正", low):
        return "MODIFY"
    return "MODIFY"


# ───────────────────────────────────────────── 多方辩论的辅助件
def _is_target(value: Any) -> bool:
    return (isinstance(value, dict) and bool(str(value.get("capability") or "").strip())
            and bool(str(value.get("action") or "").strip()))


def _people(params: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """把参数归一成 `(模式, 发言者列表, 裁判)`。

    · 有 `participants[]` → panel；每项要 `capability` + `action`，`name` 缺省用
      `capability/action`，`role` 只用于报告里标明身份（如 `bull` / `bear` / `risk-aggressive`）；
    · 否则 `collector` + `auditor` → pair（老路径，行为不变）。
    """
    raw = params.get("participants")
    if isinstance(raw, list) and raw:
        people: List[Dict[str, Any]] = []
        for item in raw:
            if not _is_target(item):
                raise ValueError("debate/run 的 participants[] 每项都需要 capability 与 action")
            people.append({
                "capability": str(item["capability"]).strip(),
                "action": str(item["action"]).strip(),
                "name": str(item.get("name") or item.get("role")
                            or f"{item['capability']}/{item['action']}"),
                "role": str(item.get("role") or item.get("name") or "participant"),
            })
        judge = params.get("judge")
        if judge is not None:
            if not _is_target(judge):
                raise ValueError("debate/run 的 judge 需要 capability 与 action")
            judge = {"capability": str(judge["capability"]).strip(),
                     "action": str(judge["action"]).strip(),
                     "name": str(judge.get("name") or f"{judge['capability']}/{judge['action']}")}
        return "panel", people, judge

    collector, auditor = params.get("collector") or {}, params.get("auditor") or {}
    # 老路径只要求 capability（与历史行为一致）：action 缺失仍由 _submit 直接报出来，
    # 免得旧调用方因为"多校验一层"而拿到不一样的错误信息。
    if (not isinstance(collector, dict) or not isinstance(auditor, dict)
            or not str(collector.get("capability") or "").strip()
            or not str(auditor.get("capability") or "").strip()):
        raise ValueError("debate/run 需要 collector 与 auditor（或 participants[] + judge）"
                         "的 capability/action")
    return "pair", [
        {"capability": str(collector["capability"]),
         "action": str(collector.get("action") or ""),
         "name": str(collector.get("name") or "collector"), "role": "collector"},
        {"capability": str(auditor["capability"]),
         "action": str(auditor.get("action") or ""),
         "name": str(auditor.get("name") or "auditor"), "role": "auditor"},
    ], None


def _transcript_text(transcript: List[Dict[str, Any]], limit: int) -> str:
    """把已有发言压成给下一位发言者看的上下文（每条按条数均分预算）。"""
    if not transcript:
        return ""
    per = max(120, int(limit / max(1, len(transcript))))
    lines = [f"[R{t.get('round')} {t.get('speaker') or t.get('role') or '-'}] "
             f"{str(t.get('text') or '').strip()[:per]}" for t in transcript]
    blob = "\n".join(lines)
    return blob[-limit:] if len(blob) > limit else blob


def _stance_of(value: Any) -> Optional[str]:
    """取发言者的显式立场（只认显式字段，避免从长文里"猜"立场）。"""
    if not isinstance(value, dict):
        return None
    raw = value.get("stance") or value.get("position") or value.get("side")
    if not isinstance(raw, str):
        return None
    key = raw.strip().upper()
    if key in _ALIAS:
        return _ALIAS[key]
    low = raw.lower()
    if any(w in low for w in ("通过", "同意", "看多", "利多", "上涨", "buy")):
        return "APPROVE"
    if any(w in low for w in ("驳回", "反对", "看空", "利空", "下跌", "sell")):
        return "REJECT"
    return None


def _judge_verdict(value: Any) -> Tuple[str, bool]:
    """裁判回执 → `(decision, converged)`；`converged` 也可由 `APPROVE` 推出。"""
    text = _coerce_text(value)
    decision = _decision(text)
    converged = bool(_CONVERGED_RE.search(text))
    if isinstance(value, dict):
        verdict = value.get("verdict") if isinstance(value.get("verdict"), dict) else {}
        raw_decision = value.get("decision") or verdict.get("decision")
        if isinstance(raw_decision, str):
            decision = _ALIAS.get(raw_decision.strip().upper(), decision)
        raw_conv = value.get("converged", verdict.get("converged"))
        if isinstance(raw_conv, bool):
            converged = raw_conv
        elif isinstance(raw_conv, str):
            converged = raw_conv.strip().lower() in ("true", "yes", "1", "是")
    return decision, converged or decision == "APPROVE"


def _majority(transcript: List[Dict[str, Any]]) -> str:
    """没有裁判时的兜底结论：本轮显式立场里过半者胜，平票按 MODIFY。"""
    votes = [t["stance"] for t in transcript if t.get("stance")]
    if not votes:
        return "MODIFY"
    for label in ("APPROVE", "REJECT"):
        if votes.count(label) * 2 > len(votes):
            return label
    return "MODIFY"


class DebateRunner:
    """多轮评议执行器：两方（采集官 ⇄ 审计官）或 N 方 + 裁判（≤ 轮数 / 发言数上限）。"""

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
        debate_lim = self.limits.debate
        if params.get("no_news"):
            return {"ok": True, "mode": "skipped", "rounds": 0, "speeches": 0,
                    "decision": "SKIPPED", "stopped_reason": "no_major_news",
                    "transcript": [], "chars_used": 0, "limits": debate_lim.as_dict()}
        try:
            mode, people, judge = _people(params)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if mode == "panel":
            return self._run_panel(params, people, judge, debate_lim)
        return self._run_pair(params, people, debate_lim)

    # ---------- 老路径：两方（行为保持不变） ----------
    def _run_pair(self, params: Dict[str, Any], people: List[Dict[str, Any]],
                  lim: Any) -> Dict[str, Any]:
        debate_lim, audit_lim = lim, self.limits.audit
        collector, auditor = people[0], people[1]
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
                        "mode": "pair", "rounds": round_no, "decision": "ERROR",
                        "stopped_reason": "agent_error", "transcript": transcript,
                        "chars_used": chars}

            chars += len(draft) + len(verdict)
            decision = _decision(verdict)
            transcript.append({"round": round_no, "draft": draft, "audit": verdict,
                              "decision": decision, "chars": len(draft) + len(verdict)})
            if decision == "APPROVE":
                return self._done("APPROVE", "approved", round_no, transcript, chars,
                                  mode="pair", people=people, speeches=round_no)
            if chars >= budget:
                return self._done(decision, "char_budget", round_no, transcript, chars,
                                  mode="pair", people=people, speeches=round_no)
            critique = verdict
        return self._done(decision, "max_rounds", max_rounds, transcript, chars,
                          mode="pair", people=people, speeches=max_rounds)

    # ---------- 新路径：N 方 + 可选裁判 ----------
    def _run_panel(self, params: Dict[str, Any], people: List[Dict[str, Any]],
                   judge: Optional[Dict[str, Any]], lim: Any) -> Dict[str, Any]:
        debate_lim = lim
        max_rounds = int(params.get("max_rounds") or debate_lim.max_rounds)
        max_rounds = max(1, min(max_rounds, debate_lim.max_rounds))
        cap_speeches = int(getattr(debate_lim, "max_speeches", 24))
        max_speeches = int(params.get("max_speeches") or cap_speeches)
        max_speeches = max(len(people), min(max_speeches, cap_speeches))
        budget = int(debate_lim.max_total_chars)
        timeout_s = float(params.get("timeout_s") or 300)
        topic = _coerce_text(params.get("topic"))
        context = params.get("context") if isinstance(params.get("context"), dict) else {}
        judge_every = bool(params.get("judge_every_round", True))
        stop_on_consensus = bool(params.get("stop_on_consensus", True))
        names = [p["name"] for p in people]

        transcript: List[Dict[str, Any]] = []
        chars, decision, judge_info = 0, "MODIFY", None
        rounds_done = 0

        def _finish(reason: str, round_no: int) -> Dict[str, Any]:
            return self._done(decision, reason, round_no, transcript, chars,
                              mode="panel", people=people, judge=judge_info,
                              speeches=len(transcript))

        def _judge_once(round_no: int) -> Optional[Dict[str, Any]]:
            """跑一次裁判；返回提前结束的回执或 None。"""
            nonlocal decision, judge_info, chars
            if judge is None:
                return None
            try:
                raw = self._submit(judge, {
                    "topic": topic, "round": round_no, "participants": names,
                    "transcript": _transcript_text(transcript, debate_lim.max_message_chars),
                    **context}, timeout_s)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"{type(e).__name__}: {e}", "mode": "panel",
                        "rounds": round_no, "decision": "ERROR",
                        "stopped_reason": "judge_error", "transcript": transcript,
                        "chars_used": chars}
            text = truncate(_coerce_text(raw), debate_lim.max_message_chars)
            decision, converged = _judge_verdict(raw)
            judge_info = {"name": judge["name"], "decision": decision,
                          "converged": converged, "round": round_no, "text": text}
            chars += len(text)
            if converged:
                return _finish("judge_converged", round_no)
            return None

        for round_no in range(1, max_rounds + 1):
            rounds_done = round_no
            for order, person in enumerate(people, start=1):
                try:
                    raw = self._submit(person, {
                        "topic": topic, "round": round_no, "speaker": person["name"],
                        "role": person["role"], "order": order, "participants": names,
                        "transcript": _transcript_text(transcript, debate_lim.max_message_chars),
                        "prior": transcript[-1]["text"] if transcript else "",
                        **context}, timeout_s)
                    text = truncate(_coerce_text(raw), debate_lim.max_message_chars)
                except Exception as e:  # noqa: BLE001
                    return {"ok": False, "error": f"{type(e).__name__}: {e}", "mode": "panel",
                            "rounds": round_no, "speeches": len(transcript),
                            "decision": "ERROR", "stopped_reason": "agent_error",
                            "transcript": transcript, "chars_used": chars}
                transcript.append({"round": round_no, "speaker": person["name"],
                                   "role": person["role"], "order": order,
                                   "text": text, "stance": _stance_of(raw),
                                   "chars": len(text)})
                chars += len(text)
                if chars >= budget:
                    decision = _majority([t for t in transcript if t.get("round") == round_no])
                    return _finish("char_budget", round_no)
                if len(transcript) >= max_speeches:
                    decision = _majority([t for t in transcript if t.get("round") == round_no])
                    return _finish("max_speeches", round_no)

            if judge_every:
                stop = _judge_once(round_no)
                if isinstance(stop, dict):
                    return stop
                if chars >= budget:
                    return _finish("char_budget", round_no)

            if stop_on_consensus:
                stances = [t["stance"] for t in transcript
                           if t.get("round") == round_no and t.get("stance")]
                if len(stances) == len(people) and len(set(stances)) == 1:
                    decision = stances[0]
                    return _finish("consensus", round_no)

        if judge is not None and not judge_every:
            # 只在最后一轮裁决：前面都不裁，省 token
            stop = _judge_once(max_rounds)
            if isinstance(stop, dict):
                return stop
        if decision == "MODIFY":
            decision = _majority([t for t in transcript if t.get("round") == rounds_done])
        return _finish("max_rounds", max_rounds)

    def _done(self, decision: str, reason: str, rounds: int,
              transcript: List[Dict[str, Any]], chars: int, *, mode: str = "pair",
              people: Optional[List[Dict[str, Any]]] = None,
              judge: Optional[Dict[str, Any]] = None,
              speeches: Optional[int] = None) -> Dict[str, Any]:
        lim = self.limits.debate
        out: Dict[str, Any] = {
            "ok": True, "mode": mode, "rounds": rounds,
            "speeches": int(speeches if speeches is not None else rounds),
            "decision": decision, "stopped_reason": reason,
            "transcript": transcript, "chars_used": chars,
            "limits": {"max_rounds": lim.max_rounds,
                       "max_message_chars": lim.max_message_chars,
                       "max_total_chars": lim.max_total_chars,
                       "max_participants": getattr(lim, "max_participants", 6),
                       "max_speeches": getattr(lim, "max_speeches", 24)},
        }
        if people:
            out["participants"] = [p.get("name") if isinstance(p, dict) else str(p)
                                   for p in people]
        if judge:
            out["judge"] = judge
        return out


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