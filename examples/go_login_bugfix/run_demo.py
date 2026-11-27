"""Go 版：一条 error 怎么变成补丁 → 重新出包 → 过审计 → 同步到目标服务器。

    python examples/go_login_bugfix/run_demo.py
    GO_BIN=/usr/local/go/bin/go python examples/go_login_bugfix/run_demo.py

和 Python 版 `examples/login_bug_watch` 的区别：那边补丁落在脚本上；这边缺陷在
**已经编译成二进制**的 Go 服务里，改完源码必须重新 `go build` 出包，审计跑的是新包，
再由「同步服务器」Worker 把包搬到目标端并在目标端自证日志。

链路（每段都是一个 capability，谁在线谁干活）：

    bug/collect → dev/locate → dev/propose → dev/implement → dev/build
                → auditor/verify →（verdict=pass 才放行）→ sync/push → report/build

跑两个 run：

    run-fix    正常链路：补丁 → 新包 → 审计 PASS → 同步 → 远端日志正确
    run-guard  负向用例：build 拿旧包冒充新包 → 审计 FAIL → sync 被跳过，目标端没被污染

全 PASS 退出码 0。
"""
from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))          # 示例自带 golib/，框架目录零改动

from golib import (find_go, go_build, run_login, selfheal,          # noqa: E402
                   sync_artifact, tool_specs)
from multi_agent.client import AgentRuntime                        # noqa: E402
from multi_agent.coordinator import CoordinatorAgent               # noqa: E402
from multi_agent.projects.base import RoleProject                  # noqa: E402
from multi_agent.projects.flows import register_flow               # noqa: E402
from multi_agent.server import HubServer                           # noqa: E402

TOKEN = "go-login-bugfix-demo"
EXAMPLE_DIR = Path(__file__).resolve().parent
FIXTURE = EXAMPLE_DIR / "fixture"
OUT = EXAMPLE_DIR / "out"

UP = "error 登录失败: bob"          # 修复后应该长这样（冒号 + 用户名）
SECRET = "123456"                   # 修复后日志里不该出现的口令

KEYWORDS = ("登录失败", "LOGIN_FAILED")


# --------------------------------------------------------------------------- #
# 四个角色项目（一个角色 = 一个 capability = 一个 Agent）
# --------------------------------------------------------------------------- #
class BugCollectProject(RoleProject):
    capability = "bug"
    actions = ("collect",)
    role_description = "采集：扫 error report 里的登录失败行"

    def handlers(self) -> Dict[str, Dict[str, Any]]:
        return {"bug": {"collect": collect_log}}


class GoDevProject(RoleProject):
    capability = "dev"
    actions = ("locate", "propose", "implement", "build")
    role_description = "开发：定位 / 出补丁 / 落补丁 / 调 go build 出包"

    def handlers(self) -> Dict[str, Dict[str, Any]]:
        return {"dev": {
            "locate": lambda p, c: locate(p or {}, c),
            "propose": lambda p, c: propose(p or {}, c),
            "implement": lambda p, c: implement(p or {}, c),
            "build": lambda p, c: build(p or {}, c),
        }}


def heal(params: Dict[str, Any], _ctx: Dict[str, Any]) -> Dict[str, Any]:
    """编过了就直接透传；编不过才进自愈（0 token，不碰模型）。"""
    prev = params.get("build") or {}
    if prev.get("ok"):
        return {"ok": True, "needed": False, "rounds": 0, "attempts": [],
                "deferred": [], "tool_calls": [], "artifact": prev,
                "llm": {"used": False, "token": 0}}
    out = os.path.join(str(params.get("out_dir") or (OUT / "run")), "bin", "login")
    return selfheal(str(params.get("repo_root") or ""), out)


class GoHealProject(RoleProject):
    capability = "heal"
    actions = ("selfheal",)
    role_description = "编译修复：编不过就解析报错行、只读 function call 定位、规则修（0 token）"

    def handlers(self) -> Dict[str, Dict[str, Any]]:
        return {"heal": {"selfheal": lambda p, c: heal(p or {}, c)}}


class GoAuditProject(RoleProject):
    capability = "auditor"
    actions = ("verify",)
    role_description = "审计：把新包放到测试目录真跑一次，读日志判 pass/fail"

    def handlers(self) -> Dict[str, Dict[str, Any]]:
        return {"auditor": {"verify": lambda p, c: verify(p or {}, c)}}


class SyncServerProject(RoleProject):
    capability = "sync"
    actions = ("push",)
    role_description = "同步服务器：审计通过后把包同步到目标端并自证"

    def handlers(self) -> Dict[str, Dict[str, Any]]:
        return {"sync": {"push": lambda p, c: push(p or {}, c)}}


class GoReportProject(RoleProject):
    capability = "report"
    actions = ("build",)
    role_description = "报告：把一条链的凭据写成 markdown"

    def handlers(self) -> Dict[str, Dict[str, Any]]:
        return {"report": {"build": lambda p, c: report(p or {}, c)}}


# --------------------------------------------------------------------------- #
# 1) 采集
# --------------------------------------------------------------------------- #
def collect_log(params: Dict[str, Any], _ctx: Dict[str, Any]) -> Dict[str, Any]:
    log_path = str(params.get("log_path") or "")
    if not log_path or not os.path.isfile(log_path):
        return {"ok": False, "degraded": True, "reason": "log_not_found", "bugs": []}
    with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
        lines = [ln.rstrip("\n") for ln in fh]

    hits = 0
    seen = set()
    bugs: List[Dict[str, Any]] = []
    for no, raw in enumerate(lines, 1):
        if not any(k in raw for k in KEYWORDS):
            continue
        hits += 1
        message = raw.split(" error ", 1)[-1].strip()
        key = re.sub(r"\d{3,}", "<secret>", message)      # 同一缺陷只报一次
        if key in seen:
            continue
        seen.add(key)
        bugs.append({"bug_id": f"B{len(bugs) + 1}", "level": "error", "line": no,
                     "group": "login_fail", "message": message, "raw": raw})

    return {"ok": True, "bugs": bugs, "hits": hits, "scanned": len(lines),
            "deduped": len(bugs), "keyword": list(KEYWORDS)}


# --------------------------------------------------------------------------- #
# 2) 定位
# --------------------------------------------------------------------------- #
def locate(params: Dict[str, Any], _ctx: Dict[str, Any]) -> Dict[str, Any]:
    repo = os.path.realpath(str(params.get("repo_root") or ""))
    if not os.path.isdir(repo):
        return {"ok": False, "findings": [], "counts": {"located": 0}}
    findings: List[Dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in {".git", "bin", "dist"}]
        for name in filenames:
            if not name.endswith(".go"):
                continue
            full = os.path.join(dirpath, name)
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                for no, line in enumerate(fh, 1):
                    if "登录失败" in line and "logf" in line:
                        findings.append({"file": os.path.relpath(full, repo), "line": no,
                                         "text": line.strip(), "confidence": "high",
                                         "how": "关键字 grep（登录失败 + logf）"})
    return {"ok": bool(findings), "findings": findings,
            "counts": {"located": len(findings), "unlocated": 0}}


# --------------------------------------------------------------------------- #
# 3) 出补丁（只产 edits，不落地）
# --------------------------------------------------------------------------- #
def propose(params: Dict[str, Any], _ctx: Dict[str, Any]) -> Dict[str, Any]:
    # patch_mode 只影响"补丁写得好不好"，模拟两种手误，用来验编译自愈
    mode = str(params.get("patch_mode") or "good")
    fixed = {"good": "*user", "typo": "*usr", "unfixable": "*phantomUser"}[mode]
    edit = {
        "file": "main.go",
        "find": 'logf(*logPath, "error", "登录失败 %s", *pass)',
        "replace": 'logf(*logPath, "error", "登录失败: %s", %s)' % ("%s", fixed),
        "why": "失败日志把口令原样打出来了；应打用户名，口径 error 登录失败: <用户名>",
    }
    ws = {
        "id": "fix-login-fail-log",
        "name": "修复登录失败日志（口令 → 用户名）",
        "files": ["main.go"],
        "edits": [edit],
        "acceptance": [f"新包日志出现「{UP}」", f"日志中不出现口令 {SECRET}"],
    }
    return {"ok": True, "mode": "rule", "workstream": ws, "workstreams": [ws],
            "deferred": [], "budget": {"chars": 0}}


# --------------------------------------------------------------------------- #
# 4) 落补丁：find/replace + 三道门（路径在仓库内 / find 唯一命中 / 交给 build 验语法）
# --------------------------------------------------------------------------- #
def implement(params: Dict[str, Any], _ctx: Dict[str, Any]) -> Dict[str, Any]:
    ws = params.get("workstream") or {}
    repo = os.path.realpath(str(params.get("repo_root") or ""))
    applied: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []

    for edit in ws.get("edits") or []:
        rel = str(edit.get("file") or "")
        path = os.path.realpath(os.path.join(repo, rel))
        if not path.startswith(repo + os.sep) or not os.path.isfile(path):
            dropped.append({**edit, "reason": "path_outside_repo_or_missing"})
            continue
        with open(path, "r", encoding="utf-8") as fh:
            before = fh.read()
        find, replace = str(edit.get("find") or ""), str(edit.get("replace") or "")
        count = before.count(find)
        if not find or count != 1:
            dropped.append({**edit, "reason": f"find_miss(count={count})"})
            continue
        after = before.replace(find, replace)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(after)
        diff = "".join(difflib.unified_diff(
            before.splitlines(True), after.splitlines(True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}", n=1))
        applied.append({"path": rel, "status": "modified", "diff": diff})

    return {"ok": bool(applied) and not dropped, "applied": bool(applied),
            "workstream": ws.get("id"), "files": applied, "dropped": dropped,
            "repo_root": repo}


# --------------------------------------------------------------------------- #
# 5) function call：调 Go 编译器出包
# --------------------------------------------------------------------------- #
def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build(params: Dict[str, Any], _ctx: Dict[str, Any]) -> Dict[str, Any]:
    # 负向用例：故意拿「事故版本」的旧包冒充新包，审计必须拦住
    fake = params.get("sabotage_bin")
    if fake and os.path.isfile(str(fake)):
        return {"ok": True, "bin": os.path.realpath(str(fake)),
                "sha256": _sha256(str(fake)), "bytes": os.path.getsize(str(fake)),
                "source": "sabotage_old_binary", "duration_ms": 0}

    out_dir = str(params.get("out_dir") or (OUT / "run"))
    artifact = go_build(str(params.get("repo_root") or ""),
                        out=os.path.join(out_dir, "bin", "login"))
    artifact["source"] = "go_build"
    artifact["tool_spec"] = tool_specs()[0]["function"]["name"]
    return artifact


# --------------------------------------------------------------------------- #
# 6) 审计：真跑新包，读日志
# --------------------------------------------------------------------------- #
def verify(params: Dict[str, Any], _ctx: Dict[str, Any]) -> Dict[str, Any]:
    art = params.get("artifact") or {}
    out_dir = str(params.get("out_dir") or (OUT / "run"))
    audit_dir = os.path.join(out_dir, "audit")
    new_fail = run_login(str(art.get("bin") or ""), "bob", SECRET,
                         os.path.join(audit_dir, "new", "fail.log"))
    new_ok = run_login(str(art.get("bin") or ""), "alice", "s3cret",
                       os.path.join(audit_dir, "new", "ok.log"))

    baseline = params.get("baseline_bin")
    base_run = run_login(str(baseline), "bob", SECRET,
                         os.path.join(audit_dir, "baseline", "fail.log")) \
        if baseline and os.path.isfile(str(baseline)) else None

    fail_text = new_fail.get("text") or ""
    ok_text = new_ok.get("text") or ""
    checks: List[Tuple[str, bool]] = [
        ("新包可执行（退出码 0）", bool(new_fail.get("ok"))),
        (f"失败日志为「{UP}」", UP in fail_text),
        (f"日志不含口令 {SECRET}", SECRET not in fail_text),
        ("成功路径为「info 登录成功 alice」", "info 登录成功 alice" in ok_text),
    ]
    if base_run is not None:
        checks.append((f"旧包确实泄露口令（缺陷可复现）",
                       SECRET in (base_run.get("text") or "")))

    failed = [name for name, ok in checks if not ok]
    return {
        "ok": not failed, "verdict": "pass" if not failed else "fail",
        "severity": "none" if not failed else "error",
        "findings": [{"level": "error", "message": name} for name in failed],
        "counts": {"checks": len(checks), "failed": len(failed)},
        "checks": [{"name": n, "ok": o} for n, o in checks],
        "artifact_sha256": art.get("sha256"),
        "baseline_log": (base_run or {}).get("lines") or [],
        "new_log": new_fail.get("lines") or [],
        "new_ok_log": new_ok.get("lines") or [],
    }


# --------------------------------------------------------------------------- #
# 7) 同步服务器：搬到目标端 + 远端自证
# --------------------------------------------------------------------------- #
def push(params: Dict[str, Any], _ctx: Dict[str, Any]) -> Dict[str, Any]:
    audit = params.get("audit") or {}
    if audit.get("verdict") != "pass":
        return {"synced": False, "verified": False, "error": "audit_not_passed",
                "audit_verdict": audit.get("verdict")}
    res = sync_artifact(params.get("artifact") or {},
                        str(params.get("target_root") or (OUT / "target-b")),
                        host=str(params.get("host") or "host-b"),
                        expected=UP)
    res["audit_verdict"] = audit.get("verdict")
    return res


# --------------------------------------------------------------------------- #
# 8) 报告
# --------------------------------------------------------------------------- #
def report(params: Dict[str, Any], _ctx: Dict[str, Any]) -> Dict[str, Any]:
    out_dir = str(params.get("out_dir") or (OUT / "run"))
    os.makedirs(out_dir, exist_ok=True)
    bugs = params.get("bugs") or {}
    loc = params.get("locate") or {}
    prop = params.get("propose") or {}
    fix = params.get("fix") or {}
    art = params.get("build") or {}
    heal = params.get("heal") or {}
    audit = params.get("audit") or {}
    sync = params.get("sync") or {}

    lines = ["# 登录失败：Go 补丁 → 新包 → 审计 → 同步", ""]
    lines += ["## 1. 采集（error report）", ""]
    for bug in bugs.get("bugs") or []:
        lines.append(f"- `{bug.get('raw')}` → 规则 {bug.get('group')}，命中行 {bug.get('line')}")
    lines += ["", f"扫描 {bugs.get('scanned', 0)} 行，命中 {bugs.get('hits', 0)}，去重后 {bugs.get('deduped', 0)} 条", ""]

    lines += ["## 2. 定位 / 补丁", ""]
    for f in loc.get("findings") or []:
        lines.append(f"- 定位：`{f.get('file')}:{f.get('line')}`（{f.get('confidence')}）")
    for ed in (prop.get("workstream") or {}).get("edits") or []:
        lines += ["", "```diff", f"- {ed.get('find')}", f"+ {ed.get('replace')}", "```"]
    lines += ["", f"落地：{'applied' if fix.get('applied') else 'dropped'}"
                  f"（{len(fix.get('files') or [])} 个文件）", ""]

    lines += ["## 3. 重新出包（go build）", ""]
    final_art = heal.get("artifact") or art
    lines.append(f"- 首次编译：{'ok' if art.get('ok') else (art.get('error') or 'failed')}"
                 f"（{art.get('duration_ms')} ms）")
    lines.append(f"- 最终产物：`{final_art.get('bin')}`")
    lines.append(f"- sha256：`{final_art.get('sha256')}`（{final_art.get('bytes')} bytes）")

    lines += ["", "## 3.5 编译自愈（0 token，无模型）", ""]
    if heal.get("needed"):
        final = heal.get("artifact") or {}
        lines.append(f"- 首次编译失败，交由 heal 角色：{heal.get('rounds')} 轮，"
                     f"结果 {'通过' if heal.get('ok') else '仍未通过'}")
        for tc in heal.get("tool_calls") or []:
            lines.append(f"- function call `{tc.get('tool')}` {tc.get('args')}（命中 {tc.get('hits')}）")
        for att in heal.get("attempts") or []:
            lines.append(f"- 第 {att.get('round')} 轮：`{att.get('file')}:{att.get('line')}` "
                         f"`{att.get('find')}` → `{att.get('replace')}`（{att.get('why')}）")
        for miss in heal.get("deferred") or []:
            lines.append(f"- 未修：{miss.get('reason')} {miss.get('error') or ''}")
        lines.append(f"- 自愈后产物：`{final.get('bin')}` sha256 `{str(final.get('sha256'))[:12]}…`")
    else:
        lines.append("- 首次编译即通过，heal 未介入")
    lines += ["", "## 4. 审计（跑包读日志）", ""]
    for c in audit.get("checks") or []:
        lines.append(f"- [{'x' if c.get('ok') else ' '}] {c.get('name')}")
    lines.append(f"- 结论：**{audit.get('verdict')}**")
    lines += ["", "新包日志：", "```"] + [f"{ln}" for ln in (audit.get("new_log") or [])] + ["```", ""]

    lines += ["## 5. 同步服务器", ""]
    if sync:
        lines.append(f"- 目标：`{sync.get('host')}` / `{sync.get('remote_bin')}`")
        lines.append(f"- sha256 一致：{sync.get('synced')}；远端日志核验：{sync.get('verified')}")
        lines += ["- 远端日志："] + [f"  - `{ln}`" for ln in (sync.get("remote_log") or [])]
    else:
        lines.append("- 审计未通过，Coordinator 未派发同步（目标端未被污染）")
    lines.append("")

    path = os.path.join(out_dir, "bugfix_report.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return {"ok": True, "report_path": path, "markdown": "\n".join(lines)}


# --------------------------------------------------------------------------- #
# 编排：只写 capability/action
# --------------------------------------------------------------------------- #
GO_LOGIN_FLOW: Dict[str, Any] = {
    "name": "go_login_bugfix",
    "description": "error report → 定位 → Go 补丁 → 重新出包 → 审计 → 同步服务器",
    "steps": [
        {"name": "bugs", "capability": "bug", "action": "collect",
         "params": {"log_path": "${params.log_path}"}},
        {"name": "locate", "capability": "dev", "action": "locate",
         "params": {"bugs": "${steps.bugs.result.bugs}",
                    "repo_root": "${params.repo_root}"},
         "depends_on": ["bugs"],
         "when": {"step": "bugs", "path": "result.bugs", "op": "gt", "value": 0}},
        {"name": "propose", "capability": "dev", "action": "propose",
         "params": {"bugs": "${steps.bugs.result.bugs}",
                    "locate": "${steps.locate.result}",
                    "repo_root": "${params.repo_root}",
                    "patch_mode": "${params.patch_mode}"},
         "depends_on": ["locate"]},
        {"name": "fix", "capability": "dev", "action": "implement",
         "params": {"workstream": "${steps.propose.result.workstream}",
                    "repo_root": "${params.repo_root}",
                    "out_dir": "${params.out_dir}"},
         "depends_on": ["propose"]},
        {"name": "build", "capability": "dev", "action": "build",
         "params": {"repo_root": "${params.repo_root}",
                    "out_dir": "${params.out_dir}",
                    "sabotage_bin": "${params.sabotage_bin}"},
         "depends_on": ["fix"], "timeout_s": 300},
        # 编过了直接透传；编不过由 heal 角色接手：报错行 → 只读 function call 定位 → 规则修 → 重编
        {"name": "heal", "capability": "heal", "action": "selfheal",
         "params": {"build": "${steps.build.result}",
                    "repo_root": "${params.repo_root}",
                    "out_dir": "${params.out_dir}"},
         "depends_on": ["build"], "timeout_s": 300},
        {"name": "audit", "capability": "auditor", "action": "verify",
         "params": {"artifact": "${steps.heal.result.artifact}",
                    "baseline_bin": "${params.baseline_bin}",
                    "out_dir": "${params.out_dir}"},
         "depends_on": ["heal"], "timeout_s": 300},
        # 审计 PASS 才派给「同步服务器」；FAIL 就跳过，目标端一个字节都不动
        {"name": "sync", "capability": "sync", "action": "push",
         "params": {"artifact": "${steps.heal.result.artifact}",
                    "audit": "${steps.audit.result}",
                    "target_root": "${params.target_root}",
                    "host": "${params.host}"},
         "depends_on": ["audit"],
         "when": {"step": "audit", "path": "result.verdict", "op": "eq", "value": "pass"}},
        {"name": "report", "capability": "report", "action": "build",
         "params": {"bugs": "${steps.bugs.result}", "locate": "${steps.locate.result}",
                    "propose": "${steps.propose.result}", "fix": "${steps.fix.result}",
                    "build": "${steps.build.result}", "audit": "${steps.audit.result}",
                    "heal": "${steps.heal.result}",
                    "sync": "${steps.sync.result}", "out_dir": "${params.out_dir}"},
         "depends_on": ["sync"], "run_on_skipped": True},
    ],
}


# --------------------------------------------------------------------------- #
# 起栈 / 跑一条 / 判定
# --------------------------------------------------------------------------- #
def prepare_run(name: str) -> Dict[str, Any]:
    """每条 run 一份干净的仓库副本：补丁只改副本，不污染 fixture。"""
    run_dir = OUT / name
    if run_dir.exists():
        shutil.rmtree(run_dir)
    repo = run_dir / "repo"
    shutil.copytree(FIXTURE / "repo", repo, ignore=shutil.ignore_patterns(".git"))
    return {"run_dir": run_dir, "repo": repo}


def run_stack(projects: List[RoleProject]) -> Tuple[Any, List[Any], CoordinatorAgent]:
    srv = HubServer(host="127.0.0.1", port=0, db_path=":memory:", token=TOKEN)
    srv.start_background()
    runtimes: List[Any] = []
    for project in projects:
        rt = AgentRuntime(srv.url, f"go-{project.capability}", project.manifests(),
                          token=TOKEN, concurrency=2, poll_wait=1)
        project.register(rt)
        rt.start()
        runtimes.append(rt)
    coord = CoordinatorAgent(srv.url, token=TOKEN)
    rt_coord = AgentRuntime(srv.url, "go-coord",
                            [{"name": "coordinator",
                              "actions": ["start", "status", "runs", "resume"]}],
                            token=TOKEN, concurrency=2, poll_wait=1)
    coord.attach(rt_coord)
    rt_coord.start()
    runtimes.append(rt_coord)
    return srv, runtimes, coord


def steps_of(snap: Dict[str, Any]) -> Dict[str, Any]:
    return snap.get("steps") or {}


def main() -> int:
    go = find_go()
    print(f"go: {go or '未找到（GO_BIN 可指定）'}")
    if not go:
        print("FAIL：没有 Go 工具链，无法验证「重新出包」")
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    # 事故版本：直接拿 fixture 原样编译，留作审计对照
    baseline = go_build(str(FIXTURE / "repo"), out=os.path.join(OUT, "baseline", "login-buggy"))
    if not baseline.get("ok"):
        print(f"FAIL：事故版本都编不出来 → {baseline.get('error')} {baseline.get('stderr', '')[:400]}")
        return 2
    baseline_bin = baseline["bin"]
    print(f"事故包：{baseline_bin} sha256={baseline['sha256'][:12]}…")

    # 每条 run 前清空目标端：验「没同步」时不能靠上一轮残留
    for target in ("target-b", "target-heal", "target-unfixable", "target-neg"):
        shutil.rmtree(OUT / target, ignore_errors=True)

    register_flow(GO_LOGIN_FLOW)
    projects = [BugCollectProject(), GoDevProject(), GoHealProject(),
                GoAuditProject(), SyncServerProject(), GoReportProject()]
    srv, runtimes, coord = run_stack(projects)
    try:
        fix_env = prepare_run("run-fix")
        p1 = {"log_path": str(FIXTURE / "logs" / "server.log"),
              "repo_root": str(fix_env["repo"]), "out_dir": str(fix_env["run_dir"]),
              "baseline_bin": baseline_bin, "target_root": str(OUT / "target-b"),
              "host": "host-b", "sabotage_bin": None, "patch_mode": "good"}
        run1 = coord.coord.start("go_login_bugfix", p1)
        snap1 = coord.coord.wait(run1["run_id"], timeout=180)

        heal_env = prepare_run("run-selfheal")
        p2 = {**p1, "repo_root": str(heal_env["repo"]),
              "out_dir": str(heal_env["run_dir"]),
              "target_root": str(OUT / "target-heal"),
              "patch_mode": "typo"}
        run2 = coord.coord.start("go_login_bugfix", p2)
        snap2 = coord.coord.wait(run2["run_id"], timeout=180)

        hard_env = prepare_run("run-unfixable")
        p3 = {**p1, "repo_root": str(hard_env["repo"]),
              "out_dir": str(hard_env["run_dir"]),
              "target_root": str(OUT / "target-unfixable"),
              "patch_mode": "unfixable"}
        run3 = coord.coord.start("go_login_bugfix", p3)
        snap3 = coord.coord.wait(run3["run_id"], timeout=180)

        guard_env = prepare_run("run-guard")
        p4 = {**p1, "repo_root": str(guard_env["repo"]),
              "out_dir": str(guard_env["run_dir"]),
              "target_root": str(OUT / "target-neg"),
              "sabotage_bin": baseline_bin}
        run4 = coord.coord.start("go_login_bugfix", p4)
        snap4 = coord.coord.wait(run4["run_id"], timeout=180)
    finally:
        for rt in runtimes:
            try:
                rt.stop()
            except Exception:  # noqa: BLE001 - 关停失败不影响判定
                pass
        srv.stop()

    s1, s2 = steps_of(snap1), steps_of(snap2)
    s3, s4 = steps_of(snap3), steps_of(snap4)
    checks: List[Tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    bugs = (s1.get("bugs") or {}).get("result") or {}
    loc = (s1.get("locate") or {}).get("result") or {}
    fix = (s1.get("fix") or {}).get("result") or {}
    art = (s1.get("build") or {}).get("result") or {}
    heal1 = (s1.get("heal") or {}).get("result") or {}
    audit = (s1.get("audit") or {}).get("result") or {}
    sync = (s1.get("sync") or {}).get("result") or {}

    check("采集：error report 里认出登录失败", (bugs.get("hits") or 0) >= 2,
          f"命中 {bugs.get('hits')}，去重 {bugs.get('deduped')}")
    located_at = ", ".join("%s:%s" % (f.get("file"), f.get("line"))
                           for f in (loc.get("findings") or []))
    check("定位：落到 main.go 的具体行", bool(loc.get("findings")), located_at)
    check("补丁：find 命中一次并落地", bool(fix.get("applied")),
          f"dropped={len(fix.get('dropped') or [])}")
    check("编译：go build 真出货", bool(art.get("ok")) and bool(art.get("sha256")),
          f"{art.get('bytes')} bytes / {art.get('duration_ms')} ms")
    check("编译：新包与事故包不是同一个二进制",
          bool(art.get("sha256")) and art.get("sha256") != baseline["sha256"])
    check("审计：旧包复现口令泄露", SECRET in "\n".join(audit.get("baseline_log") or []))
    check(f"审计：新包日志为「{UP}」", UP in "\n".join(audit.get("new_log") or []),
          (audit.get("new_log") or ["(空)"])[0])
    check(f"审计：日志里没有口令 {SECRET}",
          SECRET not in "\n".join(audit.get("new_log") or []))
    check("审计：结论 pass", audit.get("verdict") == "pass")
    check("同步：目标端 sha256 与本地一致", bool(sync.get("synced")),
          str(sync.get("remote_sha256", ""))[:12])
    check("同步：目标端日志正确", bool(sync.get("verified")),
          (sync.get("remote_log") or ["(空)"])[0])
    check("编排：正向 run 全绿", snap1.get("state") == "succeeded", str(snap1.get("state")))

    # ---- 编译自愈：正常不触发 / 写错能修 / 修不了如实上报 ----
    h_build = (s2.get("build") or {}).get("result") or {}
    h_heal = (s2.get("heal") or {}).get("result") or {}
    h_audit = (s2.get("audit") or {}).get("result") or {}
    h_sync = (s2.get("sync") or {}).get("result") or {}
    u_heal = (s3.get("heal") or {}).get("result") or {}
    u_audit = (s3.get("audit") or {}).get("result") or {}
    h_calls = h_heal.get("tool_calls") or []

    check("自愈：正常补丁时 heal 不介入", heal1.get("needed") is False)
    check("自愈：补丁写错变量名时 go build 失败", h_build.get("ok") is False,
          next((ln for ln in (h_build.get("stderr") or "").splitlines()
                if "undefined" in ln), ""))
    check("自愈：heal 一轮内修好并重编通过",
          bool(h_heal.get("ok")) and int(h_heal.get("rounds") or 0) >= 1,
          f"rounds={h_heal.get('rounds')}")
    check("自愈：靠只读 function call 定位（grep_code + read_file）",
          any(t.get("tool") == "grep_code" for t in h_calls)
          and any(t.get("tool") == "read_file" for t in h_calls),
          f"{len(h_calls)} 次调用")
    check("自愈：全程 0 token（llm.used=False）",
          (h_heal.get("llm") or {}).get("used") is False)
    check("自愈：只改报错行的 token（usr → user）",
          any(a.get("find") == "usr" and a.get("replace") == "user"
              for a in (h_heal.get("attempts") or [])))
    check("自愈后审计仍 pass（闸门没被绕开）", h_audit.get("verdict") == "pass")
    check("自愈后同步成功且远端自证", bool(h_sync.get("synced")) and bool(h_sync.get("verified")),
          str(h_sync.get("remote_sha256", ""))[:12])
    check("自愈修不了时如实 deferred（不硬凑）",
          bool(u_heal.get("deferred")) and not u_heal.get("ok"),
          str((u_heal.get("deferred") or [{}])[0].get("reason")))
    check("修不了：审计 fail 且同步 skipped",
          u_audit.get("verdict") == "fail" and (s3.get("sync") or {}).get("state") == "skipped")
    check("修不了：目标端没有产物",
          not list((OUT / "target-unfixable").glob("*/releases/*/*")))

    g_audit = (s4.get("audit") or {}).get("result") or {}
    g_sync = s4.get("sync") or {}
    neg_release = (OUT / "target-neg").glob("*/releases/*/*")
    check("闸门：旧包冒充新包时审计判 fail", g_audit.get("verdict") == "fail",
          str(g_audit.get("counts")))
    check("闸门：审计 fail 后同步被跳过", g_sync.get("state") == "skipped",
          str(g_sync.get("state")))
    check("闸门：目标端没有被污染", not list(neg_release))
    check("闸门：整条 run 降级而不是假成功", snap4.get("state") == "degraded",
          str(snap4.get("state")))

    for title, steps in (("run-fix", s1),
                         ("run-selfheal（补丁写错变量名）", s2),
                         ("run-unfixable（自愈也修不了）", s3),
                         ("run-guard（旧包冒充新包）", s4)):
        print(f"\n=== {title} ===")
        for name, step in steps.items():
            print(f"  · {name:<8} {step.get('state'):<10} {step.get('error') or ''}")

    print("\n=== 判定 ===")
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"\n{passed}/{len(checks)} PASS")
    print(f"报告：{(s1.get('report') or {}).get('result', {}).get('report_path')}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())