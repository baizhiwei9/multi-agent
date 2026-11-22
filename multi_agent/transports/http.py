"""简易 HTTP（REST）传输：现有的 `/v1/*` 接口。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..client import HubClient
from .base import Transport

_CONTROL_KEYS = {"timeout_s"}


class HttpTransport(Transport):
    name = "http"

    def __init__(self, base_url: str, token: Optional[str] = None):
        self.client = HubClient(base_url, token=token)

    def call(self, method: str, params: Optional[Dict[str, Any]] = None,
             timeout: Optional[float] = None) -> Any:
        p = params or {}
        c = self.client
        if method == "hub.register":
            return c.register(p["agent_id"], p["roles"], p.get("endpoint", ""),
                              p.get("labels"), p.get("ttl_s", 60))
        if method == "hub.heartbeat":
            return c.heartbeat(p["agent_id"])
        if method == "hub.agents":
            return c.agents()
        if method == "hub.inbox":
            return c.inbox(p["agent_id"], p.get("wait", 20))
        if method == "hub.submit":
            return c.submit(p["capability"], p["action"], p.get("params"),
                            p.get("timeout_s", 60), p.get("wait", False),
                            p.get("max_retries", 0), p.get("idempotency_key"),
                            p.get("labels"))
        if method == "hub.task":
            return c.task(p["task_id"])
        if method == "hub.tasks":
            return {"tasks": c.tasks(p.get("limit", 50))}
        if method == "hub.cancel":
            return c.cancel(p["task_id"])
        if method == "hub.result":
            return c.result(p["task_id"], p["agent_id"], p["ok"],
                            p.get("result"), p.get("error"))
        if method.startswith("db.") or method.startswith("coordinator."):
            capability, action = method.split(".", 1)
            payload = {k: v for k, v in p.items() if k not in _CONTROL_KEYS}
            return c.submit(capability, action, payload, wait=True,
                            timeout_s=float(p.get("timeout_s", 60)))
        raise ValueError(f"HTTP 传输不支持的方法：{method}")

    def close(self) -> None:
        return None