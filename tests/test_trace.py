import pytest

from multi_agent.client import AgentRuntime, HubClient
from multi_agent.server import HubServer

TOKEN = "trace-token"


@pytest.fixture
def stack():
    srv = HubServer(host="127.0.0.1", port=0, db_path=":memory:", token=TOKEN)
    srv.start_background()
    rt = AgentRuntime(srv.url, "echo-agent",
                      [{"name": "demo", "actions": ["echo", "boom"]}],
                      token=TOKEN, concurrency=1, poll_wait=1)
    rt.add_handler("demo", "echo", lambda p, c: {"echo": p["x"]})
    rt.add_handler("demo", "boom", lambda p, c: (_ for _ in ()).throw(RuntimeError("boom")))
    rt.start()
    yield srv, HubClient(srv.url, token=TOKEN)
    rt.stop()
    srv.stop()


def test_trace_success(stack):
    _, c = stack
    task = c.submit("demo", "echo", {"x": 1}, wait=True, timeout_s=5)
    trace = c.trace(task["id"])
    events = [e["event"] for e in trace["events"]]
    assert events[0] == "created"
    assert "assigned" in events
    assert "succeeded" in events


def test_trace_failure_has_error(stack):
    _, c = stack
    task = c.submit("demo", "boom", {}, wait=True, timeout_s=5)
    trace = c.trace(task["id"])
    last = trace["events"][-1]
    assert last["event"] == "failed"
    assert last["detail"]["error"]["code"] == "handler_error"