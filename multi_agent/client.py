"""Agent 侧 SDK：注册能力清单、心跳、长轮询取任务、本地执行、回传结果。

关键点：Agent 只发起**出站** HTTP 请求（register / heartbeat / inbox / result），
不需要公网入口，所以内网、家里、K8s Pod 都能接进来。
"""

from __future__ import annotations

import json
import socket
import threading
import time
import traceback
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from . import protocol

Handler = Callable[[Dict[str, Any], Dict[str, Any]], Any]


class HubError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, code: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.code = code


class HubClient:
    def __init__(self, base_url: str, token: Optional[str] = None, timeout: float = 10):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # ---------- HTTP ----------
    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
                 timeout: Optional[float] = None) -> Any:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json; charset=utf-8")
        if self.token:
            req.add_header("X-Hub-Token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")
            code = None
            try:
                code = (json.loads(detail).get("error") or {}).get("code")
            except ValueError:
                pass
            raise HubError(f"{method} {path} -> {e.code}: {detail[:300]}", e.code, code) from None
        except urllib.error.URLError as e:
            raise HubError(f"{method} {path} 连接失败：{e.reason}") from None
        except socket.timeout:
            raise HubError(f"{method} {path} 超时") from None

    # ---------- API ----------
    def register(self, agent_id: str, roles: List[Dict[str, Any]],
                 endpoint: str = "", labels: Optional[Dict[str, Any]] = None,
                 ttl_s: float = 60) -> Dict[str, Any]:
        msg = protocol.envelope("register", agent_id=agent_id, roles=roles,
                                endpoint=endpoint, labels=labels or {}, ttl_s=ttl_s)
        return self._request("POST", "/v1/register", msg)

    def heartbeat(self, agent_id: str) -> Dict[str, Any]:
        return self._request("POST", "/v1/heartbeat",
                             protocol.envelope("heartbeat", agent_id=agent_id))

    def inbox(self, agent_id: str, wait: float = 20) -> Optional[Dict[str, Any]]:
        path = f"/v1/agents/{agent_id}/inbox?wait={max(0.0, min(wait, 30.0))}"
        return self._request("GET", path, timeout=(wait + 10))

    def submit(self, capability: str, action: str, params: Optional[Dict[str, Any]] = None,
               timeout_s: float = 60, wait: bool = False, max_retries: int = 0,
               idempotency_key: Optional[str] = None,
               labels: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._request("POST", "/v1/tasks", {
            "capability": capability, "action": action, "params": params or {},
            "timeout_s": timeout_s, "wait": wait, "max_retries": max_retries,
            "idempotency_key": idempotency_key, "labels": labels or {},
        }, timeout=timeout_s + 20 if wait else None)

    def task(self, task_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/v1/tasks/{task_id}")

    def tasks(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self._request("GET", f"/v1/tasks?limit={limit}").get("tasks", [])

    def agents(self) -> Dict[str, Any]:
        return self._request("GET", "/v1/agents")

    def result(self, task_id: str, agent_id: str, ok: bool,
               result: Any = None, error: Optional[Dict[str, Any]] = None,
               duration_ms: Optional[int] = None) -> Dict[str, Any]:
        return self._request("POST", f"/v1/tasks/{task_id}/result",
                             protocol.result_message(task_id, agent_id, ok, result, error, duration_ms))

    def cancel(self, task_id: str) -> Dict[str, Any]:
        return self._request("POST", f"/v1/tasks/{task_id}/cancel",
                             protocol.envelope("cancel", task_id=task_id))

    def deregister(self, agent_id: str) -> Dict[str, Any]:
        return self._request("DELETE", f"/v1/agents/{agent_id}")


class AgentRuntime:
    """一个 Agent 进程：可绑多个角色，每个角色多个动作。

    用法::

        rt = AgentRuntime("http://hub:8787", "agent-a",
                          roles=[{"name": "A", "actions": ["build_sheet"]}])
        rt.add_handler("A", "build_sheet", lambda params, ctx: {...})
        rt.start()
        ...
        rt.stop()
    """

    def __init__(self, hub_url: str, agent_id: str, roles: List[Dict[str, Any]],
                 token: Optional[str] = None, concurrency: int = 2, poll_wait: float = 20,
                 ttl_s: float = 60, heartbeat_interval: Optional[float] = None,
                 labels: Optional[Dict[str, Any]] = None, endpoint: str = ""):
        self.client = HubClient(hub_url, token=token)
        self.agent_id = agent_id
        self.roles = [protocol.validate_role(r) for r in roles]
        self.concurrency = max(1, int(concurrency))
        self.poll_wait = max(1.0, min(float(poll_wait), 30.0))
        self.ttl_s = max(5.0, min(float(ttl_s), 600.0))
        self.heartbeat_interval = heartbeat_interval or max(5.0, self.ttl_s / 3)
        self.labels = labels or {}
        self.endpoint = endpoint
        self._handlers: Dict[tuple[str, str], Handler] = {}
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._started = False
        self.last_error: Optional[str] = None

    # ---------- 注册动作 ----------
    def add_handler(self, capability: str, action: str, fn: Handler) -> "AgentRuntime":
        self._handlers[(capability, action)] = fn
        return self

    def handlers(self) -> Dict[tuple[str, str], Handler]:
        return dict(self._handlers)

    def manifest(self) -> List[Dict[str, Any]]:
        """只登记有 handler 的动作，避免 Hub 把活派给空壳。"""
        out = []
        for role in self.roles:
            actions = [a for a in role["actions"] if (role["name"], a) in self._handlers]
            if actions:
                out.append({"name": role["name"], "actions": actions,
                            "description": role.get("description", "")})
        return out

    # ---------- 生命周期 ----------
    def start(self) -> Dict[str, Any]:
        if self._started:
            return {"ok": True, "already": True}
        manifest = self.manifest()
        if not manifest:
            raise RuntimeError("没有注册任何 handler：角色动作表为空")
        resp = self._register_with_retry(manifest)
        self._started = True
        self._threads.append(threading.Thread(target=self._heartbeat_loop,
                                              name=f"hb-{self.agent_id}", daemon=True))
        for i in range(self.concurrency):
            self._threads.append(threading.Thread(target=self._worker,
                                                  args=(i,), name=f"w{i}-{self.agent_id}",
                                                  daemon=True))
        for t in self._threads:
            t.start()
        return resp

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2)
        try:
            self.client.deregister(self.agent_id)
        except HubError:
            pass
        self._started = False

    def _register_with_retry(self, manifest: List[Dict[str, Any]], attempts: int = 5) -> Dict[str, Any]:
        last = None
        for i in range(attempts):
            try:
                return self.client.register(self.agent_id, manifest,
                                            endpoint=self.endpoint, labels=self.labels,
                                            ttl_s=self.ttl_s)
            except HubError as e:
                last = e
                time.sleep(min(5, 0.5 * (2 ** i)))
        raise HubError(f"注册失败（{attempts} 次）：{last}")

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval):
            try:
                self.client.heartbeat(self.agent_id)
            except HubError as e:
                self.last_error = str(e)
                if e.status == 404:
                    try:
                        self._register_with_retry(self.manifest())
                    except HubError as e2:
                        self.last_error = str(e2)
                # 网络抖动就等下一拍，不退出

    # ---------- 执行 ----------
    def _worker(self, index: int) -> None:
        while not self._stop.is_set():
            try:
                msg = self.client.inbox(self.agent_id, wait=self.poll_wait)
            except HubError as e:
                self.last_error = str(e)
                self._stop.wait(2)
                continue
            if not msg:
                continue
            self._handle(msg)

    def _handle(self, msg: Dict[str, Any]) -> None:
        task_id = msg.get("task_id", "")
        capability = msg.get("capability", "")
        action = msg.get("action", "")
        params = msg.get("params") or {}
        started = time.time()
        fn = self._handlers.get((capability, action))
        if fn is None:
            self._post_result(task_id, False, error={
                "code": "handler_error",
                "message": f"未注册动作 {capability}/{action}",
            }, duration_ms=0)
            return
        try:
            value = fn(params, {"agent_id": self.agent_id, "task_id": task_id,
                                "capability": capability, "action": action})
            self._post_result(task_id, True, result=value,
                              duration_ms=int((time.time() - started) * 1000))
        except Exception as e:  # noqa: BLE001
            tb = traceback.format_exc(limit=3)
            self.last_error = f"{type(e).__name__}: {e}"
            self._post_result(task_id, False, error={
                "code": "handler_error",
                "message": f"{type(e).__name__}: {e}",
                "detail": tb[-800:],
            }, duration_ms=int((time.time() - started) * 1000))

    def _post_result(self, task_id: str, ok: bool, result: Any = None,
                     error: Optional[Dict[str, Any]] = None,
                     duration_ms: Optional[int] = None) -> None:
        for i in range(3):
            try:
                self.client.result(task_id, self.agent_id, ok, result, error, duration_ms)
                return
            except HubError as e:
                self.last_error = str(e)
                time.sleep(0.5 * (i + 1))