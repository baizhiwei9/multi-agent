"""game-server 分支的验收：声明自洽 + 审计真的抓得到缺陷 + 闸门真的会拦 + 发布包可校验。

刻意只测"框架契约与规则命中"，不测业务数值：
· 审计：故意留缺陷的 fixture 必须被抓到（协议缺失/越权/迁移缺口/配置经济/硬编码密钥）；
· 闸门：审计 fail、容量打满、回放不确定、经济通胀都要 block；干净输入要 pass；
· 交付：report/manifest/checksums 落盘且 sha256 与文件一致；
· 端到端：开发报告 → 拆包 → 并行开发 → 审计 → 闸门 → 发布包，全链跑通。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

from multi_agent import pipelines as P
from multi_agent.integrations.audit_profiles import PROFILES, profile_names
from multi_agent.integrations.game_server_audit import (audit_code, collect_snapshot,
                                                       parse_endpoint_table)
from multi_agent.integrations.game_server_delivery import build_delivery
from multi_agent.integrations.game_server_risk import gate, run_replay
from multi_agent.projects.game_server import GameServerProject, install

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples" / "game_server" / "fixture"
WORK_REPO = FIXTURE / "repo"


def _copy_repo(tmp_path: Path) -> Path:
    dest = tmp_path / "repo"
    shutil.copytree(WORK_REPO, dest, ignore=shutil.ignore_patterns("__pycache__"))
    return dest


# ───────────────────────────────────────────── 声明
def test_profile_registered_and_project_consistent():
    assert "game_server" in profile_names()
    assert PROFILES["game_server"].name == "game_server"

    p = GameServerProject()
    assert p.validate() == []
    steps = [s["name"] for s in p.pipeline["steps"]]
    assert steps == ["intake", "plan", "dev", "pack", "audit", "gate",
                     "ledger", "report", "store"]
    caps = {r["capability"] for r in p.describe()["roles"]}
    assert {"producer", "dev", "supervisor", "auditor", "server_risk", "report"} <= caps


def test_builtin_roles_are_not_stubbed():
    """审计/风控/经理由框架注册；项目 handlers 里出现它们就会覆盖真实现。"""
    h = GameServerProject().handlers()
    assert set(h) == {"producer", "dev", "report"}
    for cap in ("supervisor", "auditor", "server_risk"):
        assert cap not in h


def test_install_registers_pipeline_and_agent_plan():
    p = install()
    try:
        assert "game_server_release" in P.PIPELINES
        plan = p.agent_plan()
        by_cap = {r["capability"]: r for r in plan["roles"]}
        assert by_cap["dev"]["instances"] == 3
        assert "auditor" in by_cap and "server_risk" in by_cap
    finally:
        P.PIPELINES.pop("game_server_release", None)


# ───────────────────────────────────────────── 审计
def test_audit_catches_seeded_defects(tmp_path):
    repo = _copy_repo(tmp_path)
    ticket = json.loads((FIXTURE / "ticket.json").read_text(encoding="utf-8"))
    out = audit_code({"repo_root": str(repo), "doc_root": str(repo / "docs"),
                      "ticket": ticket})
    codes = {f["code"] for f in out["findings"]}
    assert out["verdict"] == "fail"
    assert {
        "endpoint_missing",        # 文档声明了 logout，代码没实现
        "auth_missing",            # ban 声明要鉴权，实现 auth=False
        "migration_gap",           # 001/002/004 缺 003
        "migration_destructive",   # DROP COLUMN 且没有备份
        "drop_prob_sum",           # 掉落概率合计 > 1
        "secret_hardcoded",        # 硬编码密钥
    } <= codes
    # 审计依据确实来自仓库里的文档与协议文件
    summary = out["snapshot_summary"]
    assert summary["counts"]["declared"] >= 8
    assert summary["docs"] and summary["spec_files"]


def test_acceptance_coverage_uses_docs(tmp_path):
    repo = _copy_repo(tmp_path)
    ticket = json.loads((FIXTURE / "ticket.json").read_text(encoding="utf-8"))
    snap = collect_snapshot(str(repo), str(repo / "docs"), None, ticket)
    acc = snap["acceptance"]
    assert acc["total"] == 4
    assert acc["coverage"] == 0.75          # 三条有文档依据，迁移回滚那条没有
    assert acc["uncovered"] == ["迁移必须可按版本顺序回滚"]


def test_endpoint_table_parser_reads_markdown():
    md = (WORK_REPO / "docs" / "design.md").read_text(encoding="utf-8")
    rows = parse_endpoint_table(md)
    keys = {(r["method"], r["path"]) for r in rows}
    assert ("POST", "/v1/player/logout") in keys
    assert ("GET", "/v1/legacy/rank") in keys


def test_clean_repo_passes_audit(tmp_path):
    """没有缺陷的最小仓库必须判 pass —— 规则不能"为了拦截而拦截"。"""
    repo = tmp_path / "clean"
    (repo / "server").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "migrations").mkdir()
    (repo / "config").mkdir()
    (repo / "server" / "app.py").write_text(
        'from fastapi import FastAPI\n\napp = FastAPI()\n\n\n'
        '@app.get("/v1/ping", auth=False)\ndef ping():\n    return {"ok": True}\n',
        encoding="utf-8")
    (repo / "proto").mkdir()
    (repo / "proto" / "spec.json").write_text(json.dumps(
        {"endpoints": [{"method": "GET", "path": "/v1/ping", "auth": False}]}),
        encoding="utf-8")
    (repo / "docs" / "design.md").write_text(
        "# 设计\n\n## 探活\n\n服务提供 GET /v1/ping 探活接口，不需要鉴权。\n\n"
        "| 方法 | 路径 | 鉴权 | 废弃 |\n|---|---|---|---|\n| GET | /v1/ping | 否 | 否 |\n",
        encoding="utf-8")
    (repo / "migrations" / "001_init.sql").write_text(
        "BEGIN;\nCREATE TABLE t (id INTEGER PRIMARY KEY);\nCOMMIT;\n", encoding="utf-8")
    (repo / "config" / "items.json").write_text('{"items": [{"id": "gold"}]}',
                                                encoding="utf-8")
    (repo / "config" / "economy.json").write_text(
        '{"faucet_daily_cap": 1000, "inflation_daily_pct": 0.1}', encoding="utf-8")
    out = audit_code({"repo_root": str(repo), "doc_root": str(repo / "docs")})
    assert out["verdict"] == "pass", out["findings"]


# ───────────────────────────────────────────── 闸门
def _clean_gate_inputs():
    return {
        "audit": {"verdict": "pass", "findings": []},
        "capacity": {"expected_ccu": 1000, "ccu_limit": 2000,
                     "tick_budget_ms": 50, "p99_tick_ms": 30},
        "replay": {"deterministic": True, "runs": 2, "errors": []},
        "rollback": {"plan": "回滚", "script": "ops/rollback.sh", "tested": True},
        "canary_pct": 5,
        "economy": {"faucet_daily_cap": 10000, "inflation_daily_pct": 0.1},
    }


def test_gate_passes_on_clean_evidence():
    res = gate(_clean_gate_inputs())
    assert res["gate"] == "pass", res["checks"]
    assert res["blocked_by"] == []


def test_gate_blocks_on_audit_fail_and_capacity():
    inputs = _clean_gate_inputs()
    inputs["audit"] = {"verdict": "fail", "findings": [
        {"code": "auth_missing", "severity": "error", "message": "越权"}]}
    inputs["capacity"] = {"expected_ccu": 5200, "ccu_limit": 5000,
                          "tick_budget_ms": 50, "p99_tick_ms": 62}
    res = gate(inputs)
    assert res["gate"] == "block"
    assert {"audit", "audit_critical", "capacity_ccu", "capacity_tick"} <= set(res["blocked_by"])


def test_gate_blocks_on_nondeterministic_replay(tmp_path):
    engine = tmp_path / "engine.py"
    engine.write_text(
        "import os\n\n"
        "def initial_state():\n    return {'pid': os.getpid()}\n\n"
        "def step(state, cmd):\n    state['n'] = state.get('n', 0) + 1\n    return state\n",
        encoding="utf-8")
    inputs = _clean_gate_inputs()
    inputs["replay"] = {"engine": str(engine), "scenario": [], "allow_exec": True, "runs": 2}
    res = gate(inputs)
    assert res["gate"] == "block"
    assert "replay" in res["blocked_by"]


def test_replay_detects_non_determinism_and_requires_flag(tmp_path):
    engine = tmp_path / "engine.py"
    engine.write_text(
        "import os\n\ndef initial_state():\n    return {'pid': os.getpid()}\n\n"
        "def step(state, cmd):\n    state['n'] = state.get('n', 0) + 1\n    return state\n",
        encoding="utf-8")
    blocked = run_replay({"engine": str(engine), "scenario": []})
    assert blocked["skipped"] is True and blocked["deterministic"] is None

    res = run_replay({"engine": str(engine), "scenario": [], "allow_exec": True, "runs": 2})
    assert res["deterministic"] is False

    sim = WORK_REPO / "server" / "sim.py"
    ok = run_replay({"engine": str(sim), "scenario": str(FIXTURE / "scenario.json"),
                     "allow_exec": True, "runs": 2})
    assert ok["deterministic"] is True and ok["ok"] is True


# ───────────────────────────────────────────── 交付
def test_bundle_writes_manifest_and_checksums(tmp_path):
    dev = {"workstream": "auth", "patch": None,
           "summary": "补丁：无", "files": [], "tests": []}
    res = build_delivery({
        "out_dir": str(tmp_path / "release"),
        "ticket": {"ticket_id": "gs-1", "title": "测试工单", "acceptance": ["A"]},
        "dev": [dev],
        "audit": {"verdict": "suspect", "counts": {"error": 0, "warn": 1, "info": 0},
                  "findings": [{"severity": "warn", "code": "todo_left",
                                "message": "TODO", "where": "code"}],
                  "snapshot_summary": {"counts": {"declared": 1, "implemented": 1},
                                       "docs": ["design.md"]}},
        "gate": {"gate": "warn", "checks": [{"name": "canary", "level": "warn",
                                            "detail": "灰度未提供"}],
                 "blocked_by": [], "warns": ["canary"]},
        "ledger": {"count": 2},
    })
    out = Path(res["out_dir"])
    report = out / "release_report.md"
    manifest_path = out / "manifest.json"
    checksums = out / "checksums.sha256"
    assert report.exists() and manifest_path.exists() and checksums.exists()
    assert "审计结论" in res["markdown"] and "风控闸门" in res["markdown"]

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["count"] == len(manifest["files"]) >= 3
    for item in manifest["files"]:
        actual = hashlib.sha256((out / item["path"]).read_bytes()).hexdigest()
        assert actual == item["sha256"]
    assert len(checksums.read_text(encoding="utf-8").splitlines()) == manifest["count"]


# ───────────────────────────────────────────── 端到端
def test_game_server_pipeline_end_to_end():
    sys.modules.pop("examples.game_server.run_demo", None)
    from examples.game_server import run_demo

    snap = run_demo.run(quiet=True)
    assert snap["state"] in ("succeeded", "degraded")
    for name in ("intake", "plan", "dev", "pack", "audit", "gate", "ledger", "report"):
        assert snap["steps"][name]["state"] == "succeeded", snap["steps"][name]

    dev = snap["steps"]["dev"]["result"]
    assert isinstance(dev, list) and len(dev) == 3
    assert all(d.get("patch") and d.get("applied") for d in dev)

    audit = snap["steps"]["audit"]["result"]
    assert audit["verdict"] == "fail"
    assert "migration_gap" in {f["code"] for f in audit["findings"]}

    gate_res = snap["steps"]["gate"]["result"]
    assert gate_res["gate"] == "block"
    assert "audit" in gate_res["blocked_by"]

    report = snap["steps"]["report"]["result"]
    out_dir = Path(report["out_dir"])
    assert (out_dir / "release_report.md").exists()
    assert (out_dir / "manifest.json").exists()
    assert (out_dir / "checksums.sha256").exists()
    assert out_dir.name == "gs-1042"
    assert "【未通过】" in report["markdown"]
    assert "release_report.md" in {f["path"] for f in report["files"]}


# ───────────────────────────────────────────── 开发报告（Markdown）
def test_markdown_dev_report_becomes_ticket():
    """经典的 Markdown 开发报告：标题/范围/验收/模块/接口表都要被解析出来。"""
    from multi_agent.projects.game_server import load_ticket

    ticket, reason = load_ticket(str(FIXTURE / "ticket_classic.md"))
    assert reason == "" and ticket
    assert ticket["title"].startswith("开发报告")
    assert len(ticket["acceptance"]) >= 6
    assert len(ticket["modules"]) >= 4

    keys = {(e["method"], e["path"]) for e in ticket["endpoints"]}
    assert {
        ("POST", "/v1/match/enter"),          # 客户端 → 网关 → 匹配服
        ("POST", "/v1/cross/room/allocate"),  # 匹配服 → 跨服房间服
        ("POST", "/v1/pay/order/callback"),   # 支付平台 → 商城服
    } <= keys
    # 报告里"鉴权"列写"是"的接口，必须被识别成需要鉴权
    enter = next(e for e in ticket["endpoints"] if e["path"] == "/v1/match/enter")
    assert enter["auth"] is True


def test_report_endpoints_feed_audit_as_declared(tmp_path):
    """报告接口表 = 声明清单的一部分：报告写了、代码没实现，审计必须报 endpoint_missing。"""
    from multi_agent.projects.game_server import load_ticket

    repo = _copy_repo(tmp_path)
    ticket, _ = load_ticket(str(FIXTURE / "ticket_classic.md"))
    snap = collect_snapshot(str(repo), str(repo / "docs"), None, ticket)
    declared = snap["endpoints"]["declared"]
    assert any(e["source"] == "report" for e in declared)
    missing = {(m["method"], m["path"]) for m in snap["endpoints"]["missing"]}
    assert ("POST", "/v1/match/enter") in missing
    assert ("POST", "/v1/pay/order/callback") in missing

    out = audit_code({"repo_root": str(repo), "doc_root": str(repo / "docs"),
                      "ticket": ticket})
    codes = {f["code"] for f in out["findings"]}
    assert "endpoint_missing" in codes
    assert out["verdict"] == "fail"