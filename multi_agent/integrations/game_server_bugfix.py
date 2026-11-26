"""游戏服务端 bugfix 链：**采集到的 bug → 分诊 → 定位 → 修复方案 → 应用 → 审计**。

和 `game_server_release` 的分工
------------------------------
发布链的入口是"开发报告"，起点想好了要改什么；
bugfix 链的入口是"线上炸了"，起点只知道哪里炸了。所以中间多了三段：

| 动作 | 干什么 | 花不花 token |
|---|---|---|
| `triage/classify` | 按接口把 bug 聚成可派单的簇，排优先级 | 0 |
| `dev/locate` | 栈顶帧 → 路由表 → 关键词，三级定位到文件行 | 0 |
| `dev/propose` | 规则模板产 `edits`；可选模型补丁（只产 JSON，不执行） | 0（默认） |
| `dev/implement` | 复用发布链：find/replace + 语法检查 + 出 diff | 0 |

三条边界
--------
1. **定位不猜**：栈顶帧的文件在仓库里不存在就不算命中，降级到路由表/关键词，最后如实写
   "未定位"——宁可让人看一眼，也不给一个假行号；
2. **补丁不越权**：模型只产 `edits`（file/find/replace），必须先通过 `find` 命中校验
   与语法检查才落地；路径一律限制在 `repo_root` 内；
3. **改不动就说改不动**：没有可套的模板就进 `deferred`（建议单），不硬凑一个补丁。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .game_server_delivery import (copy_artifacts, sha256_file, write_evidence,
                                   write_manifest)

BUGFIX_ACTIONS = ("locate", "propose")

_RE_ROUTE_LINE = re.compile(r"auth\s*=\s*(?P<auth>True|False)")
_BROAD_EXCEPT = "    except Exception:\n        pass"
_BUSY_WAIT = "    while True:\n        pass"
_AUTH_HINTS = ("AUTH", "FORBIDDEN", "UNAUTHORIZED", "UNAUTH", "PERMISSION")
_TX_HINTS = ("TX", "DUP", "IDEMPOT", "CONFLICT", "REPLAY", "TRANSACTION")

#: 公开接口：登录/注册/令牌这类按约定本来就不要鉴权。规则模板不能把
#: `auth=False` 改成 `auth=True`——改完等于把登录口封死，是错补丁。
_PUBLIC_PATH = re.compile(
    r"/(?:login|signin|sign_in|signup|sign_up|register|logout|token|oauth|"
    r"captcha|health|healthz|ping|metrics)(?:/|$)", re.IGNORECASE)


# ═══════════════════════════════════════════════════════════ 小工具
def _slug(text: Any) -> str:
    s = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", str(text or "").strip().lower())
    return s.strip("-") or "bug"


def _safe_join(root: Optional[str], rel: Any) -> Optional[str]:
    """把相对路径限制在 root 内（防 .. 穿越 / 绝对路径逃逸）。"""
    if not root or not isinstance(rel, str) or not rel.strip():
        return None
    root_abs = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_abs, rel))
    if target != root_abs and not target.startswith(root_abs + os.sep):
        return None
    return target


def _read(path: Optional[str]) -> str:
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _limits():
    from ..limits import load_limits

    return load_limits()


_WEIGHT = {"critical": 3, "error": 2, "warn": 1, "info": 0}


# ═══════════════════════════════════════════════════════════ 1) 分诊
def triage(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """`triage/classify`：bug 列表 → 可派单的簇（按接口聚合，严重度×次数排序）。

    只做归并与排序，不做根因判断——根因是 `locate` 的活，动代码是 `implement` 的活。
    """
    params = params or {}
    bugs = [b for b in (params.get("bugs") or []) if isinstance(b, dict)]
    if not bugs:
        return {"ok": False, "degraded": True, "reason": "没有 bug（采集为空或 degraded）",
                "clusters": [], "counts": {}, "unrouted": []}

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for b in bugs:
        endpoint = str(b.get("endpoint") or "")
        key = endpoint or f"{b.get('service') or 'svc'}:{b.get('error_code') or 'unrouted'}"
        groups.setdefault(key, []).append(b)

    clusters: List[Dict[str, Any]] = []
    for key, items in groups.items():
        weight = max(_WEIGHT.get(str(b.get("severity")), 0) for b in items)
        occ = sum(int(b.get("occurrences") or 1) for b in items)
        clusters.append({
            "id": _slug(key),
            "endpoint": str(items[0].get("endpoint") or ""),
            "name": f"{key}（{len(items)} 类错误 / {occ} 次）",
            "max_severity": max(items, key=lambda b: _WEIGHT.get(str(b.get("severity")), 0))
                             .get("severity"),
            "occurrences": occ,
            "bug_ids": [b.get("bug_id") for b in items],
            "error_codes": sorted({str(b.get("error_code") or "") for b in items} - {""}),
            "services": sorted({str(b.get("service") or "") for b in items} - {""}),
            "weight": weight,
        })
    clusters.sort(key=lambda c: (-int(c["weight"]), -int(c["occurrences"]), c["id"]))

    try:
        max_clusters = int(params.get("max_clusters") or _limits().bug.max_clusters)
    except Exception:  # noqa: BLE001
        max_clusters = 8
    kept = clusters[:max_clusters]

    unrouted = [c["id"] for c in clusters if not c["endpoint"]]
    counts = {"clusters": len(kept), "dropped": max(0, len(clusters) - len(kept)),
              "bugs": len(bugs),
              "critical": sum(1 for b in bugs if b.get("severity") == "critical")}
    return {"ok": True, "degraded": False, "clusters": kept, "unrouted": unrouted,
            "counts": counts, "reason": ""}


# ═══════════════════════════════════════════════════════════ 2) 定位
def locate(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """`dev/locate`：给每个 bug 找到"最可能在哪个文件哪一行"，找不到就如实说。

    三级：
    1. 栈顶帧（traceback 里的 `File "...", line N, in f`）——置信度 high；
    2. 路由表（`game_server_audit.scan_code` 扫出来的 method/path → file:line）——medium；
    3. 关键词（错误码 / 消息里的标识符，grep 前 400 个代码文件）——low。
    """
    params = params or {}
    bugs = [b for b in (params.get("bugs") or []) if isinstance(b, dict)]
    repo_root = params.get("repo_root")
    findings: List[Dict[str, Any]] = []
    unlocated: List[Dict[str, Any]] = []

    routes = []
    if isinstance(repo_root, str) and os.path.isdir(repo_root):
        from .game_server_audit import scan_code

        try:
            routes = scan_code(repo_root).get("endpoints") or []
        except Exception:  # noqa: BLE001 - 扫不动就退化到栈帧/关键词
            routes = []
    by_path: Dict[str, Dict[str, Any]] = {}
    for r in routes:
        by_path.setdefault(str(r.get("path") or ""), r)

    for b in bugs:
        bug_id = str(b.get("bug_id") or "")
        frame = b.get("frame") if isinstance(b.get("frame"), dict) else {}
        hit = None
        if frame.get("file"):
            full = _safe_join(repo_root, frame.get("file"))
            if full and os.path.isfile(full):
                hit = {"bug_id": bug_id, "file": frame["file"], "line": int(frame.get("line") or 0),
                       "func": frame.get("func") or "", "confidence": "high",
                       "how": "traceback"}
        if hit is None and b.get("endpoint"):
            r = by_path.get(str(b.get("endpoint")))
            if r and r.get("file"):
                hit = {"bug_id": bug_id, "file": r["file"], "line": int(r.get("line") or 0),
                       "func": "", "confidence": "medium", "how": "route-table"}
        if hit is None:
            hit = _grep_locate(repo_root, b)
            if hit:
                hit["bug_id"] = bug_id
        if hit:
            findings.append(hit)
        else:
            unlocated.append({"bug_id": bug_id,
                              "error_code": b.get("error_code") or "",
                              "reason": "栈帧文件不在仓库、路由表无此接口、关键词也没命中"})

    return {"ok": True, "degraded": False, "findings": findings, "unlocated": unlocated,
            "counts": {"located": len(findings), "unlocated": len(unlocated),
                       "routes": len(routes)},
            "reason": ""}


def _grep_locate(repo_root: Optional[str], bug: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(repo_root, str) or not os.path.isdir(repo_root):
        return None
    needles = [n for n in (
        str(bug.get("error_code") or ""),
        str((bug.get("message") or "")[:40]),
    ) if n and len(n) >= 4]
    if not needles:
        return None
    from .game_server_audit import _CODE_EXTS, _iter_files

    for path in _iter_files(repo_root, _CODE_EXTS, max_files=400):
        text = _read(path)
        for needle in needles:
            idx = text.find(needle)
            if idx < 0:
                continue
            line = text.count("\n", 0, idx) + 1
            return {"file": os.path.relpath(path, repo_root).replace(os.sep, "/"),
                    "line": line, "func": "", "confidence": "low", "how": "grep"}
    return None


# ═══════════════════════════════════════════════════════════ 3) 修复方案
def _wants_auth(bug: Dict[str, Any]) -> bool:
    blob = f"{bug.get('error_code') or ''} {bug.get('message') or ''}".upper()
    return any(h in blob for h in _AUTH_HINTS) or "越权" in blob


def _wants_tx(bug: Dict[str, Any]) -> bool:
    blob = f"{bug.get('error_code') or ''} {bug.get('message') or ''}".upper()
    return any(h in blob for h in _TX_HINTS) or any(k in blob for k in ("事务", "重放", "重复", "幂等"))


def _is_public_path(path: Any) -> bool:
    """登录/注册/令牌这类接口按约定就是公开的，不该被改成必须鉴权。"""
    return bool(_PUBLIC_PATH.search(str(path or "")))


def _rule_edits(repo_root: Optional[str], bug: Dict[str, Any],
                finding: Optional[Dict[str, Any]],
                routes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """零 token 的补丁模板：只覆盖"模式明确、改法唯一"的几类缺陷。"""
    edits: List[Dict[str, Any]] = []
    files: List[str] = []
    if finding and finding.get("file"):
        files.append(str(finding["file"]))
    endpoint = str(bug.get("endpoint") or "")
    for r in routes:
        if endpoint and str(r.get("path") or "") == endpoint and r.get("file"):
            if str(r["file"]) not in files:
                files.append(str(r["file"]))

    for rel in files[:2]:
        full = _safe_join(repo_root, rel)
        text = _read(full)
        if not text:
            continue
        lines = text.splitlines()

        for r in routes:
            if str(r.get("file") or "") != rel:
                continue
            if endpoint and str(r.get("path") or "") != endpoint:
                continue
            idx = int(r.get("line") or 0) - 1
            if not (0 <= idx < len(lines)):
                continue
            line = lines[idx]
            if (_wants_auth(bug) and not _is_public_path(endpoint)
                    and _RE_ROUTE_LINE.search(line)
                    and "auth=False" in line.replace(" ", "")):
                new = re.sub(r"auth\s*=\s*False", "auth=True", line)
                edits.append({"file": rel, "find": line, "replace": new,
                              "why": "接口声明要鉴权、实现没鉴权（auth_missing）"})
            if _wants_tx(bug) and not r.get("tx"):
                nxt = lines[idx + 1] if idx + 1 < len(lines) else ""
                if nxt.strip().startswith("def ") and "@tx" not in line:
                    edits.append({"file": rel, "find": line + "\n" + nxt,
                                  "replace": line + "\n@tx\n" + nxt,
                                  "why": "写接口没有事务（tx_missing）"})

        if _BROAD_EXCEPT in text:
            edits.append({"file": rel, "find": _BROAD_EXCEPT,
                          "replace": "    except Exception as e:\n        return {\"ok\": False, \"error\": str(e)}",
                          "why": "裸 except 吞异常（broad_except）"})
        if _BUSY_WAIT in text:
            edits.append({"file": rel, "find": _BUSY_WAIT,
                          "replace": "    while True:\n        time.sleep(0.01)",
                          "why": "忙等（busy_wait）"})
            if "import time" not in text:
                head = lines[0] if lines else ""
                if head.strip().startswith('"""') and head.strip().endswith('"""'):
                    edits.append({"file": rel, "find": head,
                                  "replace": head + "\n\nimport time",
                                  "why": "忙等修复需要 time 模块"})
    return edits


def _validate_edits(repo_root: Optional[str],
                    edits: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """落地前校验：路径不出仓库、find 必须真能命中。不通过的进 dropped，不硬改。"""
    kept, dropped, seen = [], [], set()
    for e in edits:
        if not isinstance(e, dict):
            continue
        rel, find, repl = e.get("file"), e.get("find"), e.get("replace", "")
        key = (str(rel), str(find))
        if key in seen:
            continue
        seen.add(key)
        full = _safe_join(repo_root, rel)
        if full is None:
            dropped.append({"file": rel, "reason": "路径越界"})
            continue
        text = _read(full)
        if not text:
            dropped.append({"file": rel, "reason": "文件不存在或读不了"})
            continue
        if not find or find not in text:
            dropped.append({"file": rel, "reason": "find 未命中", "find": str(find)[:80]})
            continue
        kept.append({"file": rel, "find": find, "replace": repl, "why": e.get("why") or ""})
    return kept, dropped


def _llm_edits(params: Dict[str, Any], bugs: Sequence[Dict[str, Any]],
               findings: Dict[str, Dict[str, Any]],
               repo_root: Optional[str],
               tools: Optional[Sequence[Dict[str, Any]]] = None,
               tool_impls: Optional[Dict[str, Any]] = None
               ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """可选：让模型补"模板覆盖不到"的补丁。只收 JSON，落地前仍走 _validate_edits。

    `tools` 非空时走 function call：模型可以先 read_file / grep_code 自己找代码，
    再返回 `edits`。工具只能是只读的，写仍然只走 `edits` + 三道门。
    """
    from .llm import chat

    snippets: List[str] = []
    for f in findings.values():
        rel = f.get("file")
        text = _read(_safe_join(repo_root, rel))
        if not text:
            continue
        lines = text.splitlines()
        center = max(0, int(f.get("line") or 1) - 1)
        lo, hi = max(0, center - 8), min(len(lines), center + 12)
        snippets.append(f"--- {rel}（第 {lo + 1}-{hi} 行）\n" + "\n".join(lines[lo:hi]))
        if len("\n".join(snippets)) > 4000:
            break

    prompt = (
        "你在修游戏服务端的线上 bug。只输出 JSON，不要解释。\n"
        '格式：{"edits":[{"file":"相对路径","find":"原文片段","replace":"替换后","why":"一句话"}]}\n'
        "规则：find 必须是给定代码里的**原样片段**（含缩进），replace 只改必要的部分；"
        "不确定就返回空 edits。\n\n"
        f"bug：{json.dumps(list(bugs)[:5], ensure_ascii=False)}\n\n"
        f"相关代码：\n" + "\n\n".join(snippets)
    )
    req: Dict[str, Any] = {"prompt": prompt, "system": "只输出 JSON"}
    if tools:
        req.update({"tools": list(tools), "tool_impls": dict(tool_impls or {}),
                    "max_tool_rounds": int(params.get("max_tool_rounds") or 4)})
        tc = params.get("llm_tool_caller") or params.get("tool_caller")
        if callable(tc):
            req["tool_caller"] = tc
    res = chat(req, caller=None)
    if not res.get("ok"):
        return [], res
    data = _extract_json(res.get("text") or "")
    edits = data.get("edits") if isinstance(data, dict) else None
    if not isinstance(edits, list):
        res["reason"] = "模型回包里没有 edits 数组"
        return [], res
    return [e for e in edits if isinstance(e, dict)], res


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    lo, hi = text.find("{"), text.rfind("}")
    if lo < 0 or hi <= lo:
        return None
    try:
        data = json.loads(text[lo:hi + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def propose(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """`dev/propose`：bug + 定位 → 可执行的 `edits` 包（每个簇一个 workstream）。

    默认纯规则（0 token）；`params.llm=True` 时先问模型，模型失败/无补丁就退回规则。
    """
    params = params or {}
    bugs = [b for b in (params.get("bugs") or []) if isinstance(b, dict)]
    repo_root = params.get("repo_root")
    tri = params.get("triage") or {}
    loc = params.get("locate") or {}
    findings = {str(f.get("bug_id")): f for f in (loc.get("findings") or [])
                if isinstance(f, dict)}
    clusters = [c for c in (tri.get("clusters") or []) if isinstance(c, dict)]

    routes: List[Dict[str, Any]] = []
    if isinstance(repo_root, str) and os.path.isdir(repo_root):
        from .game_server_audit import scan_code

        try:
            routes = scan_code(repo_root).get("endpoints") or []
        except Exception:  # noqa: BLE001
            routes = []

    use_llm = bool(params.get("llm"))
    llm_info: Dict[str, Any] = {"used": False, "backend": "", "reason": "未启用（llm=False）"}
    llm_pool: List[Dict[str, Any]] = []
    dev_tools: List[Dict[str, Any]] = []
    dev_impls: Dict[str, Any] = {}
    if use_llm and (params.get("tools") or params.get("dev_tools")):
        from .dev_tools import build

        dev_tools, dev_impls = build(repo_root)
    if use_llm:
        picked, llm_info = _llm_edits(params, bugs, findings, repo_root,
                                      tools=dev_tools, tool_impls=dev_impls)
        llm_info["used"] = bool(llm_info.get("ok"))
        llm_pool, llm_dropped = _validate_edits(repo_root, picked)
        if llm_dropped:
            llm_info["dropped"] = llm_dropped[:3]

    workstreams: List[Dict[str, Any]] = []
    deferred: List[Dict[str, Any]] = []
    dropped_all: List[Dict[str, Any]] = []

    for c in clusters:
        ids = set(c.get("bug_ids") or [])
        cluster_bugs = [b for b in bugs if str(b.get("bug_id")) in ids]
        raw: List[Dict[str, Any]] = []
        for b in cluster_bugs:
            raw += _rule_edits(repo_root, b, findings.get(str(b.get("bug_id"))), routes)
        for e in llm_pool:
            if e not in raw:
                raw.append(e)
        kept, dropped = _validate_edits(repo_root, raw)
        dropped_all += dropped
        if not kept:
            ep = str(c.get("endpoint") or "")
            if _is_public_path(ep) and any(_wants_auth(b) for b in cluster_bugs):
                reason = ("公开接口（登录/注册/令牌等）不做鉴权改造，"
                          "规则模板无解，需要人工或模型补丁")
            else:
                reason = "没有可套用的修复模板，需要人工或模型补丁"
            deferred.append({
                "cluster": c.get("id"), "endpoint": c.get("endpoint"),
                "bug_ids": sorted(ids), "reason": reason,
            })
            continue
        files = sorted({e["file"] for e in kept})
        workstreams.append({
            "id": f"fix-{c.get('id')}",
            "name": f"修复 {c.get('endpoint') or c.get('id')}",
            "scope": "；".join(sorted({e.get("why") or "" for e in kept} - {""})),
            "files": files,
            "edits": kept,
            "acceptance": [f"{c.get('endpoint') or c.get('id')} 相关缺陷已修复且语法通过"],
        })

    try:
        max_ws = int(params.get("max_workstreams") or _limits().bug.max_clusters)
    except Exception:  # noqa: BLE001
        max_ws = 8

    mode = "rule"
    if use_llm and llm_info.get("used") and llm_pool:
        mode = "rule+llm"

    return {
        "ok": bool(workstreams) or not bugs,
        "degraded": not workstreams,
        "reason": "" if workstreams else "所有簇都没有可执行的补丁（见 deferred）",
        "workstreams": workstreams[:max_ws],
        "deferred": deferred,
        "dropped": dropped_all,
        "mode": mode,
        "llm": llm_info,
        "counts": {"workstreams": len(workstreams[:max_ws]), "deferred": len(deferred),
                   "edits": sum(len(w["edits"]) for w in workstreams[:max_ws])},
    }


def run_action(action: str, params: Dict[str, Any],
               ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if action == "locate":
        return locate(params, ctx)
    if action == "propose":
        return propose(params, ctx)
    raise ValueError(f"bugfix 不支持的动作：{action}")


def register(runtime: Any, capability: str = "dev",
             actions: tuple = BUGFIX_ACTIONS) -> None:
    """把 `dev/locate` 与 `dev/propose` 挂到开发角色上（`implement` 由项目实现）。"""
    for action in actions:
        runtime.add_handler(capability, action,
                            (lambda a: (lambda p, c: run_action(a, p, c)))(action))


# ═══════════════════════════════════════════════════════════ 4) 报告交付
def build_bugfix_delivery(params: Dict[str, Any],
                          ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """渲染 bug 修复报告 + 补丁 + 证据，产出与发布链同规格的交付目录。"""
    params = params or {}
    out_dir = params.get("out_dir") or os.path.join(
        os.getcwd(), "release", "bugfix-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    evidence = {"bugs": params.get("bugs"), "triage": params.get("triage"),
                "locate": params.get("locate"), "propose": params.get("propose"),
                "dev": params.get("dev"), "pack": params.get("pack"),
                "audit": params.get("audit"), "gate": params.get("gate"),
                "ledger": params.get("ledger")}
    entries = write_evidence(out_dir, evidence)

    artifacts: List[Any] = []
    dev = params.get("dev")
    if isinstance(dev, list):
        artifacts += [d.get("patch") for d in dev if isinstance(d, dict) and d.get("patch")]
    elif isinstance(dev, dict) and dev.get("patch"):
        artifacts.append(dev["patch"])
    entries += copy_artifacts(out_dir, artifacts)

    # 报告里要显示"交付文件 N 个"：先按已有 entries 给一个预览
    # （与发布链同口径：不含报告自身、manifest 与 checksums）
    preview = {"count": len(entries),
               "total_bytes": sum(int(e.get("bytes") or 0) for e in entries),
               "files": entries}
    md = render_bugfix_report({**params, "manifest": preview})
    report_path = os.path.join(out_dir, "bugfix_report.md")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(md)
    entries.append({"path": "bugfix_report.md", "kind": "report",
                    "bytes": os.path.getsize(report_path),
                    "sha256": sha256_file(report_path)})
    manifest = write_manifest(out_dir, entries)

    gate = params.get("gate") or {}
    audit = params.get("audit") or {}
    return {
        "ok": True, "out_dir": out_dir, "report_path": report_path, "markdown": md,
        "manifest": manifest, "files": manifest["files"], "count": manifest["count"],
        "total_bytes": manifest["total_bytes"],
        "gate": gate.get("gate") or gate.get("verdict"),
        "audit_verdict": audit.get("verdict") or audit.get("decision"),
        "degraded": (gate.get("gate") or gate.get("verdict")) == "block",
        "counts": {"bugs": len(params.get("bugs") or []),
                   "fixed": sum(len(w.get("edits") or [])
                                for w in (params.get("propose") or {}).get("workstreams") or [])},
    }


def render_bugfix_report(params: Dict[str, Any]) -> str:
    """结论前置：闸门 / bug / 分诊 / 定位 / 修复 / 开发 / 审计 / 清单 / 未决项。"""
    bugs = params.get("bugs") or []
    tri = params.get("triage") or {}
    loc = params.get("locate") or {}
    prop = params.get("propose") or {}
    dev = params.get("dev") or []
    audit = params.get("audit") or {}
    gate = params.get("gate") or {}
    manifest = params.get("manifest") or {}
    gate_v = gate.get("gate") or gate.get("verdict") or "unknown"
    gate_cn = {"pass": "通过", "warn": "有条件通过", "block": "未通过",
               "unknown": "未评估"}.get(gate_v, gate_v)
    audit_v = audit.get("verdict") or audit.get("decision") or "unknown"
    as_of = datetime.now().strftime("%Y-%m-%d %H:%M")

    title = "线上 bug 修复报告"
    if gate_v == "block":
        title = "【未通过】" + title

    lines: List[str] = [f"# {title}", "", f"- 生成时间：{as_of}",
                        f"- 代码根目录：`{params.get('repo_root') or '—'}`",
                        f"- bug 数：{len(bugs)}（分诊 {len(tri.get('clusters') or [])} 簇，"
                        f"定位 {len(loc.get('findings') or [])} 条）", ""]

    lines += ["## 结论", ""]
    lines.append(f"- 上线闸门：**{gate_cn}**（audit={audit_v}）")
    if gate.get("blocked_by"):
        lines.append(f"- 拦截项：{', '.join(gate['blocked_by'])}")
    lines.append(f"- 自动生成补丁：{prop.get('counts', {}).get('edits', 0)} 条，"
                 f"覆盖 {prop.get('counts', {}).get('workstreams', 0)} 个分片")
    if prop.get("deferred"):
        lines.append(f"- 需人工补丁：{len(prop['deferred'])} 个簇（无模板可套）")
    lines.append(f"- 交付文件：{manifest.get('count', 0)} 个")
    lines.append("")

    lines += ["## bug 清单", ""]
    if bugs:
        lines += ["| 严重度 | 接口 | 错误码 | 出现 | 位置 |", "|---|---|---|---|---|"]
        for b in bugs:
            frame = b.get("frame") or {}
            where = (f"`{frame.get('file')}:{frame.get('line')}`" if frame.get("file") else "—")
            lines.append(f"| {b.get('severity')} | {b.get('endpoint') or '—'} "
                         f"| `{b.get('error_code') or '—'}` | {b.get('occurrences') or 1} "
                         f"| {where} |")
    else:
        lines.append("- 未采集到 bug（或采集 degraded）")
    lines.append("")

    lines += ["## 分诊", ""]
    for c in tri.get("clusters") or []:
        lines.append(f"- **{c.get('id')}** {c.get('name')}：错误码 "
                     f"{', '.join(c.get('error_codes') or []) or '—'}")
    if not tri.get("clusters"):
        lines.append("- 无")
    lines.append("")

    lines += ["## 定位", ""]
    for f in loc.get("findings") or []:
        lines.append(f"- `{f.get('file')}:{f.get('line')}`（{f.get('confidence')}，"
                     f"{f.get('how')}）← bug {f.get('bug_id')}")
    for u in loc.get("unlocated") or []:
        lines.append(f"- 未定位：{u.get('error_code') or u.get('bug_id')}（{u.get('reason')}）")
    if not (loc.get("findings") or loc.get("unlocated")):
        lines.append("- 无")
    lines.append("")

    lines += ["## 修复方案", ""]
    for w in prop.get("workstreams") or []:
        lines.append(f"- **{w.get('name')}**：{len(w.get('edits') or [])} 条补丁 → "
                     f"{', '.join(w.get('files') or [])}")
        for e in (w.get("edits") or [])[:5]:
            lines.append(f"  - `{e.get('file')}` {e.get('why') or ''}")
    for d in prop.get("deferred") or []:
        lines.append(f"- 待人工：{d.get('endpoint') or d.get('cluster')}（{d.get('reason')}）")
    if not (prop.get("workstreams") or prop.get("deferred")):
        lines.append("- 无")
    lines.append("")

    lines += ["## 开发产出", ""]
    if isinstance(dev, list) and dev:
        lines += ["| 分片 | 变更 | 检查 | 补丁 |", "|---|---|---|---|"]
        for d in dev:
            if not isinstance(d, dict):
                continue
            tests = d.get("tests") or []
            ok = sum(1 for t in tests if t.get("status") == "passed")
            lines.append(f"| {d.get('workstream')} | {d.get('applied')} | "
                         f"{ok}/{len(tests)} | `{os.path.basename(str(d.get('patch') or '—'))}` |")
    else:
        lines.append("- 无（没有可执行补丁）")
    lines.append("")

    lines += ["## 审计", ""]
    counts = audit.get("counts") or {}
    lines.append(f"- 结论：**{audit_v}**（error {counts.get('error', 0)} / "
                 f"warn {counts.get('warn', 0)}）")
    for f in (audit.get("findings") or [])[:10]:
        if isinstance(f, dict):
            lines.append(f"- `{f.get('code')}` {f.get('severity')}：{f.get('message')}")
    lines.append("")

    lines += ["## 未决项", ""]
    open_items = []
    if loc.get("unlocated"):
        open_items.append(f"{len(loc['unlocated'])} 个 bug 未定位")
    if prop.get("deferred"):
        open_items.append(f"{len(prop['deferred'])} 个簇没有自动补丁")
    if gate_v != "pass":
        open_items.append(f"闸门 {gate_cn}")
    lines += [f"- {x}" for x in open_items] or ["- 无"]
    lines.append("")

    lines += ["## 文件清单", ""]
    for e in manifest.get("files") or []:
        lines.append(f"- `{e.get('path')}`（{e.get('bytes')} B，`{str(e.get('sha256'))[:12]}…`）")
    lines.append("")
    return "\n".join(lines)