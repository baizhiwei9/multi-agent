"""游戏服务端开发项目（game-server）：一条"开发报告 → 发布包"的完整链路。

角色链（与需求一一对应）
------------------------
    后台管理者 producer     接开发报告/需求单 → 工单 + 验收标准 + 拆包
        │
    Coordinator（框架）     按 DAG 派活；`foreach` 把包分给 N 个 dev 分片
        │
    dev × N                 写代码/补丁/语法检查（真实项目里接 runner/编码 Agent）
        │
    经理 supervisor（框架）  pack 把各分片压成送审件，ledger 记台账
        │
    审计官 auditor（框架）   game_server profile：真的去读文档/协议/代码/迁移/配置，
        │                    审"开发的和文档理解的对不对"
    风控官 server_risk（框架）容量 / 确定性回放 / 回滚 / 灰度 / 经济 → pass/warn/block
        │
    Reporter report         产出：代码补丁 + 发布报告 + manifest（sha256）+ 证据 JSON

两条边界
--------
· 审计只读：`auditor/code` 不写代码、不改配置、不联网（rule 级 0 token）；
· 风控只算：回放执行被审代码时必须显式 `allow_exec=True`，且在子进程 + 超时里跑。
"""
from __future__ import annotations

import difflib
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from ..base import Project, RoleSpec
from ...integrations.game_server_audit import parse_endpoint_table
from ...integrations.game_server_delivery import build_delivery

#: 一次发布最多拆几个开发分片（防止一个工单扇出几十个任务）
MAX_WORKSTREAMS = 8

GAME_SERVER_PIPELINE: Dict[str, Any] = {
    "name": "game_server_release",
    "project": "game-server",
    "description": "开发报告 → 拆包 → 并行开发 → 经理送审 → 审计（协议/代码/迁移/配置）"
                   " → 风控门（回放/容量/回滚） → 发布包",
    "steps": [
        # 1) 后台管理者：把开发报告/需求单变成工单（零 token，取不到就 degraded）
        {"name": "intake", "capability": "producer", "action": "intake",
         "params": {"ticket": "${params.ticket}", "doc_root": "${params.doc_root}"}},

        # 2) 接口规范前置门：报告接口表不合法就不拆包、不下发开发（纯程序，0 token）
        {"name": "spec_gate", "capability": "producer", "action": "spec_check",
         "params": {"brief": "${steps.intake.result}"},
         "depends_on": ["intake"]},

        # 3) 拆包：规范通过才按模块/分片切成可并行的工作流
        {"name": "plan", "capability": "producer", "action": "plan",
         "params": {"brief": "${steps.intake.result}",
                    "max_workstreams": "${params.max_workstreams}"},
         "depends_on": ["spec_gate"],
         "when": {"step": "spec_gate", "path": "result.ok", "op": "eq",
                  "value": True}},

        # 4) 并行开发：每个分片一个 dev 任务（扇出上限 8）
        {"name": "dev", "capability": "dev", "action": "implement",
         "foreach": {"from": "${steps.plan.result.workstreams}", "as": "ws", "max": 8},
         "params": {"workstream": "${ws}",
                    "repo_root": "${params.repo_root}",
                    "out_dir": "${params.out_dir}"},
         "depends_on": ["plan"], "timeout_s": 600},

        # 5) 测试准备：按通过校验的接口生成用例（可选调模型写测试报告，默认不烧 token）
        {"name": "test_plan", "capability": "auditor", "action": "test_plan",
         "params": {"endpoints": "${steps.plan.result.endpoints}",
                    "llm": "${params.llm_test_report}"},
         "depends_on": ["plan"], "timeout_s": 300},

        # 6) 经理：把各分片压成送审件（只打包/截断，不审）
        {"name": "pack", "capability": "supervisor", "action": "pack",
         "params": {"payload": "${steps.dev.result}", "kind": "dev_bundle"},
         "depends_on": ["dev"]},

        # 7) 审计官：读文档/协议/代码/迁移/配置，出 verdict + findings
        {"name": "audit", "capability": "auditor", "action": "code",
         "params": {"repo_root": "${params.repo_root}",
                    "doc_root": "${params.doc_root}",
                    "ticket": "${steps.plan.result}",
                    "level": "${params.audit_level}"},
         "depends_on": ["pack"], "timeout_s": 600},

        # 8) 风控官：容量 + 回放 + 回滚 + 灰度 + 经济 → 闸门
        {"name": "gate", "capability": "server_risk", "action": "gate",
         "params": {"audit": "${steps.audit.result}",
                    "capacity": "${params.capacity}",
                    "replay": "${params.replay}",
                    "rollback": "${params.rollback}",
                    "canary_pct": "${params.canary_pct}",
                    "economy": "${params.economy}",
                    "strict": "${params.strict}"},
         "depends_on": ["audit"], "timeout_s": 300},

        # 9) 经理台账：送审件 + 审计/闸门结论
        {"name": "ledger", "capability": "supervisor", "action": "ledger",
         "params": {"pack": "${steps.pack.result}",
                    "entries": [
                        {"round": 1, "state": "${steps.audit.result.verdict}",
                         "chars": "${steps.pack.result.chars}", "delivered": True},
                        {"round": 2, "state": "${steps.gate.result.gate}",
                         "chars": 0, "delivered": True}]},
         "depends_on": ["gate"]},

        # 10) 报告官：代码补丁 + 报告 + manifest（sha256）+ 证据
        #     规范门不过时 ledger/test_plan 都被跳过，本步仍要出"未拆包"报告
        {"name": "report", "capability": "report", "action": "build",
         "params": {"ticket": "${steps.plan.result}",
                    "spec": "${steps.spec_gate.result}",
                    "test_plan": "${steps.test_plan.result}",
                    "dev": "${steps.dev.result}",
                    "pack": "${steps.pack.result}",
                    "audit": "${steps.audit.result}",
                    "gate": "${steps.gate.result}",
                    "ledger": "${steps.ledger.result}",
                    "repo_root": "${params.repo_root}",
                    "out_dir": "${params.out_dir}"},
         "depends_on": ["ledger", "test_plan"], "run_on_skipped": True},

        # 11) 发布记录入库（唯一 DB 入口）
        {"name": "store", "capability": "db", "action": "write",
         "params": {"collection": "game_releases",
                    "key": "${params.ticket_id}:latest",
                    "value": {"markdown": "${steps.report.result.markdown}",
                              "out_dir": "${steps.report.result.out_dir}",
                              "gate": "${steps.report.result.gate}",
                              "spec_ok": "${steps.spec_gate.result.ok}",
                              "audit_verdict": "${steps.report.result.audit_verdict}",
                              "files": "${steps.report.result.files}"}},
         "depends_on": ["report"]},
    ],
}

_EXAMPLES = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                                         "examples", "game_server"))
_FIXTURE = os.path.join(_EXAMPLES, "fixture")


# ═══════════════════════════════════════════════════════════ 工单解析
def _slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", str(text or "").strip().lower())
    return s.strip("-") or "ws"


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _parse_markdown_ticket(text: str) -> Dict[str, Any]:
    """从 Markdown 开发报告里抽：标题、范围、验收项、模块、接口表。

    接口表（方法/路径/鉴权/废弃）会被当成"声明清单"的一部分送进审计：
    报告写了、代码没实现 → endpoint_missing；报告说要鉴权、实现没鉴权 → auth_missing。
    """
    title = ""
    scope_lines: List[str] = []
    acceptance: List[str] = []
    modules: List[str] = []
    section = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            title = title or s[2:].strip()
            continue
        m = re.match(r"^#{2,3}\s+(.*)$", s)
        if m:
            section = m.group(1).strip()
            continue
        if not s:
            continue
        bullet = re.match(r"^[-*+]\s+(?:\[[ xX]\]\s*)?(.*)$", s)
        if "验收" in section and bullet:
            acceptance.append(bullet.group(1).strip())
        elif ("模块" in section or "分片" in section or "工作流" in section) and bullet:
            modules.append(re.split(r"[：:（(]", bullet.group(1).strip())[0].strip())
        elif section in ("范围", "背景", "目标") and not bullet:
            scope_lines.append(s)
    endpoints = parse_endpoint_table(text)
    for e in endpoints:
        e["source"] = "report"
    return {"title": title or "未命名工单", "scope": " ".join(scope_lines)[:200],
            "acceptance": acceptance, "modules": modules, "endpoints": endpoints}


def load_ticket(source: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    """把 ticket 参数变成 dict（支持 inline dict / JSON 文件 / Markdown 文件）。"""
    if isinstance(source, dict):
        return dict(source), "inline"
    if not isinstance(source, str) or not source.strip():
        return None, "未提供 ticket（开发报告/需求单）"
    if not os.path.isfile(source):
        return None, f"ticket 文件不存在：{source}"
    text = _read_text(source)
    if source.lower().endswith((".json", ".jsonc")):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            return None, f"ticket JSON 解析失败：{e}"
        return (dict(data) if isinstance(data, dict) else None,
                "" if isinstance(data, dict) else "ticket JSON 顶层必须是对象")
    parsed = _parse_markdown_ticket(text)
    parsed["source_text"] = text
    return parsed, ""


def _normalize_workstream(raw: Any, idx: int) -> Dict[str, Any]:
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict):
        raw = {"name": f"分片 {idx + 1}"}
    name = str(raw.get("name") or raw.get("title") or f"分片 {idx + 1}").strip()
    ws_id = str(raw.get("id") or _slug(name) or f"ws{idx + 1}")
    files = raw.get("files") or raw.get("paths") or []
    if isinstance(files, str):
        files = [files]
    edits = raw.get("edits") or []
    acceptance = raw.get("acceptance") or []
    return {
        "id": ws_id,
        "name": name,
        "scope": str(raw.get("scope") or raw.get("description") or ""),
        "files": [str(f) for f in files],
        "edits": [e for e in edits if isinstance(e, dict)],
        "acceptance": [str(a) for a in acceptance],
    }


# ═══════════════════════════════════════════════════════════ 路径与补丁
# 应用补丁 / 出 diff / 语法检查是"开发角色"的本事，已抽到框架层
# （`integrations/game_server_dev.py`）；这里保留同名转调，既有调用方不用改 import。
def _join_under(root: str, rel: str) -> Optional[str]:
    from ...integrations.game_server_dev import _join_under as f

    return f(root, rel)


def _apply_edits(text: str, edits: List[Dict[str, Any]],
                 rel: str) -> Tuple[str, List[Dict[str, Any]]]:
    from ...integrations.game_server_dev import _apply_edits as f

    return f(text, edits, rel)


def _syntax_check(rel: str, text: str) -> Dict[str, Any]:
    from ...integrations.game_server_dev import _syntax_check as f

    return f(rel, text)


# ═══════════════════════════════════════════════════════════ 项目
class GameServerProject(Project):
    """游戏后台开发项目：一条链路把工单变成可交付的发布包。"""

    name = "game-server"
    title = "游戏服务端开发与发布评审"
    description = "开发报告 → 并行开发 → 经理送审 → 协议/代码/迁移/配置审计 → 风控闸门 → 发布包"
    audit_project = "game_server"
    pipeline = GAME_SERVER_PIPELINE
    default_params = {
        "ticket": os.path.join(_FIXTURE, "ticket.json"),
        "repo_root": os.path.join(_FIXTURE, "repo"),
        "doc_root": os.path.join(_FIXTURE, "repo", "docs"),
        "ticket_id": "gs-demo",
        "out_dir": os.path.join(_EXAMPLES, "out"),
        "audit_level": "rule",
        "max_workstreams": MAX_WORKSTREAMS,
        "capacity": {"expected_ccu": 4200, "ccu_limit": 5000,
                     "tick_budget_ms": 50, "p99_tick_ms": 38},
        "replay": {"engine": os.path.join(_FIXTURE, "repo", "server", "sim.py"),
                   "scenario": os.path.join(_FIXTURE, "scenario.json"),
                   "allow_exec": True, "runs": 2},
        "rollback": {"plan": "回滚镜像并反向执行 004 迁移", "script": None, "tested": False},
        "canary_pct": 5,
        "economy": {"faucet_daily_cap": 0, "inflation_daily_pct": 1.2},
        "strict": True,
    }

    roles = [
        RoleSpec("producer", ["intake", "spec_check", "plan"],
                 "后台管理者：接开发报告，先按接口规范校验（不合格不拆包），再拆分片"),
        RoleSpec("dev", ["implement"],
                 "服务端开发：按分片改代码/出补丁/做语法与单测检查", instances=3),
        RoleSpec("supervisor", ["pack", "handoff", "ledger"],
                 "经理/监理：把分片压成送审件、记送审台账（框架实现）"),
        RoleSpec("auditor", ["code", "test_plan"],
                 "审计官：按文档/协议审代码、迁移、配置；按接口规范生成测试用例（框架实现）"),
        RoleSpec("server_risk", ["gate", "capacity", "replay"],
                 "风控官：容量/确定性回放/回滚/灰度/经济闸门（框架实现）"),
        RoleSpec("report", ["build"],
                 "报告官：发布报告 + manifest（sha256）+ 证据落盘"),
        RoleSpec("db", ["write"], "唯一数据库入口（DBManager 提供）"),
    ]

    # ---------- 后台管理者 ----------
    def _intake(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        source = params.get("ticket")
        if source is None and ctx.get("params"):
            source = ctx["params"].get("ticket")
        ticket, reason = load_ticket(source)
        if ticket is None:
            return {"ok": False, "degraded": True, "reason": reason,
                    "ticket_id": params.get("ticket_id") or "unknown",
                    "title": "未提供开发报告", "acceptance": [], "workstreams": []}

        ticket_id = str(ticket.get("ticket_id") or ticket.get("id")
                        or params.get("ticket_id") or "").strip()
        if not ticket_id:
            ticket_id = "gs-" + _slug(str(ticket.get("title") or "ticket"))[:24]
        acceptance = [str(a) for a in (ticket.get("acceptance") or ticket.get("accept") or [])]
        raw_ws = ticket.get("workstreams") or ticket.get("modules") or []
        if not raw_ws:
            raw_ws = [{"name": ticket.get("title") or "整体交付",
                       "files": ticket.get("files") or [],
                       "edits": ticket.get("edits") or [],
                       "acceptance": acceptance}]
        workstreams = [_normalize_workstream(w, i) for i, w in enumerate(raw_ws[:MAX_WORKSTREAMS])]
        for w in workstreams:
            w["ticket_id"] = ticket_id

        doc_root = params.get("doc_root")
        docs = []
        if isinstance(doc_root, str) and os.path.isdir(doc_root):
            docs = sorted(f for f in os.listdir(doc_root) if f.lower().endswith((".md", ".txt")))

        return {
            "ok": True,
            "degraded": False,
            "ticket_id": ticket_id,
            "title": str(ticket.get("title") or ticket_id),
            "scope": str(ticket.get("scope") or ticket.get("background") or "")[:400],
            "acceptance": acceptance,
            "endpoints": list(ticket.get("endpoints") or []),
            "workstreams": workstreams,
            "docs": docs,
            "changed_files": sorted({f for w in workstreams for f in w.get("files") or []}),
            "source": "inline" if isinstance(source, dict) else str(source),
        }

    def _plan(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        brief = params.get("brief") if isinstance(params.get("brief"), dict) else {}
        if not brief or brief.get("degraded"):
            return {"ok": False, "degraded": True,
                    "reason": (brief or {}).get("reason") or "上游工单不可用",
                    "ticket_id": (brief or {}).get("ticket_id") or "unknown",
                    "acceptance": [], "workstreams": [], "count": 0}
        workstreams = [_normalize_workstream(w, i)
                       for i, w in enumerate(brief.get("workstreams") or [])]
        limit = int(params.get("max_workstreams") or MAX_WORKSTREAMS)
        dropped = max(0, len(workstreams) - limit)
        workstreams = workstreams[:limit]
        for w in workstreams:
            w["ticket_id"] = brief.get("ticket_id")
            if not w.get("acceptance"):
                w["acceptance"] = list(brief.get("acceptance") or [])
        if not workstreams:
            workstreams = [{"id": "core", "name": "整体交付", "scope": brief.get("scope"),
                            "files": brief.get("changed_files") or [], "edits": [],
                            "acceptance": brief.get("acceptance") or [],
                            "ticket_id": brief.get("ticket_id")}]
        return {
            "ok": True, "degraded": False,
            "ticket_id": brief.get("ticket_id"),
            "title": brief.get("title"),
            "scope": brief.get("scope"),
            "acceptance": brief.get("acceptance") or [],
            "endpoints": brief.get("endpoints") or [],
            "workstreams": workstreams,
            "count": len(workstreams),
            "dropped": dropped,
            "docs": brief.get("docs") or [],
        }

    # ---------- 开发（真实项目把这里换成 runner / 编码 Agent） ----------
    def _implement(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        """开发角色：实现放在框架层（`integrations/game_server_dev.py`）。"""
        from ...integrations.game_server_dev import implement

        return implement(params, ctx)

    # ---------- 接口规范前置门 ----------
    def _spec_check(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        """报告接口表 → 规范校验；不合格就不拆包（下游 plan 用 when 卡住）。"""
        from ...integrations.game_server_spec import spec_check
        return spec_check(params, ctx)

    # ---------- 报告官 ----------
    def _build(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        return build_delivery(params, ctx)

    # ---------- 注册 ----------
    def handlers(self) -> Dict[str, Dict[str, Any]]:
        # supervisor / auditor(case/test_plan) / server_risk 由框架注册，这里不能覆盖
        return {
            "producer": {"intake": self._intake, "spec_check": self._spec_check,
                         "plan": self._plan},
            "dev": {"implement": self._implement},
            "report": {"build": self._build},
        }

    def register(self, runtime: Any) -> None:
        super().register(runtime)
        client = getattr(runtime, "client", None)
        from ...integrations.supervisor import register as register_supervisor
        from ...integrations.game_server_audit import register as register_game_audit
        from ...integrations.game_server_risk import register as register_server_risk
        from ...integrations.game_server_spec import register_spec, register_test_plan

        register_supervisor(runtime, client=client)   # pack / ledger 零 token
        register_game_audit(runtime)                  # auditor/code
        register_server_risk(runtime)                 # server_risk/gate|capacity|replay
        register_spec(runtime)                        # producer/spec_check（前置门）
        register_test_plan(runtime)                   # auditor/test_plan（用例 + 可选模型）


def _hash(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def install(registry: Any = None) -> GameServerProject:
    """把项目和流水线登记进框架（与示例项目同一套继承语义）。"""
    from ..base import REGISTRY, install_pipelines

    reg = registry or REGISTRY
    project = GameServerProject()
    reg.register(project, replace=True)
    install_pipelines(reg)
    return project