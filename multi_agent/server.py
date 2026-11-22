"""HTTP/JSON Server：把 Hub 暴露成跨网络可调用的接口。

为什么是 HTTP
------------
- 跨机器、跨 NAT、K8s Pod、云主机都能走，反代/网关/TLS 生态成熟；
- Agent 只需要**出站**长轮询，不需要被反向连接，家里/公司内网都能用；
- 私有二进制协议只有在同机房、且自己控 MTU/防火墙时才划算，这里不做。

认证：所有 `/v1/*` 带 `X-Hub-Token`（或 `Authorization: Bearer`）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from . import protocol
from .hub import Hub
from .console import CONSOLE_HTML
from .mcp import MCPServer
from .rpc import RpcError, RpcMethods

MAX_BODY = 2 * 1024 * 1024


class HubServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 8787,
                 db_path: str = "hub.db", token: Optional[str] = None,
                 ttl_s: float = 90, registry_json: Optional[str] = None):
        self.hub = Hub(db_path=db_path, ttl_s=ttl_s, registry_json=registry_json)
        self.recovered = self.hub.recover()
        self.token = token or None
        self.rpc = RpcMethods(self.hub)
        self.mcp = MCPServer(hub=self.hub)

        handler = _make_handler(self.hub, self.token, self.rpc, self.mcp)
        self._httpd = ThreadingHTTPServer((host, port), handler)
        self._httpd.daemon_threads = True
        self.host = host
        self.port = self._httpd.server_address[1]
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{host}:{self.port}"

    def serve_forever(self) -> None:
        self._httpd.serve_forever()

    def start_background(self) -> threading.Thread:
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="hub-http", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self.hub.store.close()


def _make_handler(hub: Hub, token: Optional[str], rpc: RpcMethods, mcp: MCPServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MultiAgentHub/1.0"
        protocol_version = "HTTP/1.1"

        # ---------- 基础 ----------
        def log_message(self, fmt: str, *args: Any) -> None:  # 静默访问日志
            return

        def _authorized(self) -> bool:
            if not token:
                return True
            supplied = self.headers.get("X-Hub-Token")
            if not supplied:
                auth = self.headers.get("Authorization", "")
                if auth.startswith("Bearer "):
                    supplied = auth[7:]
            return bool(supplied) and _const_eq(supplied, token)

        def _send(self, status: int, payload: Any = None) -> None:
            body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            if payload is not None:
                self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _error(self, status: int, code: str, message: str) -> None:
            self._send(status, {"ok": False, "error": {"code": code, "message": message}})

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                raise protocol.ProtocolError("请求体为空")
            if length > MAX_BODY:
                raise protocol.ProtocolError("请求体过大")
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as e:
                raise protocol.ProtocolError(f"JSON 解析失败：{e}") from None

        # ---------- 路由 ----------
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)

            if path == "/healthz":
                self._send(200, {"ok": True, "version": protocol.VERSION, "ts": protocol.now_iso()})
                return
            if path == "/":
                body = CONSOLE_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if not self._authorized():
                self._error(401, "unauthorized", "token 缺失或不匹配")
                return
            try:
                if path == "/v1/agents":
                    self._send(200, {"agents": [r.as_dict() for r in hub.registry.list()],
                                     "stats": hub.stats()})
                elif path == "/v1/tasks":
                    limit = int(query.get("limit", ["50"])[0])
                    state = query.get("state", [None])[0]
                    self._send(200, {"tasks": hub.store.list(limit=limit, state=state)})
                elif path.startswith("/v1/agents/") and path.endswith("/inbox"):
                    agent_id = path.split("/")[3]
                    wait_s = min(float(query.get("wait", ["20"])[0]), 30.0)
                    msg = hub.next_message(agent_id, wait_s=wait_s)
                    if msg is None:
                        self._send(204, None)
                    else:
                        self._send(200, msg)
                elif path.startswith("/v1/agents/"):
                    agent_id = path.split("/")[3]
                    rec = hub.registry.get(agent_id)
                    if rec is None:
                        self._error(404, "not_found", f"agent {agent_id} 未注册")
                    else:
                        self._send(200, rec.as_dict())
                elif path.startswith("/v1/tasks/") and path.endswith("/trace"):
                    task_id = path.split("/")[3]
                    self._send(200, {"task_id": task_id, "events": hub.store.events(task_id)})
                elif path.startswith("/v1/tasks/"):
                    task_id = path.split("/")[3]
                    task = hub.store.get(task_id)
                    if task is None:
                        self._error(404, "not_found", f"task {task_id} 不存在")
                    else:
                        self._send(200, task)
                else:
                    self._error(404, "not_found", "未知路径")
            except protocol.ProtocolError as e:
                self._error(400, "bad_request", str(e))
            except Exception as e:  # noqa: BLE001
                self._error(500, "internal", f"{type(e).__name__}: {e}")

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")
            if not self._authorized():
                self._error(401, "unauthorized", "token 缺失或不匹配")
                return
            try:
                body = self._body()
                if path == "/rpc":
                    self._handle_rpc(body)
                    return
                if path == "/mcp":
                    self._handle_mcp(body)
                    return
                if path == "/v1/register":
                    msg = protocol.validate(body)
                    if msg["type"] != "register":
                        raise protocol.ProtocolError("该接口只接受 register 消息")
                    rec = hub.registry.register(msg)
                    self._send(200, {"ok": True, "agent_id": rec.agent_id,
                                     "ttl_s": rec.ttl_s,
                                     "heartbeat_interval_s": max(5, min(30, rec.ttl_s / 3))})
                elif path == "/v1/heartbeat":
                    msg = protocol.validate(body)
                    if msg["type"] != "heartbeat":
                        raise protocol.ProtocolError("该接口只接受 heartbeat 消息")
                    ok = hub.registry.heartbeat(msg["agent_id"])
                    if not ok:
                        self._error(404, "not_found", "agent 未注册，请重新 register")
                    else:
                        self._send(200, {"ok": True})
                elif path == "/v1/tasks":
                    task = self._submit(body)
                    self._send(200, task)
                elif path.startswith("/v1/tasks/") and path.endswith("/result"):
                    task_id = path.split("/")[3]
                    msg = protocol.validate(body)
                    if msg["type"] != "result":
                        raise protocol.ProtocolError("该接口只接受 result 消息")
                    if msg["task_id"] != task_id:
                        raise protocol.ProtocolError("路径 task_id 与消息不一致")
                    task = hub.complete(task_id, msg["agent_id"], msg["ok"],
                                        result=msg.get("result"), error=msg.get("error"),
                                        duration_ms=msg.get("duration_ms"))
                    self._send(200, task)
                elif path.startswith("/v1/tasks/") and path.endswith("/cancel"):
                    task_id = path.split("/")[3]
                    self._send(200, hub.cancel(task_id))
                else:
                    self._error(404, "not_found", "未知路径")
            except protocol.ProtocolError as e:
                self._error(400, "bad_request", str(e))
            except KeyError as e:
                self._error(404, "not_found", f"任务不存在：{e}")
            except PermissionError as e:
                self._error(403, "forbidden", str(e))
            except Exception as e:  # noqa: BLE001
                self._error(500, "internal", f"{type(e).__name__}: {e}")

        def do_DELETE(self) -> None:
            if not self._authorized():
                self._error(401, "unauthorized", "token 缺失或不匹配")
                return
            path = urlparse(self.path).path.rstrip("/")
            if path.startswith("/v1/agents/"):
                agent_id = path.split("/")[3]
                self._send(200, {"ok": hub.registry.deregister(agent_id)})
            else:
                self._error(404, "not_found", "未知路径")

        # ---------- 多协议入口 ----------
        def _handle_rpc(self, body: Dict[str, Any]) -> None:
            mid = body.get("id")
            method = body.get("method")
            params = body.get("params") or {}
            try:
                result = rpc.dispatch(method, params)
            except RpcError as e:
                self._send(200, {"jsonrpc": "2.0", "id": mid,
                                 "error": {"code": e.code, "message": e.message,
                                           "data": e.data}})
                return
            except protocol.ProtocolError as e:
                self._send(200, {"jsonrpc": "2.0", "id": mid,
                                 "error": {"code": -32602, "message": str(e)}})
                return
            except Exception as e:  # noqa: BLE001
                self._send(200, {"jsonrpc": "2.0", "id": mid,
                                 "error": {"code": -32000,
                                           "message": f"{type(e).__name__}: {e}"}})
                return
            self._send(200, {"jsonrpc": "2.0", "id": mid, "result": result})

        def _handle_mcp(self, body: Dict[str, Any]) -> None:
            resp = mcp.handle(body)
            if resp is None:
                self._send(202, None)  # 通知类消息：202 + 空体
            else:
                self._send(200, resp)

        # ---------- 提交任务 ----------
        def _submit(self, body: Dict[str, Any]) -> Dict[str, Any]:
            capability = str(body.get("capability") or "").strip()
            action = str(body.get("action") or "").strip()
            if not capability or not action:
                raise protocol.ProtocolError("capability / action 必填")
            params = body.get("params") or {}
            if not isinstance(params, dict):
                raise protocol.ProtocolError("params 必须是对象")
            return hub.submit(
                capability=capability,
                action=action,
                params=params,
                timeout_s=float(body.get("timeout_s") or 60),
                wait=bool(body.get("wait") or False),
                max_retries=int(body.get("max_retries") or 0),
                idempotency_key=body.get("idempotency_key"),
                labels=body.get("labels") or {},
            )

    return Handler


def _const_eq(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))