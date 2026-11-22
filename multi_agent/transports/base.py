"""传输层抽象：同一套调用面，底层可换 HTTP / JSON-RPC / MCP。

Agent 与协调端只依赖 `HubAPI`，不关心线缆是 REST、JSON-RPC 还是 MCP；
新增协议（私有二进制、消息队列）只要实现 `Transport.call` 即可。
"""

from __future__ import annotations

import abc
from typing import Any, Dict, List, Optional


class Transport(abc.ABC):
    name = "base"

    @abc.abstractmethod
    def call(self, method: str, params: Optional[Dict[str, Any]] = None,
             timeout: Optional[float] = None) -> Any:
        """执行一次协议调用；method 形如 `hub.submit` / `db.write`。"""

    def close(self) -> None:  # pragma: no cover - 可选
        return None


class HubAPI:
    """统一调用面：注册、提交、取结果、DB、编排都用同一组方法。"""

    def __init__(self, transport: Transport):
        self.transport = transport

    @property
    def protocol(self) -> str:
        return self.transport.name

    # ---- Agent 生命周期 ----
    def register(self, agent_id: str, roles: List[Dict[str, Any]],
                 endpoint: str = "", labels: Optional[Dict[str, Any]] = None,
                 ttl_s: float = 60) -> Any:
        return self.transport.call("hub.register", {
            "agent_id": agent_id, "roles": roles, "endpoint": endpoint,
            "labels": labels or {}, "ttl_s": ttl_s,
        })

    def heartbeat(self, agent_id: str) -> Any:
        return self.transport.call("hub.heartbeat", {"agent_id": agent_id})

    def inbox(self, agent_id: str, wait: float = 20) -> Any:
        return self.transport.call("hub.inbox", {"agent_id": agent_id, "wait": wait},
                                   timeout=wait + 10)

    def result(self, task_id: str, agent_id: str, ok: bool,
               result: Any = None, error: Any = None) -> Any:
        return self.transport.call("hub.result", {
            "task_id": task_id, "agent_id": agent_id, "ok": ok,
            "result": result, "error": error,
        })

    # ---- 任务 ----
    def submit(self, capability: str, action: str, params: Optional[Dict[str, Any]] = None,
               timeout_s: float = 60, wait: bool = False, max_retries: int = 0,
               idempotency_key: Optional[str] = None,
               labels: Optional[Dict[str, Any]] = None) -> Any:
        return self.transport.call("hub.submit", {
            "capability": capability, "action": action, "params": params or {},
            "timeout_s": timeout_s, "wait": wait, "max_retries": max_retries,
            "idempotency_key": idempotency_key, "labels": labels or {},
        }, timeout=(timeout_s + 20) if wait else None)

    def task(self, task_id: str) -> Any:
        return self.transport.call("hub.task", {"task_id": task_id})

    def tasks(self, limit: int = 50) -> Any:
        return self.transport.call("hub.tasks", {"limit": limit})

    def cancel(self, task_id: str) -> Any:
        return self.transport.call("hub.cancel", {"task_id": task_id})

    def agents(self) -> Any:
        return self.transport.call("hub.agents", {})

    # ---- DB / 编排（通过协议转成 db.* / coordinator.*）----
    def db(self, action: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.transport.call(f"db.{action}", params or {})

    def coordinator(self, action: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.transport.call(f"coordinator.{action}", params or {})