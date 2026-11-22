"""MCP（Model Context Protocol）适配：stdio + HTTP 两种传输。

方向一：**MCP Server** —— 把 Hub 的能力清单暴露成 MCP tools，
        外部 MCP 客户端（Claude Desktop、IDE、其它 Agent）可直接 tools/call，
        只传 tool 名 + 参数，不塞上下文，token 消耗最低。

方向二：**MCP Agent** —— 把一个外部 MCP Server 的工具挂到 Hub 上，
        让协调者像调本地角色一样调用它们。

协议版本：2024-11-05（JSON-RPC 2.0 行分隔）。
"""

from __future__ import annotations

import json
import re
import select
import shlex
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Union

from .client import AgentRuntime, HubClient

MCP_VERSION = "2024-11-05"


def _tool_name(capability: str, action: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"{capability}__{action}")


class MCPServer:
    """把 Hub 能力暴露为 MCP tools；既可本地（hub 对象）也可远程（hub_url）。"""

    def __init__(self, hub=None, hub_url: Optional[str] = None,
                 token: Optional[str] = None, name: str = "multi-agent-hub"):
        self.hub = hub
        self.client = HubClient(hub_url, token=token) if hub is None else None
        if hub is None and not hub_url:
            raise ValueError("需要 hub 或 hub_url 之一")
        self.name = name

    # ---------- MCP 主循环 ----------
    def handle(self, req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = req.get("method")
        mid = req.get("id")
        if method == "initialize":
            return _ok(mid, {
                "protocolVersion": MCP_VERSION,
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": self.name, "version": "1.0.0"},
            })
        if method and method.startswith("notifications/"):
            return None
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            return _ok(mid, {"tools": self.tools()})
        if method == "tools/call":
            return self._call(mid, req.get("params") or {})
        if method in ("resources/list", "prompts/list"):
            return _ok(mid, {"resources": []} if method == "resources/list" else {"prompts": []})
        return _err(mid, -32601, f"未知方法：{method}")

    # ---------- 工具 ----------
    def tools(self) -> List[Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for agent in self._agents():
            for role in agent.get("roles") or []:
                cap = role.get("name")
                for action in role.get("actions") or []:
                    if action == "*":
                        continue
                    name = _tool_name(cap, action)
                    out.setdefault(name, {
                        "name": name,
                        "description": role.get("description") or f"{cap}/{action}",
                        "inputSchema": {"type": "object", "additionalProperties": True},
                    })
        return sorted(out.values(), key=lambda t: t["name"])

    def _call(self, mid: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        args = dict(params.get("arguments") or {})
        target = self._resolve(name)
        if not target:
            return _err(mid, -32602, f"未知工具：{name}")
        capability, action = target
        timeout_s = float(args.pop("_timeout_s", 60))
        task = self._submit(capability, action, args, timeout_s)
        text = json.dumps(task, ensure_ascii=False)
        return _ok(mid, {
            "content": [{"type": "text", "text": text}],
            "isError": task.get("state") != "succeeded",
        })

    def _resolve(self, name: Optional[str]):
        if not name:
            return None
        for agent in self._agents():
            for role in agent.get("roles") or []:
                for action in role.get("actions") or []:
                    if action != "*" and _tool_name(role.get("name"), action) == name:
                        return role.get("name"), action
        return None

    def _agents(self) -> List[Dict[str, Any]]:
        if self.hub is not None:
            return [r.as_dict() for r in self.hub.registry.list()]
        return self.client.agents().get("agents", [])

    def _submit(self, capability: str, action: str, args: Dict[str, Any],
                timeout_s: float) -> Dict[str, Any]:
        if self.hub is not None:
            return self.hub.submit(capability=capability, action=action, params=args,
                                   timeout_s=timeout_s, wait=True)
        return self.client.submit(capability, action, args, timeout_s=timeout_s, wait=True)


class MCPClient:
    """最小 MCP 客户端：启动 stdio server，initialize / tools/list / tools/call。"""

    def __init__(self, command: Union[str, Sequence[str]], env: Optional[Dict[str, str]] = None,
                 cwd: Optional[str] = None, timeout: float = 30):
        self.command = shlex.split(command) if isinstance(command, str) else list(command)
        self.env = env
        self.cwd = cwd
        self.timeout = timeout
        self.proc: Optional[subprocess.Popen] = None
        self._id = 0

    def start(self) -> Dict[str, Any]:
        self.proc = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, env=self.env, cwd=self.cwd)
        info = self.request("initialize", {
            "protocolVersion": MCP_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "multi-agent", "version": "1.0.0"},
        })
        self.notify("notifications/initialized")
        return info

    def request(self, method: str, params: Optional[Dict[str, Any]] = None,
                timeout: Optional[float] = None) -> Any:
        if not self.proc or not self.proc.stdin or not self.proc.stdout:
            raise RuntimeError("MCP server 未启动")
        self._id += 1
        mid = self._id
        payload = {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            ready, _, _ = select.select([self.proc.stdout], [], [], max(0.0, deadline - time.time()))
            if not ready:
                break
            line = self.proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") != mid:
                continue  # 通知/别的响应
            if msg.get("error"):
                raise RuntimeError(f"MCP {method} 失败：{msg['error']}")
            return msg.get("result")
        raise TimeoutError(f"MCP {method} 超时")

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("MCP server 未启动")
        self.proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": params or {}},
            ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def list_tools(self) -> List[Dict[str, Any]]:
        return (self.request("tools/list") or {}).get("tools", [])

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("tools/call", {"name": name, "arguments": arguments or {}},
                            timeout=120)

    def close(self) -> None:
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None


class MCPAgent:
    """把一个外部 MCP Server 的工具注册成 Hub 能力（capability 默认 `mcp`）。"""

    def __init__(self, hub_url: str, agent_id: str, command: Union[str, Sequence[str]],
                 token: Optional[str] = None, prefix: str = "mcp",
                 concurrency: int = 2):
        self.hub_url = hub_url
        self.agent_id = agent_id
        self.command = command
        self.token = token
        self.prefix = prefix
        self.concurrency = concurrency
        self.client = MCPClient(command)
        self.runtime: Optional[AgentRuntime] = None

    def start(self) -> Dict[str, Any]:
        info = self.client.start()
        tools = self.client.list_tools()
        # 用简化后的动作名，保证协议里的 action 干净
        mapping = {}
        for tool in tools:
            action = _action_name(tool["name"])
            mapping[action] = tool["name"]
        self.runtime = AgentRuntime(
            self.hub_url, self.agent_id,
            [{"name": self.prefix, "actions": list(mapping)}],
            token=self.token, concurrency=self.concurrency)
        for action, tool_name in mapping.items():
            self.runtime.add_handler(self.prefix, action,
                                     self._make_handler(tool_name))
        reg = self.runtime.start()
        return {"mcp": info, "tools": list(mapping), "registered": reg}

    def _make_handler(self, tool_name: str):
        def _handler(params: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
            return self.client.call_tool(tool_name, params)
        return _handler

    def stop(self) -> None:
        if self.runtime:
            self.runtime.stop()
        self.client.close()


def _action_name(tool_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", tool_name)


def _ok(mid: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}