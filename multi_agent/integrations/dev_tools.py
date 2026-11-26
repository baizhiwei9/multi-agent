"""开发角色的只读工具集（给 function call 用）。

只给读，不给写：改代码仍然只能通过 `edits` 走 find/replace + 语法门 + 回放门。
三个工具覆盖"找代码"的最小闭环：

    read_file  读一个文件（截断）
    grep_code  按正则搜，返回 file:line:text
    list_dir   列一层目录

两个硬边界：

1. **路径不出仓库**：`read_file` / `list_dir` 的路径先 `realpath` 再判前缀，越界直接拒；
2. **结果有上限**：单文件 4000 字、grep 50 条、目录 100 项，超了 `truncated=True`，
   不静默截断。

`build(repo_root)` 返回 `(tool_specs, impls)`：specs 是 OpenAI function 格式，
impls 是 `name -> callable(args) -> dict`，直接喂给 `llm/chat` 的 `tools` + `tool_impls`。
"""
from __future__ import annotations

import os
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "dist", "build",
              ".pytest_cache", ".mypy_cache", ".idea"}
_CODE_EXTS = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".rb", ".rs",
              ".c", ".h", ".cpp", ".cs", ".php", ".sql", ".sh", ".yml", ".yaml",
              ".json", ".toml", ".ini", ".cfg", ".md")
_MAX_FILES = 400


def _safe(repo_root: Optional[str], rel: str) -> Optional[str]:
    """仓库内路径 → 绝对路径；越界返回 None。"""
    root = os.path.realpath(str(repo_root or "."))
    target = os.path.realpath(os.path.join(root, str(rel or ".")))
    if target != root and not target.startswith(root + os.sep):
        return None
    return target


def read_file(repo_root: Optional[str], path: str,
              max_chars: int = 4000) -> Dict[str, Any]:
    full = _safe(repo_root, path)
    if full is None:
        return {"error": "path_outside_repo", "path": path}
    if not os.path.isfile(full):
        return {"error": "not_found", "path": path}
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        return {"error": f"read_failed: {exc}", "path": path}
    cut = len(text) > max_chars
    return {"path": path, "lines": text.count("\n") + 1,
            "content": text[:max_chars], "truncated": cut}


def grep_code(repo_root: Optional[str], pattern: str, max_hits: int = 50,
              exts: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    if not pattern:
        return {"error": "empty_pattern", "hits": []}
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return {"error": f"bad_regex: {exc}", "hits": []}
    root = os.path.realpath(str(repo_root or "."))
    if not os.path.isdir(root):
        return {"error": "repo_not_found", "hits": []}
    wanted = tuple(str(e) for e in exts) if exts else _CODE_EXTS

    hits: List[Dict[str, Any]] = []
    scanned = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if not name.endswith(wanted):
                continue
            scanned += 1
            if scanned > _MAX_FILES:
                truncated = True
                break
            full = os.path.join(dirpath, name)
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    for no, line in enumerate(fh, start=1):
                        if rx.search(line):
                            hits.append({
                                "file": os.path.relpath(full, root).replace(os.sep, "/"),
                                "line": no, "text": line.strip()[:200]})
                            if len(hits) >= max_hits:
                                truncated = True
                                break
            except OSError:
                continue
            if len(hits) >= max_hits or truncated:
                break
        if len(hits) >= max_hits or truncated:
            break
    return {"pattern": pattern, "hits": hits, "count": len(hits),
            "scanned_files": min(scanned, _MAX_FILES), "truncated": truncated}


def list_dir(repo_root: Optional[str], path: str = "",
             max_entries: int = 100) -> Dict[str, Any]:
    full = _safe(repo_root, path)
    if full is None:
        return {"error": "path_outside_repo", "path": path}
    if not os.path.isdir(full):
        return {"error": "not_a_directory", "path": path}
    entries = []
    for name in sorted(os.listdir(full)):
        if name in _SKIP_DIRS:
            continue
        child = os.path.join(full, name)
        entries.append({"name": name, "type": "dir" if os.path.isdir(child) else "file"})
        if len(entries) >= max_entries:
            break
    return {"path": path or ".", "entries": entries, "count": len(entries),
            "truncated": len(entries) >= max_entries}


def build(repo_root: Optional[str], max_chars: int = 4000,
          max_hits: int = 50) -> Tuple[List[Dict[str, Any]], Dict[str, Callable[[Dict[str, Any]], Any]]]:
    """构造 tools 规格与实现。默认三个只读工具，返回顺序与这里一致。"""

    def _read(args: Dict[str, Any]) -> Dict[str, Any]:
        return read_file(repo_root, str(args.get("path") or ""),
                         int(args.get("max_chars") or max_chars))

    def _grep(args: Dict[str, Any]) -> Dict[str, Any]:
        return grep_code(repo_root, str(args.get("pattern") or ""),
                         int(args.get("max_hits") or max_hits), args.get("exts"))

    def _ls(args: Dict[str, Any]) -> Dict[str, Any]:
        return list_dir(repo_root, str(args.get("path") or ""))

    specs = [
        {"type": "function", "function": {
            "name": "read_file",
            "description": "读仓库内的一个代码/配置文件，返回内容（可能截断）",
            "parameters": {"type": "object",
                           "properties": {"path": {"type": "string",
                                                   "description": "相对仓库根的路径"}},
                           "required": ["path"]}}},
        {"type": "function", "function": {
            "name": "grep_code",
            "description": "在仓库里按正则搜索代码行，返回 file / line / text",
            "parameters": {"type": "object",
                           "properties": {"pattern": {"type": "string"},
                                          "max_hits": {"type": "integer"}},
                           "required": ["pattern"]}}},
        {"type": "function", "function": {
            "name": "list_dir",
            "description": "列仓库内一层目录，确认文件都在哪",
            "parameters": {"type": "object",
                           "properties": {"path": {"type": "string"}},
                           "required": []}}},
    ]
    impls: Dict[str, Callable[[Dict[str, Any]], Any]] = {
        "read_file": _read, "grep_code": _grep, "list_dir": _ls}
    return specs, impls