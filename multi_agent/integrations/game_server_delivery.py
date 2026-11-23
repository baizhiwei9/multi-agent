"""游戏服务端交付（Reporter 侧）：把"代码补丁 + 审计 + 风控"打成可交付的发布包。

交付契约
--------
一次发布产出一个目录，里面必须同时有：

    release_report.md      ← 人看的结论（闸门/审计/清单/未决项）
    manifest.json          ← 机器看的清单（每个文件 path/bytes/sha256/kind）
    checksums.sha256       ← 可直接 `sha256sum -c` 校验
    artifacts/             ← 开发补丁、回放证据、审计/风控 JSON
    evidence/              ← 审计与风控原始回执（可复算）

三条硬规矩（与 `projects/reporter.py` 一致）
------------------------------------------
1. **不重算**：报告只消费上游结果（工单/开发包/审计/闸门/台账），缺数据就写"未提供"；
2. **可复算**：报告里出现的数字（端点缺失数、error 数、CCU 比）都能在 evidence 里找到；
3. **零 token**：纯模板拼接；要润色由调用方在报告外做。

路径安全：只复制 `artifacts` 里显式给出的文件，落点固定在 `out_dir/artifacts/`
（同名冲突自动加序号），不跟随目录、不做递归拷贝。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 注意：这里**不继承** projects/reporter.Reporter。projects 包在导入时会加载
# projects/game_server.py，而它又要 import 本模块；从 projects.reporter 取基类会形成
# 包级循环导入。发布评审报告本来就只服务本分支，独立实现 build/save 即可。

_KIND_BY_EXT = {
    ".patch": "patch", ".diff": "patch", ".md": "report", ".json": "evidence",
    ".sql": "migration", ".py": "code", ".js": "code", ".ts": "code",
    ".go": "code", ".java": "code", ".sh": "script", ".txt": "text",
}
_LEVEL_ICON = {"ok": "✅", "warn": "⚠️", "block": "❌"}


# ═══════════════════════════════════════════════════════════ 文件与哈希
def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _kind_of(path: str) -> str:
    return _KIND_BY_EXT.get(os.path.splitext(path)[1].lower(), "file")


def _unique_dest(dest_dir: str, name: str, used: set) -> str:
    base, ext = os.path.splitext(name)
    candidate = name
    i = 1
    while candidate in used or os.path.exists(os.path.join(dest_dir, candidate)):
        candidate = f"{base}-{i}{ext}"
        i += 1
    used.add(candidate)
    return candidate


def copy_artifacts(out_dir: str, artifacts: Sequence[Any]) -> List[Dict[str, Any]]:
    """把开发补丁/证据复制进 `artifacts/`，返回清单（相对路径）。"""
    dest_dir = os.path.join(out_dir, "artifacts")
    os.makedirs(dest_dir, exist_ok=True)
    entries: List[Dict[str, Any]] = []
    used: set = set()
    for item in artifacts:
        src = item.get("path") if isinstance(item, dict) else item
        if not isinstance(src, str) or not os.path.isfile(src):
            continue
        name = item.get("name") if isinstance(item, dict) and item.get("name") \
            else os.path.basename(src)
        dest_name = _unique_dest(dest_dir, name, used)
        dest = os.path.join(dest_dir, dest_name)
        shutil.copyfile(src, dest)
        entries.append({
            "path": f"artifacts/{dest_name}",
            "kind": (item.get("kind") if isinstance(item, dict) and item.get("kind")
                     else _kind_of(src)),
            "bytes": os.path.getsize(dest),
            "sha256": sha256_file(dest),
        })
    return entries


def write_manifest(out_dir: str, entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    manifest = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "out_dir": os.path.abspath(out_dir),
        "count": len(entries),
        "total_bytes": sum(int(e.get("bytes") or 0) for e in entries),
        "files": entries,
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "checksums.sha256"), "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(f"{e['sha256']}  {e['path']}\n")
    return manifest


def write_evidence(out_dir: str, evidence: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把审计/风控/开发回执落成 JSON，报告里的数字都能在这里复算。"""
    dest_dir = os.path.join(out_dir, "evidence")
    os.makedirs(dest_dir, exist_ok=True)
    files: List[Dict[str, Any]] = []
    for name, payload in evidence.items():
        if payload is None:
            continue
        path = os.path.join(dest_dir, f"{name}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
        files.append({"path": f"evidence/{name}.json", "kind": "evidence",
                      "bytes": os.path.getsize(path), "sha256": sha256_file(path)})
    return files


# ═══════════════════════════════════════════════════════════ 报告器
def _short(text: Any, n: int = 70) -> str:
    s = str(text or "").replace("\n", " ")
    return s if len(s) <= n else s[:n - 1] + "…"


def _gate_text(gate: Dict[str, Any]) -> str:
    verdict = (gate or {}).get("gate") or (gate or {}).get("verdict")
    return {"pass": "通过", "warn": "有条件通过", "block": "拦截"}.get(verdict, "未评估")


class GameServerReporter:
    """游戏服务端发布评审报告：结论前置，证据可追。"""

    name = "game-server-release"
    description = "发布评审（工单/开发/审计/闸门/清单/文件哈希）"
    project = "game-server"
    title = "游戏服务端发布评审"

    def save(self, markdown: str, path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(markdown)
        return path

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "project": self.project,
                "description": self.description}

    def build(self, context: Dict[str, Any]) -> str:
        ctx = context or {}
        ticket = ctx.get("ticket") or {}
        dev = ctx.get("dev") or []
        audit = ctx.get("audit") or {}
        gate = ctx.get("gate") or {}
        manifest = ctx.get("manifest") or {}
        as_of = ctx.get("as_of") or datetime.now().strftime("%Y-%m-%d %H:%M")
        gate_verdict = gate.get("gate") or gate.get("verdict") or "unknown"
        verdict_cn = {"pass": "通过", "warn": "有条件通过", "block": "未通过",
                      "unknown": "未评估"}.get(gate_verdict, gate_verdict)
        audit_verdict = audit.get("verdict") or audit.get("decision") or "unknown"
        title = ctx.get("title") or f"{self.title}：{ticket.get('title') or ticket.get('ticket_id') or '未命名工单'}"
        if gate_verdict == "block":
            title = "【未通过】" + title

        lines: List[str] = [f"# {title}", "", f"- 生成时间：{as_of}"]
        for key, label in (("ticket_id", "工单"), ("scope", "范围"), ("repo_root", "代码根目录")):
            if ticket.get(key) or ctx.get(key):
                lines.append(f"- {label}：{ticket.get(key) or ctx.get(key)}")
        lines.append("")

        # ---- 结论
        lines += ["## 结论", ""]
        lines.append(f"- 上线闸门：**{verdict_cn}**（audit={audit_verdict}）")
        if gate.get("blocked_by"):
            lines.append(f"- 拦截项：{', '.join(gate['blocked_by'])}")
        elif gate.get("warns"):
            lines.append(f"- 待确认：{', '.join(gate['warns'])}")
        lines.append(f"- 交付文件：{manifest.get('count', 0)} 个，"
                     f"合计 {manifest.get('total_bytes', 0)} 字节")
        if ctx.get("out_dir"):
            lines.append(f"- 交付目录：`{ctx['out_dir']}`")
        lines += ["", "> 结论只认证据：审计看代码/文档/协议，闸门看容量/回放/回滚/经济；"
                      "任何一项为 block 都不会被写成\"已通过\"。", ""]

        # ---- 工单与验收
        lines += ["## 工单与验收", ""]
        acc = ticket.get("acceptance") or []
        if acc:
            covered = set((audit.get("snapshot_summary") or {}).get("acceptance", {})
                          .get("covered") or [])
            for item in acc:
                mark = "✅" if item in covered else ("❓" if not covered else "⚠️")
                lines.append(f"- {mark} {item}")
        else:
            lines.append("- 未提供验收标准（无法判断\"开发的和要的对不对\"）")
        ws_list = ticket.get("workstreams") or []
        if ws_list:
            lines += ["", "拆包：", "",
                      "| 工单分片 | 负责范围 | 变更文件 |", "|---|---|---|"]
            for w in ws_list:
                lines.append(f"| {w.get('name') or w.get('id')} | {w.get('scope') or '—'} "
                             f"| {', '.join(w.get('files') or []) or '—'} |")
        lines.append("")

        # ---- 开发产出
        lines += ["## 开发产出", ""]
        if isinstance(dev, list) and dev:
            lines += ["| 分片 | 补丁 | 变更文件 | 语法/单测 |", "|---|---|---|---|"]
            for item in dev:
                if not isinstance(item, dict):
                    continue
                tests = item.get("tests") or []
                ok = sum(1 for t in tests if t.get("status") == "passed")
                lines.append(
                    f"| {item.get('workstream') or item.get('id') or '—'} "
                    f"| {os.path.basename(item.get('patch') or '') or '—'} "
                    f"| {len(item.get('files') or [])} "
                    f"| {ok}/{len(tests) if tests else 0} |")
            pending = [d for d in dev if isinstance(d, dict) and d.get("degraded")]
            if pending:
                lines += ["", f"- ⚠️ {len(pending)} 个分片降级："
                              + "；".join(_short(d.get("reason")) for d in pending)]
        else:
            lines.append("- 未提供开发产出")
        lines.append("")

        # ---- 审计
        lines += ["## 审计结论", ""]
        counts = audit.get("counts") or {}
        lines.append(f"- 判定：**{audit_verdict}**（error {counts.get('error', 0)} / "
                     f"warn {counts.get('warn', 0)} / info {counts.get('info', 0)}）")
        summary = audit.get("snapshot_summary") or {}
        sc = summary.get("counts") or {}
        if sc:
            lines.append(f"- 依据：协议 {sc.get('declared', 0)} 个接口，代码 {sc.get('implemented', 0)} 个，"
                         f"文档 {(summary.get('docs') or []) and len(summary.get('docs') or []) or 0} 份，"
                         f"迁移 {sc.get('migrations', 0)} 个")
        findings = sorted((audit.get("findings") or []),
                          key=lambda f: {"error": 0, "warn": 1, "info": 2}.get(
                              (f or {}).get("severity"), 3))
        if findings:
            lines += ["", "| 级别 | 规则 | 说明 | 位置 |", "|---|---|---|---|"]
            for f in findings[:14]:
                if not isinstance(f, dict):
                    continue
                lines.append(f"| {f.get('severity')} | `{f.get('code')}` "
                             f"| {_short(f.get('message'), 80)} | {f.get('where') or '—'} |")
            if len(findings) > 14:
                lines.append(f"| … | … | 其余 {len(findings) - 14} 条见 evidence/audit.json | — |")
        else:
            lines.append("- 未发现规则级问题")
        lines.append("")

        # ---- 风控闸门
        lines += ["## 风控闸门", ""]
        checks = gate.get("checks") or []
        if checks:
            lines += ["| 检查 | 结论 | 说明 |", "|---|---|---|"]
            for c in checks:
                if not isinstance(c, dict):
                    continue
                lines.append(f"| {c.get('name')} | {_LEVEL_ICON.get(c.get('level'), c.get('level'))} "
                             f"{c.get('level')} | {_short(c.get('detail'), 90)} |")
        else:
            lines.append("- 未提供闸门结果")
        rp = gate.get("replay") or {}
        if rp:
            lines.append("")
            lines.append(f"- 回放：deterministic={rp.get('deterministic')}，"
                         f"runs={rp.get('runs')}，errors={rp.get('errors') or '无'}")
        lines.append("")

        # ---- 上线清单
        lines += ["## 上线清单", ""]
        checklist = gate.get("checklist") or _checklist(gate, audit)
        for item in checklist:
            lines.append(f"- [ ] {item}")
        lines.append("")

        # ---- 交付文件
        lines += ["## 交付文件", ""]
        files = manifest.get("files") or []
        if files:
            lines += ["| 相对路径 | 类型 | 字节 | sha256 |", "|---|---|---|---|"]
            for f in files:
                if not isinstance(f, dict):
                    continue
                lines.append(f"| `{f.get('path')}` | {f.get('kind')} | {f.get('bytes')} "
                             f"| `{str(f.get('sha256'))[:16]}…` |")
        else:
            lines.append("- 未生成文件清单")
        lines.append("")

        # ---- 未决项
        lines += ["## 未决项", ""]
        unresolved: List[str] = []
        for c in checks:
            if isinstance(c, dict) and c.get("level") in ("block", "warn"):
                unresolved.append(f"{c.get('name')}：{c.get('detail')}")
        for f in findings:
            if isinstance(f, dict) and f.get("severity") == "error":
                unresolved.append(f"`{f.get('code')}`：{f.get('message')}")
        uncovered = (summary.get("acceptance") or {}).get("uncovered") or []
        for item in uncovered:
            unresolved.append(f"验收无文档依据：{item}")
        if unresolved:
            seen = set()
            for item in unresolved:
                key = _short(item, 120)
                if key in seen:
                    continue
                seen.add(key)
                lines.append(f"- {item}")
        else:
            lines.append("- 无")
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _checklist(gate: Dict[str, Any], audit: Dict[str, Any]) -> List[str]:
    items: List[str] = []
    if (gate or {}).get("blocked_by"):
        items.append("先清掉 blocked_by 里的拦截项，再重新送审")
    items += [
        "应用开发补丁并在干净环境重建",
        "按版本顺序执行迁移，确认无缺口/无破坏性变更",
        "执行回滚脚本演练（或确认可一键回退到上个版本）",
        f"灰度发布（建议 1-5%，当前 {gate.get('canary_pct', '未提供')}）",
        "观察 30 分钟：错误率、p99 延迟、掉线率、经济产出",
    ]
    if (audit or {}).get("verdict") != "pass":
        items.append("审计未通过：按 findings 逐条整改后重跑 audit → gate")
    return items


# ═══════════════════════════════════════════════════════════ 一键交付
def build_delivery(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None
                   ) -> Dict[str, Any]:
    """渲染报告 + 复制补丁/证据 + 生成 manifest/checksums。"""
    params = params or {}
    out_dir = params.get("out_dir")
    if not out_dir:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = os.path.join(os.getcwd(), "release", stamp)
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    evidence = {
        "audit": params.get("audit"),
        "gate": params.get("gate"),
        "dev": params.get("dev"),
        "pack": params.get("pack"),
        "ledger": params.get("ledger"),
        "ticket": params.get("ticket"),
    }
    entries = write_evidence(out_dir, evidence)

    artifacts: List[Any] = []
    dev = params.get("dev")
    if isinstance(dev, list):
        artifacts += [d.get("patch") for d in dev if isinstance(d, dict) and d.get("patch")]
    elif isinstance(dev, dict) and dev.get("patch"):
        artifacts.append(dev["patch"])
    if params.get("artifacts"):
        artifacts += list(params["artifacts"])
    entries += copy_artifacts(out_dir, artifacts)

    context = {
        "title": params.get("title"),
        "as_of": params.get("as_of"),
        "ticket": params.get("ticket") or {},
        "dev": params.get("dev") or [],
        "audit": params.get("audit") or {},
        "gate": params.get("gate") or {},
        "pack": params.get("pack") or {},
        "ledger": params.get("ledger") or {},
        "repo_root": params.get("repo_root"),
        "out_dir": out_dir,
        "manifest": {"count": len(entries),
                     "total_bytes": sum(int(e.get("bytes") or 0) for e in entries),
                     "files": entries},
    }
    report_md = GameServerReporter().build(context)
    report_path = os.path.join(out_dir, "release_report.md")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(report_md)
    entries.append({"path": "release_report.md", "kind": "report",
                    "bytes": os.path.getsize(report_path),
                    "sha256": sha256_file(report_path)})
    manifest = write_manifest(out_dir, entries)

    gate = params.get("gate") or {}
    return {
        "ok": True,
        "out_dir": out_dir,
        "report_path": report_path,
        "markdown": report_md,
        "manifest": manifest,
        "files": manifest["files"],
        "count": manifest["count"],
        "total_bytes": manifest["total_bytes"],
        "gate": gate.get("gate") or gate.get("verdict"),
        "audit_verdict": (params.get("audit") or {}).get("verdict"),
        "degraded": (gate.get("gate") or gate.get("verdict")) == "block",
    }