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

from .base import Project, RoleSpec
from ..integrations.game_server_delivery import build_delivery

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

        # 2) 拆包：按模块/分片切成可并行的工作流
        {"name": "plan", "capability": "producer", "action": "plan",
         "params": {"brief": "${steps.intake.result}",
                    "max_workstreams": "${params.max_workstreams}"},
         "depends_on": ["intake"]},

        # 3) 并行开发：每个分片一个 dev 任务（扇出上限 8）
        {"name": "dev", "capability": "dev", "action": "implement",
         "foreach": {"from": "${steps.plan.result.workstreams}", "as": "ws", "max": 8},
         "params": {"workstream": "${ws}",
                    "repo_root": "${params.repo_root}",
                    "out_dir": "${params.out_dir}"},
         "depends_on": ["plan"], "timeout_s": 600},

        # 4) 经理：把各分片压成送审件（只打包/截断，不审）
        {"name": "pack", "capability": "supervisor", "action": "pack",
         "params": {"payload": "${steps.dev.result}", "kind": "dev_bundle"},
         "depends_on": ["dev"]},

        # 5) 审计官：读文档/协议/代码/迁移/配置，出 verdict + findings
        {"name": "audit", "capability": "auditor", "action": "code",
         "params": {"repo_root": "${params.repo_root}",
                    "doc_root": "${params.doc_root}",
                    "ticket": "${steps.plan.result}",
                    "level": "${params.audit_level}"},
         "depends_on": ["pack"], "timeout_s": 600},

        # 6) 风控官：容量 + 回放 + 回滚 + 灰度 + 经济 → 闸门
        {"name": "gate", "capability": "server_risk", "action": "gate",
         "params": {"audit": "${steps.audit.result}",
                    "capacity": "${params.capacity}",
                    "replay": "${params.replay}",
                    "rollback": "${params.rollback}",
                    "canary_pct": "${params.canary_pct}",
                    "economy": "${params.economy}",
                    "strict": "${params.strict}"},
         "depends_on": ["audit"], "timeout_s": 300},

        # 7) 经理台账：送审件 + 审计/闸门结论
        {"name": "ledger", "capability": "supervisor", "action": "ledger",
         "params": {"pack": "${steps.pack.result}",
                    "entries": [
                        {"round": 1, "state": "${steps.audit.result.verdict}",
                         "chars": "${steps.pack.result.chars}", "delivered": True},
                        {"round": 2, "state": "${steps.gate.result.gate}",
                         "chars": 0, "delivered": True}]},
         "depends_on": ["gate"]},

        # 8) 报告官：代码补丁 + 报告 + manifest（sha256）+ 证据
        {"name": "report", "capability": "report", "action": "build",
         "params": {"ticket": "${steps.plan.result}",
                    "dev": "${steps.dev.result}",
                    "pack": "${steps.pack.result}",
                    "audit": "${steps.audit.result}",
                    "gate": "${steps.gate.result}",
                    "ledger": "${steps.ledger.result}",
                    "repo_root": "${params.repo_root}",
                    "out_dir": "${params.out_dir}"},
         "depends_on": ["ledger"]},

        # 9) 发布记录入库（唯一 DB 入口）
        {"name": "store", "capability": "db", "action": "write",
         "params": {"collection": "game_releases",
                    "key": "${params.ticket_id}:latest",
                    "value": {"markdown": "${steps.report.result.markdown}",
                              "out_dir": "${steps.report.result.out_dir}",
                              "gate": "${steps.report.result.gate}",
                              "audit_verdict": "${steps.report.result.audit_verdict}",
                              "files": "${steps.report.result.files}"}},
         "depends_on": ["report"]},
    ],
}

_EXAMPLES = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
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
    """从 Markdown 开发报告里抽：标题、范围、验收项、模块。"""
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
    return {"title": title or "未命名工单", "scope": " ".join(scope_lines)[:200],
            "acceptance": acceptance, "modules": modules}


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
def _join_under(root: str, rel: str) -> Optional[str]:
    """把相对路径限制在 root 内（防 .. 穿越 / 绝对路径逃逸）。"""
    if not root or not isinstance(rel, str) or not rel.strip():
        return None
    root_abs = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_abs, rel))
    if target != root_abs and not target.startswith(root_abs + os.sep):
        return None
    return target


def _apply_edits(text: str, edits: List[Dict[str, Any]],
                 rel: str) -> Tuple[str, List[Dict[str, Any]]]:
    """在内存里应用 find/replace；找不到 find 就记一条 warning，不假装改过。"""
    notes: List[Dict[str, Any]] = []
    new = text
    for e in edits:
        target = e.get("file")
        if target and os.path.normpath(str(target)) != os.path.normpath(rel):
            continue
        find = e.get("find")
        repl = e.get("replace", "")
        if not isinstance(find, str) or not find:
            notes.append({"file": rel, "status": "skipped", "reason": "edit 缺 find"})
            continue
        if find not in new:
            notes.append({"file": rel, "status": "miss", "reason": "find 未命中",
                          "find": find[:80]})
            continue
        count = new.count(find)
        new = new.replace(find, str(repl))
        notes.append({"file": rel, "status": "applied", "count": count})
    return new, notes


def _syntax_check(rel: str, text: str) -> Dict[str, Any]:
    if not rel.endswith(".py"):
        return {"name": f"syntax:{rel}", "status": "skipped", "detail": "非 Python 文件"}
    try:
        compile(text, rel, "exec")
        return {"name": f"syntax:{rel}", "status": "passed", "detail": ""}
    except SyntaxError as e:
        return {"name": f"syntax:{rel}", "status": "failed",
                "detail": f"{e.msg} (line {e.lineno})"}


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
        RoleSpec("producer", ["intake", "plan"],
                 "后台管理者：接开发报告/需求单，转工单与验收标准，拆成可并行分片"),
        RoleSpec("dev", ["implement"],
                 "服务端开发：按分片改代码/出补丁/做语法与单测检查", instances=3),
        RoleSpec("supervisor", ["pack", "handoff", "ledger"],
                 "经理/监理：把分片压成送审件、记送审台账（框架实现）"),
        RoleSpec("auditor", ["code"],
                 "审计官：按文档/协议审代码、迁移、配置、事务与验收覆盖（框架实现）"),
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
            "workstreams": workstreams,
            "count": len(workstreams),
            "dropped": dropped,
            "docs": brief.get("docs") or [],
        }

    # ---------- 开发（真实项目把这里换成 runner / 编码 Agent） ----------
    def _implement(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        ws = params.get("workstream") if isinstance(params.get("workstream"), dict) else {}
        repo_root = params.get("repo_root")
        out_dir = params.get("out_dir") or os.path.join(os.getcwd(), "release")
        ticket_id = str(ws.get("ticket_id") or params.get("ticket_id") or "ticket")
        ws_id = str(ws.get("id") or "ws")
        name = str(ws.get("name") or ws_id)

        if not isinstance(repo_root, str) or not os.path.isdir(repo_root):
            return {"ok": False, "degraded": True, "workstream": ws_id,
                    "reason": f"repo_root 不可用：{repo_root!r}"}
        files = ws.get("files") or []
        if not files:
            return {"ok": False, "degraded": True, "workstream": ws_id,
                    "reason": "该分片没有指定 files，无法产出可审的代码"}

        edits = [e for e in (ws.get("edits") or []) if isinstance(e, dict)]
        diffs: List[str] = []
        file_rows: List[Dict[str, Any]] = []
        tests: List[Dict[str, Any]] = []
        notes: List[Dict[str, Any]] = []
        changed = 0

        for rel in files:
            full = _join_under(repo_root, rel)
            if full is None:
                notes.append({"file": rel, "status": "blocked", "reason": "路径越界，拒绝写入"})
                continue
            if not os.path.isfile(full):
                notes.append({"file": rel, "status": "missing", "reason": "文件不存在"})
                continue
            before = _read_text(full)
            after, applied = _apply_edits(before, edits, rel)
            notes.extend(applied)
            status = "unchanged"
            if after != before:
                status = "modified"
                changed += 1
                # 先落盘到工作副本（CI checkout 的语义），再出补丁
                with open(full, "w", encoding="utf-8") as fh:
                    fh.write(after)
                diff = difflib.unified_diff(
                    before.splitlines(True), after.splitlines(True),
                    fromfile=f"a/{rel}", tofile=f"b/{rel}")
                diffs.append("".join(diff))
            file_rows.append({
                "path": rel, "status": status,
                "sha256_before": _hash(before), "sha256_after": _hash(after),
                "bytes": len(after.encode("utf-8")),
            })
            tests.append(_syntax_check(rel, after))

        patch_dir = os.path.join(out_dir, "patches", ticket_id)
        os.makedirs(patch_dir, exist_ok=True)
        patch_path = os.path.join(patch_dir, f"{ws_id}.patch")
        patch_text = "\n".join(diffs).strip() or "# 本分片无代码变更（见 notes）\n"
        with open(patch_path, "w", encoding="utf-8") as fh:
            fh.write(patch_text + "\n")

        failed_tests = [t for t in tests if t.get("status") == "failed"]
        summary_lines = [
            f"分片：{name}（{ws_id}）",
            f"补丁：{patch_path}",
            f"变更文件：{changed}/{len(file_rows)}",
            f"检查：{sum(1 for t in tests if t.get('status') == 'passed')} 通过"
            f" / {len(failed_tests)} 失败",
        ]
        for n in notes:
            if n.get("status") in ("miss", "missing", "blocked"):
                summary_lines.append(f"注意：{n.get('file')} {n.get('reason')}")
        return {
            "ok": not failed_tests,
            "degraded": bool(failed_tests) or changed == 0,
            "workstream": ws_id,
            "name": name,
            "patch": patch_path,
            "applied": changed > 0,
            "files": file_rows,
            "tests": tests,
            "notes": notes,
            "chars": len(patch_text),
            "summary": "\n".join(summary_lines),
            "reason": "" if changed else "没有产生任何代码变更",
        }

    # ---------- 报告官 ----------
    def _build(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        return build_delivery(params, ctx)

    # ---------- 注册 ----------
    def handlers(self) -> Dict[str, Dict[str, Any]]:
        # supervisor / auditor / server_risk 由框架注册，这里绝不能实现（否则覆盖真实现）
        return {
            "producer": {"intake": self._intake, "plan": self._plan},
            "dev": {"implement": self._implement},
            "report": {"build": self._build},
        }

    def register(self, runtime: Any) -> None:
        super().register(runtime)
        client = getattr(runtime, "client", None)
        from ..integrations.supervisor import register as register_supervisor
        from ..integrations.game_server_audit import register as register_game_audit
        from ..integrations.game_server_risk import register as register_server_risk

        register_supervisor(runtime, client=client)   # pack / ledger 零 token
        register_game_audit(runtime)                  # auditor/code
        register_server_risk(runtime)                 # server_risk/gate|capacity|replay


def _hash(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def install(registry: Any = None) -> GameServerProject:
    """把项目和流水线登记进框架（与示例项目同一套继承语义）。"""
    from .base import REGISTRY, install_pipelines

    reg = registry or REGISTRY
    project = GameServerProject()
    reg.register(project, replace=True)
    install_pipelines(reg)
    return project