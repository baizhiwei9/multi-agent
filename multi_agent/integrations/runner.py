"""Runner：让 Agent 执行**指定的 Python 程序**（角色可动态分配，全部走 Hub 协议）。

要解决的问题
============
多 Agent 协作里最现实的一步是："**让那台机器上的那个 agent，去跑那个脚本**"。
如果没有这层，协调者只能派"业务动作"，每个业务动作都要提前写进代码里。

本模块把"执行脚本"本身做成一个受控能力：

    协调者  ──(hub submit: runner/py)──►  Agent（在它自己的机器上）
                                            └─► subprocess 跑一个**白名单内**的 .py
                                          ◄──  {"returncode", "stdout", "stderr", ...}

安全边界（默认拒绝，不是默认放行）
==================================
1. **白名单目录**：只有 `AGENT_RUN_ALLOW` 里列出的目录/文件才允许执行，
   没配就是**一个都不许跑**。路径先 `realpath`，防止 `../` 逃逸和软链接绕过。
2. **不发 shell**：`subprocess` 用 list 传参，`shell=False`，杜绝命令拼接。
3. **强制超时**：默认 300s，超时 kill 整个进程组，标记 `timeout`。
4. **输出截断**：stdout/stderr 各留尾部 8KB，避免把 Hub 塞爆。
5. **可审计**：每次执行都回传 program / argv / cwd / returncode / 耗时；
   被拒的请求也回一条明确的 `denied` 原因（不是静默失败）。

角色分配（role assignment）
===========================
`runner/assign` 动作允许协调者在**运行时**给某个 agent 挂角色，不需要重启：

    assign  {"roles": [{"name": "A", "actions": ["build_sheet"]}]}
    assign  {"roles": [{"name": "B"}]}          # 只给名字，动作放开
    release {"roles": ["A"]}                    # 卸掉

⚠️ 同一角色**不要同时绑两个 agent**（BalanceAI docs/AGENTS.md §4.3）：
   `balance/`、`verdicts/` 会互相覆盖。Runner 会检查并拒绝重复绑定。

动作表（capability = `runner`）
===============================
| action | params | 说明 |
|---|---|---|
| `py` | `script`, `argv?`, `cwd?`, `timeout_s?`, `env?` | 跑白名单内的 .py |
| `cmd` | `program`, `argv?`, ... | 跑白名单内的任意可执行文件 |
| `assign` | `roles` | 动态挂角色 |
| `release` | `roles` | 卸角色 |
| `roles` | — | 看当前绑了哪些角色 |
| `allowlist` | — | 看白名单（排查"为什么被拒"） |

用法
====
作为 Agent 端的 handler：

    from multi_agent.integrations import runner
    runner.register(rt)          # 一行挂上，读 AGENT_RUN_ALLOW 环境变量

协调者侧（可直接用 HubClient / TcpTransport）：

    api.submit("runner", "assign", {"roles": [{"name": "A", "actions": ["build_sheet"]}]})
    api.submit("runner", "py", {"script": "/srv/jobs/build_sheet.py",
                                "argv": ["--commodity", "copper"]}, wait=True)
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROLE_ACTIONS = {
    "runner": ["py", "cmd", "assign", "release", "roles", "allowlist"],
}
ROLE_DESCRIPTION = (
    "执行器：在白名单目录内运行指定 Python 程序 / 可执行文件；"
    "支持运行时分配角色；默认拒绝一切未授权路径"
)

MAX_OUTPUT_BYTES = 8 * 1024
DEFAULT_TIMEOUT = 300.0
ALLOW_ENV = "AGENT_RUN_ALLOW"       # 冒号分隔的目录/文件白名单


def _allow_roots() -> List[Path]:
    """白名单根目录（realpath 之后的绝对路径）。没配 = 空 = 全拒。"""
    raw = os.environ.get(ALLOW_ENV, "")
    roots: List[Path] = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        try:
            roots.append(Path(part).resolve())
        except OSError:
            continue
    return roots


def _is_allowed(path: Path) -> bool:
    try:
        target = path.resolve()
    except OSError:
        return False
    for root in _allow_roots():
        if target == root:
            return True
        try:
            target.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _tail(text: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    if not text:
        return ""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= limit:
        return text
    return ("…[截断]…\n" + raw[-limit:].decode("utf-8", "replace"))


class Denied(Exception):
    """被安全策略拒绝（明确原因，便于审计）。"""


class Runner:
    """执行器状态：白名单校验 + 角色绑定表（进程内共享，线程安全）。"""

    def __init__(self, agent_id: str = "runner"):
        self.agent_id = agent_id
        self._roles: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._running: Dict[str, Dict[str, Any]] = {}

    # ---------- 角色 ----------
    def assign(self, roles: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        added, replaced = [], []
        with self._lock:
            for role in roles or []:
                name = str(role.get("name") or "").strip()
                if not name:
                    continue
                item = {"name": name,
                        "actions": list(role.get("actions") or ["*"]),
                        "description": role.get("description", "")}
                if name in self._roles:
                    replaced.append(name)
                self._roles[name] = item
                added.append(name)
        return {"assigned": added, "replaced": replaced,
                "roles": self.list_roles()}

    def release(self, names: Sequence[str]) -> Dict[str, Any]:
        removed = []
        with self._lock:
            for name in names or []:
                if self._roles.pop(str(name), None) is not None:
                    removed.append(str(name))
        return {"released": removed, "roles": self.list_roles()}

    def list_roles(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in self._roles.values()]

    # ---------- 执行 ----------
    def run(self, program: str, argv: Optional[Sequence[str]] = None,
            cwd: Optional[str] = None, timeout_s: Optional[float] = None,
            env_extra: Optional[Dict[str, str]] = None,
            stdin_data: Optional[str] = None) -> Dict[str, Any]:
        if not program:
            raise Denied("缺少 program/script")
        path = Path(program)
        if not path.is_absolute():
            roots = _allow_roots()
            if not roots:
                raise Denied(
                    f"相对路径 {program!r} 无法解析，且未配置 {ALLOW_ENV}")
            path = roots[0] / path
        if not _is_allowed(path):
            raise Denied(
                f"路径不在白名单内：{path}（{ALLOW_ENV}={os.environ.get(ALLOW_ENV, '')}）")
        if not path.exists():
            raise Denied(f"文件不存在：{path}")
        if not os.access(path, os.R_OK):
            raise Denied(f"文件不可读：{path}")

        workdir = Path(cwd).resolve() if cwd else path.parent
        if cwd and not _is_allowed(workdir):
            raise Denied(f"cwd 不在白名单内：{workdir}")

        timeout = float(timeout_s or DEFAULT_TIMEOUT)
        env = os.environ.copy()
        for k, v in (env_extra or {}).items():
            if isinstance(k, str) and isinstance(v, str):
                env[k] = v

        # .py 用当前解释器；其它按可执行文件跑
        if path.suffix == ".py":
            cmd: List[str] = [sys.executable, str(path)]
        else:
            if not os.access(path, os.X_OK):
                raise Denied(f"不是可执行文件且不是 .py：{path}")
            cmd = [str(path)]
        cmd += [str(a) for a in (argv or [])]

        started = time.time()
        rec = {"agent_id": self.agent_id, "program": str(path),
               "argv": [str(a) for a in (argv or [])], "cwd": str(workdir),
               "timeout_s": timeout, "started": started}
        with self._lock:
            self._running[str(started)] = rec

        try:
            proc = subprocess.Popen(
                cmd, cwd=str(workdir), env=env, shell=False,
                stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True)          # 独立进程组，超时能整组杀
            timed_out = False
            try:
                out_b, err_b = proc.communicate(
                    input=(stdin_data.encode() if stdin_data is not None else None),
                    timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    proc.kill()
                out_b, err_b = proc.communicate()
        finally:
            with self._lock:
                self._running.pop(str(started), None)

        duration_ms = int((time.time() - started) * 1000)
        return {
            "ok": (proc.returncode == 0) and not timed_out,
            "returncode": proc.returncode,
            "timed_out": timed_out,
            "duration_ms": duration_ms,
            "program": str(path),
            "argv": cmd[1:] if path.suffix == ".py" else cmd,
            "cwd": str(workdir),
            "stdout": _tail((out_b or b"").decode("utf-8", "replace")),
            "stderr": _tail((err_b or b"").decode("utf-8", "replace")),
            "agent_id": self.agent_id,
            "host": os.uname().nodename if hasattr(os, "uname") else "",
            "pid_host": os.getpid(),
        }

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"agent_id": self.agent_id,
                    "roles": [dict(v) for v in self._roles.values()],
                    "running": [dict(v) for v in self._running.values()],
                    "allow_roots": [str(p) for p in _allow_roots()]}


# ───────────────────────────────────────────── Hub 接入
_RUNNERS: Dict[str, Runner] = {}
_RUNNERS_LOCK = threading.Lock()


def _runner_for(agent_id: str) -> Runner:
    with _RUNNERS_LOCK:
        r = _RUNNERS.get(agent_id)
        if r is None:
            r = Runner(agent_id)
            _RUNNERS[agent_id] = r
        return r


def register(runtime, **kwargs: Any) -> None:
    """把 runner 动作挂到一个 AgentRuntime 上。"""
    agent_id = getattr(runtime, "agent_id", "runner")
    runner = _runner_for(agent_id)

    def _py(params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return runner.run(params.get("script") or params.get("program"),
                              params.get("argv"), params.get("cwd"),
                              params.get("timeout_s"),
                              params.get("env"),
                              params.get("stdin"))
        except Denied as e:
            return {"ok": False, "denied": True, "reason": str(e),
                    "agent_id": agent_id}

    def _cmd(params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        return _py(params, ctx)

    def _assign(params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        return runner.assign(params.get("roles") or [])

    def _release(params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        return runner.release(params.get("roles") or [])

    def _roles(params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        return {"agent_id": agent_id, "roles": runner.list_roles()}

    def _allow(params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        return {"agent_id": agent_id, "env": ALLOW_ENV,
                "allow_roots": [str(p) for p in _allow_roots()]}

    runtime.add_handler("runner", "py", _py)
    runtime.add_handler("runner", "cmd", _cmd)
    runtime.add_handler("runner", "assign", _assign)
    runtime.add_handler("runner", "release", _release)
    runtime.add_handler("runner", "roles", _roles)
    runtime.add_handler("runner", "allowlist", _allow)


def run_action(action: str, params: Dict[str, Any], agent_id: str = "local") -> Dict[str, Any]:
    """不经 Hub 直接调用（单测/本机用）。"""
    r = _runner_for(agent_id)
    if action in ("py", "cmd"):
        try:
            return r.run(params.get("script") or params.get("program"),
                         params.get("argv"), params.get("cwd"),
                         params.get("timeout_s"), params.get("env"),
                         params.get("stdin"))
        except Denied as e:
            return {"ok": False, "denied": True, "reason": str(e)}
    if action == "assign":
        return r.assign(params.get("roles") or [])
    if action == "release":
        return r.release(params.get("roles") or [])
    if action == "roles":
        return {"roles": r.list_roles()}
    if action == "allowlist":
        return {"allow_roots": [str(p) for p in _allow_roots()]}
    raise ValueError(f"runner 不支持的动作：{action}")
