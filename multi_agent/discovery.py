"""局域网自动发现（可选）：UDP 广播 Hub 地址。

只在同一广播域里有效（办公室/家庭/同机房）；跨网段请直接用 IP/域名。
默认不启用：HTTP 的地址配置已经足够，UDP 只是少配一次 IP。
"""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import List, Optional

MAGIC = "MULTI_AGENT_HUB"
DEFAULT_PORT = 8788


def start_beacon(hub_port: int, broadcast_port: int = DEFAULT_PORT,
                 interval: float = 5.0, stop_event: Optional[threading.Event] = None) -> threading.Thread:
    stop_event = stop_event or threading.Event()

    def _loop() -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        payload = json.dumps({"magic": MAGIC, "port": hub_port}).encode()
        try:
            while not stop_event.wait(interval):
                try:
                    sock.sendto(payload, ("255.255.255.255", broadcast_port))
                except OSError:
                    pass
        finally:
            sock.close()

    t = threading.Thread(target=_loop, name="hub-beacon", daemon=True)
    t.start()
    return t


def discover(timeout: float = 3.0, broadcast_port: int = DEFAULT_PORT) -> List[str]:
    """监听广播，返回去重后的 hub URL 列表。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", broadcast_port))
    except OSError:
        sock.close()
        return []
    sock.settimeout(0.5)
    found: List[str] = []
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                continue
            try:
                info = json.loads(data.decode())
            except ValueError:
                continue
            if info.get("magic") != MAGIC:
                continue
            url = f"http://{addr[0]}:{info.get('port')}"
            if url not in found:
                found.append(url)
    finally:
        sock.close()
    return found