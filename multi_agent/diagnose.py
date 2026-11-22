"""分步体检：一眼看出"卡在哪一步"。

每一步独立计时、独立失败，不会因为前面挂了就看不到后面。
覆盖：HTTP 健康、注册表、DB 读写、TCP、rendezvous hello、UDP 打洞。
"""

from __future__ import annotations

import json
import time
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from .client import HubClient
from .dbmanager import DBClient
from .nat import HolePuncher
from .tcp import TCPClient


def _step(name: str, fn: Callable[[], Any]) -> Dict[str, Any]:
    t0 = time.time()
    try:
        detail = fn()
        ok = True
    except Exception as e:  # noqa: BLE001
        ok = False
        detail = f"{type(e).__name__}: {e}"
    return {"step": name, "ok": ok, "detail": detail,
            "ms": round((time.time() - t0) * 1000)}


def diagnose(hub: Optional[str] = None, tcp: Optional[str] = None,
             rendezvous: Optional[str] = None, peer: Optional[str] = None,
             token: Optional[str] = None, db: bool = True) -> List[Dict[str, Any]]:
    steps: List[Dict[str, Any]] = []

    if hub:
        steps.append(_step("http.health", lambda: _http_health(hub)))
        steps.append(_step("hub.agents", lambda: _agents(hub, token)))
        if db:
            steps.append(_step("db.roundtrip", lambda: _db_roundtrip(hub, token)))

    if tcp:
        steps.append(_step("tcp.call", lambda: _tcp_agents(tcp, token)))

    if rendezvous:
        steps.append(_step("rendezvous.hello", lambda: _hello(rendezvous, token)))
        if peer:
            steps.append(_step(f"punch.{peer}", lambda: _punch(rendezvous, peer, token)))

    return steps


def _http_health(base: str) -> Dict[str, Any]:
    url = base.rstrip("/") + "/healthz"
    with urllib.request.urlopen(url, timeout=5) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(f"healthz 返回 {data}")
    return data


def _agents(base: str, token: Optional[str]) -> Dict[str, Any]:
    data = HubClient(base, token=token).agents()
    agents = data.get("agents", [])
    return {"count": len(agents),
            "ids": [a.get("agent_id") for a in agents]}


def _db_roundtrip(base: str, token: Optional[str]) -> Dict[str, Any]:
    db = DBClient(base, token=token, timeout=10)
    key = "diag:" + uuid.uuid4().hex[:8]
    db.write("diagnostics", key, {"ts": time.time()})
    got = db.read("diagnostics", key)
    if not got.get("found"):
        raise RuntimeError("写进去读不出来")
    return {"key": key, "value": got.get("value")}


def _tcp_agents(target: str, token: Optional[str]) -> Dict[str, Any]:
    conn = TCPClient.from_url(target if target.startswith("tcp://") else f"tcp://{target}",
                              token=token)
    try:
        data = conn.call("hub.agents")
    finally:
        conn.close()
    return {"count": len(data.get("agents", []))}


def _split_udp(target: str) -> Tuple[str, int]:
    rest = target[len("udp://"):] if target.startswith("udp://") else target
    host, _, port = rest.partition(":")
    return host, int(port or 8791)


def _hello(rendezvous: str, token: Optional[str]) -> Dict[str, Any]:
    host, port = _split_udp(rendezvous)
    p = HolePuncher("diag-" + uuid.uuid4().hex[:6], (host, port), token=token, timeout=5)
    try:
        info = p.start()
        return {"observed": list(p.observed or ()), "peers": len(info.get("peers") or {})}
    finally:
        p.close()


def _punch(rendezvous: str, peer: str, token: Optional[str]) -> Dict[str, Any]:
    host, port = _split_udp(rendezvous)
    p = HolePuncher("diag-" + uuid.uuid4().hex[:6], (host, port), token=token, timeout=5)
    try:
        p.start()
        res = p.connect(peer, timeout=4)
        return {"mode": res.get("mode"), "peer": res.get("peer"),
                "reason": res.get("reason")}
    finally:
        p.close()