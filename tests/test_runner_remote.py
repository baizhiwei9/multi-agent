"""Runner / RemoteCoordinator 测试：远程执行 + 角色分配 + 安全边界。

重点（都是实跑出来的行为，不是设计意图）：
1. 白名单**默认拒绝**：没配 AGENT_RUN_ALLOW 时一个都跑不了；
2. 路径逃逸（`../`、软链接）必须被拦；
3. 超时真的 kill 整个进程组，不是只标记；
4. 被拒是**正常响应**（ok=True + denied=True），不是任务失败 —— 否则调用方看不到理由；
5. 同一角色不能同时绑两个 agent（双写会互相覆盖）；
6. 点名派活真的只到指定 agent，不会 round-robin 给别的。
"""
import json
import os
import tempfile
import threading
import time

import pytest

from multi_agent.client import AgentRuntime, HubClient, HubError
from multi_agent.hub import Hub
from multi_agent.integrations import runner as R
from multi_agent.remote import (BindingConflict, RemoteCoordinator, RoleBinding,
                                plan_roles)
from multi_agent.tcp import TCPClient, TCPHubServer

TOKEN = "test-" + "token"


# ───────────────────────────────────────────── 通用夹具
@pytest.fixture
def jobs_dir():
    d = tempfile.mkdtemp(prefix="jobs-")
    with open(os.path.join(d, "ok.py"), "w") as fh:
        fh.write("import json,sys\n"
                 "print(json.dumps({'argv': sys.argv[1:], 'cwd': __import__('os').getcwd()}))\n")
    with open(os.path.join(d, "fail.py"), "w") as fh:
        fh.write("import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n")
    with open(os.path.join(d, "slow.py"), "w") as fh:
        fh.write("import time\ntime.sleep(30)\n")
    with open(os.path.join(d, "big.py"), "w") as fh:
        fh.write("print('x' * 100000)\n")
    return d


def _clean_env(monkeypatch, allow=None):
    if allow is None:
        monkeypatch.delenv(R.ALLOW_ENV, raising=False)
    else:
        monkeypatch.setenv(R.ALLOW_ENV, allow)


# ───────────────────────────────────────────── 白名单（默认拒绝）
def test_no_allowlist_denies_everything(monkeypatch, jobs_dir):
    _clean_env(monkeypatch)
    res = R.run_action("py", {"script": os.path.join(jobs_dir, "ok.py")}, agent_id="t1")
    assert res["denied"] is True
    assert "未配置" in res["reason"] or "白名单" in res["reason"]


def test_allowlist_permits_inside(monkeypatch, jobs_dir):
    _clean_env(monkeypatch, jobs_dir)
    res = R.run_action("py", {"script": os.path.join(jobs_dir, "ok.py"),
                              "argv": ["--x", "1"]}, agent_id="t2")
    assert not res.get("denied"), res
    assert res["ok"] and res["returncode"] == 0
    assert json.loads(res["stdout"])["argv"] == ["--x", "1"]


def test_outside_path_denied(monkeypatch, jobs_dir):
    _clean_env(monkeypatch, jobs_dir)
    outside = tempfile.mktemp(suffix=".py")
    with open(outside, "w") as fh:
        fh.write("print('nope')")
    res = R.run_action("py", {"script": outside}, agent_id="t3")
    assert res["denied"] is True
    assert "白名单" in res["reason"]


def test_path_traversal_denied(monkeypatch, jobs_dir):
    _clean_env(monkeypatch, jobs_dir)
    escape = os.path.join(jobs_dir, "..", os.path.basename(tempfile.mktemp()))
    with open(escape, "w") as fh:
        fh.write("print('nope')")
    res = R.run_action("py", {"script": os.path.join(jobs_dir, "..",
                                                     os.path.basename(escape))},
                       agent_id="t4")
    assert res["denied"] is True


def test_symlink_escape_denied(monkeypatch, jobs_dir):
    _clean_env(monkeypatch, jobs_dir)
    target = tempfile.mktemp(suffix=".py")
    with open(target, "w") as fh:
        fh.write("print('nope')")
    link = os.path.join(jobs_dir, "link.py")
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("不支持创建软链接")
    res = R.run_action("py", {"script": link}, agent_id="t5")
    assert res["denied"] is True, "软链接必须按 realpath 判定"


def test_missing_file_denied(monkeypatch, jobs_dir):
    _clean_env(monkeypatch, jobs_dir)
    res = R.run_action("py", {"script": os.path.join(jobs_dir, "nope.py")}, agent_id="t6")
    assert res["denied"] is True and "不存在" in res["reason"]


def test_nonzero_exit_reported_not_denied(monkeypatch, jobs_dir):
    _clean_env(monkeypatch, jobs_dir)
    res = R.run_action("py", {"script": os.path.join(jobs_dir, "fail.py")}, agent_id="t7")
    assert not res.get("denied")
    assert res["ok"] is False and res["returncode"] == 3
    assert "boom" in res["stderr"]


def test_timeout_kills_process(monkeypatch, jobs_dir):
    _clean_env(monkeypatch, jobs_dir)
    t0 = time.time()
    res = R.run_action("py", {"script": os.path.join(jobs_dir, "slow.py"),
                              "timeout_s": 0.5}, agent_id="t8")
    elapsed = time.time() - t0
    assert res["timed_out"] is True
    assert res["ok"] is False
    assert elapsed < 8, f"超时没能及时 kill：{elapsed:.1f}s"


def test_output_truncated(monkeypatch, jobs_dir):
    _clean_env(monkeypatch, jobs_dir)
    res = R.run_action("py", {"script": os.path.join(jobs_dir, "big.py"), "timeout_s": 20},
                       agent_id="t9")
    assert res["ok"]
    assert len(res["stdout"].encode()) <= R.MAX_OUTPUT_BYTES + 64
    assert "截断" in res["stdout"]


def test_cwd_must_be_allowed(monkeypatch, jobs_dir):
    _clean_env(monkeypatch, jobs_dir)
    outside = tempfile.mkdtemp(prefix="outside-")
    res = R.run_action("py", {"script": os.path.join(jobs_dir, "ok.py"), "cwd": outside},
                       agent_id="t10")
    assert res["denied"] is True and "cwd" in res["reason"]


def test_env_passthrough(monkeypatch, jobs_dir):
    _clean_env(monkeypatch, jobs_dir)
    script = os.path.join(jobs_dir, "env.py")
    with open(script, "w") as fh:
        fh.write("import os\nprint(os.environ.get('MY_VAR', ''))\n")
    res = R.run_action("py", {"script": script, "env": {"MY_VAR": "hello"}}, agent_id="t11")
    assert res["ok"] and res["stdout"].strip() == "hello"


# ───────────────────────────────────────────── 角色分配（本地）
def test_assign_and_release(monkeypatch):
    r = R.Runner("u1")
    out = r.assign([{"name": "A", "actions": ["build_sheet"]},
                    {"name": "F", "actions": ["check_inventory"]}])
    assert set(out["assigned"]) == {"A", "F"}
    names = {x["name"] for x in r.list_roles()}
    assert names == {"A", "F"}
    rel = r.release(["A"])
    assert rel["released"] == ["A"]
    assert {x["name"] for x in r.list_roles()} == {"F"}


def test_assign_defaults_actions_to_star():
    r = R.Runner("u2")
    out = r.assign([{"name": "B"}])
    assert out["roles"][0]["actions"] == ["*"]


# ───────────────────────────────────────────── RoleBinding
def test_binding_conflict_blocks_double_bind():
    b = RoleBinding({"A": "agent-1"})
    assert b.agent_for("A") == "agent-1"
    with pytest.raises(BindingConflict):
        b.bind("A", "agent-2")


def test_binding_force_replaces():
    b = RoleBinding({"A": "agent-1"})
    out = b.bind("A", "agent-2", force=True)
    assert out["replaced"] and out["previous"] == "agent-1"
    assert b.agent_for("A") == "agent-2"


def test_plan_roles_detects_duplicates():
    out = plan_roles({"openclaw-1": ["A", "F", "E"],
                      "hermes-1": ["B", "C", "A"]})
    assert out["ok"] is False
    assert out["duplicates"][0]["role"] == "A"


def test_plan_roles_clean():
    out = plan_roles({"openclaw-1": ["A", "F", "E"],
                      "hermes-1": ["B", "C"],
                      "coze-1": ["D", "Q", "J"]})
    assert out["ok"] is True and out["duplicates"] == []


# ───────────────────────────────────────────── TCP 端到端
@pytest.fixture
def tcp_hub():
    hub = Hub(db_path=":memory:", ttl_s=120)
    srv = TCPHubServer(hub, host="127.0.0.1", port=0, token=TOKEN)
    # TCPHubServer 暴露 host/port
    srv.start_background()
    yield srv
    srv.stop()


def _tcp_agent(srv, agent_id, allowed_dir, stop):
    """最小 TCP agent（和 examples/tcp_roles_demo.py 同一条循环）。"""
    os.environ[R.ALLOW_ENV] = allowed_dir
    client = TCPClient("127.0.0.1", srv.port, token=TOKEN)
    client.call("hub.register", {
        "agent_id": agent_id,
        "roles": [{"name": "runner", "actions": R.ROLE_ACTIONS["runner"]}],
        "labels": {"agent_id": agent_id}, "ttl_s": 120})

    def _loop():
        while not stop.is_set():
            try:
                msg = client.call("hub.inbox", {"agent_id": agent_id, "wait": 0.5},
                                  timeout=5)
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
                continue
            if not msg:
                continue
            tid, cap, act = msg["task_id"], msg["capability"], msg["action"]
            params = msg.get("params") or {}
            try:
                result = R.run_action(act, params, agent_id=agent_id)
                client.call("hub.result", {"task_id": tid, "agent_id": agent_id,
                                           "ok": True, "result": result})
            except Exception as e:  # noqa: BLE001
                client.call("hub.result", {"task_id": tid, "agent_id": agent_id,
                                           "ok": False,
                                           "error": {"code": "err", "message": str(e)}})
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t


def _submit_wait(srv, capability, action, params, labels=None, timeout=30):
    c = TCPClient("127.0.0.1", srv.port, token=TOKEN)
    try:
        t = c.call("hub.submit", {"capability": capability, "action": action,
                                  "params": params, "timeout_s": timeout,
                                  "wait": False, "labels": labels or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = c.call("hub.task", {"task_id": t["id"]})
            if s.get("state") in ("succeeded", "failed", "timeout", "canceled"):
                return s
            time.sleep(0.1)
        return c.call("hub.task", {"task_id": t["id"]})
    finally:
        c.close()


def test_tcp_remote_python_execution(tcp_hub, monkeypatch, jobs_dir):
    stop = threading.Event()
    _tcp_agent(tcp_hub, "agent-x", jobs_dir, stop)
    time.sleep(0.3)
    try:
        snap = _submit_wait(tcp_hub, "runner", "py",
                            {"script": os.path.join(jobs_dir, "ok.py"),
                             "argv": ["a"]},
                            labels={"agent_id": "agent-x"})
        assert snap["state"] == "succeeded", snap
        out = json.loads(snap["result"]["stdout"])
        assert out["argv"] == ["a"]
        assert snap["result"]["returncode"] == 0
    finally:
        stop.set()


def test_tcp_denied_is_normal_response(tcp_hub, monkeypatch, jobs_dir):
    """被拒必须走 succeeded + denied，不能变成 failed（否则看不到理由）。"""
    stop = threading.Event()
    _tcp_agent(tcp_hub, "agent-y", jobs_dir, stop)
    time.sleep(0.3)
    outside = tempfile.mktemp(suffix=".py")
    with open(outside, "w") as fh:
        fh.write("print(1)")
    try:
        snap = _submit_wait(tcp_hub, "runner", "py", {"script": outside},
                            labels={"agent_id": "agent-y"})
        assert snap["state"] == "succeeded"
        assert snap["result"]["denied"] is True
        assert "白名单" in snap["result"]["reason"]
    finally:
        stop.set()


def test_tcp_labels_route_to_exact_agent(tcp_hub, monkeypatch, jobs_dir):
    """点名派活：只有被点名的 agent 收到任务。"""
    stop = threading.Event()
    _tcp_agent(tcp_hub, "agent-p", jobs_dir, stop)
    _tcp_agent(tcp_hub, "agent-q", jobs_dir, stop)
    time.sleep(0.4)
    try:
        for target in ("agent-p", "agent-q", "agent-p"):
            snap = _submit_wait(tcp_hub, "runner", "py",
                                {"script": os.path.join(jobs_dir, "ok.py")},
                                labels={"agent_id": target})
            assert snap["state"] == "succeeded"
            assert snap["agent_id"] == target
    finally:
        stop.set()


def test_tcp_assign_role_then_query(tcp_hub, monkeypatch, jobs_dir):
    stop = threading.Event()
    _tcp_agent(tcp_hub, "agent-r", jobs_dir, stop)
    time.sleep(0.3)
    try:
        snap = _submit_wait(tcp_hub, "runner", "assign",
                            {"roles": [{"name": "A", "actions": ["build_sheet"]},
                                       {"name": "F"}]},
                            labels={"agent_id": "agent-r"})
        assert snap["state"] == "succeeded"
        assert set(snap["result"]["assigned"]) == {"A", "F"}

        got = _submit_wait(tcp_hub, "runner", "roles", {},
                           labels={"agent_id": "agent-r"})
        names = {r["name"] for r in got["result"]["roles"]}
        assert names == {"A", "F"}

        rel = _submit_wait(tcp_hub, "runner", "release", {"roles": ["A"]},
                           labels={"agent_id": "agent-r"})
        assert rel["result"]["released"] == ["A"]
    finally:
        stop.set()


# ───────────────────────────────────────────── RemoteCoordinator
def test_remote_coordinator_rejects_unknown_agent(tcp_hub):
    """TCP 线缆下点一个不存在的 agent，必须当场报错而不是等超时。"""
    coord = RemoteCoordinator.from_tcp("127.0.0.1", tcp_hub.port, token=TOKEN)
    with pytest.raises(HubError):
        coord.submit_to("ghost", "runner", "py", {"script": "/tmp/x.py"})


def test_remote_assign_writes_binding(monkeypatch):
    b = RoleBinding()
    coord = RemoteCoordinator("http://127.0.0.1:1", token=TOKEN, binding=b)
    out = coord.assign_role("agent-1", [{"name": "A"}], force=False)
    # 远端会失败（没起 Hub），但绑定表应该已经记下
    assert out["binds"][0]["role"] == "A"
    assert b.agent_for("A") == "agent-1"
    assert out["ok"] is False          # 远端确实失败了，如实上报


def test_health_flags_duplicate_roles(monkeypatch):
    b = RoleBinding({"A": "agent-1"})
    coord = RemoteCoordinator("http://127.0.0.1:1", token=TOKEN, binding=b)

    class _FakeClient:
        def _request(self, method, path, body=None, timeout=None):
            return {"agents": [
                {"agent_id": "agent-1", "alive": True,
                 "roles": [{"name": "A", "actions": ["*"]}]},
                {"agent_id": "agent-2", "alive": True,
                 "roles": [{"name": "A", "actions": ["*"]}]},
            ]}

    coord.client = _FakeClient()
    h = coord.health()
    assert h["ok"] is False
    assert h["duplicate_roles"][0]["role"] == "A"
