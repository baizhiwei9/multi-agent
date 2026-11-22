import pytest

from multi_agent.client import AgentRuntime, HubError
from multi_agent.server import HubServer
from multi_agent.transports.jsonrpc import JsonRpcClient

TOKEN = "rpc-token"


@pytest.fixture
def stack():
    srv = HubServer(host="127.0.0.1", port=0, db_path=":memory:", token=TOKEN)
    srv.start_background()
    rt = AgentRuntime(srv.url, "echo-agent",
                      [{"name": "demo", "actions": ["echo"]}],
                      token=TOKEN, concurrency=1, poll_wait=1)
    rt.add_handler("demo", "echo", lambda p, c: {"echo": p["x"]})
    rt.start()
    yield srv, JsonRpcClient(srv.url, token=TOKEN)
    rt.stop()
    srv.stop()


def test_rpc_agents_and_submit(stack):
    _, rpc = stack
    agents = rpc.call("hub.agents")["agents"]
    assert any(a["agent_id"] == "echo-agent" for a in agents)

    task = rpc.call("hub.submit", {
        "capability": "demo", "action": "echo",
        "params": {"x": 7}, "wait": True, "timeout_s": 5,
    })
    assert task["state"] == "succeeded"
    assert task["result"] == {"echo": 7}


def test_rpc_task_and_cancel(stack):
    _, rpc = stack
    task = rpc.call("hub.submit", {
        "capability": "demo", "action": "echo", "params": {"x": 1}, "wait": False,
    })
    got = rpc.call("hub.task", {"task_id": task["id"]})
    assert got["id"] == task["id"]


def test_rpc_unknown_method(stack):
    _, rpc = stack
    with pytest.raises(HubError) as e:
        rpc.call("hub.nope")
    assert e.value.code == -32601


def test_rpc_missing_param(stack):
    _, rpc = stack
    with pytest.raises(HubError) as e:
        rpc.call("hub.submit", {"capability": "demo"})
    assert e.value.code == -32602