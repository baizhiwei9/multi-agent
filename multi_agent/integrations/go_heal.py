"""编译失败的自愈闭环：0 token，不碰模型。

    parse_build_errors(stderr)       Go 编译器的报错 → 结构化（file / line / message）
    selfheal(repo_root, out, ...)    编译 → 失败就定位 → 规则修 → 重编（最多 N 轮）

定位用的是 dev 那套**只读 function call**（`grep_code` / `read_file`），每次调用都记进
`tool_calls`，报告里能看清"它到底查了什么才改的"——不是黑盒猜测。

只修能机械判定的错，典型就是写错标识符：

    ./main.go:41:2: undefined: usr        → 同文件里最近的已声明标识符是 user，按行替换

修不了的（缺 import、语法错、改完还是错、超过轮次）原样进 `deferred`，
`ok=False` 交回编排，绝不"改到能编过为止"式硬凑。

返回的 `artifact` 与 `go_build` 同口径（ok/bin/sha256/bytes），下游审计拿它当凭据。
"""
from __future__ import annotations

import difflib
import os
import re
from typing import Any, Dict, List, Optional

from .dev_tools import grep_code, read_file
from .go_toolchain import go_build

_ERR = re.compile(r"^(?:\./)?([^\s:]+\.go):(\d+):(\d+):\s*(.+)$", re.M)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_GO_KEYWORDS = {
    "break", "case", "chan", "const", "continue", "default", "defer", "else",
    "fallthrough", "for", "func", "go", "goto", "if", "import", "interface",
    "map", "package", "range", "return", "select", "struct", "switch", "type",
    "var", "nil", "true", "false", "string", "int", "bool", "any", "byte",
    "error", "float64", "main", "fmt", "os", "time", "flag",
}
_MAX_TOOL_CALLS = 24


def parse_build_errors(stderr: str) -> List[Dict[str, Any]]:
    """把 `go build` 的 stderr 拆成结构化错误，解析不了的不算。"""
    errors: List[Dict[str, Any]] = []
    for m in _ERR.finditer(stderr or ""):
        errors.append({"file": m.group(1), "line": int(m.group(2)),
                       "col": int(m.group(3)), "message": m.group(4).strip()})
    return errors


def _identifiers(text: str) -> List[str]:
    return sorted({w for w in _IDENT.findall(text or "") if w not in _GO_KEYWORDS})


def _suggest(err: Dict[str, Any], repo_root: str,
             tool_calls: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """一条错误 → 一条按行编辑；没有可套的规则就返回 None。"""
    message = str(err.get("message") or "")
    if not message.startswith("undefined:"):
        return None
    name = message.split(":", 1)[1].strip()
    if not name:
        return None

    # function call 1：这个陌生标识符在仓库里到底有没有出现过
    if len(tool_calls) < _MAX_TOOL_CALLS:
        hits = grep_code(repo_root, r"\b%s\b" % re.escape(name), max_hits=20)
        tool_calls.append({"tool": "grep_code", "args": {"pattern": name},
                           "hits": len(hits.get("hits") or []),
                           "error": hits.get("error")})

    # function call 2：把出错文件读回来，拿它的标识符当候选
    text = ""
    if len(tool_calls) < _MAX_TOOL_CALLS:
        got = read_file(repo_root, err["file"], max_chars=20000)
        text = str(got.get("content") or "")
        tool_calls.append({"tool": "read_file", "args": {"path": err["file"]},
                           "lines": got.get("lines")})

    candidates = [w for w in _identifiers(text) if w != name]
    best = difflib.get_close_matches(name, candidates, n=1, cutoff=0.6)
    if not best:
        return None
    target = best[0]
    return {"file": err["file"], "line": int(err["line"]), "find": name,
            "replace": target, "rule": "undefined_identifier",
            "why": f"{name} 未定义，同文件最近的已声明标识符是 {target}"}


def _apply_line_edit(repo_root: str, edit: Dict[str, Any]) -> Dict[str, Any]:
    """按行替换，只动报错那一行，不碰别的同名 token。"""
    path = os.path.realpath(os.path.join(repo_root, edit["file"]))
    if not path.startswith(os.path.realpath(repo_root) + os.sep):
        return {"applied": False, "reason": "path_outside_repo"}
    if not os.path.isfile(path):
        return {"applied": False, "reason": "file_missing"}
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    idx = edit["line"] - 1
    if not 0 <= idx < len(lines):
        return {"applied": False, "reason": "line_out_of_range"}
    old = lines[idx]
    new = re.sub(r"\b%s\b" % re.escape(edit["find"]), edit["replace"], old, count=1)
    if new == old:
        return {"applied": False, "reason": "token_not_on_line"}
    lines[idx] = new
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    diff = "".join(difflib.unified_diff([old], [new],
                                        fromfile=f"a/{edit['file']}",
                                        tofile=f"b/{edit['file']}", n=0))
    return {"applied": True, "diff": diff}


def selfheal(repo_root: str, out: str, go_bin: Optional[str] = None,
             max_rounds: int = 3) -> Dict[str, Any]:
    """先编一次；编不过就定位→修→重编，最多 max_rounds 轮。"""
    tool_calls: List[Dict[str, Any]] = []
    attempts: List[Dict[str, Any]] = []
    deferred: List[Dict[str, Any]] = []

    artifact = go_build(repo_root, out=out, go_bin=go_bin)
    rounds = 0
    while not artifact.get("ok") and rounds < max_rounds:
        rounds += 1
        errors = parse_build_errors(artifact.get("stderr") or "")
        if not errors:
            deferred.append({"reason": "unparsable_stderr",
                             "stderr": (artifact.get("stderr") or "")[-800:]})
            break
        applied_any = False
        for err in errors:
            edit = _suggest(err, repo_root, tool_calls)
            if not edit:
                deferred.append({"reason": "no_rule", "error": err})
                continue
            result = _apply_line_edit(repo_root, edit)
            attempts.append({"round": rounds, **edit, **result})
            applied_any = applied_any or bool(result.get("applied"))
        if not applied_any:
            deferred.append({"reason": "nothing_applied", "errors": errors})
            break
        artifact = go_build(repo_root, out=out, go_bin=go_bin)

    return {
        "ok": bool(artifact.get("ok")),
        "needed": bool(attempts) or bool(deferred),
        "rounds": rounds,
        "attempts": attempts,
        "deferred": deferred,
        "tool_calls": tool_calls,
        "artifact": artifact,
        "llm": {"used": False, "token": 0},
    }