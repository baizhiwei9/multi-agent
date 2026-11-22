"""成本计量：一次任务传了多少帧/字节，以及执行路径里有没有大模型。

为什么单独做这个
----------------
"中继/打洞到底烧不烧 token"这类问题，靠读代码吵不出结论，靠贴一张截图也不够。
这里把它变成**可以自己跑一遍的数字**：

    python3 scripts/measure_cost.py

三个场景，同一条任务语义（一次请求 + 一次响应）：

- `http`  ：Hub REST（含 1 个 Worker 的注册后长轮询与结果回传）
- `udp`   ：打洞直连（HolePuncher 双端直连收发）
- `relay` ：打洞失败，走 rendezvous 中继转发

计量口径（写清楚，避免夸大）
----------------------------
- HTTP：按**请求体 + 响应体**的 UTF-8 字节计，不含 TCP/TLS 头、不含 HTTP 头。
- UDP ：按 `sendto` 的 datagram 计（含 `PUNCH `/`D:` 前缀），不含 IP/UDP 头（20+8 字节）。
- 只统计**任务期间**：注册、hello、punch 交换都先做完再清零计数。
- 打洞收尾可能还有一两帧控制报文，属于真实开销，不剔除。

这些数字能证明什么、不能证明什么，见 docs/COST.md。
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.request
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

# 与大模型 SDK 同名的一律视为"执行路径里混进了模型"
_FORBIDDEN = (
    "openai", "anthropic", "dashscope", "zhipuai", "google.generativeai",
    "cohere", "replicate", "ollama", "transformers", "vllm", "litellm",
)


class Counters:
    """HTTP 与 UDP 分开计数；`reset()` 之后只算任务期间。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.data: Dict[str, int] = {
            "http_calls": 0, "http_bytes_out": 0, "http_bytes_in": 0,
            "udp_frames_out": 0, "udp_bytes_out": 0,
            "udp_frames_in": 0, "udp_bytes_in": 0,
        }

    def reset(self) -> None:
        with self.lock:
            for k in self.data:
                self.data[k] = 0

    def add(self, key: str, n: int) -> None:
        with self.lock:
            self.data[key] += n

    def snapshot(self) -> Dict[str, int]:
        with self.lock:
            return dict(self.data)


class _RespProxy:
    """把 urlopen 的响应换成"已读完的字节"，顺带计数。"""

    def __init__(self, resp: Any, raw: bytes) -> None:
        self._resp = resp
        self._raw = raw

    def read(self, *a: Any, **kw: Any) -> bytes:
        return self._raw

    def __enter__(self) -> "_RespProxy":
        return self

    def __exit__(self, *exc: Any) -> bool:
        try:
            self._resp.close()
        except Exception:  # noqa: BLE001
            pass
        return False


@contextmanager
def counting():
    """在上下文内统计 HTTP 请求/响应体与 UDP datagram。"""
    st = Counters()
    real_urlopen = urllib.request.urlopen
    real_sendto = socket.socket.sendto
    real_recvfrom = socket.socket.recvfrom

    def _urlopen(req: Any, *a: Any, **kw: Any) -> Any:
        body = getattr(req, "data", None) or b""
        resp = real_urlopen(req, *a, **kw)
        raw = resp.read()
        st.add("http_calls", 1)
        st.add("http_bytes_out", len(body))
        st.add("http_bytes_in", len(raw))
        return _RespProxy(resp, raw)

    def _sendto(self: socket.socket, data: Any, *addr: Any) -> int:
        if getattr(self, "type", None) == socket.SOCK_DGRAM:
            st.add("udp_frames_out", 1)
            st.add("udp_bytes_out", len(data))
        return real_sendto(self, data, *addr)

    def _recvfrom(self: socket.socket, *a: Any) -> Any:
        data, addr = real_recvfrom(self, *a)
        if getattr(self, "type", None) == socket.SOCK_DGRAM:
            st.add("udp_frames_in", 1)
            st.add("udp_bytes_in", len(data))
        return data, addr

    urllib.request.urlopen = _urlopen  # type: ignore[assignment]
    socket.socket.sendto = _sendto      # type: ignore[assignment]
    socket.socket.recvfrom = _recvfrom  # type: ignore[assignment]
    try:
        yield st
    finally:
        urllib.request.urlopen = real_urlopen  # type: ignore[assignment]
        socket.socket.sendto = real_sendto     # type: ignore[assignment]
        socket.socket.recvfrom = real_recvfrom  # type: ignore[assignment]


# ---------------------------------------------------------------- 场景
def scenario_http(n: int = 1) -> Dict[str, Any]:
    """Hub REST：注册完成后，量一次 submit→execute→result 的往返。"""
    from .client import AgentRuntime, HubClient
    from .server import HubServer

    srv = HubServer(host="127.0.0.1", port=0, db_path=":memory:")
    srv.start_background()
    try:
        with counting() as st:
            # 从注册开始算：一次任务在 Hub 架构下的真实 HTTP 开销
            # = 注册 + 长轮询取活 + 结果回传 + 提交方的 submit
            rt = AgentRuntime(srv.url, "cost-worker",
                              [{"name": "demo", "actions": ["echo"]}], poll_wait=1.0)
            rt.add_handler("demo", "echo", lambda p, c: {"ok": True, "n": p.get("n")})
            rt.start()
            client = HubClient(srv.url)
            t0 = time.time()
            task = client.submit("demo", "echo", {"n": n}, wait=True, timeout_s=10)
            cost_ms = int((time.time() - t0) * 1000)
            stats = st.snapshot()
        assert task["state"] == "succeeded", task
    finally:
        try:
            rt.stop()
        except Exception:  # noqa: BLE001
            pass
        srv.stop()
    return {"scenario": "http", "task_ms": cost_ms, **stats}


def scenario_udp(relay: bool = False) -> Dict[str, Any]:
    """打洞收发：直连一条 1KB 消息，或强制走中继。"""
    from .nat import HolePuncher, RendezvousServer

    rv = RendezvousServer(host="127.0.0.1", port=0)
    rv.start_background()
    a = HolePuncher("cost-a", ("127.0.0.1", rv.port), force_relay=relay, timeout=5)
    b = HolePuncher("cost-b", ("127.0.0.1", rv.port), timeout=5)
    a.start()
    b.start()
    try:
        with counting() as st:
            conn = a.connect("cost-b", timeout=5)
            time.sleep(0.3)   # 让打洞收尾的控制帧过去，再清零
            st.reset()
            payload = b"x" * 1024
            a.send(payload)
            got = b.recv(5)
            stats = st.snapshot()
        assert got == payload, "收发的字节不一致"
    finally:
        a.close()
        b.close()
        rv.stop()
    return {"scenario": "relay" if relay else "udp",
            "mode": conn.get("mode"), "payload_bytes": len(payload), **stats}


def llm_import_check() -> Dict[str, Any]:
    """执行路径里有没有混进大模型 SDK（同进程已加载的模块为准）。"""
    hits: List[str] = sorted({m for m in _FORBIDDEN
                              if m in sys.modules or
                              any(k == m or k.startswith(m + ".") for k in sys.modules)})
    return {"ok": not hits, "llm_modules_loaded": hits}


def measure(skip_relay: bool = False) -> Dict[str, Any]:
    out: Dict[str, Any] = {"http": scenario_http(), "udp": scenario_udp(False)}
    if not skip_relay:
        out["relay"] = scenario_udp(True)
    out["llm"] = llm_import_check()
    out["note"] = ("HTTP 按请求体+响应体；UDP 按 datagram；"
                   "均不含 TCP/UDP/IP 协议头；只统计任务期间")
    return out


def format_table(rep: Dict[str, Any]) -> str:
    rows = ["场景      帧数 字节数  说明"]
    for key in ("http", "udp", "relay"):
        r = rep.get(key)
        if not r:
            continue
        frames = r["http_calls"] if key == "http" else r["udp_frames_out"]
        size = r["http_bytes_out"] + r["http_bytes_in"] if key == "http" else r["udp_bytes_out"]
        note = {"http": "REST 请求+响应", "udp": "打洞直连", "relay": "中继转发"}[key]
        rows.append(f"{key:<9} {frames:>4} {size:>6}  {note}")
    llm = rep.get("llm") or {}
    rows.append("")
    rows.append("执行路径加载的大模型 SDK："
                + ("无" if llm.get("ok") else f"有 {llm.get('llm_modules_loaded')}"))
    return "\n".join(rows)