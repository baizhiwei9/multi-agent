"""JSON-RPC 2.0 服务端分发：把 Hub 能力映射成扁平的 method。

方法表
------
hub.register / hub.heartbeat / hub.agents / hub.inbox
hub.submit / hub.task / hub.tasks / hub.cancel / hub.result
db.<action>            （内部转成 capability=db 的任务）
coordinator.<action>   （内部转成 capability=coordinator 的任务）

比 REST 少一层 URL 语义，适合脚本/Agent 直调；payload 仍是 JSON，无 LLM 参与。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from . import protocol


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class RpcMethods:
    def __init__(self, hub):
        self.hub = hub

    def dispatch(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        p = params or {}
        try:
            if method == "hub.register":
                rec = self.hub.registry.register(p)
                return rec.as_dict()
            if method == "hub.heartbeat":
                ok = self.hub.registry.heartbeat(p["agent_id"])
                if not ok:
                    raise RpcError(-32004, "agent 未注册")
                return {"ok": True}
            if method == "hub.agents":
                return {"agents": [r.as_dict() for r in self.hub.registry.list()],
                        "stats": self.hub.stats()}
            if method == "hub.inbox":
                return self.hub.next_message(p["agent_id"], wait_s=float(p.get("wait", 20)))
            if method == "hub.submit":
                return self.hub.submit(
                    capability=p["capability"], action=p["action"],
                    params=p.get("params") or {},
                    timeout_s=float(p.get("timeout_s") or 60),
                    wait=bool(p.get("wait") or False),
                    max_retries=int(p.get("max_retries") or 0),
                    idempotency_key=p.get("idempotency_key"),
                    labels=p.get("labels") or {},
                )
            if method == "hub.trace":
                return {"task_id": p["task_id"],
                        "events": self.hub.store.events(p["task_id"])}
            if method == "hub.task":
                task = self.hub.store.get(p["task_id"])
                if task is None:
                    raise RpcError(-32004, "任务不存在")
                return task
            if method == "hub.tasks":
                return {"tasks": self.hub.store.list(limit=int(p.get("limit") or 50),
                                                     state=p.get("state"))}
            if method == "hub.cancel":
                return self.hub.cancel(p["task_id"])
            if method == "hub.result":
                return self.hub.complete(
                    p["task_id"], p["agent_id"], bool(p["ok"]),
                    result=p.get("result"), error=p.get("error"),
                    duration_ms=p.get("duration_ms"))
            if method.startswith("db.") or method.startswith("coordinator."):
                capability, action = method.split(".", 1)
                payload = {k: v for k, v in p.items() if k != "timeout_s"}
                return self.hub.submit(capability=capability, action=action,
                                       params=payload, wait=True,
                                       timeout_s=float(p.get("timeout_s") or 60))
            raise RpcError(-32601, f"未知方法：{method}")
        except RpcError:
            raise
        except protocol.ProtocolError as e:
            raise RpcError(-32602, str(e)) from None
        except KeyError as e:
            raise RpcError(-32602, f"缺少参数：{e}") from None