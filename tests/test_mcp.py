import json
import os
import sys
import urllib.request
from pathlib import Path

import pytest

from multi_agent.client import AgentRuntime
from multi_agent.mcp import MCPClient, MCPServer
from multi_agent.server import HubServer

TOKEN = "mcp-token"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def stack():
    srv = HubServer(host="127.0.0.1", port=0, db_path=":memory:", token=TOKEN)
    srv.start_background()
    rt = AgentRuntime(srv.url, "echo-agent",
                      [{"name": "demo", "actions": ["echo"]}],
                      token=TOKEN, concurrency=1, poll_wait=1)
    rt.add_handler("demo", "echo", lambda p, c: {"echo": p["x"]})
    rt.start()
    yield srv
    rt.stop()
    srv.stop()


def test_mcp_initialize_and_tools(stack):
    m = MCPServer(hub=stack.hub)
    init = m.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["protocolVersion"]
    tools = m.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert "demo__echo" in [t["name"] for t in tools]
    assert m.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_mcp_tools_call(stack):
    m = MCPServer(hub=stack.hub)
    resp = m.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "demo__echo", "arguments": {"x": 9}}})
    task = json.loads(resp["result"]["content"][0]["text"])
    assert task["state"] == "succeeded"
    assert task["result"] == {"echo": 9}
    assert resp["result"]["isError"] is False


def test_mcp_http_endpoint(stack):
    body = json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/list"}).encode()
    req = urllib.request.Request(stack.url + "/mcp", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Hub-Token", TOKEN)
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read().decode())
    assert "demo__echo" in [t["name"] for t in data["result"]["tools"]]


def test_mcp_stdio_client(stack):
    cmd = [sys.executable, "-m", "multi_agent.cli", "mcp-server",
           "--hub", stack.url, "--token", TOKEN]
    env = dict(os.environ)
    client = MCPClient(cmd, env=env, cwd=str(ROOT))
    try:
        info = client.start()
        assert info["protocolVersion"]
        tools = client.list_tools()
        assert "demo__echo" in [t["name"] for t in tools]
        res = client.call_tool("demo__echo", {"x": 3})
        task = json.loads(res["content"][0]["text"])
        assert task["result"] == {"echo": 3}
    finally:
        client.close()