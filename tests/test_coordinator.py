import time

import pytest

from multi_agent.client import AgentRuntime, HubClient
from multi_agent.coordinator import Coordinator, CoordinatorAgent
from multi_agent.dbmanager import DBClient, DBManager
from multi_agent.server import HubServer

TOKEN = "coord-token"


@pytest.fixture
def stack():
    srv = HubServer(host="127.0.0.1", port=0, db_path=":memory:", token=TOKEN)
    srv.start_background()

    dbm = DBManager(":memory:")
    rt_db = AgentRuntime(srv.url, "db-manager",
                         [{"name": "db", "actions": list(DBManager.ACTIONS)}],
                         token=TOKEN, concurrency=4, poll_wait=1)
    dbm.attach(rt_db)
    rt_db.start()

    rt_demo = AgentRuntime(srv.url, "demo-agent",
                           [{"name": "demo", "actions": ["produce", "consume"]}],
                           token=TOKEN, concurrency=2, poll_wait=1)
    rt_demo.add_handler("demo", "produce",
                        lambda p, c: {"n": p["n"], "items": list(range(int(p["n"])))})
    rt_demo.add_handler("demo", "consume", lambda p, c: {"input": p.get("input")})
    rt_demo.start()

    db = DBClient(srv.url, token=TOKEN)
    ca = CoordinatorAgent(srv.url, token=TOKEN, db=db)
    rt_co = AgentRuntime(srv.url, "coordinator",
                         [{"name": "coordinator",
                           "actions": ["start", "status", "runs", "notify"]}],
                         token=TOKEN, concurrency=2, poll_wait=1)
    ca.attach(rt_co)
    rt_co.start()

    yield srv, dbm, db, ca, rt_co
    rt_co.stop(); rt_demo.stop(); rt_db.stop(); srv.stop(); dbm.close()


def test_demo_pipeline_and_db(stack):
    srv, _, db, ca, _ = stack
    run = ca.coord.start("demo", {"n": 3, "key": "latest"})
    snap = ca.coord.wait(run["run_id"], timeout=15)
    assert snap["state"] == "succeeded"
    for name in ("produce", "store", "consume"):
        assert snap["steps"][name]["state"] == "succeeded"
    stored = db.read("demo", "latest")
    assert stored["found"] and stored["value"]["items"] == [0, 1, 2]


def test_start_via_protocol(stack):
    srv, _, _, _, _ = stack
    c = HubClient(srv.url, token=TOKEN)
    t = c.submit("coordinator", "start",
                 {"pipeline": "demo", "params": {"n": 2, "key": "p2"}},
                 wait=True, timeout_s=10)
    assert t["state"] == "succeeded"
    run_id = t["result"]["run_id"]
    deadline = time.time() + 15
    while time.time() < deadline:
        st = c.submit("coordinator", "status", {"run_id": run_id}, wait=True, timeout_s=10)
        snap = st["result"]
        if snap["state"] in ("succeeded", "failed"):
            break
        time.sleep(0.2)
    assert snap["state"] == "succeeded"


def test_condition_skip(stack):
    _, _, db, ca, _ = stack
    spec = {
        "name": "cond",
        "steps": [
            {"name": "produce", "capability": "demo", "action": "produce",
             "params": {"n": 1}},
            {"name": "consume", "capability": "demo", "action": "consume",
             "params": {}, "depends_on": ["produce"],
             "when": {"step": "produce", "path": "result.n", "op": "gt", "value": 5}},
        ],
    }
    run = ca.coord.start(spec, {})
    snap = ca.coord.wait(run["run_id"], timeout=15)
    assert snap["state"] == "succeeded"
    assert snap["steps"]["produce"]["state"] == "succeeded"
    assert snap["steps"]["consume"]["state"] == "skipped"


def test_failure_propagates(stack):
    srv, _, _, _, _ = stack
    rt = AgentRuntime(srv.url, "boom-agent",
                      [{"name": "boom", "actions": ["fail"]}],
                      token=TOKEN, concurrency=1, poll_wait=1)
    rt.add_handler("boom", "fail", lambda p, c: (_ for _ in ()).throw(RuntimeError("boom")))
    rt.start()
    try:
        coord = Coordinator(srv.url, token=TOKEN)
        spec = {"name": "bad", "steps": [
            {"name": "a", "capability": "boom", "action": "fail", "params": {}},
            {"name": "b", "capability": "demo", "action": "consume",
             "params": {}, "depends_on": ["a"]},
        ]}
        run = coord.start(spec, {})
        snap = coord.wait(run["run_id"], timeout=15)
        assert snap["state"] == "failed"
        assert snap["steps"]["a"]["state"] == "failed"
        assert snap["steps"]["b"]["state"] == "skipped"
    finally:
        rt.stop()