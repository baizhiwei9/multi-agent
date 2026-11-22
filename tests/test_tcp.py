import socket

import pytest

from multi_agent.client import AgentRuntime, HubError
from multi_agent.server import HubServer
from multi_agent.tcp import TCPClient, TCPHubServer, TcpTransport

TOKEN = "tcp-token"


@pytest.fixture
def stack():
    srv = HubServer(host="127.0.0.1", port=0, db_path=":memory:", token=TOKEN)
    srv.start_background()
    rt = AgentRuntime(srv.url, "echo-agent",
                      [{"name": "demo", "actions": ["echo"]}],
                      token=TOKEN, concurrency=1, poll_wait=1)
    rt.add_handler("demo", "echo", lambda p, c: {"echo": p["x"]})
    rt.start()
    tcp = TCPHubServer(srv.hub, host="127.0.0.1", port=0, token=TOKEN)
    tcp.start_background()
    yield srv, tcp
    tcp.stop()
    rt.stop()
    srv.stop()


def test_tcp_auth_and_submit(stack):
    _, tcp = stack
    conn = TCPClient("127.0.0.1", tcp.port, token=TOKEN)
    try:
        agents = conn.call("hub.agents")["agents"]
        assert any(a["agent_id"] == "echo-agent" for a in agents)
        task = conn.call("hub.submit", {
            "capability": "demo", "action": "echo",
            "params": {"x": 2}, "wait": True, "timeout_s": 5,
        })
        assert task["state"] == "succeeded"
        assert task["result"] == {"echo": 2}
    finally:
        conn.close()


def test_tcp_transport_from_url(stack):
    _, tcp = stack
    tr = TcpTransport.from_url(f"tcp://127.0.0.1:{tcp.port}", token=TOKEN)
    try:
        assert tr.call("hub.agents")["agents"]
    finally:
        tr.close()


def test_tcp_wrong_token(stack):
    _, tcp = stack
    with pytest.raises(HubError) as e:
        TCPClient("127.0.0.1", tcp.port, token="bad")
    assert e.value.code == -32001


def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))  # 只选路，不真的发包
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return "127.0.0.1"


def test_tcp_non_loopback_address(stack):
    """模拟内网地址：绑 0.0.0.0，用本机网卡 IP 连。"""
    srv, _ = stack
    t = TCPHubServer(srv.hub, host="0.0.0.0", port=0, token=TOKEN)
    t.start_background()
    ip = _local_ip()
    try:
        conn = TCPClient(ip, t.port, token=TOKEN)
        try:
            assert conn.call("hub.agents")["agents"]
        finally:
            conn.close()
    finally:
        t.stop()