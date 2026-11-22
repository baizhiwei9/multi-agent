"""JSON-RPC 2.0 传输：`POST /rpc`，比 REST 少一层路径语义，适合程序间直调。"""

from __future__ import annotations

import itertools
import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from ..client import HubError
from .base import Transport


class JsonRpcClient:
    def __init__(self, base_url: str, token: Optional[str] = None,
                 timeout: float = 30, path: str = "/rpc"):
        self.url = base_url.rstrip("/") + path
        self.token = token
        self.timeout = timeout
        self._ids = itertools.count(1)

    def call(self, method: str, params: Optional[Dict[str, Any]] = None,
             timeout: Optional[float] = None) -> Any:
        payload = {"jsonrpc": "2.0", "id": next(self._ids),
                   "method": method, "params": params or {}}
        req = urllib.request.Request(
            self.url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json; charset=utf-8"})
        if self.token:
            req.add_header("X-Hub-Token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise HubError(f"JSON-RPC HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}",
                           e.code) from None
        except urllib.error.URLError as e:
            raise HubError(f"JSON-RPC 连接失败：{e.reason}") from None

        if body.get("error"):
            err = body["error"]
            raise HubError(f"{method} -> {err.get('code')}: {err.get('message')}",
                           code=err.get("code"))
        return body.get("result")


class JsonRpcTransport(Transport):
    name = "jsonrpc"

    def __init__(self, base_url: str, token: Optional[str] = None):
        self.client = JsonRpcClient(base_url, token=token)

    def call(self, method: str, params: Optional[Dict[str, Any]] = None,
             timeout: Optional[float] = None) -> Any:
        return self.client.call(method, params, timeout=timeout)