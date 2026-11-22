"""NAT 打洞：公网 rendezvous + UDP hole punching + 中继回退。

为什么需要
----------
Agent 都在内网，彼此不知道对方的公网 IP:port。做法：

1. 公网协调者（Rendezvous）开一个 UDP 端口；
2. 每个 Agent 用**同一个 UDP socket** 向协调者发 `hello`，协调者从收到的包
   里看到 NAT 映射后的公网 `ip:port`（STUN 同款原理）；
3. A 想连 B：向协调者发 `punch`，协调者把 A 的公网地址转给 B、把 B 的转给 A；
4. 双方从同一个 socket 向对方的公网地址反复发 `PUNCH`，NAT 放行回包后即打通；
5. 打不通（对称 NAT / 严格防火墙）→ 自动走协调者中继（`relay`）。

关键点：**hello / punch / 业务数据必须复用同一个 socket**，否则 NAT 映射端口不同，
打洞必失败。
"""

from __future__ import annotations

import base64
import json
import socket
import threading
import time
import uuid
from collections import deque
from typing import Any, Dict, Optional, Tuple

DEFAULT_PORT = 8791
TTL_S = 180.0

PUNCH = b"PUNCH "
PONG = b"PONG "
DATA = b"D:"


# --------------------------------------------------------------------------
# 公网协调者
# --------------------------------------------------------------------------
class RendezvousServer:
    def __init__(self, host: str = "0.0.0.0", port: int = DEFAULT_PORT,
                 token: Optional[str] = None, ttl: float = TTL_S):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self.host, self.port = self._sock.getsockname()[:2]
        self.token = token or None
        self.ttl = ttl
        self._peers: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def address(self) -> str:
        return f"udp://{self.host}:{self.port}"

    def serve_forever(self) -> None:
        self._sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                raw, addr = self._sock.recvfrom(65535)
            except socket.timeout:
                self._expire()
                continue
            except OSError:
                break
            try:
                msg = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            resp = self.handle(msg, addr)
            if resp is not None:
                self._send(addr, resp)

    def start_background(self) -> threading.Thread:
        self._thread = threading.Thread(target=self.serve_forever,
                                        name="rendezvous", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass

    # ---------- 消息 ----------
    def handle(self, msg: Dict[str, Any], addr: Tuple[str, int]) -> Optional[Dict[str, Any]]:
        if self.token and msg.get("token") != self.token:
            return {"type": "error", "code": "unauthorized", "message": "token 不匹配"}
        mtype = msg.get("type")
        agent_id = str(msg.get("agent_id") or msg.get("from") or "")
        if mtype == "hello":
            if not agent_id:
                return {"type": "error", "code": "bad_request", "message": "缺少 agent_id"}
            with self._lock:
                self._peers[agent_id] = {"addr": addr, "last": time.time()}
                peers = {k: {"addr": list(v["addr"]), "age_s": round(time.time() - v["last"], 1)}
                         for k, v in self._peers.items() if k != agent_id}
            return {"type": "hello_ok", "agent_id": agent_id,
                    "observed": list(addr), "peers": peers}

        if mtype == "punch":
            target = str(msg.get("to") or "")
            with self._lock:
                peer = self._peers.get(target)
                self._peers.setdefault(agent_id, {"addr": addr, "last": time.time()})
            if not peer:
                return {"type": "error", "code": "peer_offline",
                        "message": f"{target or '?'} 还没 hello 到协调者"}
            session = str(msg.get("session") or uuid.uuid4().hex[:12])
            self._send(peer["addr"], {"type": "punch", "from": agent_id,
                                      "peer": list(addr), "session": session})
            return {"type": "punch_ok", "peer": list(peer["addr"]), "session": session,
                    "peer_agent": target}

        if mtype == "relay":
            target = str(msg.get("to") or "")
            with self._lock:
                peer = self._peers.get(target)
            if not peer:
                return {"type": "error", "code": "peer_offline", "message": "中继目标不在线"}
            self._send(peer["addr"], {"type": "relay", "from": agent_id,
                                      "payload": msg.get("payload")})
            return {"type": "relay_ok", "to": target}

        if mtype in ("ping", "keepalive"):
            with self._lock:
                if agent_id:
                    self._peers[agent_id] = {"addr": addr, "last": time.time()}
            return {"type": "pong", "observed": list(addr)}

        return {"type": "error", "code": "unknown_type", "message": str(mtype)}

    def peers(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            return {k: {"addr": list(v["addr"]), "age_s": round(now - v["last"], 1)}
                    for k, v in self._peers.items()}

    def _expire(self) -> None:
        now = time.time()
        with self._lock:
            dead = [k for k, v in self._peers.items() if now - v["last"] > self.ttl]
            for k in dead:
                self._peers.pop(k, None)

    def _send(self, addr: Tuple[str, int], payload: Dict[str, Any]) -> None:
        try:
            self._sock.sendto(json.dumps(payload, ensure_ascii=False).encode("utf-8"), addr)
        except OSError:
            pass


# --------------------------------------------------------------------------
# 打洞客户端
# --------------------------------------------------------------------------
class HolePuncher:
    """一个 Agent 一个实例；hello / punch / 业务数据共用同一个 UDP socket。"""

    def __init__(self, agent_id: str, rendezvous: Tuple[str, int],
                 token: Optional[str] = None, bind: Tuple[str, int] = ("0.0.0.0", 0),
                 force_relay: bool = False, timeout: float = 8.0):
        self.agent_id = agent_id
        self.rendezvous = tuple(rendezvous)
        self.token = token
        self.force_relay = force_relay
        self.timeout = timeout

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(bind)
        self.sock.settimeout(0.2)
        self.observed: Optional[Tuple[str, int]] = None
        self.peers: Dict[str, Any] = {}
        self.peer: Optional[Tuple[str, int]] = None
        self.peer_id: Optional[str] = None
        self.session: Optional[str] = None
        self.mode: Optional[str] = None

        self._connected = threading.Event()
        self._queue: deque = deque()
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._punch_until = 0.0

    # ---------- 启动 ----------
    def start(self) -> Dict[str, Any]:
        self._send_rv({"type": "hello", "agent_id": self.agent_id})
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                raw, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            if addr != self.rendezvous:
                continue
            msg = json.loads(raw.decode("utf-8"))
            if msg.get("type") == "hello_ok":
                self.observed = tuple(msg["observed"])
                self.peers = msg.get("peers") or {}
                self._thread = threading.Thread(target=self._loop, name=f"nat-{self.agent_id}",
                                                daemon=True)
                self._thread.start()
                return {"observed": self.observed, "peers": self.peers}
            if msg.get("type") == "error":
                raise RuntimeError(f"rendezvous 拒绝：{msg}")
        raise TimeoutError("rendezvous hello 超时")

    # ---------- 连接 ----------
    def connect(self, peer_agent: str, timeout: Optional[float] = None) -> Dict[str, Any]:
        self.peer_id = peer_agent
        if self.force_relay:
            self.mode = "relay"
            return {"mode": "relay", "peer_agent": peer_agent, "observed": self.observed}

        session = uuid.uuid4().hex[:12]
        self.session = session
        self._send_rv({"type": "punch", "to": peer_agent, "session": session})
        if self._connected.wait(timeout or self.timeout):
            self.mode = "direct"
            return {"mode": "direct", "peer": list(self.peer or ()),
                    "peer_agent": peer_agent, "observed": self.observed}
        self.mode = "relay"
        return {"mode": "relay", "peer_agent": peer_agent, "observed": self.observed,
                "reason": "打洞超时，回退中继"}

    # ---------- 收发 ----------
    def send(self, payload: bytes) -> None:
        if self.mode == "direct" and self.peer:
            self.sock.sendto(DATA + payload, self.peer)
            return
        if not self.peer_id:
            raise RuntimeError("既没有直连 peer，也没有 peer_id，无法发送")
        self._send_rv({"type": "relay", "to": self.peer_id,
                       "payload": base64.b64encode(payload).decode("ascii")})

    def recv(self, timeout: float = 5.0) -> Optional[bytes]:
        deadline = time.time() + timeout
        with self._cv:
            while not self._queue:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cv.wait(remaining)
            return self._queue.popleft()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        try:
            self.sock.close()
        except OSError:
            pass

    # ---------- 内部 ----------
    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.peer and time.time() < self._punch_until:
                self._send_peer(PUNCH + (self.session or "").encode())
            try:
                raw, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            if addr == self.rendezvous:
                self._on_rendezvous(raw)
                continue
            if raw.startswith(PUNCH):
                self._mark_direct(addr)
                self._send_peer(PONG + (self.session or "").encode())
            elif raw.startswith(PONG):
                self._mark_direct(addr)
            elif raw.startswith(DATA):
                with self._cv:
                    self._queue.append(raw[2:])
                    self._cv.notify_all()

    def _on_rendezvous(self, raw: bytes) -> None:
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        mtype = msg.get("type")
        if mtype in ("punch", "punch_ok"):
            self.peer = tuple(msg["peer"])
            self.session = msg.get("session") or self.session
            if msg.get("from"):
                self.peer_id = msg["from"]
            self._punch_until = time.time() + 5.0
            self._send_peer(PUNCH + (self.session or "").encode())
            if mtype == "punch_ok":
                self.mode = "direct"
        elif mtype == "relay":
            if msg.get("from"):
                self.peer_id = msg["from"]
            try:
                payload = base64.b64decode(msg.get("payload") or "")
            except (ValueError, TypeError):
                return
            with self._cv:
                self._queue.append(payload)
                self._cv.notify_all()

    def _mark_direct(self, addr: Tuple[str, int]) -> None:
        if self.peer is None:
            self.peer = addr
        if addr == self.peer:
            self.mode = "direct"
            self._connected.set()

    def _send_peer(self, payload: bytes) -> None:
        if not self.peer:
            return
        try:
            self.sock.sendto(payload, self.peer)
        except OSError:
            pass

    def _send_rv(self, msg: Dict[str, Any]) -> None:
        if self.token:
            msg["token"] = self.token
        msg.setdefault("agent_id", self.agent_id)
        self.sock.sendto(json.dumps(msg, ensure_ascii=False).encode("utf-8"),
                         self.rendezvous)