"""signal：用公网中继（ntfy）交换门票 → 打洞直连 → 打不通再走中继。

为什么需要这一层
================
大佬 17:57 的链路要求：

    ① 协调者用大模型 @一下某个 agent，让它 "执行某个角色"
    ② 对方执行起来后，把它的**打洞门票**传回来
    ③ 双方开始跑穿透逻辑（STUN + UDP 打洞）
    ④ 打通了就一直走直连（零 token）；打不通就走中继

第 ② 步的"传门票"本身不能再烧 token —— 所以用**公网中继**（ntfy.sh）
自动交换，一次 HTTP POST + 一次 poll，成本 ≈ 0。

三段式（和 bootstrap.py 配合）
==============================
    ┌──────────┐   ①post 自己的票        ┌─────────┐
    │ openclaw │ ──────────────────────► │ ntfy.sh │
    │          │ ◄────────────────────── │ (topic) │
    └────┬─────┘   ②poll 对方的票        └─────────┘
         │                                    ▲
         │ ③ 双方拿到对方公网地址后直接 UDP 打洞  │
         ▼                                    │
    ┌──────────┐                              │
    │  hermes  │ ─────────────────────────────┘
    └──────────┘
         ④ 打通 → 直连（0 token）
            打不通 → 继续用 ntfy 当中继（每次 ~1.5s）

为什么中继兜底很重要
====================
实测本机在 CGNAT 后面（外网 curl 18799/18899 全是 000），对称 NAT 概率高。
所以**不能只会打洞**，必须有"确定能用"的兜底。ntfy 是双方都主动出站，
NAT 完全不挡，只是慢一点。

用法（协调者侧）
================
    from multi_agent.signal import Signal

    sig = Signal(topic="mergen-7f3a91", agent_id="openclaw", port=8796)
    info = sig.discover()                       # STUN
    print(sig.ticket())                         # 门票（要给人看就这一行）

    peer = sig.wait_peer_ticket("hermes", timeout=180)   # 等等对方 post 票
    res = sig.connect(peer)                     # 打洞，失败自动转中继
    # res = {"mode": "direct"|"relay", ...}

    sig.send({"action": "run", "script": "/srv/jobs/build_sheet.py"})
    print(sig.recv(timeout=60))

用法（被派的那台）
==================
    sig = Signal(topic="mergen-7f3a91", agent_id="hermes", port=8797)
    sig.discover()
    sig.announce()                              # 把票 post 上去
    peer = sig.wait_peer_ticket("openclaw")      # 等协调者的票
    sig.connect(peer)
    sig.serve(on_run=lambda script, argv: ...)   # 收 run 就执行
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import safepath

from .bootstrap import DirectLink, make_ticket, parse_ticket, run_local

# 容器 IPv6 不通：解析出 IPv6 会静默卡死
_og = socket.getaddrinfo
socket.getaddrinfo = lambda *a, **k: [r for r in _og(*a, **k) if r[0] == socket.AF_INET]

NTFY_BASE = os.environ.get("SIGNAL_BASE", "https://ntfy.sh").rstrip("/")
# ntfy 的 poll 有 ~2-3s 缓存延迟，必须等一会儿再读，否则读不到刚发的
POLL_SETTLE_S = float(os.environ.get("SIGNAL_SETTLE", "3.0"))
HTTP_TIMEOUT = float(os.environ.get("SIGNAL_HTTP_TIMEOUT", "20"))


class SignalError(RuntimeError):
    pass


def _post(topic: str, payload: Dict[str, Any], title: str = "") -> bool:
    body = json.dumps(payload, ensure_ascii=False).encode()
    hdrs = {"Content-Type": "application/json; charset=utf-8"}
    if title:
        # HTTP header 只能 latin-1，中文会炸
        hdrs["X-Title"] = title.encode("ascii", "ignore").decode() or "msg"
    req = urllib.request.Request(
        f"{NTFY_BASE}/{urllib.parse.quote(topic)}", data=body, method="POST",
        headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError):
        return False


def _poll(topic: str, since: str = "10m", limit: int = 200) -> List[Dict[str, Any]]:
    url = (f"{NTFY_BASE}/{urllib.parse.quote(topic)}/json"
           f"?poll=1&since={urllib.parse.quote(since)}")
    out: List[Dict[str, Any]] = []
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as r:
            raw = r.read().decode()
    except (urllib.error.URLError, OSError):
        return out
    for line in raw.strip().splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("event") != "message":
            continue
        try:
            body = json.loads(d.get("message") or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(body, dict):
            body.setdefault("_ts", d.get("time"))
            out.append(body)
    return out[-limit:]


class Signal:
    """门票交换 + 打洞 + 中继兜底。

    状态：`idle` → `discovered` → `connected(mode=direct|relay)` → `closed`
    """

    def __init__(self, topic: str, agent_id: str,
                 port: int = 0, verbose: bool = True):
        if not topic:
            raise SignalError("缺少 topic（双方必须用同一个频道名）")
        self.topic = topic
        self.agent_id = agent_id
        self.verbose = verbose
        self.link = DirectLink(agent_id, port, verbose=verbose)
        self.mode = "idle"
        self.peer: Optional[Dict[str, Any]] = None
        self.peer_agent: Optional[str] = None
        self._relay_seen: set = set()
        self._stop = threading.Event()
        self._serve_thread: Optional[threading.Thread] = None

    # ---------- 日志 ----------
    def _log(self, *a: Any) -> None:
        if self.verbose:
            print("[signal]", *a, flush=True)

    # ---------- ① 探测 ----------
    def discover(self) -> Dict[str, Any]:
        info = self.link.discover()
        self.mode = "discovered"
        return info

    def ticket(self) -> str:
        return self.link.ticket()

    # ---------- ② 交换门票 ----------
    def announce(self) -> bool:
        """把门票 post 到频道（对方 poll 就能拿到）。"""
        if not self.link.public:
            self.discover()
        ok = _post(self.topic, {
            "kind": "ticket", "id": self.agent_id,
            "ticket": self.link.ticket(),
            "local_port": self.link.local_port,
            "nonce": uuid.uuid4().hex[:8],
        }, title=f"ticket-{self.agent_id}")
        self._log(f"announce ticket ok={ok} ({self.link.public[0]}:{self.link.public[1]})")
        return ok

    def _find_ticket(self, peer_id: Optional[str]) -> Optional[Dict[str, Any]]:
        for m in reversed(_poll(self.topic, since="30m")):
            if m.get("kind") != "ticket":
                continue
            if peer_id and m.get("id") != peer_id:
                continue
            if m.get("id") == self.agent_id:
                continue          # 自己的票，跳过
            if not m.get("ticket"):
                continue
            return m
        return None

    def wait_peer_ticket(self, peer_id: Optional[str] = None,
                         timeout: float = 180.0) -> Dict[str, Any]:
        """轮询等对方的门票。默认等 3 分钟。"""
        deadline = time.time() + timeout
        last_log = 0.0
        while time.time() < deadline and not self._stop.is_set():
            hit = self._find_ticket(peer_id)
            if hit:
                self._log(f"got peer ticket from {hit['id']}")
                return hit
            now = time.time()
            if now - last_log > 10:
                last_log = now
                self._log(f"waiting peer ticket… {int(deadline - now)}s left")
            time.sleep(POLL_SETTLE_S)
        raise SignalError(
            f"等 {peer_id or '任意对端'} 的门票超时（{timeout:.0f}s）。"
            f"检查：① 对方用同一个 topic（{self.topic}）② 对方已 announce ③ 网络可达 ntfy")

    # ---------- ③ 连接（打洞 → 失败转中继） ----------
    def connect(self, peer_ticket: str, punch_timeout: float = 25.0,
                allow_relay: bool = True) -> Dict[str, Any]:
        info = parse_ticket(peer_ticket)
        self.peer_agent = info.get("agent_id")
        self._log(f"punching {info['ip']}:{info['public_port']} "
                  f"(peer={self.peer_agent})")
        self.link.punch_timeout = punch_timeout
        res = self.link.punch(peer_ticket)
        if res["ok"]:
            self.mode = "direct"
            self.peer = info
            self._log(f"✅ direct  rtt≈{res.get('rtt_ms')}ms")
            return {"mode": "direct", **res}
        if not allow_relay:
            self.mode = "failed"
            return {"mode": "failed", **res}
        # 打洞失败 → 走中继（双方都主动出站，NAT 不挡）
        self.mode = "relay"
        self.peer = info
        self._log("⚠ 打洞失败，切换到中继模式（慢但一定能通）")
        return {"mode": "relay", "reason": res.get("reason"),
                "peer": info, "relay": self.topic}

    def wait_connect(self, peer_id: Optional[str] = None,
                     punch_timeout: float = 60.0) -> Dict[str, Any]:
        """**被动模式**：等对方主动打过来（我方 IP 固定、对方只能出站时用）。

        大佬 18:17：
        > 让他那边往这往这边打，就他那边是出口，因为这边的 IP 是固定的。

        我 announce 自己的票 → 对方拿着我的票主动 PUNCH →
        我收到 PUNCH 就回 PONG 并把对方地址记成 peer → 直连建立。
        之后收发和 punch 模式完全一样。
        """
        self.mode = "waiting"
        res = self.link.wait_punch(None, on_connected=None)
        if not res.get("ok"):
            self.mode = "failed"
            return {"mode": "failed", **res}
        peer = res.get("peer") or {}
        self.peer = peer
        self.peer_agent = peer.get("agent_id") or peer_id
        self.mode = "direct"
        self._log(f"✅ direct(passive) peer={peer.get('ip')}:{peer.get('public_port')} "
                  f"rtt≈{res.get('rtt_ms')}ms")
        return {"mode": "direct", "passive": True, **res}

    # ---------- ④ 收发（两种模式统一接口） ----------
    def send(self, payload: Dict[str, Any], to: Optional[str] = None) -> bool:
        msg = dict(payload)
        msg["_from"] = self.agent_id
        msg["_to"] = to or self.peer_agent or "*"
        msg["_mid"] = uuid.uuid4().hex[:10]
        if self.mode == "direct":
            return self.link.send(msg)
        if self.mode in ("relay", "discovered"):
            return _post(self.topic, {"kind": "app", **msg},
                         title=f"app-{self.agent_id}")
        raise SignalError(f"未连接（mode={self.mode}），先 discover/announce/connect")

    def _collect_relay(self, since: str = "10m") -> List[Dict[str, Any]]:
        got = []
        for m in _poll(self.topic, since=since):
            if m.get("kind") != "app":
                continue
            if m.get("_from") == self.agent_id:
                continue                      # 自己发的
            if m.get("_to") not in ("*", self.agent_id, None):
                continue                      # 不是给我的
            mid = m.get("_mid")
            if mid and mid in self._relay_seen:
                continue
            if mid:
                self._relay_seen.add(mid)
            got.append(m)
        return got

    def recv(self, timeout: float = 30.0,
             filter_fn: Optional[Callable[[Dict[str, Any]], bool]] = None
             ) -> Optional[Dict[str, Any]]:
        """收一条；direct 走 socket，relay 走 ntfy poll。"""
        deadline = time.time() + timeout
        if self.mode == "direct":
            while time.time() < deadline:
                m = self.link.recv(min(2.0, max(0.2, deadline - time.time())))
                if m and (filter_fn is None or filter_fn(m)):
                    return m
            return None
        # relay
        while time.time() < deadline:
            for m in self._collect_relay():
                if filter_fn is None or filter_fn(m):
                    return m
            time.sleep(min(POLL_SETTLE_S, max(0.5, deadline - time.time())))
        return None

    # ---------- 执行端 ----------
    def serve(self, on_run: Optional[Callable[[str, List[str]], Dict[str, Any]]] = None,
              allowed_prefixes: Optional[List[str]] = None,
              background: bool = True) -> Optional[threading.Thread]:
        """当执行端：收 `{action:"run"}` 就在本机跑，把结果回给对方。"""
        # ⚠️ fail-closed：None 表示**拒绝一切**，而不是"不限制"。
        #    判定走 safepath：按 realpath 比目录归属，前缀相同不算数、.. 也穿不出去。
        def _run(script: str, argv: List[str]) -> Dict[str, Any]:
            ok, reason = safepath.check(script, allowed_prefixes)
            if not ok:
                return {"ok": False, "denied": True, "reason": reason}
            if on_run:
                return on_run(script, argv)
            return run_local(script, argv)

        if self.mode == "direct":
            def _on_msg(m: Dict[str, Any]) -> None:
                if m.get("action") != "run":
                    return
                res = _run(str(m.get("script") or ""), list(m.get("argv") or []))
                self.send({"action": "result", "id": m.get("_mid"),
                           "result": res}, to=m.get("_from"))
            self.link.on_message = _on_msg
            return None

        def _loop() -> None:
            while not self._stop.is_set():
                for m in self._collect_relay():
                    if m.get("action") != "run":
                        continue
                    res = _run(str(m.get("script") or ""), list(m.get("argv") or []))
                    self.send({"action": "result", "id": m.get("_mid"),
                               "result": res}, to=m.get("_from"))
                time.sleep(POLL_SETTLE_S)

        if background:
            t = threading.Thread(target=_loop, daemon=True,
                                 name=f"serve-{self.agent_id}")
            t.start()
            self._serve_thread = t
            return t
        _loop()
        return None

    # ---------- 体检 ----------
    def status(self) -> Dict[str, Any]:
        return {"agent_id": self.agent_id, "topic": self.topic, "mode": self.mode,
                "public": list(self.link.public) if self.link.public else None,
                "local_port": self.link.local_port,
                "peer": self.peer, "peer_agent": self.peer_agent,
                "link_state": self.link.state, "rtt_ms": self.link.rtt_ms}

    def close(self) -> None:
        self._stop.set()
        self.link.close()
        self.mode = "closed"


# ───────────────────────────────────────────── CLI
def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(
        prog="python3 -m multi_agent.signal",
        description="公网中继交换门票 → 打洞 → 中继兜底（零 token 通信）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s1 = sub.add_parser("listen", help="当被派的那台：出票 + 等对端 + 执行程序")
    s1.add_argument("--topic", required=True)
    s1.add_argument("--id", required=True)
    s1.add_argument("--port", type=int, default=0)
    s1.add_argument("--allow", default=None, help="允许执行的目录前缀（冒号分隔）")
    s1.add_argument("--wait", type=float, default=300)
    s1.add_argument("--json", action="store_true")

    s2 = sub.add_parser("connect", help="协调者侧：拿对端票去打通并派活")
    s2.add_argument("--topic", required=True)
    s2.add_argument("--id", required=True)
    s2.add_argument("--port", type=int, default=0)
    s2.add_argument("--peer", required=True, help="对端 agent_id")
    s2.add_argument("--wait-ticket", type=float, default=180)
    s2.add_argument("--run", default=None, help="连上后让对方执行的脚本")
    s2.add_argument("--run-argv", default="", help="脚本参数（空格分隔）")
    s2.add_argument("--wait-result", type=float, default=120)
    s2.add_argument("--json", action="store_true")

    s4 = sub.add_parser("wait", help="被动模式：等对方主动打过来（我方 IP 固定时用）")
    s4.add_argument("--topic", required=True)
    s4.add_argument("--id", required=True)
    s4.add_argument("--port", type=int, default=0)
    s4.add_argument("--peer", default=None, help="期待的对端 id（可选，防串台）")
    s4.add_argument("--timeout", type=float, default=120)
    s4.add_argument("--allow", default=None)
    s4.add_argument("--wait", type=float, default=600)
    s4.add_argument("--json", action="store_true")

    s3 = sub.add_parser("selftest", help="本机双进程自测（不需要另一台机器）")
    s3.add_argument("--topic", default=None)

    a = ap.parse_args(argv)

    if a.cmd == "selftest":
        return _selftest(a.topic)

    if a.cmd == "wait":
        allow = [p for p in (a.allow or "").split(":") if p] or None
        sig = Signal(a.topic, a.id, a.port, verbose=not a.json)
        try:
            sig.discover()
            tk = sig.ticket()
            print(tk, flush=True)
            sig.announce()
            print(f"[signal] 被动等待对端打过来（{a.timeout:.0f}s）…", file=sys.stderr)
            sig.link.punch_timeout = a.timeout
            res = sig.wait_connect(peer_id=a.peer, punch_timeout=a.timeout)
            if a.json:
                print(json.dumps({**res, "my_ticket": tk,
                                  "status": sig.status()}, ensure_ascii=False))
            print(f"[signal] mode={res['mode']}", file=sys.stderr)
            if res["mode"] != "direct":
                return 1
            sig.serve(allowed_prefixes=allow)
            deadline = time.time() + a.wait
            while time.time() < deadline:
                time.sleep(0.5)
            return 0
        except SignalError as e:
            print(f"[signal] {e}", file=sys.stderr)
            return 2
        finally:
            sig.close()

    if a.cmd == "listen":
        allow = [p for p in (a.allow or "").split(":") if p] or None
        sig = Signal(a.topic, a.id, a.port, verbose=not a.json)
        try:
            sig.discover()
            tk = sig.ticket()
            print(tk, flush=True)                 # 门票：可以贴给人
            sig.announce()
            peer = sig.wait_peer_ticket(timeout=a.wait)
            res = sig.connect(peer["ticket"])
            if a.json:
                print(json.dumps({**res, "my_ticket": tk,
                                  "status": sig.status()}, ensure_ascii=False))
            sig.serve(allowed_prefixes=allow)
            deadline = time.time() + a.wait
            while time.time() < deadline:
                time.sleep(0.5)
            return 0
        except SignalError as e:
            print(f"[signal] {e}", file=sys.stderr)
            return 2
        finally:
            sig.close()

    if a.cmd == "connect":
        sig = Signal(a.topic, a.id, a.port, verbose=not a.json)
        try:
            sig.discover()
            print(sig.ticket(), flush=True)
            sig.announce()
            peer = sig.wait_peer_ticket(a.peer, timeout=a.wait_ticket)
            res = sig.connect(peer["ticket"])
            if not a.json:
                print(f"[signal] mode={res['mode']}")
            if not a.run:
                print(json.dumps({**res, "status": sig.status()},
                                 ensure_ascii=False, indent=2))
                return 0
            argv_list = a.run_argv.split() if a.run_argv else []
            sig.send({"action": "run", "script": a.run, "argv": argv_list})
            got = sig.recv(timeout=a.wait_result,
                           filter_fn=lambda m: m.get("action") == "result")
            out = {"mode": res["mode"], "connect": res,
                   "result": (got or {}).get("result"),
                   "received": got is not None}
            print(json.dumps(out, ensure_ascii=False, indent=2)
                  if a.json else
                  f"[result] {json.dumps((got or {}).get('result'), ensure_ascii=False)}",
                  flush=True)
            return 0 if got else 1
        except SignalError as e:
            print(f"[signal] {e}", file=sys.stderr)
            return 2
        finally:
            sig.close()

    return 2


def _selftest(topic: Optional[str] = None) -> int:
    """本机双进程自测：一个当听者、一个当连接者，走真实 ntfy + 真实打洞。"""
    import sys
    import tempfile

    topic = topic or ("selftest-" + uuid.uuid4().hex[:10])
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write("import json,socket\n"
                 "print(json.dumps({'selftest': True, 'host': socket.gethostname()}))\n")
        script = fh.name

    print(f"[selftest] topic={topic}")
    holder: Dict[str, Any] = {}

    def _listener() -> None:
        try:
            sig = Signal(topic, "listener", 0, verbose=False)
            sig.discover()
            sig.announce()
            peer = sig.wait_peer_ticket("connector", timeout=90)
            res = sig.connect(peer["ticket"], punch_timeout=15)
            holder["listen_mode"] = res["mode"]
            sig.serve(allowed_prefixes=["*"])
            got = sig.recv(timeout=90, filter_fn=lambda m: m.get("action") == "run")
            if got:
                r = run_local(str(got.get("script")), list(got.get("argv") or []))
                sig.send({"action": "result", "id": got.get("_mid"), "result": r},
                         to=got.get("_from"))
            time.sleep(3)
            sig.close()
        except Exception as e:  # noqa: BLE001
            holder["listen_error"] = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=_listener, daemon=True)
    t.start()
    time.sleep(2.0)

    try:
        sig = Signal(topic, "connector", 0, verbose=False)
        sig.discover()
        sig.announce()
        peer = sig.wait_peer_ticket("listener", timeout=90)
        res = sig.connect(peer["ticket"], punch_timeout=15)
        print(f"[selftest] connector mode={res['mode']}")
        sig.send({"action": "run", "script": script, "argv": ["--selftest"]})
        got = sig.recv(timeout=90, filter_fn=lambda m: m.get("action") == "result")
        ok = bool(got and (got.get("result") or {}).get("ok"))
        print(json.dumps({"ok": ok, "mode": res["mode"],
                          "listen_mode": holder.get("listen_mode"),
                          "result": (got or {}).get("result"),
                          "listen_error": holder.get("listen_error")},
                         ensure_ascii=False, indent=2))
        sig.close()
        return 0 if ok else 1
    finally:
        t.join(timeout=5)
        try:
            os.unlink(script)
        except OSError:
            pass


if __name__ == "__main__":
    import sys
    sys.exit(main())
