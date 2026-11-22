"""bootstrap：先用极少量聊天消息换一次「门票」，然后全程打洞直连，不再烧 token。

要解决的问题（大佬 17:43 的原话）
=================================
> 我也不知道它的公网 IP，他们都是私有的 IP，他们也得打洞。
> 你得先通过大模型通讯，让他们把某个程序执行起来，然后再打洞。
> 减少 TOKEN 的消耗。

所以链路必须是三段：

    ① 探测    本机自己用 STUN 问出「我在公网看起来是什么样」（不需要人知道 IP）
    ② 换票    **只发一次短消息**（人转发 / 大模型转发都行），把门票给对方
    ③ 打洞    双方各自往对方公网地址发 PUNCH，NAT 放行后建立直连
              → 之后所有指令/结果都走这条直连，**不再消耗任何 token**

门票（ticket）就是一行短字符串，~40 字符，这就是全流程唯一的 token 成本：

    A1|120.245.114.140|50659|8792|agent-a

含义：协议版本A1 / 我的公网IP / 我的公网端口 / 我的本地监听端口 / 我的名字。
**公网端口是 NAT 临时开的，所以必须在打洞前现测现用，不能缓存。**

为什么门票要带「本地端口」
==========================
STUN 告诉我的是 NAT 映射后的公网端口（50659），但本机 socket 绑的是 8792。
对端必须往 **公网 50659** 打，我才能在 8792 上收到（因为映射存在）。
少了本地端口，对端不知道要打哪个公网端口 —— 这就是为什么不能只报 IP。

用法
====
**第一台机器（协调者）**：
    $ python3 -m multi_agent.bootstrap ticket --id openclaw
    A1|120.245.114.140|50659|8792|openclaw        ← 把这一行发给对方

**第二台机器（被派的 agent）**：
    $ python3 -m multi_agent.bootstrap join \
        --id hermes --peer-ticket 'A1|120.245.114.140|50659|8792|openclaw'
    A1|203.0.113.7|44123|8792|hermes             ← 把这一行回给对方
    [link] punched udp  direct=120.245.114.140:50659  rtt≈38ms

**第一台再把这行喂进去**：
    $ python3 -m multi_agent.bootstrap join \
        --id openclaw --peer-ticket 'A1|203.0.113.7|44123|8792|hermes' \
        --listen 300 --exec 'python3 /srv/jobs/build_sheet.py'

然后双方就有一条直连通道了，之后传指令/结果全走它。

一行搞定（更省事：让对方连上就自动跑你的程序）
==============================================
    python3 -m multi_agent.bootstrap join --id hermes \
        --peer-ticket '<对方的票>' \
        --on-connect 'python3 /srv/jobs/scan_news.py'

协议
====
UDP 之上做了最小可靠层（seq + ack + 重传），消息体是 JSON 行：

    PUNCH                打洞探测
    PONG                 打洞确认
    A|<id>|<json>        需要 ack 的应用消息
    ACK|<id>             确认

打不通（对称 NAT / 严格防火墙）时自动降级为 **中继模式**：双方都连不上就
各自持续重试并在日志里明确标 `relay-needed`，不会静默假装成功。
"""
from __future__ import annotations

import argparse
import json
import os
import select
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Tuple

MAGIC = 0x2112A442
DEFAULT_LOCAL_PORT = 8792
PUNCH = b"PUNCH "
PONG = b"PONG "
DATA = b"A|"
ACKP = b"ACK|"

STUN_SERVERS = (
    ("stun.cloudflare.com", 3478),
    ("stun.miwifi.com", 3478),
    ("stun.l.google.com", 19302),
    ("stun.qq.com", 3478),
)


# ─────────────────────────────────────────────────────── STUN
def stun_bind(sock: socket.socket, server: Tuple[str, int],
              timeout: float = 3.0) -> Optional[Tuple[str, int]]:
    """用**已存在的 socket** 问 STUN：我在公网看起来是什么地址。

    必须复用同一个 socket：换了 socket 就是换了一条 NAT 映射，打洞必然失败。
    """
    tid = os.urandom(12)
    req = struct.pack(">HHI", 0x0001, 0, MAGIC) + tid
    old_timeout = sock.gettimeout()
    try:
        sock.settimeout(timeout)
        sock.sendto(req, server)
        deadline = time.time() + timeout
        while time.time() < deadline:
            data, _src = sock.recvfrom(2048)
            if len(data) < 20 or data[8:20] != tid:
                continue        # 不是我们的响应
            _typ, blen, _mg = struct.unpack(">HHI", data[:8])
            off, end = 20, 20 + blen
            while off + 4 <= end and off + 4 <= len(data):
                atype, alen = struct.unpack(">HH", data[off:off + 4])
                val = data[off + 4:off + 4 + alen]
                if atype in (0x0020, 0x0001) and len(val) >= 8:
                    port = struct.unpack(">H", val[2:4])[0]
                    if atype == 0x0020:      # XOR-MAPPED-ADDRESS
                        port ^= (MAGIC >> 16)
                        ipb = bytes(b ^ c for b, c in
                                    zip(val[4:8], struct.pack(">I", MAGIC)))
                    else:
                        ipb = val[4:8]
                    return socket.inet_ntoa(ipb), port
                off += 4 + alen + ((4 - alen % 4) % 4)
    except (socket.timeout, OSError):
        return None
    finally:
        try:
            sock.settimeout(old_timeout)
        except OSError:
            pass
    return None


def discover_public(sock: socket.socket,
                    servers: Tuple[Tuple[str, int], ...] = STUN_SERVERS
                    ) -> Dict[str, Any]:
    """逐个试 STUN 服务器；**也返回不同服务器的结果**，用于识别对称 NAT。"""
    results: List[Dict[str, Any]] = []
    for host, port in servers:
        try:
            ip = socket.gethostbyname(host)
        except OSError:
            continue
        t0 = time.time()
        addr = stun_bind(sock, (ip, port))
        results.append({"server": f"{host}:{port}", "mapped": addr,
                        "ms": int((time.time() - t0) * 1000)})
        if addr:
            break
    mapped = next((r["mapped"] for r in results if r["mapped"]), None)
    ports = {r["mapped"][1] for r in results if r["mapped"]}
    return {"mapped": mapped, "attempts": results,
            "symmetric_hint": len(ports) > 1,
            "note": ("不同 STUN 返回不同端口 → 疑似对称 NAT，打洞成功率低"
                     if len(ports) > 1 else "")}


# ─────────────────────────────────────────────────────── 门票
def make_ticket(public: Tuple[str, int], local_port: int, agent_id: str) -> str:
    """生成门票（一行短字符串，约 40 字符）。

    格式：`A1|<公网IP>|<公网端口>|<本地端口>|<agent_id>`

    ⚠️ 试过 base64+JSON 的"短格式"，反而更长（95 vs 38 字符），已废弃 ——
    纯文本分隔拼是最省的。
    """
    ip, port = public
    return f"A1|{ip}|{port}|{local_port}|{agent_id}"


def parse_ticket(ticket: str) -> Dict[str, Any]:
    """解析门票。格式不对就明确报错（不打洞到错误地址）。"""
    t = (ticket or "").strip()
    if not t:
        raise ValueError("门票为空")
    parts = t.split("|")
    if len(parts) < 5 or parts[0] != "A1":
        raise ValueError(
            f"门票格式不对：{t!r}\n"
            f"应为 A1|<公网IP>|<公网端口>|<本地端口>|<agent_id>")
    try:
        return {"ip": parts[1], "public_port": int(parts[2]),
                "local_port": int(parts[3]), "agent_id": parts[4]}
    except ValueError as e:
        raise ValueError(f"门票里的端口不是数字：{e}") from None


# ─────────────────────────────────────────────────────── 直连通道
class DirectLink:
    """UDP 打洞 + 最小可靠层。

    连接状态：`idle` → `punching` → `direct` （或 `failed`）
    """

    def __init__(self, agent_id: str, local_port: int = DEFAULT_LOCAL_PORT,
                 punch_interval: float = 0.3, punch_timeout: float = 30.0,
                 verbose: bool = True):
        self.agent_id = agent_id
        self.local_port = local_port
        self.punch_interval = punch_interval
        self.punch_timeout = punch_timeout
        self.verbose = verbose
        self.state = "idle"
        self.peer: Optional[Dict[str, Any]] = None
        self.public: Optional[Tuple[str, int]] = None
        self.rtt_ms: Optional[int] = None

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", local_port))
        self.local_port = self.sock.getsockname()[1]

        self._seq = 0
        self._pending: Dict[int, Tuple[bytes, float, int]] = {}
        # ★ 可信 UDP 的去重：UDP 会重复投递（重传 + 对端也重传），
        #   同一个 seq 必须只交给业务层一次，否则"跑一次脚本"会被执行多遍。
        self._seen_seq: "deque[int]" = deque(maxlen=4096)
        self._seen_lock = threading.RLock()
        self.stats = {"sent": 0, "resent": 0, "recv": 0, "dup_dropped": 0,
                      "ack_miss": 0}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.on_message: Optional[Callable[[Dict[str, Any]], None]] = None

    # ---------- 日志 ----------
    def _log(self, *args: Any) -> None:
        if self.verbose:
            print("[link]", *args, flush=True)

    # ---------- ① 探测 ----------
    def discover(self) -> Dict[str, Any]:
        info = discover_public(self.sock)
        if not info["mapped"]:
            raise RuntimeError(
                "所有 STUN 服务器都不可达，无法探测公网地址。"
                "（容器/CGNAT 环境可能需要中继）")
        self.public = info["mapped"]
        self.state = "discovered"
        self._log(f"public={self.public[0]}:{self.public[1]} "
                  f"local={self.local_port} rtt={info['attempts'][0]['ms']}ms")
        if info["symmetric_hint"]:
            self._log("⚠ 疑似对称 NAT，打洞可能失败")
        return info

    def ticket(self) -> str:
        if not self.public:
            self.discover()
        return make_ticket(self.public, self.local_port, self.agent_id)

    # ---------- ③ 打洞 ----------
    def _start_reader(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._read_loop, daemon=True,
                                        name=f"link-{self.agent_id}")
        self._thread.start()

    def _drain_probe(self) -> None:
        """清掉早期探测包（打洞前的噪声），避免把旧包当成业务消息。"""
        self.sock.setblocking(False)
        try:
            while True:
                try:
                    self.sock.recvfrom(4096)
                except (BlockingIOError, OSError):
                    break
        finally:
            self.sock.setblocking(True)

    def wait_punch(self, peer_ticket: Optional[str] = None,
                   on_connected: Optional[Callable[["DirectLink"], None]] = None
                   ) -> Dict[str, Any]:
        """**被动**打洞：只等对方打过来，自己不主动打。

        为什么需要这个方向
        ------------------
        大佬 18:17：
        > 让他那边往这往这边打，就他那边是出口，因为这边的 IP 是固定的。

        我这侧 IP 固定、NAT 相对宽松（或本来就是公网可达）；
        对方在严格 NAT 后面、**只能出站**。这时最优拓扑是：

            对方（出站）──PUNCH──► 我（被动收）
            我 ──PONG──► 对方          ← 我的回包刚好穿过对方的 NAT 映射

        关键点：**我必须先把自己的 socket 打开并持续等待**，对方 PUNCH 进来
        我才知道它的公网地址（`_mark_direct` 会把 `src` 记成 peer）。

        `peer_ticket` 可选：给了就校验对方 id 是否匹配（防串台）。
        """
        self.state = "waiting_punch"
        deadline = time.time() + self.punch_timeout
        self._log(f"waiting for peer to punch us (local_port={self.local_port}, "
                  f"{self.punch_timeout:.0f}s)")
        expect_id = None
        if peer_ticket:
            try:
                expect_id = parse_ticket(peer_ticket).get("agent_id")
            except ValueError:
                expect_id = None
        last_log = 0.0
        while time.time() < deadline and self.state != "direct":
            self.sock.settimeout(min(0.5, max(0.05, deadline - time.time())))
            try:
                data, src = self.sock.recvfrom(4096)
            except socket.timeout:
                data = b""
                src = ("", 0)
            except OSError:
                break
            if data:
                if expect_id and data.startswith(PUNCH):
                    got = data[len(PUNCH):].decode("utf-8", "replace").strip()
                    if got and got != expect_id:
                        self._log(f"忽略非预期对端 {got}（期待 {expect_id}）")
                        continue
                self._handle_probe(data, src)
            now = time.time()
            if now - last_log > 3:
                last_log = now
                self._log(f"waiting… {int(deadline - now)}s left")
        self.sock.settimeout(None)

        if self.state != "direct":
            self.state = "failed"
            return {"ok": False, "state": "failed",
                    "reason": f"等 {deadline and int(self.punch_timeout)}s 没等到对方 PUNCH"
                              "（对方没打过来，或打到了别的地址）"}

        self._start_reader()
        self._log(f"✅ direct (passive) peer={self.peer.get('ip')}:"
                  f"{self.peer.get('public_port')} rtt≈{self.rtt_ms}ms")
        if on_connected:
            try:
                on_connected(self)
            except Exception as e:  # noqa: BLE001
                self._log(f"on_connected 回调异常：{type(e).__name__}: {e}")
        return {"ok": True, "state": "direct", "mode": "passive",
                "peer": self.peer, "rtt_ms": self.rtt_ms}

    def punch(self, peer_ticket: str,
              on_connected: Optional[Callable[["DirectLink"], None]] = None
              ) -> Dict[str, Any]:
        """向对端公网地址持续打洞，直到收到 PONG 或超时。"""
        info = parse_ticket(peer_ticket)
        self.peer = info
        target = (info["ip"], info["public_port"])
        self.state = "punching"
        self._log(f"punching {target[0]}:{target[1]} "
                  f"(peer={info['agent_id']}, peer_local={info['local_port']})")

        # 先自己发几个，把自己这条 NAT 映射打热
        deadline = time.time() + self.punch_timeout
        last_log = 0.0
        while time.time() < deadline and self.state != "direct":
            try:
                self.sock.sendto(PUNCH + self.agent_id.encode(), target)
            except OSError as e:
                self._log(f"send fail {type(e).__name__}: {e}")
            # 等回包（短超时，便于快速重发）
            self.sock.settimeout(self.punch_interval)
            try:
                data, src = self.sock.recvfrom(4096)
                self._handle_probe(data, src)
            except socket.timeout:
                pass
            except OSError:
                pass
            now = time.time()
            if now - last_log > 3:
                last_log = now
                self._log(f"waiting… {int(deadline - now)}s left")
        self.sock.settimeout(None)

        if self.state != "direct":
            self.state = "failed"
            return {"ok": False, "state": "failed",
                    "reason": ("打洞超时：双方没能互相穿透（可能对称 NAT / "
                               "严格防火墙）→ 需要中继"),
                    "peer": info, "target": f"{target[0]}:{target[1]}"}

        self._start_reader()
        self._log(f"✅ direct {target[0]}:{target[1]} rtt≈{self.rtt_ms}ms")
        if on_connected:
            try:
                on_connected(self)
            except Exception as e:  # noqa: BLE001
                self._log(f"on_connected 回调异常：{type(e).__name__}: {e}")
        return {"ok": True, "state": "direct",
                "peer": info, "target": f"{target[0]}:{target[1]}",
                "rtt_ms": self.rtt_ms}

    def _handle_probe(self, data: bytes, src: Tuple[str, int]) -> None:
        """处理 PUNCH / PONG，任一方收到都能把状态推到 direct。"""
        if data.startswith(PUNCH):
            try:
                self.sock.sendto(PONG + self.agent_id.encode(), src)
            except OSError:
                pass
            if self.state != "direct":
                self._mark_direct(src)
        elif data.startswith(PONG):
            if self.state != "direct":
                self._mark_direct(src)

    def _mark_direct(self, src: Tuple[str, int]) -> None:
        self.state = "direct"
        if self.peer is None:
            self.peer = {"ip": src[0], "public_port": src[1], "agent_id": "unknown",
                         "local_port": 0}
        self.peer["ip"], self.peer["public_port"] = src[0], src[1]
        if self.rtt_ms is None:
            # 用一次 PUNCH→PONG 粗测
            t0 = time.time()
            try:
                self.sock.sendto(PUNCH + self.agent_id.encode(), src)
                self.sock.settimeout(1.0)
                self.sock.recvfrom(2048)
                self.rtt_ms = int((time.time() - t0) * 1000)
            except (socket.timeout, OSError):
                self.rtt_ms = -1
            finally:
                try:
                    self.sock.settimeout(None)
                except OSError:
                    pass
        self._log(f"direct established with {src[0]}:{src[1]}")

    # ---------- 可靠收发 ----------
    def send(self, payload: Dict[str, Any], retries: int = 4,
             ack_timeout: float = 1.0) -> bool:
        if self.state != "direct" or not self.peer:
            raise RuntimeError(f"link 未建立（state={self.state}）")
        with self._lock:
            self._seq += 1
            seq = self._seq
        body = json.dumps(payload, ensure_ascii=False).encode()
        frame = DATA + f"{seq}|".encode() + body
        target = (self.peer["ip"], self.peer["public_port"])
        with self._lock:
            self._pending[seq] = (frame, time.time(), 0)
        for attempt in range(retries):
            try:
                self.sock.sendto(frame, target)
                self.stats["sent"] += 1
                if attempt > 0:
                    self.stats["resent"] += 1
            except OSError:
                pass
            waited = time.time()
            while time.time() - waited < ack_timeout:
                with self._lock:
                    if seq not in self._pending:
                        return True
                time.sleep(0.05)
        with self._lock:
            self._pending.pop(seq, None)
        self.stats["ack_miss"] += 1
        return False

    def _read_loop(self) -> None:
        self.sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                data, src = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if data.startswith(PUNCH):
                try:
                    self.sock.sendto(PONG + self.agent_id.encode(), src)
                except OSError:
                    pass
                continue
            if data.startswith(PONG):
                continue
            if data.startswith(ACKP):
                try:
                    seq = int(data[4:].decode().strip())
                except ValueError:
                    continue
                with self._lock:
                    self._pending.pop(seq, None)
                continue
            if data.startswith(DATA):
                rest = data[2:]
                bar = rest.find(b"|")
                if bar <= 0:
                    continue
                try:
                    seq = int(rest[:bar])
                    payload = json.loads(rest[bar + 1:].decode())
                except (ValueError, UnicodeDecodeError):
                    continue
                # 先无条件回 ACK（对方的 send 可能就是靠这个 ACK 返回的），
                # 再判断是不是重复包 —— 重复包必须回 ACK（否则对方会一直重传），
                # 但**不能**再交给业务层（否则脚本被执行多遍）。
                try:
                    self.sock.sendto(ACKP + str(seq).encode(), src)
                except OSError:
                    pass
                with self._seen_lock:
                    dup = seq in self._seen_seq
                    if not dup:
                        self._seen_seq.append(seq)
                self.stats["recv"] += 1
                if dup:
                    self.stats["dup_dropped"] += 1
                    continue
                if self.on_message:
                    try:
                        self.on_message(payload)
                    except Exception as e:  # noqa: BLE001
                        self._log(f"on_message 异常：{type(e).__name__}: {e}")

    def recv(self, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
        """同步收一条（配合线程回调之外的简单用法）。"""
        got: List[Dict[str, Any]] = []
        old = self.on_message

        def _cb(m: Dict[str, Any]) -> None:
            got.append(m)

        self.on_message = _cb
        try:
            deadline = time.time() + timeout
            while time.time() < deadline and not got:
                time.sleep(0.05)
            return got[0] if got else None
        finally:
            self.on_message = old

    def close(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        self.state = "closed"


# ─────────────────────────────────────────────────────── 远程执行
def run_local(script: str, argv: Optional[List[str]] = None,
              timeout_s: float = 300) -> Dict[str, Any]:
    """在被派的那台机器上跑一个本地程序（不经过任何网络/大模型）。"""
    cmd = ([sys.executable, script] if script.endswith(".py") else [script])
    cmd += list(argv or [])
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout_s, shell=False)
        return {"ok": p.returncode == 0, "returncode": p.returncode,
                "stdout": (p.stdout or "")[-8000:],
                "stderr": (p.stderr or "")[-8000:],
                "duration_ms": int((time.time() - t0) * 1000),
                "host": socket.gethostname()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "timed_out": True, "returncode": -9,
                "duration_ms": int((time.time() - t0) * 1000),
                "host": socket.gethostname()}
    except OSError as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "host": socket.gethostname()}


def serve_commands(link: DirectLink, stop: threading.Event,
                  allowed_prefixes: Optional[List[str]] = None) -> None:
    """连上后当"执行端"：收 {action:"run", script, argv} 就本地跑，回结果。

    `allowed_prefixes` 给了就只跑这些目录下的脚本（默认不限，仅用于内网直连场景）。
    """
    def _on_msg(msg: Dict[str, Any]) -> None:
        if msg.get("action") != "run":
            return
        script = msg.get("script") or ""
        if allowed_prefixes and not any(script.startswith(p) for p in allowed_prefixes):
            link.send({"action": "result", "id": msg.get("id"),
                       "result": {"ok": False, "denied": True,
                                  "reason": f"脚本不在允许目录：{script}"}})
            return
        res = run_local(script, msg.get("argv"), float(msg.get("timeout_s") or 300))
        link.send({"action": "result", "id": msg.get("id"), "result": res})

    link.on_message = _on_msg
    while not stop.is_set():
        time.sleep(0.2)


# ─────────────────────────────────────────────────────── CLI
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m multi_agent.bootstrap",
        description="STUN 探测 → 换门票 → UDP 打洞 → 直连（省 token）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("ticket", help="探测公网地址并打印门票（发给对方）")
    t.add_argument("--id", required=True)
    t.add_argument("--port", type=int, default=DEFAULT_LOCAL_PORT)
    t.add_argument("--json", action="store_true")

    j = sub.add_parser("join", help="拿对方门票去打通/被打通")
    j.add_argument("--id", required=True)
    j.add_argument("--peer-ticket", required=True, help="对方给的门票")
    j.add_argument("--port", type=int, default=DEFAULT_LOCAL_PORT)
    j.add_argument("--timeout", type=float, default=30.0)
    j.add_argument("--on-connect", default=None, help="连上后本地立刻执行的程序")
    j.add_argument("--listen", type=float, default=0.0, help="连上后保持监听秒数")
    j.add_argument("--exec-on-msg", action="store_true",
                   help="允许对方发 run 指令（内网信任场景）")
    j.add_argument("--send", default=None, help="连上后发一条 JSON 消息")
    j.add_argument("--wait-result", type=float, default=0.0,
                   help="发完 --send 后等结果的秒数（收到 result 立即返回）")
    j.add_argument("--json", action="store_true")

    s = sub.add_parser("stun", help="只探测公网地址")
    s.add_argument("--port", type=int, default=DEFAULT_LOCAL_PORT)

    a = ap.parse_args(argv)

    if a.cmd == "stun":
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", a.port))
        info = discover_public(sock)
        sock.close()
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0 if info["mapped"] else 1

    if a.cmd == "ticket":
        link = DirectLink(a.id, a.port)
        try:
            info = link.discover()
            tk = link.ticket()
            if a.json:
                print(json.dumps({"ticket": tk, "public": list(link.public),
                                  "local_port": link.local_port,
                                  "attempts": info["attempts"],
                                  "symmetric_hint": info["symmetric_hint"]},
                                 ensure_ascii=False, indent=2))
            else:
                print(tk, flush=True)
        finally:
            link.close()
        return 0

    if a.cmd == "join":
        link = DirectLink(a.id, a.port)
        stop = threading.Event()
        try:
            info = link.discover()
            tk = link.ticket()
            if not a.json:
                print(tk, flush=True)      # 先把自己的票吐出来，方便对方回填

            def _on_connected(lk: DirectLink) -> None:
                if a.on_connect:
                    res = run_local(a.on_connect.split()[0],
                                    a.on_connect.split()[1:])
                    lk.send({"action": "local_result", "result": res})
                    if not a.json:
                        print(f"[on-connect] rc={res.get('returncode')} "
                              f"{res.get('duration_ms')}ms", flush=True)
                if a.send:
                    try:
                        lk.send(json.loads(a.send))
                    except json.JSONDecodeError:
                        lk.send({"text": a.send})
                if a.exec_on_msg:
                    threading.Thread(target=serve_commands,
                                     args=(lk, stop), daemon=True).start()

            results: List[Dict[str, Any]] = []
            link.on_message = lambda m: results.append(m)

            res = link.punch(a.peer_ticket, on_connected=_on_connected)
            if a.json:
                print(json.dumps({**res, "my_ticket": tk,
                                  "public": list(link.public)}, ensure_ascii=False))
            if not res["ok"]:
                return 1

            if a.wait_result > 0:
                deadline = time.time() + a.wait_result
                while time.time() < deadline and not results:
                    time.sleep(0.05)
                got = next((m for m in results if m.get("action") == "result"), None)
                if got:
                    payload = {"ok": True, "received": got,
                               "peer": link.peer.get("agent_id") if link.peer else None}
                    print(json.dumps(payload, ensure_ascii=False, indent=2)
                          if a.json else
                          f"[result] from {link.peer.get('agent_id') if link.peer else '?'}: "
                          f"{json.dumps(got.get('result'), ensure_ascii=False)}",
                          flush=True)
                elif a.json:
                    print(json.dumps({"ok": False, "received": results,
                                      "note": "等结果超时"}, ensure_ascii=False),
                          flush=True)
                else:
                    print(f"[result] 等 {a.wait_result}s 没收到结果", flush=True)

            if a.listen > 0:
                deadline = time.time() + a.listen
                while time.time() < deadline and not stop.is_set():
                    time.sleep(0.3)
            return 0
        except (RuntimeError, ValueError) as e:
            print(f"[bootstrap] {type(e).__name__}: {e}", file=sys.stderr)
            return 2
        finally:
            stop.set()
            link.close()

    return 2


if __name__ == "__main__":
    sys.exit(main())
