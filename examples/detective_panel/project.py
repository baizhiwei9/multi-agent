"""正反辩论（detective_panel）：框架内检索 → 两正两反 → 限轮辩论 → 收口裁决。

为什么是 4 席 3 轮
------------------
多席位不等于好结论，且 token 成本随轮数**平方级**增长（每轮每个人都要重读全部历史）。
默认最小可用组合：

    正方（提"该做"）  政策与基本面 bull_policy / 资金与技术 bull_flow
    反方（提"不该做"）估值与盈利 bear_valuation / 历史与情绪反证 bear_memory

    4 席 × 3 轮 = 12 条发言 + 最后 1 次裁决 = 13 次模型调用

检索在框架内：关键词进，事件出
------------------------------
`multi_agent/frameworks/news/` 已有采集框架（`NewsWatchFramework` + `KeywordSet`），
但它只做**关键词命中过滤与分流**，取数必须注入。所以 `news/collect` 按四级取数：

    1. Hub 上的 `search/<action>` 能力（`mcp-agent` 或 `run_once --search-mcp` 挂载）
    2. 取数命令 `NEWS_FETCH_CMD` / 参数 `cmd`：命令 stdout 输出 JSON（数组或
       {"items":[...]}），字段 `title` + `summary`/`text` + `source` + `published`/
       `published_at` + `url`。这样私有数据源不必进公开仓，运行时挂就行。
    3. `NEWS_FEED_FILE` 指向的新闻池 JSON（离线/回放）
    4. 调用方粘贴正文 `--news`（退路）

关键词由调用方给（逗号/顿号/分号/空格分隔），命中、去重、拼摘要都在框架内完成；
四级都拿不到就返回 `degraded` + 具体原因，**不编造新闻**。

模型档位（默认全 DeepSeek，只有反方用思考模式）
   detective_fast  deepseek-chat       正方两席
   detective_deep  deepseek-reasoner   反方两席（挑毛病需多想一步）
   judge_pro       deepseek-reasoner   裁决官（只跑一次，收口）

**档位可由调用方覆盖**：`context.models = {"bull_policy": "judge_pro"}` 按 capability 生效。

三条硬规矩
----------
1. 所有席位只读同一份 `evidence`，不许自己找数——不统一证据，四个 Agent 会各编一套。
2. 未配模型端点时不许假装跑过：发言带 `【模拟·未配模型】`，报告标"结论不作数"。
3. 上限由 `config/limits.json` 的 `debate` 段控制（participants 6 / speeches 24 /
   rounds 10）；4 席 × 3 轮 = 12 条，留出裁决与重试余量。

引擎口径（别写错，否则立场恒为 None、裁决恒为 MODIFY）
   BULL → stance 看多, decision APPROVE
   BEAR → stance 看空, decision REJECT
   NEUTRAL → 不表态, decision MODIFY
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from multi_agent.integrations import llm as llm_layer
from multi_agent.projects import REGISTRY, Project, RoleSpec
from multi_agent.projects.reporter import MarkdownReporter, Reporter

# ───────────────────────────────────────────── 1. 四个席位（两正两反）
DETECTIVES: List[Dict[str, Any]] = [
    {
        "capability": "bull_policy", "action": "argue", "name": "正方·政策与基本面",
        "profile": "detective_fast",
        "side": "BULL",
        "focus": "从政策与基本面找支持理由：增量措辞、量化目标、盈利与估值匹配",
        "checklist": ["政策原文有没有上一次没提过的新提法",
                      "有没有可核对的量化目标（金额/比例/时限）",
                      "当前估值与盈利是否支持向上",
                      "落地主体是谁、什么时候见效"],
    },
    {
        "capability": "bull_flow", "action": "argue", "name": "正方·资金与技术",
        "profile": "detective_fast",
        "side": "BULL",
        "focus": "从资金与技术找支持理由：增量资金、均线结构、量能配合",
        "checklist": ["消息次日资金是否与表态方向一致（北向/两融/ETF 申赎）",
                      "指数相对 MA40 的位置与斜率",
                      "是否离开最近中枢、有无回抽确认",
                      "量能是温和放大还是缩量反抽"],
    },
    {
        "capability": "bear_valuation", "action": "argue", "name": "反方·估值与盈利",
        "profile": "detective_deep",
        "side": "BEAR",
        "focus": "从估值与盈利挑问题：分位、拥挤度、盈利下修、政策底≠市场底",
        "checklist": ["当前估值分位与拥挤度是否已透支",
                      "盈利预期是上修还是下修",
                      "历史上同类政策后市场平均多久才见底",
                      "哪些证据一旦出现就说明这次又是脉冲"],
    },
    {
        "capability": "bear_memory", "action": "argue", "name": "反方·历史与情绪反证",
        "profile": "detective_deep",
        "side": "BEAR",
        "focus": "用历史类比与情绪指标反证：涨停炸板、成交额脉冲、情绪退潮节奏",
        "checklist": ["之前几次同类会议后的 5/10/20 日走势",
                      "涨停家数与炸板率是否只是脉冲",
                      "成交额放大是否可持续",
                      "期权 PCR / 偏斜是否已转向乐观（乐观即拥挤）"],
    },
]

#: 裁决官：最后一公里收口，默认用高思考模型，且**只跑一次**
JUDGE: Dict[str, Any] = {
    "capability": "judge", "action": "verdict", "name": "裁决官",
    "profile": "judge_pro",
}

#: 检索官：把关键词交给框架内的取数能力
NEWS_CAP, NEWS_ACTION = "news", "collect"

#: 报告官（零 token，纯拼装）
REPORT_CAP, REPORT_ACTION = "report", "build"

SIM_TAG = "【模拟·未配模型】"

STANCE_TO_ENGINE = {"BULL": "看多", "BEAR": "看空"}
VERDICT_TO_ENGINE = {"BULL": "APPROVE", "BEAR": "REJECT", "NEUTRAL": "MODIFY"}
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


def _profile_for(person: Dict[str, Any], params: Dict[str, Any]) -> str:
    """模型档位：调用方 `models` 覆盖 > 席位默认 profile（精确匹配，写错退回默认）。"""
    override = params.get("models")
    if isinstance(override, dict):
        hit = override.get(person["capability"])
        if isinstance(hit, str) and hit.strip():
            return hit.strip()
    return str(person.get("profile") or "chat")


def _ask_llm(profile: str, system: str, prompt: str) -> Dict[str, Any]:
    """一次问答；返回 `llm.chat` 原始结果（失败时 `degraded=True`，不抛异常）。"""
    return llm_layer.chat({"profile": profile, "system": system, "prompt": prompt})


# ───────────────────────────────────────────── 3. 席位发言
def _prompt_for(person: Dict[str, Any], params: Dict[str, Any]) -> str:
    topic = str(params.get("topic") or "（未给议题）")
    ev = evidence_text(params.get("evidence"))
    note = str(params.get("evidence_note") or "").strip()
    transcript = str(params.get("transcript") or "").strip()
    prior = str(params.get("prior") or "").strip()
    rnd = params.get("round") or 1
    checklist = "\n".join(f"  {i}. {c}" for i, c in enumerate(person["checklist"], 1))
    side = person.get("side") or ""
    stance_hint = ("你这一席负责**支持**（BULL）：给出该做的理由与触发条件，"
                   "但不许编证据。" if side == "BULL" else
                   "你这一席负责**反对**（BEAR）：专挑站不住的地方，给出证伪阈值，"
                   "但不许为了反对而反对。")
    parts = [
        f"议题：{topic}",
        f"你的席位：{person['name']}（{side}）。{stance_hint}",
        "",
        "唯一可信证据（只能引用这里的字段，不许引入外部数字或记忆）：",
        ev,
    ]
    if note:
        parts += ["", f"证据说明：{note}"]
    parts += ["", f"本轮第 {rnd} 轮。你的必查项：", checklist]
    if transcript:
        parts += ["", "此前各席发言（可反驳，但要指名）：", transcript[-3500:]]
    elif prior:
        parts += ["", f"上一位说的是：{prior[-1200:]}"]
    parts += [
        "",
        "只输出三行，不要寒暄、不要复述证据全文：",
        f"立场：BULL / BEAR / NEUTRAL（你是{side}席，改主意也要说清依据）",
        "依据：（≤3 条，每条必须写明引用了哪个证据字段）",
        "证伪条件：（什么情况发生就说明你这个立场错了）",
    ]
    return "\n".join(parts)


def _simulated(person: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """模拟发言要短：它只为验证链路，不该把 token 预算撑爆。"""
    short = str(reason or "端点未配置")
    if len(short) > 60:
        short = short[:57] + "…"
    return {"text": (f"{SIM_TAG} 立场：NEUTRAL / 依据：{person['focus'][:24]}…待补"
                     f"（{short}）/ 证伪条件：补齐证据后重跑"),
            "simulated": True, "reason": reason or "端点未配置"}


def speak(person: Dict[str, Any], params: Dict[str, Any],
          ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """一个席位的发言：有模型走模型，没模型走显式模拟，绝不伪造观点。

    返回 dict：`text` 给引擎取正文，`stance` 给引擎判收敛（只有明确看多/看空才填）。
    """
    system = (f"你是{person['name']}，只负责一个维度：{person['focus']}。"
              "你只能使用给定证据，证据缺失就说缺失，不许补数字。")
    profile = _profile_for(person, params)
    res = _ask_llm(profile, system, _prompt_for(person, params))
    if res.get("degraded") or not str(res.get("text") or "").strip():
        return _simulated(person, str(res.get("reason") or ""))

    text = str(res["text"])
    label = _pick_stance(text)
    out: Dict[str, Any] = {"text": text, "stance_raw": label,
                           "profile": profile, "model": res.get("model") or ""}
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
        "各席发言（压缩稿）：", transcript[-5000:] or "（无）",
        "",
        "你是裁决官。四行，不写客套话：",
        "裁决：BULL / BEAR / NEUTRAL（三选一）",
        "置信度：0-1 之间的小数",
        "关键依据：（≤3 条，指名哪一席的哪条证据站得住）",
        "证伪触发：（1-3 个可观测阈值，触发即推翻本次裁决）",
    ])


def verdict(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """裁决官：把模型的 BULL/BEAR/NEUTRAL 翻成引擎的 decision，只翻词不改结论。"""
    system = ("你是裁决官，只做收口：不接受无证据的观点，不接受各方都说服彼此。"
              "证据不足时给 NEUTRAL 并说明缺什么。")
    res = _ask_llm(_profile_for(JUDGE, params), system, _judge_prompt(params))
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
            "profile": _profile_for(JUDGE, params),
            "model": res.get("model") or ""}


# ───────────────────────────────────────────── 5. 检索（框架内）
ITEM_TEXT_KEYS = ("summary", "text", "content", "digest")
ITEM_TIME_KEYS = ("published", "published_at", "publish_time", "datetime", "time")


def split_keywords(value: Any) -> List[str]:
    """关键词：逗号/顿号/分号/空白都当分隔符，去重保序。"""
    if isinstance(value, (list, tuple)):
        raw = [str(v) for v in value]
    else:
        raw = re.split(r"[,，、;；\s]+", str(value or ""))
    out: List[str] = []
    for item in raw:
        key = item.strip()
        if key and key not in out:
            out.append(key)
    return out


def _item_text(item: Dict[str, Any]) -> str:
    """条目正文：不同源字段名不一样（summary / text / content …）。"""
    for key in ITEM_TEXT_KEYS:
        val = str(item.get(key) or "").strip()
        if val:
            return val
    return ""


def _item_time(item: Dict[str, Any]) -> str:
    for key in ITEM_TIME_KEYS:
        val = str(item.get(key) or "").strip()
        if val:
            return val
    return ""


def news_digest(items: List[Dict[str, Any]], limit: int = 12) -> str:
    """把检索结果压成一段可塞进证据包的文本（标题·来源·时间·摘要·链接）。"""
    rows: List[str] = []
    for item in items[:limit]:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        bits = [title]
        for extra in (str(item.get("source") or "").strip(), _item_time(item)):
            if extra:
                bits.append(extra)
        line = "｜".join(bits)
        body = _item_text(item)
        if body:
            line += f"：{body[:160]}"
        url = str(item.get("url") or "").strip()
        if url:
            line += f"（{url}）"
        rows.append(f"- {line}")
    return "\n".join(rows)


def _items_from_payload(payload: Any) -> List[Dict[str, Any]]:
    """容错：list / {"items"|"results"|"data"|"events"|"articles"} / task 快照。"""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("items", "results", "data", "events", "articles"):
            val = payload.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
        result = payload.get("result")
        if result is not None:
            return _items_from_payload(result)
    return []


class NewsSearcher:
    """检索官：Hub 的 `search/*` → 取数命令 → 新闻池文件；都没有就明说。"""

    def __init__(self, client: Any = None, via: str = "search",
                 action: str = "search", limit: int = 12) -> None:
        self.client = client
        self.via = via
        self.action = action
        self.limit = max(1, int(limit))

    # -- 四种取数路径 -------------------------------------------------
    def _from_capability(self, keywords: List[str]) -> Tuple[List[Dict[str, Any]], str]:
        if self.client is None:
            return [], "没有 Hub 客户端（未经 runtime 注册）"
        items: List[Dict[str, Any]] = []
        for kw in keywords:
            task = self.client.submit(self.via, self.action,
                                      {"query": kw, "keywords": keywords,
                                       "limit": self.limit},
                                      wait=True, timeout_s=120)
            if not isinstance(task, dict) or task.get("state") != "succeeded":
                return items, f"`{self.via}/{self.action}` 调用失败：{(task or {}).get('error')}"
            items.extend(_items_from_payload(task.get("result")))
        return items, f"hub:{self.via}/{self.action}"

    def _from_cmd(self, params: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
        cmd = str(params.get("cmd") or os.environ.get("NEWS_FETCH_CMD") or "").strip()
        if not cmd:
            return [], "未配置取数命令（NEWS_FETCH_CMD 或参数 cmd）"
        timeout = float(params.get("cmd_timeout") or 180)
        try:
            proc = subprocess.run(shlex.split(cmd), capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            return [], f"取数命令超时（>{timeout:.0f}s）"
        except (OSError, ValueError) as exc:
            return [], f"取数命令起不来：{type(exc).__name__}: {exc}"
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[:200]
            return [], f"取数命令退出码 {proc.returncode}：{tail}"
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            return [], "取数命令的 stdout 不是 JSON"
        return _items_from_payload(data), "cmd"

    def _from_feed_file(self) -> Tuple[List[Dict[str, Any]], str]:
        path = os.environ.get("NEWS_FEED_FILE") or ""
        if not path:
            return [], "未设置 NEWS_FEED_FILE"
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            return [], f"NEWS_FEED_FILE 读不了：{exc}"
        return _items_from_payload(data), f"file:{path}"

    # -- 过滤：关键词命中 title 或正文即保留；同标题只留一条 -----------
    def _filter(self, items: List[Dict[str, Any]],
                keywords: List[str]) -> List[Dict[str, Any]]:
        kept: List[Dict[str, Any]] = []
        seen = set()
        for item in items:
            text = f"{item.get('title') or ''} {_item_text(item)}"
            if keywords and not any(k in text for k in keywords):
                continue
            key = str(item.get("title") or "")[:60]
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            kept.append(item)
        return kept[: self.limit]

    def collect(self, params: Dict[str, Any]) -> Dict[str, Any]:
        keywords = split_keywords(params.get("keywords"))
        if not keywords:
            return {"events": [], "degraded": True, "collector": "panel-news",
                    "reason": "未给关键词（keywords）", "keywords": []}

        items: List[Dict[str, Any]] = []
        source = ""
        errors: List[str] = []

        if params.get("via", True) is not False:
            got, note = self._from_capability(keywords)
            if got:
                items, source = got, note
            else:
                errors.append(note)
        if not items:
            got, note = self._from_cmd(params)
            if got:
                items, source = got, note
            else:
                errors.append(note)
        if not items:
            got, note = self._from_feed_file()
            if got:
                items, source = got, note
            else:
                errors.append(note)

        if not items:
            return {"events": [], "degraded": True, "collector": "panel-news",
                    "keywords": keywords,
                    "reason": "没有可用的取数源：" + "；".join(errors)}

        kept = self._filter(items, keywords)
        return {"events": kept, "scanned": len(items), "kept": len(kept),
                "collector": "panel-news", "source": source,
                "keywords": keywords, "degraded": not kept,
                "reason": "" if kept else "有回包，但没有命中关键词的条目",
                "digest": news_digest(kept, self.limit)}


# ───────────────────────────────────────────── 6. 报告器
class DetectivePanelReporter(MarkdownReporter):
    """只实现 `sections()`：标题/时间由基类处理。"""

    name = "detective-md"
    project = "detective-panel"
    title = "正反辩论裁决"

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
                      f"- 轮次：{panel.get('rounds')} 轮 / {panel.get('speeches')} 条发言",
                      f"- 运行口径：{mode}"
                      + ("（**含模拟发言，结论不作数**）" if simulated else "（模型实跑）")]),
            ("各席发言（每席首轮）", speakers),
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


# ───────────────────────────────────────────── 7. 项目（继承 Project）
class DetectivePanelProject(Project):
    name = "detective-panel"
    title = "正反辩论裁决"
    description = "框架内检索 → 两正两反四席 → 默认 3 轮 → 最后一次裁决 → 报告"
    audit_project: Optional[str] = None
    default_params = {"topic": "（未给议题）"}

    def __init__(self, reporter: Optional[Reporter] = None, instances: int = 1,
                 search_via: str = "search", search_action: str = "search",
                 news_limit: int = 12) -> None:
        self._reporter = reporter or DetectivePanelReporter()
        self._instances = max(1, int(instances))
        self._client: Any = None
        self._searcher = NewsSearcher(via=search_via, action=search_action,
                                      limit=news_limit)
        # 基类的 `roles` 是类属性（`role_specs()` 直接迭代它），不能用方法覆写，
        # 所以这里按实例赋一份列表，好让 `instances` 可配。
        self.roles = (
            [RoleSpec(d["capability"], [d["action"]], d["focus"],
                      instances=self._instances) for d in DETECTIVES]
            + [RoleSpec(NEWS_CAP, [NEWS_ACTION],
                        "检索官：关键词 → Hub 的 search/* 或取数命令，过滤在框架内",
                        instances=1),
               RoleSpec(JUDGE["capability"], [JUDGE["action"]],
                        "裁决官：收口出结论与证伪条件", instances=1),
               RoleSpec(REPORT_CAP, [REPORT_ACTION],
                        "报告官：把发言与裁决拼成 Markdown", instances=1),
               RoleSpec("debate", ["run"],
                        "评议器（框架提供）：panel 多轮发言 + 裁决", instances=1)]
        )

    def handlers(self) -> Dict[str, Dict[str, Any]]:
        # debate / moderator 由 integrations.debate.register 提供，这里必须排除
        out: Dict[str, Dict[str, Any]] = {
            NEWS_CAP: {NEWS_ACTION: self._news_collect},
            JUDGE["capability"]: {JUDGE["action"]: verdict},
            REPORT_CAP: {REPORT_ACTION: self._build},
        }
        for person in DETECTIVES:
            out.setdefault(person["capability"], {})[person["action"]] = (
                lambda p, c, _person=person: speak(_person, p, c))
        return out

    def _news_collect(self, params: Dict[str, Any],
                      ctx: Dict[str, Any]) -> Dict[str, Any]:
        return self._searcher.collect(params or {})

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
        self._client = getattr(runtime, "client", None)
        self._searcher.client = self._client
        from multi_agent.integrations.debate import register as register_debate
        register_debate(runtime, client=self._client)

    def reporter(self) -> Optional[Reporter]:
        return self._reporter


# ───────────────────────────────────────────── 8. 席位清单（DSH/流水线复用）
def participants() -> List[Dict[str, Any]]:
    """panel 的 `participants[]`：两正两反，顺序即发言顺序。"""
    return [{"capability": d["capability"], "action": d["action"],
             "name": d["name"], "role": d["side"]} for d in DETECTIVES]


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


def panel_params(topic: str, evidence: Dict[str, Any], max_rounds: int = 3,
                 judge_every_round: bool = False,
                 models: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """直接喂给 `debate/run` 的参数（DSH 侧 MCP tool 调的就是这一份）。

    · `max_rounds=3`：默认 3 轮；4 席 × 3 轮 = 12 条发言，engine 上限 24。
    · `judge_every_round=False`：只在最后一轮裁一次，省掉中间 2 次裁决调用。
    · `models`：按 capability 覆盖档位（DSH 可以用它指定谁用思考模式）。
    """
    ctx: Dict[str, Any] = {"evidence": evidence,
                           "evidence_note": "占位字段必须先补真实数据，否则结论不作数"}
    if models:
        ctx["models"] = models
    return {
        "topic": topic,
        "participants": participants(),
        "judge": judge_target(),
        "max_rounds": int(max_rounds),
        "judge_every_round": bool(judge_every_round),
        "stop_on_consensus": True,
        "context": ctx,
    }


def install(registry: Any = None, instances: int = 1,
            search_via: str = "search", search_action: str = "search",
            news_limit: int = 12) -> DetectivePanelProject:
    reg = registry if registry is not None else REGISTRY
    project = DetectivePanelProject(instances=instances, search_via=search_via,
                                    search_action=search_action, news_limit=news_limit)
    reg.register(project, replace=True)
    return project