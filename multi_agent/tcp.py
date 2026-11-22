"""TCP 传输：newline-delimited JSON-RPC 2.0。

为什么还要 TCP
-------------
- 内网/同机房/容器网络里，TCP 比 HTTP 少一层解析，长连接更省；
- 端口固定、协议简单，适合"注册完地址后直连"的场景；
- 仍然是 JSON 文本，人可读、可抓包，不引入二进制依赖。

协议
----
每条消息一行 JSON（`\\n` 结尾），字段与 `/rpc` 完全一致；
连接建立后第一件事是 `auth`（服务端启用 token 时）：

    {"jsonrpc":"2.0","id":1,"method":"auth","params":{"token":"secret"}}

之后直接发 `hub.*` / `db.*` / `coordinator.*` 方法。
"""

from __future__ import annotations

import json
import socket
import threading
from typing import Any, Dict, Optional

from . import protocol
from .client import HubError
from .rpc import RpcError, RpcMethods
from .transports.base import Transport

MAX_LINE = 4 * 1024 * 1024


class TCPHubServer:
    def __init__(self, hub, host: str = "127.0.0.1", port: int = 8790,
                 token: Optional[str] = None, rpc: Optional[RpcMethods] = None):
        self.hub = hub
        self.rpc = rpc or RpcMethods(hub)
        self.token = token or None
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(64)
        self.host, self.port = self._sock.getsockname()[:2]
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def address(self) -> str:
        return f"tcp://{self.host}:{self.port}"

    def serve_forever(self) -> None:
        self._sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def start_background(self) -> threading.Thread:
        self._thread = threading.Thread(target=self.serve_forever, name="hub-tcp", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass

    # ---------- 单连接 ----------
    def _client(self, conn: socket.socket) -> None:
        conn.settimeout(300)
        stream = conn.makefile("rwb")
        authed = not self.token
        try:
            for raw in stream:
                if len(raw) > MAX_LINE:
                    break
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    self._write(stream, _err(None, -32700, "parse error"))
                    continue
                method = msg.get("method")
                mid = msg.get("id")
                params = msg.get("params") or {}
                if method == "auth":
                    if self.token and params.get("token") != self.token:
                        self._write(stream, _err(mid, -32001, "unauthorized"))
                        continue
                    authed = True
                    self._write(stream, _ok(mid, {"ok": True, "protocol": "tcp"}))
                    continue
                if not authed:
                    self._write(stream, _err(mid, -32001, "unauthorized"))
                    continue
                try:
                    result = self.rpc.dispatch(method, params)
                except RpcError as e:
                    self._write(stream, _err(mid, e.code, e.message))
                    continue
                except protocol.ProtocolError as e:
                    self._write(stream, _err(mid, -32602, str(e)))
                    continue
                except Exception as e:  # noqa: BLE001
                    self._write(stream, _err(mid, -32000, f"{type(e).__name__}: {e}"))
                    continue
                self._write(stream, _ok(mid, result))
        finally:
            try:
                stream.close()
            finally:
                conn.close()

    @staticmethod
    def _write(stream, payload: Dict[str, Any]) -> None:
        stream.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        stream.flush()


class TCPClient:
    def __init__(self, host: str, port: int, token: Optional[str] = None,
                 timeout: float = 30, connect_timeout: float = 5):
        self.host, self.port = host, port
        self.token = token
        self.timeout = timeout
        self._sock = socket.create_connection((host, port), timeout=connect_timeout)
        self._sock.settimeout(timeout)
        self._stream = self._sock.makefile("rwb")
        self._id = 0
        if token:
            self.call("auth", {"token": token})

    @classmethod
    def from_url(cls, url: str, token: Optional[str] = None) -> "TCPClient":
        rest = url[len("tcp://"):] if url.startswith("tcp://") else url
        host, _, port = rest.partition(":")
        return cls(host, int(port or 8790), token=token)

    def call(self, method: str, params: Optional[Dict[str, Any]] = None,
             timeout: Optional[float] = None) -> Any:
        self._id += 1
        mid = self._id
        self._sock.settimeout(timeout or self.timeout)
        payload = {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}
        self._stream.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        self._stream.flush()
        while True:
            raw = self._stream.readline()
            if not raw:
                raise HubError("TCP 连接已断开")
            msg = json.loads(raw.decode("utf-8"))
            if msg.get("id") != mid:
                continue
            if msg.get("error"):
                err = msg["error"]
                raise HubError(f"{method} -> {err.get('code')}: {err.get('message')}",
                               code=err.get("code"))
            return msg.get("result")

    def close(self) -> None:
        try:
            self._stream.close()
        finally:
            self._sock.close()


class TcpTransport(Transport):
    name = "tcp"

    def __init__(self, host: str, port: int, token: Optional[str] = None):
        self.client = TCPClient(host, port, token=token)

    @classmethod
    def from_url(cls, url: str, token: Optional[str] = None) -> "TcpTransport":
        rest = url[len("tcp://"):] if url.startswith("tcp://") else url
        host, _, port = rest.partition(":")
        return cls(host, int(port or 8790), token=token)

    def call(self, method: str, params: Optional[Dict[str, Any]] = None,
             timeout: Optional[float] = None) -> Any:
        return self.client.call(method, params, timeout=timeout)

    def close(self) -> None:
        self.client.close()


def _ok(mid: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}