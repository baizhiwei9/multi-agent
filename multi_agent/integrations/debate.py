"""协调官初筛（`moderator/screen`）与多轮评议（`debate/run`）。

用户要的流程
-----------
    多路新闻采集官 → 协调官初筛（今日有没有大事 / 初轮观点）
        → 审计官  ⇄  采集官（多轮互相点评，默认最多 10 轮）
        → 报告官 + 风控官 → 终审

两条硬约束（数字都在 `config/limits.json`，见 docs/LIMITS.md）：
· 单条消息最多 `debate.max_message_chars` 字（审计官输入/输出都是 5000）；
· 全流程最多 `debate.max_total_chars` 字、最多 `debate.max_rounds` 轮，超了停并说明原因。

三种形态共用这一个执行器（`DebateRunner`），区别只在角色表与终止判据：
· `mode="audit"`（默认）：采集官出稿 ⇄ 审计官点评，审计说 APPROVE 即收敛；
· `mode="adversarial"`：多头 ⇄ 空头（`bull`/`bear`）各说一轮算一轮，跑满后交
  `judge` 裁决 BULL / BEAR / NEUTRAL。多空不设 APPROVE 早停（本来就没有"通过"），
  只靠轮数、字数预算与可选 `converge`（两边都在复读上一轮时早停）收口。
· `mode="panel"`（给 `participants[]` 即自动进入）：N 方按顺序发言，每人能看到本轮之前
  的稿子；可选 `judge` 逐轮裁决，`stop_on_consensus` 时立场一致即收敛。用于
  "多头/空头/风控三方 + 组合经理终审"这类不止两方的场子。成本按**发言条数**算，
  `max_speeches` 是硬闸（三方一轮就是 3 条）。

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


# ───────────────────────────────────────────── 多方（panel）辅助件
def _is_target(value: Any) -> bool:
    return (isinstance(value, dict) and bool(str(value.get("capability") or "").strip())
            and bool(str(value.get("action") or "").strip()))


def _people(params: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """把 `participants[]` 归一成 `(发言者列表, 裁判)`。

    每项要 `capability` + `action`；`name` 缺省取 `role`，再缺省用 `capability/action`；
    `role` 只用于标注身份（`bull` / `bear` / `risk-aggressive`）。
    """
    raw = params.get("participants")
    if not isinstance(raw, list) or not raw:
        raise ValueError("panel 模式需要 participants[]（每项 capability + action）")
    people: List[Dict[str, Any]] = []
    for item in raw:
        if not _is_target(item):
            raise ValueError("participants[] 每项都需要 capability 与 action")
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
            raise ValueError("judge 需要 capability 与 action")
        judge = {"capability": str(judge["capability"]).strip(),
                 "action": str(judge["action"]).strip(),
                 "name": str(judge.get("name") or f"{judge['capability']}/{judge['action']}")}
    return people, judge


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


MODES = ("audit", "adversarial", "panel")

#: 收敛判据默认阈值：新发言里"上一轮没出现过的字二元组"占比低于它，就算原地重复
CONVERGE_NOVELTY = 0.15

_STANCE_RE = re.compile(
    r"(?:recommendation|decision|rating|verdict|stance|结论|裁定|建议)\s*[:：=]\s*[\"']?"
    r"(BULL|BEAR|NEUTRAL|BUY|OVERWEIGHT|HOLD|UNDERWEIGHT|SELL|做多|做空|中性|看多|看空)",
    re.I)
_STANCE_ALIAS = {
    "BULL": "BULL", "BUY": "BULL", "OVERWEIGHT": "BULL", "做多": "BULL", "看多": "BULL",
    "BEAR": "BEAR", "SELL": "BEAR", "UNDERWEIGHT": "BEAR", "做空": "BEAR", "看空": "BEAR",
    "NEUTRAL": "NEUTRAL", "HOLD": "NEUTRAL", "中性": "NEUTRAL",
}


def _bigrams(text: str) -> set:
    """字二元组集合（中英文通用，不依赖分词）。"""
    s = re.sub(r"\s+", "", text or "")
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else set()


def _novelty(prev: str, cur: str) -> float:
    """本次发言相对自己上一次发言的"新内容占比"（0~1）。

    用二元组而不是整句比较：只想判断"是不是把上一轮的话又说了一遍"，
    措辞稍有变化不算重复，整段复读才算。两边都低于阈值即判收敛。
    """
    cur_set = _bigrams(cur)
    if not cur_set:
        return 0.0
    return len(cur_set - _bigrams(prev)) / len(cur_set)


def _stance(text: str) -> str:
    """从裁决词里抽立场：BULL / BEAR / NEUTRAL（多空辩论用，与审计的 decision 分开）。"""
    m = _STANCE_RE.search(text or "")
    if m:
        return _STANCE_ALIAS.get(m.group(1).upper(), _STANCE_ALIAS.get(m.group(1), "NEUTRAL"))
    low = (text or "").lower()
    if re.search(r"\b(buy|overweight|bull)\b|看多|做多|多头", low):
        return "BULL"
    if re.search(r"\b(sell|underweight|bear)\b|看空|做空|空头", low):
        return "BEAR"
    return "NEUTRAL"


def _role_of(params: Dict[str, Any], mode: str) -> tuple:
    """按模式解析三方角色；旧参数名继续可用。

    · audit（默认，向后兼容）：`collector` 出稿、`auditor` 审、`judge` 可选；
    · adversarial：`bull` 对 `bear`，`judge` 必填（裁决官）；
    · panel：不用这个函数，走 `_people(params)`。
    """
    if mode == "adversarial":
        return (params.get("bull") or params.get("proposer") or {},
                params.get("bear") or params.get("reviewer") or {},
                params.get("judge") or {})
    return (params.get("collector") or params.get("proposer") or {},
            params.get("auditor") or params.get("reviewer") or {},
            params.get("judge") or {})


def _text_chars(value: Any) -> int:
    """递归统计一段参数里会进提示词的文本量（JSON 标点、字段名不计）。"""
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(_text_chars(k) + _text_chars(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return sum(_text_chars(v) for v in value)
    return len(str(value))


def _head(text: str, limit: int) -> str:
    """截首段，保留可判定信息但压住长度（给裁决官看的压缩稿）。"""
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…（已截断）"


def _digest(transcript: List[Dict[str, Any]], per_round_chars: int) -> List[Dict[str, Any]]:
    """把 transcript 压成裁决官可读的稿子：每轮只留正反双方首段。"""
    return [{"round": item.get("round"),
             "pro": _head(item.get("draft"), per_round_chars),
             "con": _head(item.get("audit"), per_round_chars)} for item in transcript]


class DebateRunner:
    """通用评议执行器：一方提案 → 一方批评 → 收敛/触顶 → 裁决。

    一个引擎跑三种形态，差别只在角色表和终止判据：
    · `mode="audit"`（默认）：采集官出稿 ⇄ 审计官点评，审计说 APPROVE 即收敛；
    · `mode="adversarial"`：多头 ⇄ 空头各说一轮为一轮，跑满后交裁决官定 BULL/BEAR；
    · `mode="panel"`：N 方顺序发言 + 可选裁判逐轮裁决（给 `participants[]` 自动进入）。
    三种形态共用同一套硬上限：`max_rounds`、`max_message_chars`、`max_total_chars`、
    `max_speeches`（panel）、`max_tokens`（估算），另可开 `converge`（复读早停），
    审计的 APPROVE 早停只在 audit 生效。
    """

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
        lim = self.limits.debate
        # 给了 participants[] 就走 panel，不要求同时声明 mode：
        # 这样"二方 ⇄ 三方"的切换只改参数，不用改流水线。
        if isinstance(params.get("participants"), list) and params.get("participants"):
            return self._run_panel(params)
        mode = str(params.get("mode") or "audit").lower()
        if mode not in MODES:
            return {"ok": False, "error": f"mode 只能是 {MODES}（当前 {mode!r}）"}
        if mode == "panel":
            return {"ok": False, "error": "panel 模式要给 participants[]，"
                                          "而不是只写 mode=panel"}
        if params.get("no_news"):
            return {"ok": True, "mode": mode, "rounds": 0, "turns": 0,
                    "decision": "SKIPPED", "stopped_reason": "no_major_news",
                    "transcript": [], "chars_used": 0, "tokens_used": 0,
                    "limits": lim.as_dict()}

        proposer, reviewer, judge = _role_of(params, mode)
        if not proposer.get("capability") or not reviewer.get("capability"):
            need = ("bull/bear" if mode == "adversarial" else "collector/auditor")
            return {"ok": False, "error": f"debate/run 需要 {need} 的 capability/action"}
        if mode == "adversarial" and not judge.get("capability"):
            return {"ok": False, "error": "多空辩论必须给 judge（裁决官），"
                                          "否则两边只会各说各话、没有结论"}

        max_rounds = int(params.get("max_rounds") or lim.max_rounds)
        max_rounds = max(1, min(max_rounds, lim.max_rounds))
        min_rounds = int(params.get("min_rounds") or 1)
        min_rounds = max(1, min(min_rounds, max_rounds))
        policies = set(params.get("stop_policies")
                       or ("approve", "budget", "token", "rounds"))
        if params.get("converge"):
            policies.add("converge")
        novelty_at = float(params.get("converge_novelty") or CONVERGE_NOVELTY)
        char_budget = int(lim.max_total_chars)
        token_budget = max(1000, int(params.get("max_tokens") or lim.max_tokens))
        ratio = float(params.get("token_per_char") or lim.token_per_char)
        judge_reserve = (int(token_budget * float(lim.judge_reserve_ratio))
                         if mode == "adversarial" else 0)
        timeout_s = float(params.get("timeout_s") or 300)
        topic = _coerce_text(params.get("topic"))
        context = params.get("context") if isinstance(params.get("context"), dict) else {}

        transcript: List[Dict[str, Any]] = []
        chars, turns = 0, 0
        tokens_in, tokens_out = 0, 0
        critique, decision, rounds_done = "", "MODIFY", 0
        prev = {"pro": "", "con": ""}
        for round_no in range(1, max_rounds + 1):
            pro_params = {"topic": topic, "round": round_no, "critique": critique,
                          "side": "pro", **context}
            # 开轮前先按"最坏情况"估一轮：上下文 + 双方各写满 max_message_chars
            est = int((_text_chars(pro_params) + 2 * lim.max_message_chars) * ratio)
            if ("token" in policies and round_no > min_rounds
                    and tokens_in + tokens_out + est + judge_reserve > token_budget):
                return self._done(decision, "token_budget", rounds_done, turns,
                                  transcript, chars, mode, tokens_in, tokens_out,
                                  token_budget)
            tokens_in += int(_text_chars(pro_params) * ratio)
            try:
                draft_raw = self._submit(proposer, pro_params, timeout_s)
                draft = truncate(_coerce_text(draft_raw), lim.max_message_chars)
                con_params = {"report": draft, "draft": draft, "round": round_no,
                              "critique": critique, "side": "con",
                              "max_citations": self.limits.audit.max_citations, **context}
                tokens_in += int(_text_chars(con_params) * ratio)
                review_raw = self._submit(reviewer, con_params, timeout_s)
                verdict = truncate(_coerce_text(review_raw), lim.max_message_chars)
            except Exception as e:  # noqa: BLE001
                out = {"ok": False, "mode": mode, "error": f"{type(e).__name__}: {e}",
                       "rounds": round_no, "turns": turns, "decision": "ERROR",
                       "stopped_reason": "agent_error", "transcript": transcript,
                       "chars_used": chars, "tokens_used": tokens_in + tokens_out,
                       "token_budget": token_budget}
                return out

            out_tokens = int((len(draft) + len(verdict)) * ratio)
            tokens_out += out_tokens
            turns += 2                      # 一方一回合 = 2 次发言
            rounds_done = round_no
            chars += len(draft) + len(verdict)
            decision = _decision(verdict) if mode == "audit" else "IN_DEBATE"
            entry = {"round": round_no, "draft": draft, "audit": verdict,
                     "decision": decision, "chars": len(draft) + len(verdict),
                     "tokens": out_tokens}
            if "converge" in policies and round_no > 1:
                nov = {"pro": round(_novelty(prev["pro"], draft), 3),
                       "con": round(_novelty(prev["con"], verdict), 3)}
                entry["novelty"] = nov
            transcript.append(entry)
            prev = {"pro": draft, "con": verdict}

            if mode == "audit" and "approve" in policies and decision == "APPROVE":
                return self._done(decision, "approved", round_no, turns, transcript,
                                  chars, mode, tokens_in, tokens_out, token_budget)
            if ("budget" in policies and round_no >= min_rounds
                    and chars >= char_budget):
                return self._done(decision, "char_budget", round_no, turns, transcript,
                                  chars, mode, tokens_in, tokens_out, token_budget)
            if ("token" in policies and round_no >= min_rounds
                    and tokens_in + tokens_out >= token_budget):
                return self._done(decision, "token_budget", round_no, turns, transcript,
                                  chars, mode, tokens_in, tokens_out, token_budget)
            if ("converge" in policies and round_no >= max(2, min_rounds)
                    and max(entry.get("novelty", {}).values() or [1.0]) < novelty_at):
                return self._done(decision, "converged", round_no, turns, transcript,
                                  chars, mode, tokens_in, tokens_out, token_budget)
            critique = verdict

        if mode == "adversarial":
            return self._judge(judge, topic, transcript, chars, turns, timeout_s,
                               context, ratio, tokens_in, tokens_out, token_budget)
        return self._done(decision, "max_rounds", rounds_done, turns, transcript, chars,
                          mode, tokens_in, tokens_out, token_budget)

    # ---------- panel：N 方顺序发言 + 可选裁判 ----------
    def _run_panel(self, params: Dict[str, Any]) -> Dict[str, Any]:
        lim = self.limits.debate
        try:
            people, judge = _people(params)
        except ValueError as e:
            return {"ok": False, "mode": "panel", "error": str(e)}
        cap_people = int(getattr(lim, "max_participants", 6))
        if len(people) > cap_people:
            return {"ok": False, "mode": "panel",
                    "error": f"participants 最多 {cap_people} 个（当前 {len(people)}）"}

        max_rounds = int(params.get("max_rounds") or lim.max_rounds)
        max_rounds = max(1, min(max_rounds, lim.max_rounds))
        cap_speeches = int(getattr(lim, "max_speeches", 24))
        max_speeches = int(params.get("max_speeches") or cap_speeches)
        max_speeches = max(len(people), min(max_speeches, cap_speeches))
        char_budget = int(lim.max_total_chars)
        token_budget = max(1000, int(params.get("max_tokens") or lim.max_tokens))
        ratio = float(params.get("token_per_char") or lim.token_per_char)
        timeout_s = float(params.get("timeout_s") or 300)
        topic = _coerce_text(params.get("topic"))
        context = params.get("context") if isinstance(params.get("context"), dict) else {}
        judge_every = bool(params.get("judge_every_round", True))
        stop_on_consensus = bool(params.get("stop_on_consensus", True))
        names = [p["name"] for p in people]

        transcript: List[Dict[str, Any]] = []
        chars, tokens_in, tokens_out = 0, 0, 0
        decision, judge_info = "MODIFY", None
        rounds_done = 0

        def _finish(reason: str, round_no: int) -> Dict[str, Any]:
            return self._panel_done(decision, reason, round_no, transcript, chars,
                                    people=people, judge=judge_info,
                                    tokens_in=tokens_in, tokens_out=tokens_out,
                                    token_budget=token_budget)

        def _judge_once(round_no: int) -> Optional[Dict[str, Any]]:
            nonlocal decision, judge_info, chars, tokens_in, tokens_out
            if judge is None:
                return None
            j_params = {"topic": topic, "round": round_no, "participants": names,
                        "transcript": _transcript_text(transcript, lim.max_message_chars),
                        **context}
            tokens_in += int(_text_chars(j_params) * ratio)
            try:
                raw = self._submit(judge, j_params, timeout_s)
                text = truncate(_coerce_text(raw), lim.max_message_chars)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "mode": "panel", "error": f"{type(e).__name__}: {e}",
                        "rounds": round_no, "speeches": len(transcript),
                        "decision": "ERROR", "stopped_reason": "judge_error",
                        "transcript": transcript, "chars_used": chars,
                        "tokens_used": tokens_in + tokens_out}
            tokens_out += int(len(text) * ratio)
            chars += len(text)
            decision, converged = _judge_verdict(raw)
            judge_info = {"name": judge["name"], "decision": decision,
                          "converged": converged, "round": round_no, "text": text}
            if converged:
                return _finish("judge_converged", round_no)
            return None

        for round_no in range(1, max_rounds + 1):
            rounds_done = round_no
            for order, person in enumerate(people, start=1):
                speech_params = {"topic": topic, "round": round_no,
                                 "speaker": person["name"], "role": person["role"],
                                 "order": order, "participants": names,
                                 "transcript": _transcript_text(transcript,
                                                                lim.max_message_chars),
                                 "prior": transcript[-1]["text"] if transcript else "",
                                 **context}
                est = int((_text_chars(speech_params) + lim.max_message_chars) * ratio)
                if tokens_in + tokens_out + est > token_budget:
                    decision = _majority([t for t in transcript
                                          if t.get("round") == round_no])
                    return _finish("token_budget", round_no)
                tokens_in += int(_text_chars(speech_params) * ratio)
                try:
                    raw = self._submit(person, speech_params, timeout_s)
                    text = truncate(_coerce_text(raw), lim.max_message_chars)
                except Exception as e:  # noqa: BLE001
                    return {"ok": False, "mode": "panel", "error": f"{type(e).__name__}: {e}",
                            "rounds": round_no, "speeches": len(transcript),
                            "decision": "ERROR", "stopped_reason": "agent_error",
                            "participants": names, "transcript": transcript,
                            "chars_used": chars, "tokens_used": tokens_in + tokens_out}
                tokens_out += int(len(text) * ratio)
                chars += len(text)
                transcript.append({"round": round_no, "speaker": person["name"],
                                   "role": person["role"], "order": order,
                                   "text": text, "stance": _stance_of(raw),
                                   "chars": len(text)})
                if chars >= char_budget:
                    decision = _majority([t for t in transcript
                                          if t.get("round") == round_no])
                    return _finish("char_budget", round_no)
                if len(transcript) >= max_speeches:
                    decision = _majority([t for t in transcript
                                          if t.get("round") == round_no])
                    return _finish("max_speeches", round_no)

            if judge_every:
                stop = _judge_once(round_no)
                if isinstance(stop, dict):
                    return stop
                if chars >= char_budget:
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

    def _judge(self, judge: Dict[str, Any], topic: str,
               transcript: List[Dict[str, Any]], chars: int, turns: int,
               timeout_s: float, context: Dict[str, Any], ratio: float,
               tokens_in: int = 0, tokens_out: int = 0,
               token_budget: Optional[int] = None) -> Dict[str, Any]:
        """多空辩论跑满后由裁决官一次性定调（不参与轮次，避免自己把自己说服）。

        裁决官看到的是**压缩稿**（每轮各方只留首段），不是全量 transcript：
        全量 transcripts 随轮数是平方级输入，一次裁决就能吃光整场预算。
        """
        digest = _digest(transcript, self.limits.debate.judge_round_chars)
        j_params = {"topic": topic, "transcript": digest, "rounds": len(transcript),
                    **context}
        tokens_in += int(_text_chars(j_params) * ratio)
        try:
            raw = self._submit(judge, j_params, timeout_s)
            verdict = truncate(_coerce_text(raw), self.limits.debate.max_message_chars)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "mode": "adversarial", "error": f"{type(e).__name__}: {e}",
                    "rounds": len(transcript), "turns": turns, "decision": "ERROR",
                    "stopped_reason": "judge_error", "transcript": transcript,
                    "chars_used": chars, "tokens_used": tokens_in + tokens_out,
                    "token_budget": token_budget}
        tokens_out += int(len(verdict) * ratio)
        stance = _stance(verdict)
        return self._done(stance, "judged", len(transcript), turns, transcript,
                          chars + len(verdict), "adversarial", tokens_in, tokens_out,
                          token_budget, verdict=verdict)

    def _done(self, decision: str, reason: str, rounds: int, turns: int,
              transcript: List[Dict[str, Any]], chars: int, mode: str,
              tokens_in: int = 0, tokens_out: int = 0,
              token_budget: Optional[int] = None, verdict: str = "") -> Dict[str, Any]:
        lim = self.limits.debate
        out = {"ok": True, "mode": mode, "rounds": rounds, "turns": turns,
               "decision": decision, "stopped_reason": reason,
               "transcript": transcript, "chars_used": chars,
               "tokens_used": tokens_in + tokens_out,
               "tokens_in": tokens_in, "tokens_out": tokens_out,
               "token_budget": token_budget,
               "limits": {"max_rounds": lim.max_rounds,
                          "max_message_chars": lim.max_message_chars,
                          "max_total_chars": lim.max_total_chars,
                          "max_tokens": lim.max_tokens,
                          "max_participants": getattr(lim, "max_participants", 6),
                          "max_speeches": getattr(lim, "max_speeches", 24)}}
        if verdict:
            out["verdict"] = verdict
        return out

    def _panel_done(self, decision: str, reason: str, rounds: int,
                    transcript: List[Dict[str, Any]], chars: int, *,
                    people: List[Dict[str, Any]],
                    judge: Optional[Dict[str, Any]] = None,
                    tokens_in: int = 0, tokens_out: int = 0,
                    token_budget: Optional[int] = None) -> Dict[str, Any]:
        """panel 的收口回执：与 `_done` 同名字段 + `participants` / `speeches` / `judge`。"""
        lim = self.limits.debate
        out: Dict[str, Any] = {
            "ok": True, "mode": "panel", "rounds": rounds,
            "turns": len(transcript), "speeches": len(transcript),
            "decision": decision, "stopped_reason": reason,
            "participants": [p["name"] for p in people],
            "transcript": transcript, "chars_used": chars,
            "tokens_used": tokens_in + tokens_out,
            "tokens_in": tokens_in, "tokens_out": tokens_out,
            "token_budget": token_budget,
            "limits": {"max_rounds": lim.max_rounds,
                       "max_message_chars": lim.max_message_chars,
                       "max_total_chars": lim.max_total_chars,
                       "max_tokens": lim.max_tokens,
                       "max_participants": getattr(lim, "max_participants", 6),
                       "max_speeches": getattr(lim, "max_speeches", 24)},
        }
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