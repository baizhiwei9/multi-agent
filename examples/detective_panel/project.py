"""侦探团（detective_panel）：多维取证 → 限席辩论 → 高思考裁决。

设计要点
--------
1. **每个侦探绑一个模型 profile**，思考强度靠 profile 区分（默认全指 DeepSeek）：

   ==================  ==================  ====================================
   profile             默认模型            用在哪
   ==================  ==================  ====================================
   detective_fast      deepseek-chat       政策官/资金官/技术官/情绪官（快、便宜）
   detective_deep      deepseek-reasoner   反方（专挑毛病，需要多想一步）
   judge_pro           deepseek-reasoner   裁决官（收口，只出结论与证伪条件）
   ==================  ==================  ====================================

   配置写在 `config/llm.json` 的 `profiles` 里，Key 放 `config/llm.local.json`（已 gitignore）。
   同一台机器、同一个 Hub，靠 profile 切换"谁用快模型、谁用高思考模型"。

2. **发言前必须有统一证据包**：所有侦探只读同一份 `evidence`，不允许自己找数。
   这是多 Agent 讨论最容易翻车的地方——不统一证据，五个 Agent 会各编一套互相说服。

3. **未配模型时不许假装跑过**：发言会带 `【模拟·未配模型】` 前缀，报告里 `mode`
   写 `simulated`，裁决写"不构成裁决"。链路能验证，结论不能当真。

4. **席位与并发是两件事**：`debate.max_participants`（默认 6）限制同场讨论人数；
   "起 300 个 Agent"是并发 worker 数（`RoleSpec.instances`），不是讨论席位。

5. **口径对齐**：引擎里 panel 的 `decision` 用 `APPROVE / MODIFY / REJECT`，
   发言的 `stance` 也只认 `APPROVE / REJECT`（`debate.py::_stance_of`）。
   本项目在**返回里把立场翻成引擎认的词**，在**报告里再翻回 BULL / BEAR / NEUTRAL**，
   两边都不含糊：

   ========  ==========  =================
   模型输出  引擎 stance  引擎 decision
   ========  ==========  =================
   BULL      看多         APPROVE
   BEAR      看空         REJECT
   NEUTRAL   不表态       MODIFY
   ========  ==========  =================

跑法
----
    python examples/detective_panel/run_demo.py      # 零依赖，跑通链路
    bash   examples/detective_panel/start_panel.sh   # 起常驻服务给 DSH 调
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from multi_agent.integrations import llm as llm_layer
from multi_agent.projects import REGISTRY, Project, RoleSpec
from multi_agent.projects.reporter import MarkdownReporter, Reporter

# ───────────────────────────────────────────── 1. 侦探席（五个维度）
DETECTIVES: List[Dict[str, Any]] = [
    {
        "capability": "policy", "action": "read", "name": "政策官",
        "profile": "detective_fast",
        "focus": "只读政策原文，找增量措辞：新提法、量化目标、表述升级、落地主体与时限",
        "checklist": ["有没有上一次没提过的新提法",
                      "有没有可核对的量化目标（金额/比例/时限）",
                      "与上一次同类会议相比，措辞是升级还是重申",
                      "落地主体是谁、什么时候见效"],
    },
    {
        "capability": "capital", "action": "probe", "name": "资金官",
        "profile": "detective_fast",
        "focus": "政策之后钱有没有真动：北向、两融、ETF 申购赎回、主力净流入",
        "checklist": ["消息次日资金流向是否与表态方向一致",
                      "是普涨放量还是少数板块抽血",
                      "杠杆资金（两融）是加还是减",
                      "ETF 是净申购还是净赎回"],
    },
    {
        "capability": "technical", "action": "structure", "name": "技术官",
        "profile": "detective_fast",
        "focus": "底部结构是否成立：均线、中枢、缺口、量能配合",
        "checklist": ["指数相对 MA40 的位置与斜率",
                      "是否离开最近中枢、有无回抽确认",
                      "上方缺口是否回补",
                      "量能是放大还是缩量反抽"],
    },
    {
        "capability": "sentiment", "action": "track", "name": "情绪官",
        "profile": "detective_fast",
        "focus": "人气能否持续：涨跌家数、涨停/炸板、成交额、期权 PCR 与偏斜",
        "checklist": ["涨跌家数比与涨停家数是否同步改善",
                      "炸板率是否下降",
                      "成交额是温和放大还是脉冲",
                      "期权 PCR / 偏斜是否转向乐观"],
    },
    {
        "capability": "contrarian", "action": "attack", "name": "反方",
        "profile": "detective_deep",
        "focus": "专打「政策底≠市场底」，给出可验证的证伪条件",
        "checklist": ["历史上同类政策后市场平均多久才见底",
                      "当前估值/盈利是否支持反转",
                      "哪些证据一旦出现就说明这次又是脉冲",
                      "如果看错，最先从哪里露馅"],
    },
]

#: 裁决官：最后一公里收口，默认用高思考模型
JUDGE: Dict[str, Any] = {
    "capability": "judge", "action": "verdict", "name": "裁决官",
    "profile": "judge_pro",
}

#: 报告官（零 token，纯拼装）
REPORT_CAP, REPORT_ACTION = "report", "build"

SIM_TAG = "【模拟·未配模型】"

#: 模型立场 → 引擎 stance（`debate.py::_stance_of` 只认这类词）
STANCE_TO_ENGINE = {"BULL": "看多", "BEAR": "看空"}
#: 模型立场 → 引擎 decision（panel 的裁决口径）
VERDICT_TO_ENGINE = {"BULL": "APPROVE", "BEAR": "REJECT", "NEUTRAL": "MODIFY"}
#: 引擎词 → 报告用词（给人看）
DECISION_TO_READABLE = {"APPROVE": "BULL（看多）", "REJECT": "BEAR（看空）",
                        "MODIFY": "NEUTRAL（不表态）"}
STANCE_TO_READABLE = {"APPROVE": "看多", "REJECT": "看空", "MODIFY": "不表态"}

_STANCE_RE = re.compile(r"(?:立场|裁决|结论)\s*[:：]\s*(BULL|BEAR|NEUTRAL)", re.I)
_CONF_RE = re.compile(r"置信度\s*[:：]\s*([0-9]*\.?[0-9]+)")


def _pick_stance(text: str) -> str:
    """从模型回包里取立场；取不到就 NEUTRAL（不猜）。"""
    m = _STANCE_RE.search(text or "")
    return m.group(1).upper() if m else "NEUTRAL"


# ───────────────────────────────────────────── 2. 证据包 → 文本
def evidence_text(evidence: Optional[Dict[str, Any]]) -> str:
    """把证据包压成模型可读的清单；空值显式写「待补」，不静默省略。"""
    if not evidence:
        return "（证据包为空：所有字段均待补，本轮任何结论都不成立）"
    lines: List[str] = []

    def _walk(node: Any, prefix: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                _walk(value, f"{prefix}.{key}" if prefix else str(key))
        elif isinstance(node, (list, tuple)):
            head = "、".join(str(v) for v in node[:6]) or "（空）"
            lines.append(f"- {prefix}：{head}")
        else:
            shown = "待补" if node in (None, "", []) else node
            lines.append(f"- {prefix}：{shown}")

    _walk(evidence, "")
    return "\n".join(lines)


def _profile_of(person: Dict[str, Any]) -> str:
    return str(person.get("profile") or "chat")


def _ask_llm(profile: str, system: str, prompt: str) -> Dict[str, Any]:
    """一次问答；返回 `llm.chat` 原始结果（失败时 `degraded=True`，不抛异常）。"""
    return llm_layer.chat({"profile": profile, "system": system, "prompt": prompt})


# ───────────────────────────────────────────── 3. 五个侦探的发言
def _prompt_for(person: Dict[str, Any], params: Dict[str, Any]) -> str:
    topic = str(params.get("topic") or "（未给议题）")
    ev = evidence_text(params.get("evidence"))
    note = str(params.get("evidence_note") or "").strip()
    transcript = str(params.get("transcript") or "").strip()
    prior = str(params.get("prior") or "").strip()
    rnd = params.get("round") or 1
    checklist = "\n".join(f"  {i}. {c}" for i, c in enumerate(person["checklist"], 1))
    parts = [
        f"议题：{topic}",
        "",
        "唯一可信证据（只能引用这里的字段，不许引入外部数字或记忆）：",
        ev,
    ]
    if note:
        parts += ["", f"证据说明：{note}"]
    parts += ["", f"本轮第 {rnd} 轮。你的必查项：", checklist]
    if transcript:
        parts += ["", "此前各席发言（可反驳，但要指名）：", transcript[-4000:]]
    elif prior:
        parts += ["", f"上一位说的是：{prior[-1500:]}"]
    parts += [
        "",
        "只输出三行，不要寒暄、不要复述证据全文：",
        "立场：BULL / BEAR / NEUTRAL（三选一）",
        "依据：（≤3 条，每条必须写明引用了哪个证据字段）",
        "证伪条件：（什么情况发生就说明你这个立场错了）",
    ]
    return "\n".join(parts)


def _simulated(person: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return {"text": (f"{SIM_TAG} 立场：NEUTRAL / 依据：{person['focus']} 相关字段待补"
                     f"（{reason or '端点未配置'}）/ 证伪条件：补齐证据后重跑"),
            "simulated": True, "reason": reason or "端点未配置"}


def speak(person: Dict[str, Any], params: Dict[str, Any],
          ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """一个侦探席的发言：有模型走模型，没模型走显式模拟，绝不伪造观点。

    返回 dict：`text` 给引擎取正文，`stance` 给引擎判收敛（只有明确看多/看空才填，
    中性留空——不表态不等于同意）。
    """
    system = (f"你是{person['name']}，只负责一个维度：{person['focus']}。"
              "你只能使用给定证据，证据缺失就说缺失，不许补数字。")
    res = _ask_llm(_profile_of(person), system, _prompt_for(person, params))
    if res.get("degraded") or not str(res.get("text") or "").strip():
        return _simulated(person, str(res.get("reason") or ""))

    text = str(res["text"])
    label = _pick_stance(text)
    out: Dict[str, Any] = {"text": text, "stance_raw": label,
                           "profile": _profile_of(person),
                           "model": res.get("model") or ""}
    if label in STANCE_TO_ENGINE:
        out["stance"] = STANCE_TO_ENGINE[label]
    return out


# ───────────────────────────────────────────── 4. 裁决官
def _judge_prompt(params: Dict[str, Any]) -> str:
    topic = str(params.get("topic") or "（未给议题）")
    ev = evidence_text(params.get("evidence"))
    transcript = str(params.get("transcript") or "")
    return "\n".join([
        f"议题：{topic}",
        "",
        "证据：", ev,
        "",
        "各席发言：", transcript[-6000:] or "（无）",
        "",
        "你是裁决官。输出四行，不写客套话：",
        "裁决：BULL / BEAR / NEUTRAL（三选一）",
        "置信度：0-1 之间的小数",
        "关键依据：（≤3 条，指名哪一席的哪条证据站得住）",
        "证伪触发：（列出 1-3 个可观测阈值，触发即推翻本次裁决）",
    ])


def verdict(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """裁决官：把模型的 BULL/BEAR/NEUTRAL 翻成引擎的 decision，只翻词不改结论。"""
    system = ("你是裁决官，只做收口：不接受无证据的观点，不接受各方都说服彼此。"
              "证据不足时给 NEUTRAL 并说明缺什么。")
    res = _ask_llm(_profile_of(JUDGE), system, _judge_prompt(params))
    if res.get("degraded") or not str(res.get("text") or "").strip():
        return {"text": f"{SIM_TAG} 裁决：NEUTRAL（未配 judge_pro 端点，不构成裁决）",
                "decision": "MODIFY", "stance_raw": "NEUTRAL", "converged": False,
                "simulated": True, "reason": str(res.get("reason") or "端点未配置")}

    text = str(res["text"])
    label = _pick_stance(text)
    conf = _CONF_RE.search(text)
    return {"text": text,
            "decision": VERDICT_TO_ENGINE.get(label, "MODIFY"),
            "stance_raw": label,
            "confidence": conf.group(1) if conf else "",
            "converged": False,          # 收敛交给引擎的 stop_on_consensus 判
            "profile": _profile_of(JUDGE),
            "model": res.get("model") or ""}


# ───────────────────────────────────────────── 5. 报告器
class DetectivePanelReporter(MarkdownReporter):
    """只实现 `sections()`：标题/时间由基类处理。"""

    name = "detective-md"
    project = "detective-panel"
    title = "侦探团多维裁决"

    def sections(self, context: Dict[str, Any]) -> List[Any]:
        panel = context.get("panel") or {}
        topic = context.get("topic") or "（未给议题）"
        mode = str(panel.get("mode") or "panel")
        decision = str(panel.get("decision") or "MODIFY")
        transcript = panel.get("transcript") or []
        judge = panel.get("judge") or {}
        judge_text = str(judge.get("text") or "")
        simulated = (any(str(t.get("text") or "").startswith(SIM_TAG) for t in transcript)
                     or judge_text.startswith(SIM_TAG))

        speakers: List[str] = []
        seen = set()
        for item in transcript:
            name = item.get("speaker") or "-"
            if name in seen and item.get("round") != 1:
                continue
            seen.add(name)
            first = str(item.get("text") or "").replace("\n", " ")
            stance = STANCE_TO_READABLE.get(str(item.get("stance") or ""), "不表态")
            speakers.append(f"- `{item.get('round')}轮` **{name}**（{stance}）：{first[:160]}")

        return [
            ("议题", [f"- {topic}",
                      f"- 出席：{'、'.join(panel.get('participants') or []) or '—'}",
                      f"- 运行口径：{mode}"
                      + ("（**含模拟发言，结论不作数**）" if simulated else "（模型实跑）")]),
            ("各方发言", speakers),
            ("裁决", [f"- 判定：**{DECISION_TO_READABLE.get(decision, decision)}**"
                      f"（引擎口径 `{decision}`）",
                      f"- 裁决官：{judge.get('name') or '—'}"
                      + (f"，模型：{judge.get('model')}" if judge.get("model") else ""),
                      "- 收敛：是" if judge.get("converged") else "- 收敛：否"]),
            ("裁决原文", [(judge_text or "（无）").replace("\n", " ")]),
            ("预算", [f"- 字数：{panel.get('chars_used')}",
                      f"- 估算 token：{panel.get('tokens_used')}",
                      f"- 停止原因：{panel.get('stopped_reason') or '—'}"]),
        ]


# ───────────────────────────────────────────── 6. 项目（继承 Project）
class DetectivePanelProject(Project):
    name = "detective-panel"
    title = "侦探团多维裁决"
    description = "五维取证 → 限席辩论（panel）→ 高思考裁决 → 报告"
    audit_project: Optional[str] = None
    default_params = {"topic": "（未给议题）"}

    def __init__(self, reporter: Optional[Reporter] = None, instances: int = 1) -> None:
        self._reporter = reporter or DetectivePanelReporter()
        self._instances = max(1, int(instances))
        # 基类的 `roles` 是类属性（`role_specs()` 直接迭代它），不能用方法覆写，
        # 所以这里按实例赋一份列表，好让 `instances` 可配。
        self.roles = (
            [RoleSpec(d["capability"], [d["action"]], d["focus"],
                      instances=self._instances) for d in DETECTIVES]
            + [RoleSpec(JUDGE["capability"], [JUDGE["action"]],
                        "裁决官：收口出结论与证伪条件", instances=1),
               RoleSpec(REPORT_CAP, [REPORT_ACTION],
                        "报告官：把发言与裁决拼成 Markdown", instances=1),
               RoleSpec("debate", ["run"],
                        "评议器（框架提供）：panel 多轮发言 + 裁决", instances=1)]
        )

    def handlers(self) -> Dict[str, Dict[str, Any]]:
        # debate / moderator 由 integrations.debate.register 提供，这里必须排除
        out: Dict[str, Dict[str, Any]] = {
            JUDGE["capability"]: {JUDGE["action"]: verdict},
            REPORT_CAP: {REPORT_ACTION: self._build},
        }
        for person in DETECTIVES:
            out.setdefault(person["capability"], {})[person["action"]] = (
                lambda p, c, _person=person: speak(_person, p, c))
        return out

    def _build(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        md = self._reporter.build({
            "title": self.title,
            "subject": params.get("topic") or "",
            "topic": params.get("topic") or "",
            "panel": params.get("panel") or {},
        })
        return {"markdown": md, "chars": len(md)}

    def register(self, runtime: Any) -> None:
        super().register(runtime)
        from multi_agent.integrations.debate import register as register_debate
        register_debate(runtime, client=getattr(runtime, "client", None))

    def reporter(self) -> Optional[Reporter]:
        return self._reporter


# ───────────────────────────────────────────── 7. 发言席清单（给流水线/DSH 复用）
def participants() -> List[Dict[str, Any]]:
    """panel 的 `participants[]`：五个侦探，顺序即发言顺序。"""
    return [{"capability": d["capability"], "action": d["action"],
             "name": d["name"], "role": d["capability"]} for d in DETECTIVES]


def judge_target() -> Dict[str, Any]:
    return {"capability": JUDGE["capability"], "action": JUDGE["action"],
            "name": JUDGE["name"]}


def load_evidence(path: Optional[str] = None) -> Dict[str, Any]:
    """读证据包；默认用 `cases/guochanghui.demo.json`（全为占位，跑前必须填）。"""
    target = path or os.environ.get("PANEL_EVIDENCE") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "cases", "guochanghui.demo.json")
    try:
        with open(target, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def panel_params(topic: str, evidence: Dict[str, Any], max_rounds: int = 2,
                 judge_every_round: bool = True) -> Dict[str, Any]:
    """直接喂给 `debate/run` 的参数（DSH 侧 MCP tool 调的就是这一份）。"""
    return {
        "topic": topic,
        "participants": participants(),
        "judge": judge_target(),
        "max_rounds": int(max_rounds),
        "judge_every_round": bool(judge_every_round),
        "stop_on_consensus": True,
        "context": {"evidence": evidence,
                    "evidence_note": "占位字段必须先补真实数据，否则结论不作数"},
    }


def install(registry: Any = None, instances: int = 1) -> DetectivePanelProject:
    reg = registry if registry is not None else REGISTRY
    project = DetectivePanelProject(instances=instances)
    reg.register(project, replace=True)
    return project