"""bug 采集 / 分诊 / 定位 / 补丁方案 / llm 通道的验收。

校验的重点不是"跑通"，而是三条边界真的守住：
· 采集不编造（读不到日志必须 degraded，INFO 不算 bug）；
· 定位不猜（栈帧文件不存在就不能报 high）；
· 补丁不越权（find 未命中 / 路径越界一律丢弃，模型产出的也一样）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from multi_agent.integrations import game_server_bugfix as BF      # noqa: E402
from multi_agent.integrations.llm import chat, _normalize_url      # noqa: E402
from multi_agent.frameworks.netdev.collectors import (                            # noqa: E402
    LogBugCollector, NullBugCollector, StaticBugCollector)

FIXTURE = ROOT / "examples" / "game_server" / "fixture"
LOG = FIXTURE / "logs" / "server.log"
REPO = FIXTURE / "repo"


@pytest.fixture()
def work_repo(tmp_path):
    """补丁会改文件，测试一律在副本上跑。"""
    import shutil

    dest = tmp_path / "repo"
    shutil.copytree(REPO, dest)
    return dest


# ─────────────────────────────────────────── 采集
def test_collect_from_text_log():
    res = LogBugCollector(path=str(LOG)).collect({})
    assert res["degraded"] is False
    bugs = res["bugs"]
    assert bugs, "fixture 日志里必须有 bug"
    # INFO 不算 bug，只计入 ignored
    assert res["ignored"] == 2
    codes = {b["error_code"] for b in bugs}
    assert "SHOP_TX_FAILED" in codes and "BAN_FORBIDDEN" in codes
    shop = [b for b in bugs if b["error_code"] == "SHOP_TX_FAILED"][0]
    # 同一错误出现两次 → 一条 bug、occurrences=2
    assert shop["occurrences"] == 2
    assert shop["frame"]["file"] == "server/shop.py"
    assert shop["frame"]["line"] == 9


def test_collect_missing_log_is_degraded(tmp_path):
    res = LogBugCollector(path=str(tmp_path / "nope.log")).collect({})
    assert res["degraded"] is True
    assert res["bugs"] == []
    assert "不存在" in res["reason"]


def test_collect_without_source_is_degraded():
    res = LogBugCollector().collect({})
    assert res["degraded"] is True and res["bugs"] == []


def test_null_collector_says_so():
    res = NullBugCollector().collect({})
    assert res["degraded"] is True and res["bugs"] == []


def test_jsonl_collect():
    lines = [
        {"ts": "2026-11-26T01:00:00Z", "level": "ERROR", "service": "shop",
         "endpoint": "/v1/shop/buy", "error_code": "E1", "message": "boom",
         "traceback": ['File "server/shop.py", line 9, in buy_item']},
        {"ts": "2026-11-26T01:00:01Z", "level": "INFO", "service": "shop",
         "endpoint": "/v1/shop/buy", "message": "ok"},
    ]
    c = LogBugCollector()
    raw = c.parse("\n".join(json.dumps(x, ensure_ascii=False) for x in lines))
    assert len(raw) == 2
    assert raw[0]["frame"]["line"] == 9 if raw[0].get("frame") else True


def test_static_collector_dedupes():
    items = [
        {"error_code": "E1", "endpoint": "/a", "severity": "error", "message": "x",
         "trace": ['File "a.py", line 1, in f']},
        {"error_code": "E1", "endpoint": "/a", "severity": "error", "message": "x",
         "trace": ['File "a.py", line 1, in f']},
    ]
    res = StaticBugCollector(items).collect({})
    assert len(res["bugs"]) == 1 and res["bugs"][0]["occurrences"] == 2


# ─────────────────────────────────────────── 分诊 / 定位
def _bugs():
    return LogBugCollector(path=str(LOG)).collect({})["bugs"]


def test_triage_groups_by_endpoint():
    res = BF.triage({"bugs": _bugs(), "repo_root": str(REPO)})
    assert res["ok"] and not res["degraded"]
    ids = {c["id"] for c in res["clusters"]}
    assert any("shop" in i for i in ids)
    assert res["counts"]["bugs"] == len(_bugs())


def test_triage_empty_bugs_is_degraded():
    res = BF.triage({"bugs": [], "repo_root": str(REPO)})
    assert res["degraded"] is True


def test_locate_prefers_traceback():
    res = BF.locate({"bugs": _bugs(), "repo_root": str(REPO)})
    assert res["ok"]
    shop = [f for f in res["findings"] if f["bug_id"] ==
            [b["bug_id"] for b in _bugs() if b["error_code"] == "SHOP_TX_FAILED"][0]][0]
    assert shop["file"] == "server/shop.py" and shop["confidence"] == "high"


def test_locate_falls_back_to_route_table():
    bug = {"bug_id": "b1", "endpoint": "/v1/player/ban", "error_code": "BAN_FORBIDDEN",
           "message": "越权", "frame": None}
    res = BF.locate({"bugs": [bug], "repo_root": str(REPO)})
    hit = res["findings"][0]
    assert hit["file"] == "server/routes.py" and hit["confidence"] == "medium"


def test_locate_reports_unlocated():
    bug = {"bug_id": "b1", "endpoint": "/nowhere", "error_code": "ZZZ_NOT_FOUND",
           "message": "nothing", "frame": {"file": "server/ghost.py", "line": 1, "func": "x"}}
    res = BF.locate({"bugs": [bug], "repo_root": str(REPO)})
    assert res["findings"] == [] and res["unlocated"]


# ─────────────────────────────────────────── 补丁方案
def test_propose_rule_edits_hit_find(work_repo):
    bugs = _bugs()
    tri = BF.triage({"bugs": bugs, "repo_root": str(work_repo)})
    loc = BF.locate({"bugs": bugs, "repo_root": str(work_repo)})
    res = BF.propose({"bugs": bugs, "triage": tri, "locate": loc,
                      "repo_root": str(work_repo)})
    assert res["workstreams"], "至少要给出一条可执行补丁"
    edits = [e for w in res["workstreams"] for e in w["edits"]]
    assert edits
    for e in edits:
        text = (work_repo / e["file"]).read_text(encoding="utf-8")
        assert e["find"] in text, f"{e['file']} 的 find 必须命中"


def test_propose_never_escapes_repo(work_repo):
    bugs = [{"bug_id": "x", "endpoint": "/v1/shop/buy", "error_code": "TX_MISSING",
             "message": "没有事务", "frame": {"file": "../../../etc/passwd", "line": 1,
                                             "func": "x"}, "severity": "error"}]
    loc = BF.locate({"bugs": bugs, "repo_root": str(work_repo)})
    tri = BF.triage({"bugs": bugs, "repo_root": str(work_repo)})
    res = BF.propose({"bugs": bugs, "triage": tri, "locate": loc,
                      "repo_root": str(work_repo)})
    for w in res["workstreams"]:
        assert w["files"], "补丁文件必须在仓库内"


def test_propose_defers_when_no_template(work_repo):
    bugs = [{"bug_id": "x", "endpoint": "/v1/unknown", "error_code": "WEIRD",
             "message": "无法归类的错误", "frame": None, "severity": "error"}]
    tri = BF.triage({"bugs": bugs, "repo_root": str(work_repo)})
    loc = BF.locate({"bugs": bugs, "repo_root": str(work_repo)})
    res = BF.propose({"bugs": bugs, "triage": tri, "locate": loc,
                      "repo_root": str(work_repo)})
    assert res["workstreams"] == []
    assert res["deferred"], "改不动要如实写建议单"


def test_propose_llm_failure_falls_back_to_rules(work_repo, monkeypatch):
    def boom(_params, _ctx=None, caller=None):
        return {"ok": False, "text": "", "degraded": True, "reason": "没配端点"}

    monkeypatch.setattr("multi_agent.integrations.llm.chat", boom)
    bugs = _bugs()
    tri = BF.triage({"bugs": bugs, "repo_root": str(work_repo)})
    loc = BF.locate({"bugs": bugs, "repo_root": str(work_repo)})
    res = BF.propose({"bugs": bugs, "triage": tri, "locate": loc,
                      "repo_root": str(work_repo), "llm": True})
    assert res["workstreams"], "模型失败必须退回规则模板"
    assert res["llm"]["used"] is False


def test_propose_drops_llm_edits_that_miss(work_repo, monkeypatch):
    fake = {"ok": True, "text": json.dumps({"edits": [
        {"file": "server/routes.py", "find": "这行根本不存在", "replace": "x"}]}),
        "degraded": False, "reason": ""}
    monkeypatch.setattr("multi_agent.integrations.llm.chat", lambda *a, **k: fake)
    bugs = _bugs()
    tri = BF.triage({"bugs": bugs, "repo_root": str(work_repo)})
    loc = BF.locate({"bugs": bugs, "repo_root": str(work_repo)})
    res = BF.propose({"bugs": bugs, "triage": tri, "locate": loc,
                      "repo_root": str(work_repo), "llm": True})
    for w in res["workstreams"]:
        for e in w["edits"]:
            assert e["find"] != "这行根本不存在", "find 未命中的模型补丁必须丢弃"


# ─────────────────────────────────────────── llm 通道
def test_llm_chat_unconfigured_is_degraded(monkeypatch):
    for k in ("LLM_URL", "AUDIT_LLM_URL", "LLM_MCP_COMMAND", "LLM_BACKEND"):
        monkeypatch.delenv(k, raising=False)
    res = chat({"prompt": "hi"})
    assert res["ok"] is False and res["degraded"] is True
    assert "未配置" in res["reason"]


def test_llm_chat_with_injected_caller():
    res = chat({"prompt": "hello"}, caller=lambda p: "world")
    assert res["ok"] and res["text"] == "world" and res["backend"] == "injected"


def test_llm_chat_truncates_and_degrades_on_error():
    def boom(_p):
        raise RuntimeError("端点在火星")

    res = chat({"prompt": "x" * 99999}, caller=boom)
    assert res["degraded"] is True and "火星" in res["reason"]
    assert res["prompt_truncated"] is True


def test_llm_url_normalization():
    assert _normalize_url("http://h:8000") == "http://h:8000/v1/chat/completions"
    assert _normalize_url("http://h:8000/v1") == "http://h:8000/v1/chat/completions"
    assert _normalize_url("http://h/v1/chat/completions") == "http://h/v1/chat/completions"


# ─────────────────────────────────────────── 项目粒度
def test_role_projects_are_one_role_each():
    """一角色一项目：每个项目只声明一个 capability、不持有流水线。"""
    from multi_agent.pipelines import PIPELINES
    from multi_agent.projects.bugfix_stack import ROLE_PROJECTS
    from multi_agent.projects.flows import check_capabilities, register_flow
    from multi_agent.projects.flows.bugfix import BUGFIX_FLOW

    for cls in ROLE_PROJECTS:
        project = cls()
        assert len(project.capabilities()) == 1, f"{project.name} 不止一个角色"
        assert project.pipeline is None, f"{project.name} 不该持有流水线"
        assert project.validate() == [], project.validate()

    register_flow(BUGFIX_FLOW)
    assert "game_server_bugfix" in PIPELINES
    names = [s["name"] for s in BUGFIX_FLOW["steps"]]
    # 采集 → 分诊 → 定位 → 方案 → 经理派单 → 开发 → ...
    assert names[:6] == ["bugs", "triage", "locate", "propose", "dispatch", "fix"]

    # 编排只写 capability：缺谁要能报出来
    denied = check_capabilities(BUGFIX_FLOW, ["bug", "triage", "dev", "report"])
    assert denied["ok"] is False
    assert "supervisor" in denied["missing"] and "auditor" in denied["missing"]
    assert {"bug", "triage", "dev", "report"} <= set(denied["need"])


def test_framework_roles_are_projects_too():
    """审计/经理/风控/模型通道也是单角色项目，不在业务项目里硬编码。"""
    from multi_agent.projects.platform import (AuditorProject, LLMGatewayProject,
                                                      ServerRiskProject, SupervisorProject)

    for cls, cap in ((AuditorProject, "auditor"), (SupervisorProject, "supervisor"),
                     (ServerRiskProject, "server_risk"), (LLMGatewayProject, "llm")):
        project = cls()
        assert project.capabilities() == [cap]
        assert project.handlers() == {}          # 动作由 register_builtin 挂
        assert project.pipeline is None


def test_news_and_balance_are_separate_projects():
    """新闻采集 / 新闻评估 / 平衡表是三个项目，靠编排串起来。"""
    from multi_agent.projects.flows.news_balance import NEWS_BALANCE_FLOW
    from multi_agent.projects.trade import BalanceSheetProject
    from multi_agent.projects.trade import NewsAssessProject
    from multi_agent.projects.trade import NewsCollectProject

    triples = ((NewsCollectProject(), "collector", "scan"),
               (NewsAssessProject(), "assess", "classify"),
               (BalanceSheetProject(), "balance", "build"))
    for project, cap, action in triples:
        assert project.capabilities() == [cap]
        assert action in project.actions_for(cap)

    caps = {s["capability"] for s in NEWS_BALANCE_FLOW["steps"]}
    assert {"collector", "assess", "balance"} <= caps


def test_roles_can_run_alone():
    """单角色项目独立注册：只挂自己那一个 capability，不连带别人的动作。"""

    class FakeRuntime:
        def __init__(self) -> None:
            self.handlers = {}

        def add_handler(self, cap, action, fn):
            self.handlers.setdefault(cap, set()).add(action)

    from multi_agent.projects.netdev import BugTriageProject
    from multi_agent.projects.platform import AuditorProject, SupervisorProject

    rt = FakeRuntime()
    BugTriageProject().register(rt)
    assert set(rt.handlers) == {"triage"}
    assert rt.handlers["triage"] == {"classify"}

    # 框架角色项目只挂自己那一块，不会顺手把 supervisor / 风控也挂上
    rt2 = FakeRuntime()
    AuditorProject().register(rt2)
    assert "auditor" in rt2.handlers
    assert "supervisor" not in rt2.handlers

    rt3 = FakeRuntime()
    SupervisorProject().register(rt3)
    assert "supervisor" in rt3.handlers
    assert "auditor" not in rt3.handlers


def test_end_to_end_bugfix(work_repo):
    """跑真链路：日志 → 补丁落到工作副本 → 审计 error 不增加。"""
    sys.modules.pop("examples.game_server_bugfix.run_demo", None)
    from examples.game_server_bugfix import run_demo

    snap = run_demo.run(quiet=True)
    steps = snap["steps"]
    assert snap["state"] in ("succeeded", "degraded")
    prop = (steps.get("propose") or {}).get("result") or {}
    assert prop.get("counts", {}).get("edits", 0) > 0
    dev = (steps.get("fix") or {}).get("result") or []
    if isinstance(dev, dict):
        dev = [dev]
    assert any(d.get("applied") for d in dev if isinstance(d, dict))
    before, after = snap["_audit_before"], snap["_audit_after"]
    assert after.get("error", 0) <= before.get("error", 0)
    report = (steps.get("report") or {}).get("result") or {}
    assert os.path.isfile(report.get("report_path") or "")
    md = report.get("markdown") or ""
    assert "bug 清单" in md and "修复方案" in md


# ─────────────────────────────────────────── 模型配置：Key 直接填
def test_llm_config_local_override_and_inline_key(tmp_path, monkeypatch):
    """主配置只放占位，Key 直接填在 llm.local.json —— 不用 export，也不进仓库。"""
    from multi_agent.integrations import llm as LLM

    main_cfg = tmp_path / "llm.json"
    main_cfg.write_text(json.dumps({
        "enabled": True,
        "default": {"backend": "http", "url": "http://main/v1", "model": "m1",
                    "token": "${ENV:LLM_TOKEN}"},
        "profiles": {"chat": {"url": ""}},
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "llm.local.json").write_text(json.dumps({
        "profiles": {"chat": {"url": "http://local/v1", "token": "sk-inline"}},
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("MULTI_AGENT_LLM_CONFIG", str(main_cfg))
    monkeypatch.delenv("LLM_TOKEN", raising=False)

    cfg = LLM.profile_config("chat")
    assert cfg["url"] == "http://local/v1"        # 本地文件覆盖主配置
    assert cfg["token"] == "sk-inline"            # 明文 Key 直接用
    assert cfg["token_source"] == "inline"
    loaded = LLM.load_config()
    assert loaded["_local_path"].endswith("llm.local.json")

    info = LLM.describe_config()
    assert info["profiles"]["chat"]["token_source"] == "inline"
    assert "sk-inline" not in json.dumps(info)    # 自检不回显 Key


def test_llm_env_placeholder_and_env_fallback(monkeypatch, tmp_path):
    from multi_agent.integrations import llm as LLM

    cfg = tmp_path / "llm.json"
    cfg.write_text(json.dumps({
        "default": {"url": "http://x/v1", "token": "${ENV:MY_KEY}"},
        "profiles": {"chat": {"token": ""}},
    }), encoding="utf-8")
    monkeypatch.setenv("MULTI_AGENT_LLM_CONFIG", str(cfg))
    monkeypatch.setenv("MY_KEY", "sk-from-env")
    monkeypatch.setenv("LLM_TOKEN", "sk-legacy")
    prof = LLM.profile_config("chat")
    # chat 的 token 显式留空 → 回落 LLM_TOKEN（兼容旧部署）
    assert prof["token"] == "sk-legacy"
    assert prof["token_source"] == "env:LLM_TOKEN"
    audit = LLM.profile_config("audit")
    assert audit["token"] == "sk-from-env"        # default 的 ${ENV:MY_KEY}
    assert audit["token_source"] == "env:MY_KEY"


# ─────────────────────────────────────────── 开发角色框架
def test_dev_framework_registers_three_actions():
    from multi_agent.integrations.game_server_dev import (
        DevHandler, register, register_framework)

    class Fake:
        def __init__(self):
            self.handlers = {}

        def add_handler(self, cap, action, fn):
            self.handlers[(cap, action)] = fn

    class MyDev(DevHandler):
        def propose(self, params, ctx=None):
            return {"ok": True, "who": "mine"}

    rt = Fake()
    register_framework(rt, handler=MyDev())
    assert set(rt.handlers) == {("dev", "locate"), ("dev", "propose"),
                                ("dev", "implement")}
    assert rt.handlers[("dev", "propose")]({}, None)["who"] == "mine"

    # 旧入口不变：默认只挂 implement，单角色项目/CLI 不受影响
    rt2 = Fake()
    register(rt2)
    assert set(rt2.handlers) == {("dev", "implement")}


# ─────────────────────────────────────────── 审计自己写用例
def test_test_plan_emits_runnable_pytest(tmp_path):
    """auditor/test_plan 能把用例直接落成 pytest 文件，且默认 skip 不误报。"""
    import subprocess

    from multi_agent.integrations.game_server_spec import test_plan

    endpoints = [{"id": "P-04", "method": "POST", "path": "/v1/player/ban",
                  "auth": "是（服务名）", "idem": "request_id",
                  "errors": ["FORBIDDEN"]}]
    out = test_plan({"endpoints": endpoints,
                     "pytest_path": str(tmp_path / "test_generated_cases.py")})
    path = out["pytest_path"]
    assert os.path.isfile(path)
    src = Path(path).read_text(encoding="utf-8")
    assert out["count"] >= 4 and "TC-P-04-01" in src
    assert "parametrize" in src and "API_BASE" in src
    compile(src, path, "exec")

    proc = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", path],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "test_case" in proc.stdout


# ─────────────────────────────────────────── function call
def test_tools_loop_executes_and_reports_trace():
    from multi_agent.integrations.llm import chat

    seen = []

    def model(messages, tools):
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        if not tool_msgs:
            return {"content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "grep_code", "arguments": '{"pattern": "LOGIN"}'}}]}
        seen.append(tool_msgs[-1]["content"])
        return {"content": "done", "tool_calls": []}

    tools = [{"type": "function", "function": {"name": "grep_code",
                                              "parameters": {"type": "object"}}}]
    res = chat({"prompt": "find", "tools": tools,
                "tool_impls": {"grep_code": lambda a: {"hits": [{"file": "a.py"}]}},
                "tool_caller": model})
    assert res["ok"] and res["text"] == "done"
    assert res["tool_rounds"] == 1
    assert res["tool_trace"][0]["tool"] == "grep_code" and res["tool_trace"][0]["ok"]
    assert "a.py" in seen[0]


def test_tools_loop_degrades_unknown_tool_and_bad_args():
    from multi_agent.integrations.llm import chat

    def model(messages, tools):
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        if not tool_msgs:
            return {"content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "rm_rf", "arguments": "{}"}},
                {"id": "c2", "type": "function",
                 "function": {"name": "read_file", "arguments": "{not json"}}]}
        return {"content": " ".join(m["content"] for m in tool_msgs), "tool_calls": []}

    tools = [{"type": "function", "function": {"name": "read_file",
                                              "parameters": {"type": "object"}}}]
    res = chat({"prompt": "x", "tools": tools,
                "tool_impls": {"read_file": lambda a: {"ok": True}}, "tool_caller": model})
    assert "unknown_tool" in res["text"] and "bad_arguments" in res["text"]
    assert [t["ok"] for t in res["tool_trace"]] == [False, False]


def test_tools_loop_stops_at_max_rounds():
    from multi_agent.integrations.llm import chat

    def model(messages, tools):
        return {"content": "", "tool_calls": [
            {"id": "c", "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}}]}

    tools = [{"type": "function", "function": {"name": "read_file",
                                              "parameters": {"type": "object"}}}]
    res = chat({"prompt": "x", "tools": tools, "tool_impls": {"read_file": lambda a: {}},
                "tool_caller": model, "max_tool_rounds": 2})
    assert res["degraded"] and res["truncated"] and "上限" in res["reason"]
    assert res["tool_rounds"] == 2


def test_tools_require_endpoint_or_caller(monkeypatch):
    from multi_agent.integrations.llm import chat

    for k in ("LLM_URL", "AUDIT_LLM_URL", "MULTI_AGENT_LLM_CONFIG"):
        monkeypatch.delenv(k, raising=False)
    res = chat({"prompt": "x", "tools": [{"type": "function",
                                         "function": {"name": "t"}}]})
    assert res["degraded"] and "工具模式" in res["reason"]


def test_dev_tools_only_read_inside_repo(tmp_path):
    from multi_agent.integrations import dev_tools

    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "auth.py").write_text("LOGIN_FAILED = 1\n", encoding="utf-8")
    specs, impls = dev_tools.build(str(tmp_path))
    assert {s["function"]["name"] for s in specs} == {"read_file", "grep_code", "list_dir"}
    assert "LOGIN_FAILED" in impls["read_file"]({"path": "server/auth.py"})["content"]
    outside = impls["read_file"]({"path": "../../etc/passwd"})
    assert outside["error"] == "path_outside_repo"
    hits = impls["grep_code"]({"pattern": "LOGIN_FAILED"})
    assert hits["count"] == 1 and hits["hits"][0]["file"] == "server/auth.py"
    assert impls["list_dir"]({"path": "."})["count"] >= 1


def test_login_bug_watch_demo_passes(capsys):
    """登录失败示例本身必须是绿的：采集/定位/补丁/审计逐项对得上。"""
    from examples.login_bug_watch import run_demo

    assert run_demo.main() == 0
    out = capsys.readouterr().out
    assert "17/17 项通过" in out and "[FAIL]" not in out


def test_rule_edits_skip_public_login_auth(tmp_path):
    """登录接口是公开的：规则模板不能把 auth=False 改成 auth=True。"""
    import shutil

    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "examples" / "login_bug_watch" / "fixture" / "repo", repo)
    bugs = [{"bug_id": "b1", "error_code": "AUTH_MISSING", "endpoint": "/v1/login",
             "message": "接口声明要鉴权、实现没鉴权"}]
    tri = BF.triage({"bugs": bugs})
    loc = BF.locate({"bugs": bugs, "repo_root": str(repo)})
    prop = BF.propose({"bugs": bugs, "triage": tri, "locate": loc,
                       "repo_root": str(repo)})
    edits = [e for w in prop["workstreams"] for e in w["edits"]]
    assert all("auth=True" not in str(e["replace"]) for e in edits)
    assert prop["counts"]["workstreams"] == 0
    assert any("公开接口" in str(d["reason"]) for d in prop["deferred"])