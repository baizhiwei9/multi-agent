"""游戏服务端审计（profile = ``game_server``）：把"开发报告 → 代码/协议/迁移/配置"变成可判定的规则。

为什么单独一个 profile
--------------------
`integrations/audit.py` 的引擎（分级短路 / Finding / Report / 0-token 保证）是通用的；
"游戏后台要审什么"和"供需平衡表要审什么"完全不同。这里只写"查什么"，不碰引擎：

    Auditor(project="game_server").audit_balance(snapshot)

snapshot 由 `collect_snapshot()` 采集：它**真的去读仓库里的文档、协议文件、代码、迁移、
配置表**，而不是让上游把结论写进来。这样审计官才能回答"开发的和他理解的对不对"：
文档怎么说、协议怎么定、代码怎么实现、配置怎么配，四者逐项对照。

审计规则（全部零网络、零 token、可复算）
--------------------------------------
| 类别 | 规则 | 级别 |
|---|---|---|
| 协议↔实现 | 协议/文档声明但代码没实现（endpoint_missing） | error |
| | 代码有路由但协议没登记（endpoint_undeclared） | warn |
| | 声明要鉴权、实现没鉴权（auth_missing） | error |
| | 声明已废弃但仍在服务（deprecated_still_served） | warn |
| 迁移 | 版本号缺口/重复（migration_gap / migration_duplicate） | error |
| | 破坏性变更无备份（migration_destructive） | error |
| | 迁移无事务（migration_no_tx） | warn/error（阈值） |
| | NOT NULL 无 DEFAULT 的加列（migration_unsafe_alter） | warn |
| 配置/经济 | 掉落引用不存在的道具（config_item_missing） | error |
| | 掉落概率越界/合计 > 1（drop_prob_invalid / drop_prob_sum） | error |
| | 缺产出上限 / 通胀过高（economy_cap_missing / economy_inflation_high） | warn |
| 并发/幂等 | 写接口没有事务（tx_missing） | error |
| | 写接口没有幂等键（idempotency_missing） | warn |
| 代码卫生 | 硬编码密钥（secret_hardcoded） | error |
| | TODO/FIXME 超阈值（todo_left） | warn |
| | 裸 except / 忙等（broad_except / busy_wait） | warn |
| 验收覆盖 | 验收项在设计文档里找不到依据（acceptance_uncovered） | warn |
| 文档 | 没有设计文档/协议文件（docs_missing / spec_missing） | warn |

扩展方式：新增规则 = 在 `GameServerProfile.check_balance` 里加一段；新增协议格式 =
在 `scan_endpoints` / `load_spec` 里加解析。不要改 audit.py 引擎。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .audit_profiles import AuditProfile, register_profile

# ═══════════════════════════════════════════════════════════ 常量
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", "dist", "build",
    ".venv", "venv", "env", ".idea", ".vscode", "out", ".mypy_cache",
    ".pytest_cache", "target", "bin", "obj",
}
_CODE_EXTS = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
              ".go", ".java", ".cs", ".lua", ".php", ".kt"}
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_ALL_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "ANY"}

# 路由声明（按"常见写法"匹配，匹配不到不算错，只是少一条证据）
_RE_DECORATOR_ROUTE = re.compile(
    r"""@(?:app|router|bp|api|blueprint)\.(get|post|put|patch|delete)\(\s*"""
    r"""(?:path\s*=\s*)?['"](?P<path>[^'"]+)['"]""", re.I)
_RE_EXPLICIT_ROUTE = re.compile(
    r"""@(?:route|endpoint)\(\s*['"](?P<method>GET|POST|PUT|PATCH|DELETE)['"]\s*,\s*"""
    r"""['"](?P<path>[^'"]+)['"]""", re.I)
_RE_CALL_ROUTE = re.compile(
    r"""\b(?:register_route|register_endpoint|add_route|route)\(\s*"""
    r"""['"](?P<method>GET|POST|PUT|PATCH|DELETE)['"]\s*,\s*['"](?P<path>[^'"]+)['"]""", re.I)
_RE_JS_ROUTE = re.compile(
    r"""\b(?:router|app|server|r)\.(get|post|put|patch|delete)\(\s*['"](?P<path>[^'"]+)['"]""")
_RE_GO_ROUTE = re.compile(
    r"""\b(?:mux|r|router)\.(?:HandleFunc|GET|POST|PUT|PATCH|DELETE)\(\s*['"](?P<path>[^'"]+)['"]""")
_RE_JAVA_ROUTE = re.compile(
    r"""@(Get|Post|Put|Patch|Delete)Mapping\(\s*(?:value\s*=\s*)?['"](?P<path>[^'"]+)['"]""")
_RE_AUTH_ARG = re.compile(r"""\bauth\s*=\s*(True|False|true|false)""")
_RE_AUTH_MARK = re.compile(
    r"(login_required|require_auth|require_login|authenticated|"
    r"AuthRequired|PreAuthorize|permission|authorize|\bauth\b)", re.I)
_RE_TX_MARK = re.compile(
    r"(@tx\b|\btx\s*\(|transaction|atomic|BEGIN\s+IMMEDIATE|session\.begin|"
    r"@Transactional|with\s+.*\bbegin\b)", re.I)
_RE_IDEM_MARK = re.compile(r"(idempot|request_id|dedupe|dedup|unique_key)", re.I)
_RE_SECRET = re.compile(
    r"""(api[_-]?key|secret|passwd|password|access[_-]?token|private[_-]?key)"""
    r"""\s*[:=]\s*['"][^'"]{8,}['"]""", re.I)
_RE_TODO = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")
_RE_BROAD_EXCEPT = re.compile(r"except\s*(?:Exception)?\s*:\s*(?:\n\s*pass\b)?")
_RE_WHILE_TRUE = re.compile(r"while\s+True\s*:")
_RE_PRINT = re.compile(r"(?<![\w.])print\(")

_DOC_EXTS = {".md", ".markdown", ".txt", ".rst"}


# ═══════════════════════════════════════════════════════════ 通用小工具
def _read_text(path: str, limit: int = 400_000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def _sha256(data: Any) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _iter_files(root: Optional[str], exts: Optional[Iterable[str]] = None,
                max_files: int = 600) -> List[str]:
    """有界遍历：跳过依赖/构建目录，按路径排序保证结果可复算。"""
    out: List[str] = []
    if not root or not os.path.isdir(root):
        return out
    allowed = {e.lower() for e in exts} if exts else None
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for name in sorted(filenames):
            if allowed is not None and os.path.splitext(name)[1].lower() not in allowed:
                continue
            out.append(os.path.join(dirpath, name))
            if len(out) >= max_files:
                return out
    return out


def _rel(path: str, root: Optional[str]) -> str:
    try:
        return os.path.relpath(path, root).replace(os.sep, "/") if root else path
    except ValueError:
        return path


def _norm_path(p: Any) -> str:
    """归一化路径：补前导 /、折叠多余 /、把 {id} 与 :id 都折成 :param。"""
    text = str(p or "").strip()
    if not text:
        return ""
    text = text.split("?", 1)[0]
    if not text.startswith("/"):
        text = "/" + text
    text = re.sub(r"\{[^}]*\}", ":param", text)
    text = re.sub(r":([A-Za-z_]\w*)", ":param", text)
    return re.sub(r"/+", "/", text).rstrip("/") or "/"


def _looks_like_auth(text: str) -> Optional[bool]:
    """从一段文本判断是否鉴权：显式 auth=False 优先，其次鉴权标记。"""
    if not text:
        return None
    m = _RE_AUTH_ARG.search(text)
    if m:
        return m.group(1).lower() == "true"
    # 排除 "author" 之类的误判
    if re.search(r"\bauthor", text, re.I) and not re.search(
            r"(login_required|require_auth|require_login|authenticated|"
            r"AuthRequired|PreAuthorize|permission|authorize|auth=True)", text, re.I):
        return None
    if _RE_AUTH_MARK.search(text):
        return True
    return None


# ═══════════════════════════════════════════════════════════ 协议 / 文档
def parse_endpoint_table(markdown: str) -> List[Dict[str, Any]]:
    """从 Markdown 表格里解析接口清单。

    识别的表头：方法/Method、路径/Path/Endpoint、鉴权/Auth、废弃/Deprecated。
    这是"取文档"的最小实现：设计文档里怎么写，审计就按什么对。
    """
    rows: List[Dict[str, Any]] = []
    header: Optional[List[str]] = None
    for raw in markdown.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            header = None
            continue
        cells = [c.strip().strip("`*_ ") for c in line.strip("|").split("|")]
        if not cells or all(re.fullmatch(r"[-: ]*", c or "-") for c in cells):
            continue
        low = [c.lower() for c in cells]
        has_method = any(("method" in c) or ("方法" in c) for c in low)
        # 不能把"接口ID"当成路径列——优先认 path/路径/endpoint
        has_path = any(("path" in c) or ("路径" in c) or ("endpoint" in c)
                       or c.strip() in ("接口", "接口地址") for c in low)
        if has_method and has_path:
            header = low
            continue
        if not header:
            continue
        try:
            mi = next(i for i, h in enumerate(header) if "method" in h or "方法" in h)
            pi = next((i for i, h in enumerate(header)
                       if "path" in h or "路径" in h or "endpoint" in h
                       or h.strip() in ("接口", "接口地址")), None)
            if pi is None:
                # 兜底：只认"接口"但不认"接口id/接口编号"
                pi = next(i for i, h in enumerate(header)
                          if "接口" in h and "id" not in h and "编号" not in h)
        except StopIteration:
            continue
        if pi is None or pi >= len(cells) or mi >= len(cells):
            continue
        method_raw = cells[mi].strip()
        method = method_raw.upper()
        ai = next((i for i, h in enumerate(header)
                   if "auth" in h or "鉴权" in h or "权限" in h), None)
        di = next((i for i, h in enumerate(header)
                   if "deprecat" in h or "废弃" in h), None)
        ii = next((i for i, h in enumerate(header)
                   if h.strip() in ("接口id", "接口编号", "id")), None)
        ci = next((i for i, h in enumerate(header)
                   if "调用方" in h or "caller" in h), None)
        c2 = next((i for i, h in enumerate(header)
                   if "被调方" in h or "callee" in h), None)
        ki = next((i for i, h in enumerate(header)
                   if "幂等" in h or "idem" in h), None)
        ei = next((i for i, h in enumerate(header)
                   if "错误码" in h or "error" in h), None)

        def _cell(idx: Optional[int]) -> str:
            return cells[idx].strip() if idx is not None and idx < len(cells) else ""

        auth_raw = _cell(ai)
        auth = None
        if auth_raw:
            low_auth = auth_raw.lower()
            if low_auth in ("是", "yes", "true", "required", "需要", "必须", "y", "✔", "✓") \
                    or low_auth.startswith(("是", "yes", "true", "required", "需要", "必须")):
                auth = True
            elif low_auth in ("否", "no", "false", "public", "公开", "不需要", "n", "—", "-") \
                    or low_auth.startswith(("否", "no", "false", "公开", "不需要")):
                auth = False
        deprecated = False
        if di is not None and di < len(cells):
            deprecated = cells[di].strip().lower() in ("是", "yes", "true", "y", "✔", "✓", "废弃")

        caller_raw = _cell(ci)
        callee = _cell(c2)
        caller = caller_raw
        for arrow in ("→", "->", "=>"):
            if arrow in caller_raw:
                caller, callee = [x.strip() for x in caller_raw.split(arrow, 1)]
                break

        # method 非法不丢行：规范校验要能看到"FETCH 不是合法方法"，
        # 审计侧 `load_spec._add` 会按枚举过滤，不受影响。
        rows.append({
            "method": method, "path": _norm_path(cells[pi]),
            "auth": auth, "deprecated": deprecated, "source": "doc",
            "method_raw": method_raw, "method_valid": method in _ALL_METHODS,
            "id": _cell(ii), "caller": caller, "callee": callee,
            "idem": _cell(ki), "errors": _cell(ei), "auth_raw": auth_raw,
        })
    return rows


def load_docs(doc_root: Optional[str], max_files: int = 40,
              max_chars_each: int = 40_000) -> Dict[str, Any]:
    """读设计文档：标题/小标题/摘要/哈希。审计的"依据"就是这些文本。"""
    docs: List[Dict[str, Any]] = []
    for path in _iter_files(doc_root, _DOC_EXTS, max_files=max_files):
        text = _read_text(path, limit=max_chars_each)
        if not text.strip():
            continue
        title = ""
        headings: List[str] = []
        for line in text.splitlines():
            m = re.match(r"^(#{1,6})\s+(.*)$", line.strip())
            if not m:
                continue
            if not title and len(m.group(1)) == 1:
                title = m.group(2).strip()
            headings.append(m.group(2).strip())
        docs.append({
            "path": _rel(path, doc_root),
            "title": title or (os.path.basename(path)),
            "headings": headings[:40],
            "chars": len(text),
            "sha256": _sha256(text)[:16],
            "text": text,
        })
    endpoints: List[Dict[str, Any]] = []
    for d in docs:
        endpoints.extend(parse_endpoint_table(d["text"]))
    return {"count": len(docs), "files": [
        {k: d[k] for k in ("path", "title", "chars", "sha256", "headings")}
        for d in docs], "endpoints": endpoints,
        "text": "\n\n".join(d["text"] for d in docs)[:200_000]}


def load_spec(repo_root: Optional[str], spec_path: Optional[str] = None,
              docs_endpoints: Optional[Sequence[Dict[str, Any]]] = None,
              report_endpoints: Optional[Sequence[Dict[str, Any]]] = None
              ) -> Dict[str, Any]:
    """读协议文件（JSON 优先）+ 文档表格 + 开发报告接口表，合并成"声明清单"。

    优先级：协议文件（spec）> 设计文档（doc）> 开发报告（report）。
    报告里写的接口是"需求侧声明的意图"，协议文件是"冻结的契约"，代码是"实现"；
    三者不一致时审计分别报 endpoint_missing / endpoint_undeclared / auth_missing。
    """
    declared: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def _add(item: Dict[str, Any], source: str) -> None:
        method = str(item.get("method") or "").upper()
        path = _norm_path(item.get("path"))
        if method not in _ALL_METHODS or not path:
            return
        row = {
            "method": method, "path": path,
            "auth": item.get("auth") if isinstance(item.get("auth"), bool) else None,
            "deprecated": bool(item.get("deprecated")),
            "source": source,
        }
        declared[(method, path)] = row

    for item in report_endpoints or []:
        _add(dict(item), "report")

    for item in docs_endpoints or []:
        _add(dict(item), "doc")

    candidates: List[str] = []
    if spec_path:
        candidates.append(spec_path)
    else:
        for path in _iter_files(repo_root, {".json"}, max_files=600):
            low = path.lower()
            if re.search(r"/(spec|proto|api|contract)s?/", low) or re.search(
                    r"(spec|protocol|openapi|contract)[^/]*\.json$", low):
                if "/config/" not in low:
                    candidates.append(path)

    files: List[str] = []
    for path in candidates:
        data = None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        items: Any = data
        if isinstance(data, dict):
            for key in ("endpoints", "routes", "apis", "interfaces"):
                if isinstance(data.get(key), list):
                    items = data[key]
                    break
            else:
                items = []
        if not isinstance(items, list):
            continue
        files.append(_rel(path, repo_root))
        for item in items:
            if isinstance(item, dict):
                _add(item, "spec")

    return {"files": files, "endpoints": list(declared.values())}


# ═══════════════════════════════════════════════════════════ 代码扫描
def _match_endpoint(line: str) -> Optional[Tuple[str, str]]:
    """从一行代码里抽出 (method, path)；抽不到返回 None。"""
    m = _RE_EXPLICIT_ROUTE.search(line)
    if m:
        return m.group("method").upper(), m.group("path")
    m = _RE_DECORATOR_ROUTE.search(line)
    if m:
        return m.group(1).upper(), m.group("path")
    m = _RE_CALL_ROUTE.search(line)
    if m:
        return m.group("method").upper(), m.group("path")
    m = _RE_JS_ROUTE.search(line)
    if m:
        return m.group(1).upper(), m.group("path")
    m = _RE_GO_ROUTE.search(line)
    if m:
        return "ANY", m.group("path")
    m = _RE_JAVA_ROUTE.search(line)
    if m:
        return m.group(1).upper(), m.group("path")
    return None


def scan_code(repo_root: Optional[str], window: int = 24,
              max_files: int = 400) -> Dict[str, Any]:
    """扫代码：路由、鉴权、事务/幂等标记、卫生问题。"""
    endpoints: List[Dict[str, Any]] = []
    todo: List[Dict[str, Any]] = []
    secrets: List[Dict[str, Any]] = []
    broad_except: List[Dict[str, Any]] = []
    busy_wait: List[Dict[str, Any]] = []
    prints: List[Dict[str, Any]] = []
    files = _iter_files(repo_root, _CODE_EXTS, max_files=max_files)
    lines_total = 0

    for path in files:
        text = _read_text(path)
        lines = text.splitlines()
        lines_total += len(lines)
        for i, line in enumerate(lines):
            hit = _match_endpoint(line)
            if hit:
                method, raw_path = hit
                block = "\n".join(lines[i:i + window])
                auth = _looks_like_auth(line)
                if auth is None:
                    auth = _looks_like_auth(block)
                endpoints.append({
                    "method": method, "path": _norm_path(raw_path),
                    "file": _rel(path, repo_root), "line": i + 1,
                    "auth": auth,
                    "tx": bool(_RE_TX_MARK.search(block)),
                    "idempotent": bool(_RE_IDEM_MARK.search(block)),
                })
            if _RE_TODO.search(line):
                todo.append({"file": _rel(path, repo_root), "line": i + 1,
                             "text": line.strip()[:120]})
            if _RE_SECRET.search(line):
                secrets.append({"file": _rel(path, repo_root), "line": i + 1,
                                "text": re.sub(r"['\"][^'\"]*['\"]", "'***'", line.strip())[:120]})
            if _RE_BROAD_EXCEPT.search(line):
                broad_except.append({"file": _rel(path, repo_root), "line": i + 1,
                                     "text": line.strip()[:120]})
            if _RE_WHILE_TRUE.search(line):
                nxt = "\n".join(lines[i + 1:i + 6])
                if "sleep" not in nxt and "await" not in nxt:
                    busy_wait.append({"file": _rel(path, repo_root), "line": i + 1,
                                      "text": line.strip()[:120]})
            if _RE_PRINT.search(line) and "/test" not in path.replace(os.sep, "/"):
                prints.append({"file": _rel(path, repo_root), "line": i + 1})

    # 同一路由重复注册
    counts: Dict[Tuple[str, str], int] = {}
    for e in endpoints:
        key = (e["method"], e["path"])
        counts[key] = counts.get(key, 0) + 1
    duplicates = [{"method": k[0], "path": k[1], "count": v}
                  for k, v in sorted(counts.items()) if v > 1]

    return {
        "files": len(files), "lines": lines_total,
        "endpoints": endpoints, "duplicates": duplicates,
        "todo": todo, "secrets": secrets, "broad_except": broad_except,
        "busy_wait": busy_wait, "prints": prints,
    }


def match_endpoints(declared: Sequence[Dict[str, Any]],
                    implemented: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """协议 ↔ 实现对照。ANY 方法（如 Go HandleFunc）按路径匹配。"""
    impl_keys = {(e["method"], e["path"]) for e in implemented}
    impl_paths = {e["path"]: e for e in implemented}
    missing: List[Dict[str, Any]] = []
    auth_missing: List[Dict[str, Any]] = []
    deprecated_served: List[Dict[str, Any]] = []
    matched: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for d in declared:
        key = (d["method"], d["path"])
        impl = impl_paths.get(d["path"]) if d["method"] == "ANY" else None
        impl = impl or next((e for e in implemented if e["path"] == d["path"]
                             and e["method"] in (d["method"], "ANY")), None)
        if impl is None:
            missing.append(dict(d))
            continue
        matched[key] = impl
        if d.get("auth") is True and impl.get("auth") is False:
            auth_missing.append({**d, "file": impl.get("file"), "line": impl.get("line")})
        if d.get("deprecated"):
            deprecated_served.append({**d, "file": impl.get("file"), "line": impl.get("line")})

    undeclared: List[Dict[str, Any]] = []
    for e in implemented:
        hit = any(e["path"] == d["path"] and (e["method"] in (d["method"], "ANY")
                                             or d["method"] == "ANY")
                  for d in declared)
        if not hit:
            undeclared.append(e)

    return {"missing": missing, "undeclared": undeclared,
            "auth_missing": auth_missing, "deprecated_served": deprecated_served}


# ═══════════════════════════════════════════════════════════ 迁移
def scan_migrations(repo_root: Optional[str], max_files: int = 100) -> Dict[str, Any]:
    """扫 SQL 迁移：版本连续性、事务、破坏性变更、加列安全。"""
    root = os.path.join(repo_root, "migrations") if repo_root else None
    files: List[Dict[str, Any]] = []
    revisions: Dict[int, str] = {}
    duplicates: List[Dict[str, Any]] = []
    gaps: List[int] = []
    destructive: List[Dict[str, Any]] = []
    no_tx: List[Dict[str, Any]] = []
    unsafe_alter: List[Dict[str, Any]] = []
    bad_name: List[str] = []

    for path in _iter_files(root, {".sql"}, max_files=max_files):
        name = os.path.basename(path)
        text = _read_text(path)
        m = re.match(r"^(\d+)", name)
        rev = int(m.group(1)) if m else None
        if rev is None:
            bad_name.append(name)
        elif rev in revisions:
            duplicates.append({"revision": rev, "first": revisions[rev], "dup": name})
        else:
            revisions[rev] = name
        low = text.lower()
        has_tx = ("begin" in low and "commit" in low) or "transactional" in low
        destructive_hit = bool(re.search(r"\b(drop\s+(table|column|index)|truncate)\b", low))
        # 只认显式备份动作：注释里写"没有备份"不能当成已备份
        backed_up = bool(re.search(
            r"(--\s*backup\s*:|backup\s+to\b|create\s+table\s+\w*_bak\b|rename\s+table)", low))
        unsafe = bool(re.search(
            r"add\s+column\s+\w+[^;]*not\s+null(?!\s+default)", low))
        row = {"name": name, "revision": rev, "has_tx": has_tx,
               "destructive": destructive_hit, "backed_up": backed_up,
               "unsafe_alter": unsafe, "chars": len(text), "empty": not text.strip()}
        files.append(row)
        if destructive_hit and not backed_up:
            destructive.append({"name": name, "revision": rev})
        if not has_tx:
            no_tx.append({"name": name, "revision": rev})
        if unsafe:
            unsafe_alter.append({"name": name, "revision": rev})

    nums = sorted(revisions)
    for prev, nxt in zip(nums, nums[1:]):
        if nxt > prev + 1:
            gaps.extend(range(prev + 1, nxt))

    return {"dir": "migrations", "files": files, "count": len(files),
            "gaps": gaps, "duplicates": duplicates,
            "destructive": destructive, "no_tx": no_tx,
            "unsafe_alter": unsafe_alter, "bad_name": bad_name}


# ═══════════════════════════════════════════════════════════ 配置 / 经济
def scan_configs(repo_root: Optional[str], max_files: int = 60) -> Dict[str, Any]:
    """扫配置表：道具引用完整性、掉落概率、经济产出/sink。"""
    root = os.path.join(repo_root, "config") if repo_root else None
    items: set = set()
    item_sources: Dict[str, str] = {}
    drops: List[Dict[str, Any]] = []
    economy: Dict[str, Any] = {}
    parse_errors: List[Dict[str, Any]] = []
    duplicate_items: List[str] = []

    def _collect_items(data: Any, source: str) -> None:
        seq = data if isinstance(data, list) else (
            data.get("items") if isinstance(data, dict) else None)
        if not isinstance(seq, list):
            return
        for entry in seq:
            if isinstance(entry, dict):
                key = entry.get("id") or entry.get("item_id") or entry.get("itemId")
                if key is not None:
                    s = str(key)
                    if s in items and s not in duplicate_items:
                        duplicate_items.append(s)
                    items.add(s)
                    item_sources.setdefault(s, source)
            elif isinstance(entry, (str, int)):
                items.add(str(entry))

    for path in _iter_files(root, {".json"}, max_files=max_files):
        name = os.path.basename(path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            parse_errors.append({"file": name, "error": str(e)})
            continue
        _collect_items(data, name)
        # 掉落表：{"drops": [{item_id, prob}]} 或顶层 list
        seq = None
        if isinstance(data, dict) and isinstance(data.get("drops"), list):
            seq = data["drops"]
        elif isinstance(data, list) and data and isinstance(data[0], dict) \
                and any(k in data[0] for k in ("item_id", "itemId", "prob", "rate")):
            seq = data
        if seq:
            for entry in seq:
                if not isinstance(entry, dict):
                    continue
                drops.append({
                    "file": name,
                    "item_id": str(entry.get("item_id") or entry.get("itemId") or ""),
                    "prob": entry.get("prob", entry.get("rate")),
                    "source": entry.get("source") or entry.get("group"),
                })
        if isinstance(data, dict) and ("faucet" in name.lower()
                                       or "economy" in name.lower()
                                       or "faucet_daily_cap" in data):
            for key in ("faucet_daily_cap", "inflation_daily_pct", "sink_ratio",
                        "max_gold_per_day", "gold_cap"):
                if key in data:
                    economy[key] = data[key]
            if isinstance(data.get("economy"), dict):
                economy.update(data["economy"])

    return {"items": sorted(items), "item_count": len(items),
            "duplicate_items": duplicate_items, "drops": drops,
            "economy": economy, "parse_errors": parse_errors}


# ═══════════════════════════════════════════════════════════ 验收覆盖
def _coverage(acceptance: Sequence[str], docs_text: str) -> Dict[str, Any]:
    """验收项在设计文档中的覆盖度（启发式：4 字滑窗命中任意一个即视为有依据）。

    不做中文分词（stdlib 无分词器），用 4 字指纹近似：``登录与鉴权流程必须符合设计文档``
    只要文档里出现「登录与鉴」就认作覆盖；整条都没出现才算无依据。
    """
    text = re.sub(r"\s+", "", (docs_text or "").lower())
    covered: List[str] = []
    uncovered: List[str] = []
    for item in acceptance:
        raw = str(item).strip()
        if not raw:
            continue
        norm = re.sub(r"\s+", "", raw).lower()
        if len(norm) <= 4:
            hit = bool(norm) and norm in text
        else:
            hit = any(norm[i:i + 4] in text for i in range(len(norm) - 3))
        (covered if hit else uncovered).append(raw)
    total = len(covered) + len(uncovered)
    return {"total": total, "covered": covered, "uncovered": uncovered,
            "coverage": round(len(covered) / total, 3) if total else None}


# ═══════════════════════════════════════════════════════════ 采集
def collect_snapshot(repo_root: str, doc_root: Optional[str] = None,
                     spec_path: Optional[str] = None,
                     ticket: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把"被审对象"采集成快照：文档 / 协议 / 代码 / 迁移 / 配置 / 验收。

    返回的是事实，不是结论；结论由 `GameServerProfile` 按阈值判定。
    """
    repo_root = os.path.abspath(repo_root) if repo_root else ""
    if not repo_root or not os.path.isdir(repo_root):
        return {"collected": False, "error": f"repo_root 不存在：{repo_root!r}"}
    docs = load_docs(doc_root or os.path.join(repo_root, "docs"))
    spec = load_spec(repo_root, spec_path, docs["endpoints"],
                     (ticket or {}).get("endpoints"))
    code = scan_code(repo_root)
    migrations = scan_migrations(repo_root)
    configs = scan_configs(repo_root)
    match = match_endpoints(spec["endpoints"], code["endpoints"])
    acceptance = list((ticket or {}).get("acceptance") or [])
    coverage = _coverage(acceptance, docs["text"])

    snapshot = {
        "collected": True,
        "repo_root": repo_root,
        "doc_root": os.path.abspath(doc_root) if doc_root else os.path.join(repo_root, "docs"),
        "endpoints": {
            "declared": spec["endpoints"],
            "implemented": [{k: e.get(k) for k in
                             ("method", "path", "file", "line", "auth", "tx", "idempotent")}
                            for e in code["endpoints"]],
            **match,
        },
        "spec_files": spec["files"],
        "docs": {"count": docs["count"], "files": docs["files"]},
        "migrations": migrations,
        "configs": {k: configs[k] for k in
                    ("item_count", "duplicate_items", "drops", "economy", "parse_errors")},
        "code": {k: code[k] for k in ("files", "lines", "duplicates", "todo",
                                      "secrets", "broad_except", "busy_wait", "prints")},
        "acceptance": coverage,
        "counts": {
            "declared": len(spec["endpoints"]),
            "implemented": len(code["endpoints"]),
            "missing": len(match["missing"]),
            "undeclared": len(match["undeclared"]),
            "auth_missing": len(match["auth_missing"]),
            "migrations": migrations["count"],
            "migration_gaps": len(migrations["gaps"]),
            "docs": docs["count"],
            "code_files": code["files"],
        },
    }
    digest_payload = json.dumps(snapshot["counts"], sort_keys=True, ensure_ascii=False)
    snapshot["digest"] = _sha256(digest_payload)[:16]
    return snapshot


def snapshot_summary(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """审计回执里带一份精简快照，报告官不用再读仓库。"""
    if not isinstance(snapshot, dict) or not snapshot.get("collected"):
        return {"collected": False}
    eps = snapshot.get("endpoints") or {}
    mig = snapshot.get("migrations") or {}
    cfg = snapshot.get("configs") or {}
    return {
        "collected": True,
        "repo_root": snapshot.get("repo_root"),
        "digest": snapshot.get("digest"),
        "counts": snapshot.get("counts"),
        "spec_files": snapshot.get("spec_files"),
        "docs": (snapshot.get("docs") or {}).get("files"),
        "missing_endpoints": eps.get("missing") or [],
        "undeclared_endpoints": eps.get("undeclared") or [],
        "auth_missing": eps.get("auth_missing") or [],
        "deprecated_served": eps.get("deprecated_served") or [],
        "migration_gaps": mig.get("gaps") or [],
        "migration_destructive": mig.get("destructive") or [],
        "migration_no_tx": mig.get("no_tx") or [],
        "config_economy": cfg.get("economy") or {},
        "acceptance": snapshot.get("acceptance") or {},
    }


# ═══════════════════════════════════════════════════════════ Profile
class GameServerProfile(AuditProfile):
    """游戏服务端交付口径：协议/文档 ↔ 代码/迁移/配置 逐项对照。"""

    name = "game_server"
    description = "游戏后台交付审计（协议↔实现、鉴权、迁移、配置经济、事务幂等、代码卫生、验收覆盖）"
    kinds: Tuple[str, ...] = ("balance", "report", "run")
    thresholds: Dict[str, Any] = {
        "require_auth": True,
        "require_tx": True,
        "require_migration_tx": True,
        "allow_destructive_migration": False,
        "max_todo": 3,
        "max_missing_endpoints_error": 0,
        "max_undeclared": 20,
        "min_acceptance_coverage": 0.8,
        "max_inflation_daily_pct": 0.5,
        "max_prints": 20,
    }

    # ---------- 数据（这里的数据 = 代码快照） ----------
    def check_balance(self, data: Dict[str, Any], rep) -> None:
        th = dict(self.thresholds)
        th.update(rep.metrics.get("_thresholds") or {})

        if not isinstance(data, dict) or not data:
            rep.add("snapshot_empty", "error", "审计快照为空（没有可审的代码/文档）",
                    where="snapshot")
            return
        if not data.get("collected") and data.get("repo_root"):
            data = collect_snapshot(
                data["repo_root"], data.get("doc_root"), data.get("spec_path"),
                data.get("ticket"))
            if not data.get("collected"):
                rep.add("snapshot_incomplete", "error",
                        f"采集失败：{data.get('error')}", where="snapshot")
                return
        if not data.get("collected"):
            rep.add("snapshot_incomplete", "error",
                    "快照既没有 collected 标记也没有 repo_root，无法审计", where="snapshot")
            return

        eps = data.get("endpoints") or {}
        declared = eps.get("declared") or []
        implemented = eps.get("implemented") or []
        missing = eps.get("missing") or []
        undeclared = eps.get("undeclared") or []
        auth_missing = eps.get("auth_missing") or []
        deprecated = eps.get("deprecated_served") or []
        mig = data.get("migrations") or {}
        cfg = data.get("configs") or {}
        code = data.get("code") or {}
        acceptance = data.get("acceptance") or {}
        docs = data.get("docs") or {}

        rep.metrics.update({
            "snapshot_digest": data.get("digest"),
            "counts": data.get("counts"),
            "spec_files": data.get("spec_files"),
            "doc_files": [d.get("path") for d in (docs.get("files") or [])][:10],
            "endpoints_declared": len(declared),
            "endpoints_implemented": len(implemented),
            "acceptance_coverage": acceptance.get("coverage"),
        })

        # 1) 依据是否齐全
        if not declared:
            rep.add("spec_missing", "warn",
                    "没有协议/接口声明（spec JSON 或文档接口表），无法核对协议↔实现",
                    {"spec_files": data.get("spec_files")}, where="spec")
        if not docs.get("count"):
            rep.add("docs_missing", "warn",
                    "没有设计文档（docs/ 下无 .md/.txt），审计只能依据代码与协议",
                    where="docs")
        if not implemented:
            rep.add("code_empty", "error",
                    "代码里没有识别到任何路由注册，检查扫描根目录或路由写法",
                    {"code_files": code.get("files")}, where="code")

        # 2) 协议 ↔ 实现
        if missing:
            sev = "error" if len(missing) > int(th["max_missing_endpoints_error"]) else "warn"
            rep.add("endpoint_missing", sev,
                    f"协议/文档声明了 {len(missing)} 个接口但代码未实现",
                    {"count": len(missing),
                     "samples": [f"{m['method']} {m['path']}" for m in missing[:10]]},
                    where="endpoints")
        if undeclared:
            rep.add("endpoint_undeclared", "warn",
                    f"代码里注册了 {len(undeclared)} 个协议未登记的接口",
                    {"count": len(undeclared),
                     "samples": [f"{u['method']} {u['path']} ({u.get('file')}:{u.get('line')})"
                                 for u in undeclared[:10]]},
                    where="endpoints")
        if auth_missing:
            rep.add("auth_missing", "error",
                    f"{len(auth_missing)} 个接口协议要求鉴权但实现未鉴权（越权风险）",
                    {"count": len(auth_missing),
                     "samples": [f"{a['method']} {a['path']} "
                                 f"({a.get('file')}:{a.get('line')})" for a in auth_missing[:10]]},
                    where="endpoints")
        if deprecated:
            rep.add("deprecated_still_served", "warn",
                    f"{len(deprecated)} 个已声明废弃的接口仍在服务",
                    {"samples": [f"{d['method']} {d['path']}" for d in deprecated[:10]]},
                    where="endpoints")
        dup_eps = code.get("duplicates") or []
        if dup_eps:
            rep.add("endpoint_duplicate", "error",
                    f"{len(dup_eps)} 个路由被重复注册（可能互相覆盖）",
                    {"samples": [f"{d['method']} {d['path']} ×{d['count']}" for d in dup_eps[:10]]},
                    where="endpoints")

        # 3) 迁移
        if mig.get("gaps"):
            rep.add("migration_gap", "error",
                    f"迁移版本不连续，缺 {mig['gaps']}（跳到新环境会少表/少列）",
                    {"gaps": mig["gaps"]}, where="migrations")
        if mig.get("duplicates"):
            rep.add("migration_duplicate", "error",
                    f"{len(mig['duplicates'])} 个迁移版本号重复",
                    {"items": mig["duplicates"]}, where="migrations")
        if mig.get("destructive"):
            sev = "warn" if th.get("allow_destructive_migration") else "error"
            rep.add("migration_destructive", sev,
                    f"{len(mig['destructive'])} 个迁移含破坏性变更且没有备份步骤",
                    {"samples": [d["name"] for d in mig["destructive"][:10]]},
                    where="migrations")
        if mig.get("no_tx"):
            sev = "error" if th.get("require_migration_tx") else "warn"
            rep.add("migration_no_tx", sev,
                    f"{len(mig['no_tx'])} 个迁移没有显式事务（失败会留半截结构）",
                    {"samples": [d["name"] for d in mig["no_tx"][:10]]},
                    where="migrations")
        if mig.get("unsafe_alter"):
            rep.add("migration_unsafe_alter", "warn",
                    f"{len(mig['unsafe_alter'])} 个迁移加 NOT NULL 列但没有 DEFAULT",
                    {"samples": [d["name"] for d in mig["unsafe_alter"][:10]]},
                    where="migrations")
        if mig.get("bad_name"):
            rep.add("migration_bad_name", "warn",
                    f"迁移文件名没有版本号前缀：{mig['bad_name'][:5]}", where="migrations")

        # 4) 配置 / 经济
        item_ids = set(cfg.get("items") or [])
        unknown: List[Dict[str, Any]] = []
        bad_prob: List[Dict[str, Any]] = []
        sum_by_group: Dict[str, float] = {}
        for d in cfg.get("drops") or []:
            item = str(d.get("item_id") or "")
            prob = d.get("prob")
            if item and item_ids and item not in item_ids:
                unknown.append({"file": d.get("file"), "item_id": item})
            if isinstance(prob, (int, float)) and not isinstance(prob, bool):
                if prob < 0 or prob > 1:
                    bad_prob.append({"file": d.get("file"), "item_id": item, "prob": prob})
                group = f"{d.get('file')}::{d.get('source') or 'default'}"
                sum_by_group[group] = sum_by_group.get(group, 0.0) + float(prob)
        if unknown:
            rep.add("config_item_missing", "error",
                    f"掉落表引用了 {len(unknown)} 个不存在的道具",
                    {"samples": unknown[:10]}, where="config.drops")
        if bad_prob:
            rep.add("drop_prob_invalid", "error",
                    f"{len(bad_prob)} 条掉落概率不在 [0,1]",
                    {"samples": bad_prob[:10]}, where="config.drops")
        over = {g: round(v, 4) for g, v in sum_by_group.items() if v > 1.0 + 1e-9}
        if over:
            rep.add("drop_prob_sum", "error",
                    f"{len(over)} 组掉落概率合计 > 1（必出/超发）",
                    {"groups": over}, where="config.drops")
        economy = cfg.get("economy") or {}
        cap = economy.get("faucet_daily_cap", economy.get("max_gold_per_day",
                                                           economy.get("gold_cap")))
        if not economy:
            rep.add("economy_missing", "warn",
                    "没有经济配置（产出上限/回收比例），无法评估通胀风险",
                    where="config.economy")
        elif cap in (None, 0, "0"):
            rep.add("economy_cap_missing", "warn",
                    "经济配置缺每日产出上限（faucet_daily_cap）", where="config.economy")
        infl = economy.get("inflation_daily_pct")
        if isinstance(infl, (int, float)) and not isinstance(infl, bool):
            if float(infl) > float(th["max_inflation_daily_pct"]):
                rep.add("economy_inflation_high", "warn",
                        f"每日通胀 {infl}% 超阈值 {th['max_inflation_daily_pct']}%",
                        {"inflation_daily_pct": infl}, where="config.economy")

        # 5) 事务 / 幂等 / 卫生
        tx_missing = [e for e in implemented
                      if e.get("method") in _WRITE_METHODS and not e.get("tx")]
        if tx_missing and th.get("require_tx"):
            rep.add("tx_missing", "error",
                    f"{len(tx_missing)} 个写接口没有事务保护",
                    {"samples": [f"{e['method']} {e['path']} "
                                 f"({e.get('file')}:{e.get('line')})" for e in tx_missing[:10]]},
                    where="concurrency")
        idem_missing = [e for e in implemented
                        if e.get("method") in _WRITE_METHODS and not e.get("idempotent")]
        if idem_missing:
            rep.add("idempotency_missing", "warn",
                    f"{len(idem_missing)} 个写接口没有幂等键（重放会重复扣费/发货）",
                    {"samples": [f"{e['method']} {e['path']}" for e in idem_missing[:10]]},
                    where="concurrency")

        if code.get("secrets"):
            rep.add("secret_hardcoded", "error",
                    f"代码里有 {len(code['secrets'])} 处疑似硬编码密钥",
                    {"samples": code["secrets"][:5]}, where="code")
        todo = code.get("todo") or []
        if len(todo) > int(th["max_todo"]):
            rep.add("todo_left", "warn",
                    f"代码里有 {len(todo)} 处 TODO/FIXME（阈值 {th['max_todo']}）",
                    {"samples": todo[:8]}, where="code")
        if code.get("broad_except"):
            rep.add("broad_except", "warn",
                    f"{len(code['broad_except'])} 处裸 except/吞异常",
                    {"samples": code["broad_except"][:5]}, where="code")
        if code.get("busy_wait"):
            rep.add("busy_wait", "warn",
                    f"{len(code['busy_wait'])} 处 while True 没有 sleep/await（空转吃 CPU）",
                    {"samples": code["busy_wait"][:5]}, where="code")
        prints = code.get("prints") or []
        if len(prints) > int(th["max_prints"]):
            rep.add("debug_print", "warn",
                    f"服务端代码有 {len(prints)} 处 print（应走日志）",
                    {"count": len(prints)}, where="code")

        # 6) 验收覆盖
        if acceptance.get("total"):
            cov = acceptance.get("coverage") or 0
            if cov < float(th["min_acceptance_coverage"]):
                rep.add("acceptance_uncovered", "warn",
                        f"验收项在文档中覆盖 {cov:.0%}（阈值 {th['min_acceptance_coverage']:.0%}）",
                        {"uncovered": acceptance.get("uncovered", [])[:10]},
                        where="acceptance")

        for f in (cfg.get("parse_errors") or []):
            rep.add("config_parse_error", "error",
                    f"配置文件无法解析：{f.get('file')}", {"error": f.get("error")},
                    where="config")

    # ---------- 报告 ----------
    def check_report(self, report_md: str, data: Optional[Dict[str, Any]], rep) -> None:
        super().check_report(report_md, data, rep)
        if not isinstance(data, dict):
            return
        audit = data.get("audit") if isinstance(data.get("audit"), dict) else None
        snap = data.get("snapshot") if isinstance(data.get("snapshot"), dict) else None

        if audit:
            verdict = audit.get("verdict") or audit.get("decision")
            if verdict in ("fail", "suspect"):
                claims_ok = re.search(r"(全部通过|审核通过|审计通过|验收通过|approved|\bPASS\b)", report_md)
                admits = re.search(r"(不通过|未通过|受阻|block|fail|suspect|待整改)", report_md)
                if claims_ok and not admits:
                    rep.add("narrative_conflict", "error",
                            f"审计结论是 {verdict}，报告却写成通过",
                            {"audit_verdict": verdict}, where="report")
        if snap:
            counts = snap.get("counts") or {}
            if counts.get("missing"):
                if re.search(r"(接口全部实现|端点全部实现|无缺失接口)", report_md):
                    rep.add("narrative_conflict", "error",
                            f"快照显示缺 {counts['missing']} 个接口，报告说全部实现",
                            {"missing": counts["missing"]}, where="report")
            if counts.get("migration_gaps") and re.search(r"(迁移完整|迁移无缺口)", report_md):
                rep.add("narrative_conflict", "error",
                        "快照显示迁移有缺口，报告说迁移完整",
                        {"gaps": snap.get("migration_gaps")}, where="report")

    def check_run(self, snapshot: Dict[str, Any], rep) -> None:
        """流水线层面：开发分片失败 / 审计自身失败，都算交付受阻。"""
        if not isinstance(snapshot, dict):
            return
        steps = snapshot.get("steps") or {}
        for name in ("dev", "audit", "gate", "report"):
            step = steps.get(name)
            if isinstance(step, dict) and step.get("state") in ("failed", "timeout"):
                rep.add("step_failed", "error",
                        f"关键步骤 {name} 未成功：{step.get('state')}",
                        {"step": name, "error": step.get("error")}, where=f"run.{name}")

    def series_from(self, data: Dict[str, Any]) -> Dict[str, List[float]]:
        return {}


register_profile(GameServerProfile())


# ═══════════════════════════════════════════════════════════ 动作 / 注册
def audit_code(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """`auditor/code`：采集 + 审计 + 回执。params：

    · repo_root  被审代码根目录（必填，除非直接给 snapshot）
    · doc_root   设计文档目录（默认 repo_root/docs）
    · spec_path  协议文件（默认自动找 spec/proto/api 下的 JSON）
    · ticket     开发报告/工单（用于验收覆盖）
    · level      rule（默认）/ stat / llm
    · thresholds 覆盖 profile 阈值
    """
    from .audit import Auditor

    params = params or {}
    snapshot = params.get("snapshot")
    if not isinstance(snapshot, dict) or not snapshot:
        root = params.get("repo_root")
        if not root:
            return {"verdict": "fail", "severity": "error", "project": "game_server",
                    "findings": [{"code": "no_repo_root", "severity": "error",
                                  "message": "auditor/code 需要 repo_root 或 snapshot",
                                  "where": "params", "evidence": {}}],
                    "counts": {"error": 1, "warn": 0, "info": 0, "soft": 0},
                    "metrics": {}, "layers": {}, "snapshot_summary": {"collected": False},
                    "decision": "fail"}
        snapshot = collect_snapshot(root, params.get("doc_root"),
                                    params.get("spec_path"), params.get("ticket"))
    if not snapshot.get("collected"):
        return {"verdict": "fail", "severity": "error", "project": "game_server",
                "findings": [{"code": "snapshot_incomplete", "severity": "error",
                              "message": snapshot.get("error") or "快照采集失败",
                              "where": "snapshot", "evidence": {}}],
                "counts": {"error": 1, "warn": 0, "info": 0, "soft": 0},
                "metrics": {}, "layers": {}, "snapshot_summary": {"collected": False},
                "decision": "fail"}

    level = params.get("level") or "rule"
    auditor = Auditor(project="game_server", level=level,
                      thresholds=params.get("thresholds"))
    report = auditor.audit_balance(snapshot, level=level)
    out = report.as_dict()
    out["decision"] = out["verdict"]
    out["snapshot_summary"] = snapshot_summary(snapshot)
    # 报告官要用的验收/协议样本，已在 summary 里
    return out


def run_action(action: str, params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None
               ) -> Dict[str, Any]:
    if action in ("code", "spec", "full"):
        return audit_code(params, ctx)
    raise ValueError(f"game_server 审计不支持的动作：{action}")


GAME_AUDIT_ACTIONS: Tuple[str, ...] = ("code", "spec")


def register(runtime, capability: str = "auditor",
             actions: Sequence[str] = GAME_AUDIT_ACTIONS) -> None:
    """把 `auditor/code`（游戏服务端规则审计）挂到一个 Agent 上。

    与 `integrations.audit.register()` 可并存：那套注册 balance/report/run/full，
    这里只补 code/spec，不覆盖已有动作。
    """
    for action in actions:
        runtime.add_handler(capability, action,
                            (lambda a: (lambda p, c: run_action(a, p, c)))(action))