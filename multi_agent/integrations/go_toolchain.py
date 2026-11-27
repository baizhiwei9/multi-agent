"""Go 工具链 function call：把「改代码 → 编译出包」真的绑到 `go build` 上。

三个入口，一件一件事：

    go_build(repo_root, out, ...)    调 go build 出二进制，失败原样回传 stdout/stderr
    run_login(bin, user, pass, log)  跑一次登录，回读它自己写的那份日志
    tool_specs()                     同一件事的 OpenAI function 格式（给模型调）

硬约定：

1. **找不到 go 就报 `go_not_found`**，绝不假装编译成功；
2. 仓库里没有 `go.mod` 报 `not_a_go_module`，不在别处乱翻；
3. 返回的 `bin` 是绝对路径，`sha256` 是产物真实摘要 —— 审计和同步都拿它当凭据。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

_GO_CANDIDATES = ("/usr/local/go/bin/go", "~/.local/go/bin/go",
                  "~/toolchain/go/bin/go",
                  "~/../toolchain/go/bin/go")   # 会话共享 toolchain（与 js 同级）


def find_go() -> Optional[str]:
    """按 GO_BIN → PATH → 常见安装位置找 go，找不到返回 None。"""
    env = os.environ.get("GO_BIN")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("go")
    if found:
        return found
    for cand in _GO_CANDIDATES:
        path = os.path.expanduser(cand)
        if os.path.isfile(path):
            return path
    return None


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _go_env() -> Dict[str, str]:
    env = dict(os.environ)
    home = env.get("HOME") or os.path.expanduser("~")
    env.setdefault("GOCACHE", os.path.join(home, ".cache", "go-build"))
    env.setdefault("GOPATH", os.path.join(home, "go"))
    env.setdefault("GO111MODULE", "on")
    env.setdefault("GOTOOLCHAIN", "local")
    return env


def go_build(repo_root: str, out: str = "bin/login", package: str = "./",
             go_bin: Optional[str] = None, timeout_s: float = 180.0) -> Dict[str, Any]:
    """在 `repo_root` 里编译 `package`，产物写到 `out`（相对则落在 repo_root 下）。"""
    root = os.path.realpath(str(repo_root or ""))
    if not os.path.isfile(os.path.join(root, "go.mod")):
        return {"ok": False, "error": "not_a_go_module", "repo_root": root}
    tool = go_bin or find_go()
    if not tool:
        return {"ok": False, "error": "go_not_found"}

    target = out if os.path.isabs(out) else os.path.join(root, out)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    started = time.time()
    try:
        proc = subprocess.run([tool, "build", "-o", target, package], cwd=root,
                              env=_go_env(), capture_output=True, text=True,
                              timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "timeout_s": timeout_s, "go": tool}
    except OSError as exc:
        return {"ok": False, "error": f"exec_failed: {exc}", "go": tool}

    ok = proc.returncode == 0 and os.path.isfile(target)
    result: Dict[str, Any] = {
        "ok": ok, "repo_root": root, "package": package, "go": tool,
        "bin": os.path.realpath(target) if ok else target,
        "duration_ms": int((time.time() - started) * 1000),
        "exit_code": proc.returncode,
        "stdout": (proc.stdout or "")[-2000:],
        "stderr": (proc.stderr or "")[-4000:],
    }
    if ok:
        result["bytes"] = os.path.getsize(target)
        result["sha256"] = _sha256(target)
    else:
        result["error"] = "build_failed"
    return result


def run_login(bin_path: str, user: str, password: str, log_path: str,
              timeout_s: float = 30.0) -> Dict[str, Any]:
    """跑一次登录（先清空日志），返回它写出来的日志行。"""
    if not os.path.isfile(bin_path):
        return {"ok": False, "error": "bin_missing", "bin": bin_path}
    os.makedirs(os.path.dirname(os.path.abspath(log_path)) or ".", exist_ok=True)
    open(log_path, "w", encoding="utf-8").close()
    try:
        proc = subprocess.run([bin_path, "-user", user, "-pass", password,
                               "-log", log_path],
                              capture_output=True, text=True, timeout=timeout_s)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"ok": False, "error": f"run_failed: {exc}", "bin": bin_path}
    lines: List[str] = []
    if os.path.isfile(log_path):
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = [ln.rstrip("\n") for ln in fh if ln.strip()]
    return {"ok": proc.returncode == 0, "exit_code": proc.returncode,
            "bin": bin_path, "log_path": log_path, "lines": lines,
            "text": "\n".join(lines), "stderr": (proc.stderr or "")[-1000:]}


def tool_specs() -> List[Dict[str, Any]]:
    """OpenAI function 格式：模型只会请求编译，真正改代码仍走 edits。"""
    return [{
        "type": "function",
        "function": {
            "name": "go_build",
            "description": "在 Go 模块里执行 go build，把二进制出到指定路径；返回 sha256 与字节数",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_root": {"type": "string", "description": "含 go.mod 的目录"},
                    "out": {"type": "string", "description": "产物路径，相对 repo_root 或绝对"},
                    "package": {"type": "string", "description": "包路径，默认 ./"},
                },
                "required": ["repo_root"],
            },
        },
    }]