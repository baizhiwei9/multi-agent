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
                           "actions": ["start", "status", "runs", "resume", "notify"]}],
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


def test_resume_foreach_uses_fresh_idempotency_keys(stack):
    """resume 重跑 foreach：子任务要换新幂等键（`:r{n}`），否则命中旧失败任务=没重跑。

    幂等键是 `run_id:step:idx{suffix}`：不带 suffix 时第二次提交会被 Hub 按幂等键
    直接返回上一次的失败任务，task_id 不变、结果仍是旧的失败回执。
    """
    srv, _, _, ca, _ = stack
    from multi_agent.pipelines import PIPELINES

    calls = []

    def _run(p, c):
        calls.append(p.get("item"))
        if len(calls) <= 2:            # 第一轮两个分片都失败，续跑时才成功
            raise RuntimeError("flaky")
        return {"item": p.get("item")}

    rt = AgentRuntime(srv.url, "flaky-agent",
                      [{"name": "flaky", "actions": ["run"]}],
                      token=TOKEN, concurrency=1, poll_wait=1)
    rt.add_handler("flaky", "run", _run)
    rt.start()
    spec = {"name": "resume_fan", "steps": [
        {"name": "each", "capability": "flaky", "action": "run",
         "foreach": {"from": "${params.items}", "as": "item"},
         "params": {"item": "${item}"}, "timeout_s": 10},
    ]}
    PIPELINES[spec["name"]] = spec
    try:
        run = ca.coord.start(spec, {"items": ["a", "b"]})
        snap = ca.coord.wait(run["run_id"], timeout=20)
        assert snap["state"] == "failed"
        assert snap["steps"]["each"]["error"]["code"] == "fanout_failed"
        first_ids = {d["task_id"] for d in snap["steps"]["each"]["items"]}

        again = ca.coord.resume(run["run_id"])
        snap2 = ca.coord.wait(again["run_id"], timeout=20)
        assert snap2["state"] == "succeeded"
        assert snap2["resume_count"] == 1
        second_ids = {d["task_id"] for d in snap2["steps"]["each"]["items"]}
        assert first_ids and second_ids.isdisjoint(first_ids)
        assert [r["item"] for r in snap2["steps"]["each"]["result"]] == ["a", "b"]
    finally:
        PIPELINES.pop(spec["name"], None)
        rt.stop()


def test_foreach_bad_config_rejected_before_run(stack):
    """foreach 配错（max 非整数）要在建 run 之前就拒绝，不能等到执行线程里抛。"""
    srv, _, _, ca, _ = stack
    spec = {"name": "bad_foreach", "steps": [
        {"name": "each", "capability": "demo", "action": "echo",
         "foreach": {"from": "${params.items}", "max": 1.5}, "params": {}},
    ]}
    before = len(ca.coord.list_runs(limit=100))
    with pytest.raises(ValueError):
        ca.coord.start(spec, {"items": ["a"]})
    assert len(ca.coord.list_runs(limit=100)) == before      # 没留下卡在 running 的 run


def test_resume_rejects_running_but_force_allows():
    """崩溃遗留的 running 只有 force=True 才能续（正常在跑的直接拒绝）。"""
    from multi_agent.coordinator import Coordinator
    from multi_agent.pipelines import PIPELINES

    c = Coordinator("http://127.0.0.1:1", token="t")
    c._runs["r_x"] = {"run_id": "r_x", "pipeline": "empty_case", "params": {},
                      "state": "running", "steps": {}, "channels": {},
                      "created": 0, "updated": 0, "audit": {}}
    PIPELINES["empty_case"] = {"name": "empty_case", "steps": []}
    try:
        with pytest.raises(ValueError):
            c.resume("r_x")
        snap = c.resume("r_x", force=True)
        assert snap["resume_count"] == 1
    finally:
        PIPELINES.pop("empty_case", None)


# ───────────────────────────────────────────── 条件语义（列表长度 / 插值 / 类型不符）
def test_when_condition_uses_list_length(stack):
    """`result.events > 0` 这种写法：list 与数字比要按 len() 比，不能静默 False。"""
    _, _, _, ca, _ = stack
    spec = {"name": "when_len", "steps": [
        {"name": "p", "capability": "demo", "action": "produce", "params": {"n": 2}},
        {"name": "c", "capability": "demo", "action": "consume",
         "params": {"input": "${steps.p.result.items}"}, "depends_on": ["p"],
         "when": {"step": "p", "path": "result.items", "op": "gt", "value": 0}},
    ]}
    run = ca.coord.start(spec, {})
    snap = ca.coord.wait(run["run_id"], timeout=15)
    assert snap["steps"]["c"]["state"] == "succeeded"       # 有 2 条 → 条件成立


def test_when_empty_list_skips(stack):
    _, _, _, ca, _ = stack
    spec = {"name": "when_empty", "steps": [
        {"name": "p", "capability": "demo", "action": "produce", "params": {"n": 0}},
        {"name": "c", "capability": "demo", "action": "consume", "params": {},
         "depends_on": ["p"],
         "when": {"step": "p", "path": "result.items", "op": "gt", "value": 0}},
    ]}
    run = ca.coord.start(spec, {})
    snap = ca.coord.wait(run["run_id"], timeout=15)
    assert snap["steps"]["c"]["state"] == "skipped"


def test_when_value_supports_params_interpolation(stack):
    """`value: "${params.min_events}"` 要按 ctx 插值，否则条件恒 False。"""
    _, _, _, ca, _ = stack
    spec = {"name": "when_params", "steps": [
        {"name": "p", "capability": "demo", "action": "produce", "params": {"n": 2}},
        {"name": "c", "capability": "demo", "action": "consume", "params": {},
         "depends_on": ["p"],
         "when": {"step": "p", "path": "result.items", "op": "gt",
                  "value": "${params.min_events}"}},
    ]}
    low = ca.coord.start(spec, {"min_events": 1})
    assert ca.coord.wait(low["run_id"], timeout=15)["steps"]["c"]["state"] == "succeeded"
    high = ca.coord.start(spec, {"min_events": 5})
    assert ca.coord.wait(high["run_id"], timeout=15)["steps"]["c"]["state"] == "skipped"


def test_when_type_mismatch_is_recorded(stack):
    """类型不可比不能再"一声不响地 False"，至少把原因记到 when_error。"""
    _, _, _, ca, _ = stack
    spec = {"name": "when_type", "steps": [
        {"name": "p", "capability": "demo", "action": "produce", "params": {"n": 2}},
        {"name": "c", "capability": "demo", "action": "consume", "params": {},
         "depends_on": ["p"],
         "when": {"step": "p", "path": "result.items", "op": "gt", "value": "abc"}},
    ]}
    run = ca.coord.start(spec, {})
    snap = ca.coord.wait(run["run_id"], timeout=15)
    assert snap["steps"]["c"]["state"] == "skipped"
    assert "类型不可比" in snap["steps"]["c"]["when_error"]


# ───────────────────────────────────────────── resume 语义
def test_resume_reruns_steps_skipped_by_upstream_failure(stack):
    """上游失败连坐跳过的步骤，续跑时必须重排——否则 run 假成功、这条链一步没跑。"""
    srv, _, _, ca, _ = stack
    from multi_agent.pipelines import PIPELINES

    calls = {"n": 0}

    def _once(p, c):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first attempt fails")
        return {"ok": True}

    rt = AgentRuntime(srv.url, "once-agent", [{"name": "once", "actions": ["step"]}],
                      token=TOKEN, concurrency=1, poll_wait=1)
    rt.add_handler("once", "step", _once)
    rt.start()
    spec = {"name": "resume_chain", "steps": [
        {"name": "a", "capability": "once", "action": "step", "params": {}},
        {"name": "b", "capability": "demo", "action": "echo", "params": {},
         "depends_on": ["a"]},
    ]}
    PIPELINES[spec["name"]] = spec
    try:
        run = ca.coord.start(spec, {})
        snap = ca.coord.wait(run["run_id"], timeout=15)
        assert snap["state"] == "failed"
        assert snap["steps"]["b"]["state"] == "skipped"
        assert snap["steps"]["b"]["error"]["code"] == "upstream_failed"

        again = ca.coord.resume(run["run_id"])
        snap2 = ca.coord.wait(again["run_id"], timeout=15)
        assert snap2["state"] == "succeeded"
        assert snap2["steps"]["b"]["state"] == "succeeded"   # 真的执行了
    finally:
        PIPELINES.pop(spec["name"], None)
        rt.stop()


def test_resume_keeps_when_skipped_steps(stack):
    """when 条件跳过的步骤属于"本来就该跳"，续跑不该把它拉起来。"""
    _, _, _, ca, _ = stack
    from multi_agent.pipelines import PIPELINES

    spec = {"name": "resume_when", "steps": [
        {"name": "p", "capability": "demo", "action": "produce", "params": {"n": 1}},
        {"name": "c", "capability": "demo", "action": "consume", "params": {},
         "depends_on": ["p"],
         "when": {"step": "p", "path": "result.n", "op": "gt", "value": 99}},
        {"name": "boom", "capability": "demo", "action": "boom", "params": {},
         "depends_on": ["c"]},
    ]}
    PIPELINES[spec["name"]] = spec
    try:
        run = ca.coord.start(spec, {})
        snap = ca.coord.wait(run["run_id"], timeout=15)
        assert snap["steps"]["c"]["state"] == "skipped"
        again = ca.coord.resume(run["run_id"])
        snap2 = ca.coord.wait(again["run_id"], timeout=15)
        assert snap2["steps"]["c"]["state"] == "skipped"      # 条件跳过不重做
        assert snap2["steps"]["boom"]["state"] == "skipped"
    finally:
        PIPELINES.pop(spec["name"], None)


def test_foreach_resume_reuses_succeeded_shards(stack):
    """部分失败的扇出续跑只重提失败分片，已成功的分片不重复执行副作用。"""
    srv, _, _, ca, _ = stack
    from multi_agent.pipelines import PIPELINES

    attempts = {}

    def _run(p, c):
        it = p.get("item")
        attempts[it] = attempts.get(it, 0) + 1
        if it == "b" and attempts[it] == 1:
            raise RuntimeError("b failed once")
        return {"item": it}

    rt = AgentRuntime(srv.url, "shard-agent", [{"name": "shard", "actions": ["run"]}],
                      token=TOKEN, concurrency=1, poll_wait=1)
    rt.add_handler("shard", "run", _run)
    rt.start()
    spec = {"name": "resume_shard", "steps": [
        {"name": "each", "capability": "shard", "action": "run",
         "foreach": {"from": "${params.items}", "as": "item"},
         "params": {"item": "${item}"}, "timeout_s": 10},
    ]}
    PIPELINES[spec["name"]] = spec
    try:
        run = ca.coord.start(spec, {"items": ["a", "b"]})
        snap = ca.coord.wait(run["run_id"], timeout=20)
        assert snap["state"] == "failed"
        assert attempts == {"a": 1, "b": 1}

        again = ca.coord.resume(run["run_id"])
        snap2 = ca.coord.wait(again["run_id"], timeout=20)
        assert snap2["state"] == "succeeded"
        assert attempts == {"a": 1, "b": 2}          # a 没被重放
        assert [r["item"] for r in snap2["steps"]["each"]["result"]] == ["a", "b"]
        assert snap2["steps"]["each"]["items"][0]["reused"] is True
    finally:
        PIPELINES.pop(spec["name"], None)
        rt.stop()


def test_resume_force_passthrough_via_hub(stack):
    """CLI 的 --force 走 Hub → CoordinatorAgent → Coordinator.resume，force 不能丢。"""
    srv, _, _, ca, _ = stack
    from multi_agent.pipelines import PIPELINES

    PIPELINES["empty_case"] = {"name": "empty_case", "steps": []}
    ca.coord._runs["r_stuck"] = {"run_id": "r_stuck", "pipeline": "empty_case",
                                 "params": {}, "state": "running", "steps": {},
                                 "channels": {}, "created": 0, "updated": 0,
                                 "audit": {}}
    c = HubClient(srv.url, token=TOKEN)
    try:
        without = c.submit("coordinator", "resume", {"run_id": "r_stuck"},
                           wait=True, timeout_s=10)
        assert without["state"] == "failed"          # running 且没 force → 拒绝
        with_force = c.submit("coordinator", "resume",
                              {"run_id": "r_stuck", "force": True},
                              wait=True, timeout_s=10)
        assert with_force["state"] != "failed"       # force 真的透传下去了
        assert ca.coord.wait("r_stuck", timeout=15)["state"] == "succeeded"
    finally:
        PIPELINES.pop("empty_case", None)


def test_degraded_result_records_warning(stack):
    """步骤"跑完但没拿到东西"要留告警，不能再当纯成功静默。"""
    srv, _, _, ca, _ = stack
    rt = AgentRuntime(srv.url, "degraded-agent",
                      [{"name": "degr", "actions": ["run"]}],
                      token=TOKEN, concurrency=1, poll_wait=1)
    rt.add_handler("degr", "run",
                   lambda p, c: {"degraded": True, "reason": "没有新闻源", "events": []})
    rt.start()
    try:
        spec = {"name": "degr_case", "steps": [
            {"name": "n", "capability": "degr", "action": "run", "params": {}}]}
        run = ca.coord.start(spec, {})
        snap = ca.coord.wait(run["run_id"], timeout=15)
        assert snap["steps"]["n"]["state"] == "succeeded"
        assert any(w["step"] == "n" and "没有新闻源" in w["reason"]
                   for w in snap.get("warnings") or [])
    finally:
        rt.stop()


def test_when_path_supports_params_interpolation(stack):
    """`when.path` 里的 `${params.x}` 必须展开，否则参数化路径会静默判假。"""
    _, _, _, ca, _ = stack
    spec = {"name": "when_path", "steps": [
        {"name": "p", "capability": "demo", "action": "echo",
         "params": {"by_commodity": {"cu": {"major": 2}, "ag": {"major": 0}}}},
        {"name": "c", "capability": "demo", "action": "consume", "params": {},
         "depends_on": ["p"],
         "when": {"step": "p",
                  "path": "result.got.by_commodity.${params.commodity}.major",
                  "op": "gt", "value": 0}},
    ]}
    hit = ca.coord.start(spec, {"commodity": "cu"})
    assert ca.coord.wait(hit["run_id"], timeout=15)["steps"]["c"]["state"] == "succeeded"
    miss = ca.coord.start(spec, {"commodity": "ag"})
    snap = ca.coord.wait(miss["run_id"], timeout=15)
    assert snap["steps"]["c"]["state"] == "skipped"
    assert snap["steps"]["c"].get("when_error") is None      # 路径存在，只是值为 0


def test_when_missing_path_records_when_error(stack):
    """路径拼错/取不到值时要和"条件本来为假"区分开。"""
    _, _, _, ca, _ = stack
    spec = {"name": "when_missing", "steps": [
        {"name": "p", "capability": "demo", "action": "produce", "params": {"n": 2}},
        {"name": "c", "capability": "demo", "action": "consume", "params": {},
         "depends_on": ["p"],
         "when": {"step": "p", "path": "result.no_such_key", "op": "gt", "value": 0}},
    ]}
    run = ca.coord.start(spec, {})
    snap = ca.coord.wait(run["run_id"], timeout=15)
    assert snap["steps"]["c"]["state"] == "skipped"
    assert "取不到值" in snap["steps"]["c"]["when_error"]


def test_run_on_skipped_list_requires_all_skipped_deps(stack):
    """列表写法只对列出的依赖放行；别的依赖被跳过时仍必须拦住。"""
    _, _, _, ca, _ = stack
    spec = {"name": "ros_list", "steps": [
        {"name": "ok", "capability": "demo", "action": "produce", "params": {"n": 1}},
        {"name": "empty", "capability": "demo", "action": "produce", "params": {"n": 0}},
        {"name": "gate", "capability": "demo", "action": "consume", "params": {},
         "depends_on": ["empty"],
         "when": {"step": "empty", "path": "result.n", "op": "gt", "value": 0}},
        {"name": "allowed", "capability": "demo", "action": "consume", "params": {},
         "depends_on": ["ok", "gate"], "run_on_skipped": ["gate"]},
        {"name": "blocked", "capability": "demo", "action": "consume", "params": {},
         "depends_on": ["ok", "gate"], "run_on_skipped": ["ok"]},
    ]}
    run = ca.coord.start(spec, {})
    snap = ca.coord.wait(run["run_id"], timeout=15)
    assert snap["steps"]["gate"]["state"] == "skipped"
    assert snap["steps"]["allowed"]["state"] == "succeeded"
    assert snap["steps"]["blocked"]["state"] == "skipped"


def test_empty_news_day_still_produces_report(stack):
    """空事件日：新闻/审校链跳过，但报表、审计、终审仍要跑完。"""
    _, _, _, ca, _ = stack
    spec = {"name": "empty_news_report", "steps": [
        {"name": "store_balance", "capability": "demo", "action": "produce",
         "params": {"n": 1}},
        {"name": "news", "capability": "demo", "action": "produce", "params": {"n": 0}},
        {"name": "store_news", "capability": "demo", "action": "consume",
         "params": {}, "depends_on": ["news"]},
        {"name": "vet_news", "capability": "demo", "action": "consume", "params": {},
         "depends_on": ["news"],
         "when": {"step": "news", "path": "result.items", "op": "gt", "value": 0}},
        {"name": "shock", "capability": "demo", "action": "consume", "params": {},
         "depends_on": ["vet_news"]},
        {"name": "review", "capability": "demo", "action": "consume", "params": {},
         "depends_on": ["shock"]},
        {"name": "report", "capability": "demo", "action": "consume", "params": {},
         "depends_on": ["store_balance", "store_news", "review"],
         "run_on_skipped": ["review"]},
        {"name": "audit", "capability": "demo", "action": "consume", "params": {},
         "depends_on": ["report"]},
        {"name": "final", "capability": "demo", "action": "consume", "params": {},
         "depends_on": ["audit"]},
    ]}
    run = ca.coord.start(spec, {})
    snap = ca.coord.wait(run["run_id"], timeout=15)
    for name in ("vet_news", "shock", "review"):
        assert snap["steps"][name]["state"] == "skipped"
    for name in ("report", "audit", "final"):
        assert snap["steps"][name]["state"] == "succeeded"
    assert snap["state"] == "succeeded"


def test_futures_balance_report_is_decoupled_from_review():
    """真实流水线配置要和上面的空事件语义一致，防止改回去。"""
    from multi_agent.projects.futures_balance import BALANCE_PIPELINE

    steps = {s["name"]: s for s in BALANCE_PIPELINE["steps"]}
    assert steps["report"]["depends_on"] == ["store_balance", "store_news", "review"]
    assert steps["report"]["run_on_skipped"] == ["review"]
