"""开发角色的框架实现：**应用 edits → 出补丁 → 语法检查**，零 token。

为什么从项目里抽出来
------------------
"把一批 find/replace 落到工作副本、出 unified diff、跑语法检查"是**开发角色**的本事，
不是某个业务项目（游戏发布、游戏 bugfix、别的什么）的私有逻辑。以前它长在
`projects/game_server.py` 里，别的项目要用只能继承那个项目 —— 项目之间因此被绑在一起。

抽出来之后：

    · `projects/game_server.py`     仍按原样调用（行为不变）
    · `projects/roles/bugfix_dev.py` 独立项目，也只声明 `dev` 一个角色
    · 别的业务要做"开发"角色，直接 `register(runtime)` 即可

三条边界（和原来一致）
--------------------
1. **路径不越界**：所有写入都过 `_join_under`，`..` / 绝对路径一律拒绝；
2. **find 必须命中**：未命中记 `miss`，不算改过，不硬替换；
3. **语法不过就报错**：`.py` 文件跑 `compile()`，失败进 `failed`，不掩盖。
"""
from __future__ import annotations

import difflib
import hashlib
import os
from typing import Any, Dict, List, Optional, Tuple

DEV_ACTIONS = ("implement",)


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def implement(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """`dev/implement`：一个分片 → 改好的工作副本 + 补丁 + 检查结果。"""
    params = params or {}
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


def run_action(action: str, params: Dict[str, Any],
               ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if action == "implement":
        return implement(params, ctx)
    if action in ("locate", "propose"):
        from . import game_server_bugfix as bugfix

        return getattr(bugfix, action)(params, ctx)
    raise ValueError(f"dev 不支持的动作：{action}")


class DevHandler:
    """开发角色的框架骨架：收单一件事，分三步做（locate → propose → implement）。

    默认实现全部零 token（`locate`/`propose` 在 `game_server_bugfix.py`，`implement` 在本文件），
    动作名、入参、出参见 `docs/DEV_INTERFACE.md`。要换成会写代码的模型 Agent，
    继承本类、覆盖对应方法即可，编排不用改：

        class MyDev(DevHandler):
            def propose(self, params, ctx=None):
                ...  # 返回 {workstreams: [{files, edits, acceptance}]}

        register_framework(rt, handler=MyDev())

    也可以只实现一个动作（例如只接外部编码 Agent 做 implement），其余继承默认。
    """

    actions = ("locate", "propose", "implement")

    def locate(self, params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from .game_server_bugfix import locate

        return locate(params, ctx)

    def propose(self, params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from .game_server_bugfix import propose

        return propose(params, ctx)

    def implement(self, params: Dict[str, Any],
                  ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return implement(params, ctx)

    def binding(self) -> Dict[str, Any]:
        """动作名 → 绑定好 self 的处理函数（供 `register_framework` 挂载）。"""
        return {action: (lambda a: (lambda p, c: getattr(self, a)(p, c)))(action)
                for action in self.actions}


def register_framework(runtime: Any, capability: str = "dev",
                       handler: Optional[DevHandler] = None) -> None:
    """把开发角色的三个动作一起挂上：`locate` / `propose` / `implement`。"""
    for action, fn in (handler or DevHandler()).binding().items():
        runtime.add_handler(capability, action, fn)


def register(runtime: Any, capability: str = "dev",
             actions: tuple = DEV_ACTIONS,
             handler: Optional[DevHandler] = None) -> None:
    """兼容旧入口：默认只挂 `dev/implement`（单角色项目、CLI 用）。"""
    bound = (handler or DevHandler()).binding()
    for action in actions:
        if action in bound:
            runtime.add_handler(capability, action, bound[action])