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
                           [{"name": "demo",
                             "actions": ["produce", "consume", "echo", "boom"]}],
                           token=TOKEN, concurrency=2, poll_wait=1)
    rt_demo.add_handler("demo", "produce",
                        lambda p, c: {"n": p["n"], "items": list(range(int(p["n"])))})
    rt_demo.add_handler("demo", "consume", lambda p, c: {"input": p.get("input")})
    rt_demo.add_handler("demo", "echo", lambda p, c: {"got": p})

    def _boom(p, c):
        raise RuntimeError("boom")

    rt_demo.add_handler("demo", "boom", _boom)
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

# ───────────────────────────────────────────── ${...} 代入（嵌入 / 嵌套）
def _ctx_with_build():
    return {"params": {"commodity": "copper"},
            "steps": {"build": {"state": "succeeded",
                                "result": {"ok": True,
                                           "results": {"copper": {"ok": True,
                                                                  "balance": {"commodity": "copper",
                                                                              "n": 1}}}}}}}


def test_resolve_interpolates_embedded_and_nested():
    from multi_agent.coordinator import _resolve
    ctx = _ctx_with_build()
    # 嵌入形态：以前整串不等 `${...}` 就原样留着（DB key 落库成字面量）
    assert _resolve("${params.commodity}:latest", ctx) == "copper:latest"
    # 嵌套形态：内层先展开，整串命中后仍返回原类型
    assert _resolve("${steps.build.result.results.${params.commodity}.balance}", ctx) == {
        "commodity": "copper", "n": 1}
    # 整串变量保持原类型（dict），不被字符串化
    assert _resolve("${steps.build.result}", ctx)["ok"] is True
    # 缺值：嵌入按空串，整串仍返回 None
    assert _resolve("${params.nope}:latest", ctx) == ":latest"
    assert _resolve("${params.nope}", ctx) is None


def test_shipped_pipelines_store_balance_body():
    from multi_agent.coordinator import _resolve
    from multi_agent.pipelines import PIPELINES
    from multi_agent.projects.futures_balance import BALANCE_PIPELINE
    ctx = _ctx_with_build()
    for spec in (PIPELINES["balance_sheet"], BALANCE_PIPELINE):
        step = next(s for s in spec["steps"] if s["name"] == "store_balance")
        params = _resolve(step["params"], ctx)
        assert params["key"] == "copper:latest", spec.get("name")
        assert params["value"] == {"commodity": "copper", "n": 1}, spec.get("name")


# ───────────────────────────────────────────── 通道合并 / 扇出 / 续跑
def test_channels_reducer_append(stack):
    """两个并行步骤写同一通道：append 合并，下游用 ${channels.x} 取。"""
    _, _, _, ca, _ = stack
    spec = {"name": "channels", "steps": [
        {"name": "a", "capability": "demo", "action": "echo",
         "params": {"v": 1}, "emit": {"channel": "notes", "merge": "append"}},
        {"name": "b", "capability": "demo", "action": "echo",
         "params": {"v": 2}, "emit": {"channel": "notes", "merge": "append"}},
        {"name": "join", "capability": "demo", "action": "echo",
         "params": {"all": "${channels.notes}"}, "depends_on": ["a", "b"]},
    ]}
    snap = ca.coord.wait(ca.coord.start(spec, {})["run_id"], timeout=15)
    assert snap["state"] == "succeeded"
    assert snap["steps"]["join"]["result"] == {
        "got": {"all": [{"got": {"v": 1}}, {"got": {"v": 2}}]}}


def test_channels_bad_merge_fails_step(stack):
    """merge 配错（对字符串用 sum）→ emit_error 失败，不静默吞。"""
    _, _, _, ca, _ = stack
    spec = {"name": "badmerge", "steps": [
        {"name": "a", "capability": "demo", "action": "consume",
         "params": {"input": "text"},
         "emit": {"channel": "n", "path": "input", "merge": "sum"}},
    ]}
    snap = ca.coord.wait(ca.coord.start(spec, {})["run_id"], timeout=15)
    assert snap["state"] == "failed"
    assert snap["steps"]["a"]["error"]["code"] == "emit_error"


def test_foreach_fanout_collects_ordered_results(stack):
    """foreach 按列表扇出成 N 个并行任务，全部完成后按序收结果。"""
    _, _, _, ca, _ = stack
    spec = {"name": "fan", "steps": [
        {"name": "each", "capability": "demo", "action": "echo",
         "foreach": {"from": "${params.items}", "as": "item"},
         "params": {"v": "${item}"}, "timeout_s": 10,
         "emit": {"channel": "all", "merge": "append"}},
        {"name": "join", "capability": "demo", "action": "echo",
         "params": {"all": "${channels.all}"}, "depends_on": ["each"]},
    ]}
    snap = ca.coord.wait(ca.coord.start(spec, {"items": [1, 2, 3]})["run_id"], timeout=20)
    assert snap["state"] == "succeeded"
    each = snap["steps"]["each"]
    assert each["state"] == "succeeded" and len(each["items"]) == 3
    assert [r["got"]["v"] for r in each["result"]] == [1, 2, 3]
    assert [r["got"]["v"] for r in snap["steps"]["join"]["result"]["got"]["all"]] == [1, 2, 3]


def test_resume_skips_succeeded_steps(stack):
    """续跑只补未完成的：已成功步骤不重跑（task_id 不变）。"""
    _, _, _, ca, _ = stack
    from multi_agent.pipelines import PIPELINES
    spec = {"name": "resume_case", "steps": [
        {"name": "first", "capability": "demo", "action": "echo", "params": {"v": 1}},
        {"name": "boom", "capability": "demo", "action": "boom",
         "params": {}, "depends_on": ["first"]},
    ]}
    PIPELINES[spec["name"]] = spec
    try:
        run = ca.coord.start(spec, {})
        snap = ca.coord.wait(run["run_id"], timeout=15)
        assert snap["state"] == "failed"
        first_task = snap["steps"]["first"]["task_id"]
        assert first_task

        again = ca.coord.resume(run["run_id"])
        snap2 = ca.coord.wait(again["run_id"], timeout=15)
        assert snap2["steps"]["first"]["task_id"] == first_task   # 没重跑
        assert snap2["steps"]["first"]["state"] == "succeeded"
        assert snap2["steps"]["boom"].get("resumed") is True
        assert snap2["steps"]["boom"]["state"] == "failed"
    finally:
        PIPELINES.pop(spec["name"], None)
