"""管理员 / 监理（supervisor）：把"送审件"打包交给审计官，并记送审台账。

为什么要这个角色
---------------
采集官只管采、报告官只管写、审计官只管审；"谁把东西递过去、递的是哪一版、
多长、截没截、审计回执是什么"以前散在 debate 与流水线里，出了问题没人对账。
supervisor 只做三件事：

· `pack`    —— 按 `config/limits.json` 的 audit 限额把产物压成送审件
               （超长截断并标注、引用超限指出多出的条数）；
· `handoff` —— 把送审件真正交给审计官（默认 `auditor/report`），回执原样带回；
· `ledger`  —— 把每轮送审与回执汇成台账，供报告官 / 风控官引用。

它不审、不写、不采，所以可以和其他角色合在一个 Agent 上（见
`limits.roles.co_locate`）：Worker 少时 `B + supervisor` 一个进程就够，
`multi-agent project agent ... --roles B,supervisor` 即可。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..limits import load_limits, truncate

#: 送审件的正文字段（按优先级取第一个非空字符串）
_TEXT_KEYS = ("report", "draft", "opinion", "markdown", "md", "content",
              "text", "brief", "payload", "summary")
#: 引用/问题清单字段（用来数"引用了多少处错误观点"）
_CITATION_KEYS = ("issues", "citations", "errors", "findings", "problems")


def _flatten(value: Any) -> str:
    """把报告 / 采集结果摊平成一段文本（容忍 dict / list / str）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in _TEXT_KEYS:
            got = value.get(key)
            if isinstance(got, str) and got.strip():
                return got
        for key in ("result", "data", "brief"):
            if key in value:
                text = _flatten(value[key])
                if text:
                    return text
        # 协调官初筛的回包：topics 是具体事件，优先于 verdict.summary（否则送审只剩摘要）
        topics = value.get("topics")
        if isinstance(topics, list) and topics:
            lines = []
            for item in topics[:10]:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                summary = str(item.get("summary") or item.get("content") or "").strip()
                if title:
                    lines.append(f"- {title}" + (f"：{summary}" if summary else ""))
            if lines:
                return "\n".join(lines)
        verdict = value.get("verdict")
        if isinstance(verdict, dict):
            summary = verdict.get("summary")
            if isinstance(summary, str) and summary.strip():
                return summary
        return ""
    if isinstance(value, list):
        parts = [_flatten(v) for v in value]
        return "\n\n".join(p for p in parts if p)
    return str(value)


def _citations(value: Any) -> int:
    """数引用/问题条目：显式给数字就用数字，给列表就数长度。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, dict):
        for key in _CITATION_KEYS:
            got = value.get(key)
            if isinstance(got, list):
                return len(got)
            if isinstance(got, int) and not isinstance(got, bool):
                return max(0, got)
        return 0
    if isinstance(value, list):
        return len(value)
    return 0


def pack(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把产物压成送审件（零 token）。

    params：`{payload | report | draft, citations?, kind?, target?, action?, project?}`
    超 `audit.max_output_chars` 截断；引用超 `audit.max_citations` 时 `over_cited=True`
    并把多出的条数写进 `extra_citations`——**指出多出的部分**，而不是静默接受。
    """
    lim = load_limits().audit
    params = params or {}
    source = params.get("payload")
    if source is None:
        source = params.get("report") or params.get("draft") or params.get("text")
    text = _flatten(source)
    citations = _citations(params.get("citations") if params.get("citations") is not None
                           else source)

    truncated = len(text) > lim.max_output_chars
    body = truncate(text, lim.max_output_chars)
    extra = max(0, citations - lim.max_citations)
    notes: List[str] = []
    if truncated:
        notes.append(f"正文 {len(text)} 字 > 上限 {lim.max_output_chars} 字，已截断送审")
    if extra:
        notes.append(f"引用 {citations} 处 > 上限 {lim.max_citations} 处，"
                     f"多出 {extra} 处需删减后重送")
    audit_project = params.get("project")
    if not audit_project and isinstance(ctx, dict):
        audit_project = ctx.get("audit_project")

    return {
        "kind": params.get("kind") or "review_brief",
        "target": params.get("target") or "auditor",
        "action": params.get("action") or "report",
        "project": audit_project,
        "payload": body,
        "chars": len(body),
        "chars_limit": lim.max_output_chars,
        "truncated": truncated,
        "citations": citations,
        "citations_limit": lim.max_citations,
        "over_cited": bool(extra),
        "extra_citations": extra,
        "notes": notes,
        "delivered": False,
    }


def handoff(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None,
            client: Any = None) -> Dict[str, Any]:
    """把送审件交给审计官；没有 client 时明确 `degraded`，不假装送到。"""
    params = params or {}
    brief = params.get("brief")
    if not isinstance(brief, dict) or "payload" not in brief:
        brief = pack(params, ctx)
    if client is None:
        return {**brief, "delivered": False, "degraded": True,
                "reason": "未注入 client：无法把送审件交给审计官，请给 supervisor 配 Hub 连接"}

    payload = {"project": brief.get("project"), "level": params.get("level") or "rule",
               "report": brief["payload"]}
    if params.get("round") is not None:
        payload["round"] = params["round"]
    # 必须 wait=True：以前不等待，拿到的是 queued 任务，回执永远为空
    # （verdict=None、state=queued），等于"送出去但不知道审计说了什么"。
    task = client.submit(brief["target"], brief["action"], payload,
                         wait=True, timeout_s=float(params.get("timeout_s") or 300))
    result = (task or {}).get("result") or {}
    verdict = None
    if isinstance(result, dict):
        verdict = result.get("verdict") or result.get("summary") or result.get("conclusion")
    state = (task or {}).get("state")
    ok = state == "succeeded"
    return {
        **brief,
        "delivered": ok,
        "task_id": (task or {}).get("id") or (task or {}).get("task_id"),
        "state": state,
        "verdict": verdict,
        "error": (task or {}).get("error") if not ok else None,
        "round": params.get("round"),
        "degraded": not ok,
    }


def ledger(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把每轮送审与回执汇成台账（零 token），供报告官 / 风控官引用。

    真实 debate 回包用 `transcript`（每轮 draft/audit/decision/chars），
    以前只读 `history`/`rounds_detail`，台账恒为空；且成功也带
    `stopped_reason="approved"`，被记成"阻塞"。现在：
    · 读 transcript，逐轮记 state=decision；
    · 只有 max_rounds / char_budget / agent_error 才算 blocked；
      approved / no_major_news 不算。
    """
    params = params or {}
    raw = params.get("entries")
    entries: List[Dict[str, Any]] = []
    blocked_reasons = ("max_rounds", "char_budget", "agent_error")

    if isinstance(raw, dict):
        items = raw.get("transcript") or raw.get("history") or raw.get("rounds_detail") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            entries.append({
                "round": item.get("round"),
                "state": item.get("decision") or item.get("state"),
                "chars": item.get("chars"),
                "delivered": item.get("delivered", True),
            })
        reason = raw.get("stopped_reason")
        if reason in blocked_reasons:
            entries.append({"round": None, "state": "stopped",
                            "reason": reason, "delivered": False})
        elif reason and not entries:
            entries.append({"round": raw.get("rounds"), "state": str(reason),
                            "delivered": False})
        if raw.get("rounds") and not entries:
            entries.append({"round": raw.get("rounds"), "state": raw.get("decision"),
                            "delivered": True})
    elif isinstance(raw, list):
        entries = [e for e in raw if isinstance(e, dict)]

    packed = params.get("pack")
    if isinstance(packed, dict) and packed.get("payload"):
        entries.insert(0, {"round": 0, "state": "packed",
                           "chars": packed.get("chars"),
                           "truncated": packed.get("truncated"),
                           "over_cited": packed.get("over_cited"), "delivered": False})

    return {
        "entries": entries,
        "count": len(entries),
        "delivered": sum(1 for e in entries if e.get("delivered")),
        "blocked": sum(1 for e in entries if e.get("state") in ("stopped", "error")),
        "chars_total": sum(int(e.get("chars") or 0) for e in entries),
    }


def assign(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """经理派单：把分诊/补丁方案产出的工作流，派给具体能力去干（零 token）。

    这是"coordinator 往下走 → 经理 → 开发"里经理那一步：编排把活交到经理手上，
    经理决定每个工作流交给谁、干哪个动作、验收什么，开发只认这张派工单。
    """
    params = params or {}
    items = params.get("workstreams") or params.get("payload")
    if isinstance(items, dict):
        items = items.get("workstreams") or items.get("items")
    if not isinstance(items, list) or not items:
        return {"ok": False, "degraded": True, "count": 0, "assignments": [],
                "reason": "没有可分派的工作流（上游 propose 没产出或全部 deferred）"}

    cap = str(params.get("capability") or "dev")
    action = str(params.get("action") or "implement")
    assignments: List[Dict[str, Any]] = []
    for ws in items:
        if not isinstance(ws, dict):
            continue
        assignments.append({
            "workstream": str(ws.get("id") or ws.get("workstream") or f"ws-{len(assignments) + 1}"),
            "name": str(ws.get("name") or ""),
            "capability": cap,
            "action": action,
            "files": list(ws.get("files") or []),
            "acceptance": list(ws.get("acceptance") or []),
            "edits": len(ws.get("edits") or []),
        })
    return {"ok": bool(assignments), "degraded": not assignments,
            "count": len(assignments), "assignments": assignments,
            "capability": cap, "action": action, "reason": ""}


def tune(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """经理调参：给这一批命中定权重与阈值（零 token）。

    新闻链路里它在采集之后、评估之前跑：采集只说"命中了什么"，权重和
    "算不算大事"的阈值由经理统一调——换品种、换季节只改参数，不改评估代码。

    入参：
        weights       {group: 权重}，覆盖默认（默认按关键词表的组给 1.0）
        major_weight  算"大事"的阈值（0-1，默认 0.6）
        min_level     只保留这个级别以上的事件（info/warn/error/critical）
    出参：`{ok, weights, major_weight, min_level, source, reason}`
    """
    params = params or {}
    weights: Dict[str, float] = {}
    try:
        from ..frameworks.keywords import KeywordSet

        ks = KeywordSet.load(str(params.get("keyword_set") or "news"))
        if ks is not None:
            weights = {g.group: 1.0 for g in ks.groups}
    except Exception:  # noqa: BLE001 - 读不到关键词表就不给默认权重，仍可用调用方给的
        weights = {}

    for key, value in (params.get("weights") or {}).items():
        try:
            weights[str(key)] = float(value)
        except (TypeError, ValueError):
            continue

    try:
        major = float(params.get("major_weight") if params.get("major_weight") is not None else 0.6)
    except (TypeError, ValueError):
        major = 0.6
    major = min(1.0, max(0.0, major))

    level = str(params.get("min_level") or "info").lower()
    if level not in ("info", "warn", "error", "critical"):
        level = "info"

    return {"ok": True, "degraded": False, "weights": weights,
            "major_weight": major, "min_level": level,
            "source": "config+params", "reason": ""}


def run_action(action: str, params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None,
               client: Any = None) -> Dict[str, Any]:
    """动作执行体（单测可直调）：`pack` / `handoff` / `ledger` / `assign` / `tune`。"""
    if action == "pack":
        return pack(params, ctx)
    if action == "handoff":
        return handoff(params, ctx, client=client)
    if action == "ledger":
        return ledger(params, ctx)
    if action == "assign":
        return assign(params, ctx)
    if action == "tune":
        return tune(params, ctx)
    raise ValueError(f"supervisor 不支持的动作：{action}")


def register(runtime, client: Any = None) -> None:
    """把一个 Agent 注册成管理员/监理。

    ```python
    from multi_agent.integrations.supervisor import register
    register(runtime)                       # pack / ledger 零 token，不需要连接
    register(runtime, client=hub_client)    # 加上 handoff：真正把送审件交给审计官
    ```

    Worker 少时可以和采集官绑在同一个 Agent 上：
    `--roles B,supervisor`（两份 manifest 一起注册）。
    """
    hub_client = client if client is not None else getattr(runtime, "client", None)
    runtime.add_handler("supervisor", "pack", lambda p, c: run_action("pack", p, c))
    runtime.add_handler("supervisor", "handoff",
                        lambda p, c: run_action("handoff", p, c, client=hub_client))
    runtime.add_handler("supervisor", "ledger", lambda p, c: run_action("ledger", p, c))
    runtime.add_handler("supervisor", "assign", lambda p, c: run_action("assign", p, c))
    runtime.add_handler("supervisor", "tune", lambda p, c: run_action("tune", p, c))