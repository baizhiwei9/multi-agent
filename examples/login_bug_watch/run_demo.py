"""登录失败：一条 error 怎么变成补丁 —— 三种框架形态对照。

    python examples/login_bug_watch/run_demo.py

形态 A：program 关键词（零 token）
形态 B：llm 复核（模糊行 retry，模拟模型说 YES）
形态 C：tools（模型先 grep_code / read_file，再出 edits）

每个形态跑完都做逐项判定，最后打印 PASS/FAIL 汇总；退出码 0 表示全过。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

from multi_agent.frameworks.keywords import KeywordSet                  # noqa: E402
from multi_agent.frameworks.netdev.framework import BugWatchFramework  # noqa: E402
from multi_agent.integrations import game_server_bugfix as BF           # noqa: E402
from multi_agent.integrations import game_server_dev as DEV             # noqa: E402
from multi_agent.integrations.game_server_audit import audit_code       # noqa: E402
from multi_agent.integrations.supervisor import assign                  # noqa: E402

HERE = Path(__file__).resolve().parent
LOG = HERE / "fixture" / "logs" / "server.log"
REPO = HERE / "fixture" / "repo"
KEYWORDS = HERE / "keywords.json"
OUT = HERE / "out"

#: 模型看过代码后返回的补丁：失败分支补一次计数（锁定因此才可能生效）
EDITS = {
    "edits": [{
        "file": "server/auth.py",
        "find": ('    if not user or not check_password(user, password):\n'
                 '        return {"ok": False, "error": "LOGIN_FAILED"}'),
        "replace": ('    if not user or not check_password(user, password):\n'
                    '        bump_fail(username)\n'
                    '        return {"ok": False, "error": "LOGIN_FAILED"}'),
        "why": "登录失败没有计数，锁定永不触发（LOGIN_FAILED 无限重试）",
    }]
}

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


def fresh_repo() -> Path:
    work = OUT / "work"
    if work.exists():
        shutil.rmtree(work)
    work.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO, work, ignore=shutil.ignore_patterns("__pycache__"))
    return work


def audit_counts(repo: Path) -> dict:
    res = audit_code({"repo_root": str(repo), "doc_root": str(repo / "docs")})
    return dict(res.get("counts") or {})


def collect(judge):
    keywords = KeywordSet.from_file(str(KEYWORDS))
    framework = BugWatchFramework(keyword_set=keywords, path=str(LOG), judge=judge)
    return framework.collect({})


def mock_model(messages, tools):
    """模拟一个会用工具的模型：grep → read → 出补丁。"""
    offered = {str((t.get("function") or {}).get("name")) for t in tools}
    assert {"read_file", "grep_code", "list_dir"} <= offered, offered
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    if not tool_msgs:
        return {"content": "", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "grep_code",
                         "arguments": json.dumps({"pattern": "LOGIN_FAILED"})}}]}
    if len(tool_msgs) == 1:
        return {"content": "", "tool_calls": [{
            "id": "c2", "type": "function",
            "function": {"name": "read_file",
                         "arguments": json.dumps({"path": "server/auth.py"})}}]}
    return {"content": json.dumps(EDITS, ensure_ascii=False), "tool_calls": []}


def main() -> int:
    # ───────────────────────────── 形态 A：program 关键词
    print("\n=== 形态 A：program 关键词（零 token）===")
    res_a = collect(lambda hit: False)      # llm 组一律否，模拟"只走程序"
    bugs_a = res_a.get("bugs") or []
    print(f"扫 {res_a.get('scanned')} 行 / 命中 {res_a.get('matched')} 条 / 出 {len(bugs_a)} 个 bug："
          f"{[b.get('group') for b in bugs_a]}")
    login_a = [b for b in bugs_a if b.get("endpoint") == "/v1/login"]
    check("A1 采到 /v1/login 的登录失败", any(b.get("group") == "login_fail" for b in login_a))
    check("A2 错误码是 LOGIN_FAILED",
          any(b.get("error_code") == "LOGIN_FAILED" for b in login_a),
          str([b.get("error_code") for b in login_a]))
    check("A3 模糊行 retry 没被程序模式采到",
          all(b.get("group") != "suspect" for b in bugs_a))

    work_a = fresh_repo()
    tri_a = BF.triage({"bugs": bugs_a})
    loc_a = BF.locate({"bugs": bugs_a, "repo_root": str(work_a)})
    files = [f.get("file") for f in loc_a.get("findings") or []]
    check("A4 定位到 server/auth.py", "server/auth.py" in files, str(files))
    check("A5 路由表认出 /v1/login",
          int((loc_a.get("counts") or {}).get("routes") or 0) >= 1,
          str(loc_a.get("counts")))
    prop_a = BF.propose({"bugs": bugs_a, "triage": tri_a, "locate": loc_a,
                         "repo_root": str(work_a)})
    rule_edits = [e for w in (prop_a.get("workstreams") or []) for e in (w.get("edits") or [])]
    rule_text = json.dumps(rule_edits, ensure_ascii=False)
    check("A6 规则模式不再误改登录接口鉴权（公开接口跳过 auth 改造）",
          all("auth=True" not in str(e.get("replace")) for e in rule_edits),
          rule_text)
    check("A7 规则模式对该缺陷无解，如实 deferred 并说明是公开接口",
          int(prop_a["counts"]["workstreams"]) == 0
          and any("公开接口" in str(d.get("reason") or "")
                  for d in prop_a.get("deferred") or []),
          json.dumps(prop_a.get("deferred") or [], ensure_ascii=False))
    print("      → 规则模板对登录失败不再乱改鉴权；正确补丁仍要靠 C 形态（模型看代码后出 edits）。")

    # ───────────────────────────── 形态 B：llm 复核
    print("\n=== 形态 B：llm 复核（模糊行）===")
    judged = {"calls": 0}

    def judge(hit):
        judged["calls"] += 1
        return True

    res_b = collect(judge)
    bugs_b = res_b.get("bugs") or []
    print(f"出 {len(bugs_b)} 个 bug：{[b.get('group') for b in bugs_b]}")
    check("B1 模型复核后多采到 retry 行",
          any(b.get("group") == "suspect" for b in bugs_b))
    check("B2 只对 llm 组发起复核", judged["calls"] == 1, f"calls={judged['calls']}")

    # ───────────────────────────── 形态 C：tools
    print("\n=== 形态 C：tools（先查代码再出补丁）===")
    work_c = fresh_repo()
    before = audit_counts(work_c)
    tri_c = BF.triage({"bugs": bugs_a})
    loc_c = BF.locate({"bugs": bugs_a, "repo_root": str(work_c)})
    prop_c = BF.propose({"bugs": bugs_a, "triage": tri_c, "locate": loc_c,
                         "repo_root": str(work_c), "llm": True, "tools": True,
                         "llm_tool_caller": mock_model, "max_tool_rounds": 4})
    llm_info = prop_c.get("llm") or {}
    trace = llm_info.get("tool_trace") or []
    print("工具轨迹：" + json.dumps(trace, ensure_ascii=False))
    check("C1 模型调了 grep_code 并成功",
          any(t.get("tool") == "grep_code" and t.get("ok") for t in trace))
    check("C2 模型调了 read_file 并成功",
          any(t.get("tool") == "read_file" and t.get("ok") for t in trace))
    check("C3 工具轮次 >= 2 且没超上限",
          int(llm_info.get("tool_rounds") or 0) >= 2 and not llm_info.get("truncated"),
          f"rounds={llm_info.get('tool_rounds')} truncated={llm_info.get('truncated')}")

    workstreams = prop_c.get("workstreams") or []
    edits = (workstreams[0].get("edits") if workstreams else []) or []
    check("C4 产出 1 个 workstream 且 edits 命中 auth.py",
          len(workstreams) == 1 and any(e.get("file") == "server/auth.py" for e in edits),
          f"workstreams={len(workstreams)} edits={len(edits)}")

    asg = assign({"workstreams": workstreams})
    check("C5 经理派单给 dev/implement",
          asg.get("count") == 1
          and asg["assignments"][0].get("capability") == "dev"
          and asg["assignments"][0].get("action") == "implement",
          json.dumps(asg.get("assignments") or [], ensure_ascii=False))

    impl = DEV.implement({"workstream": workstreams[0], "repo_root": str(work_c),
                          "out_dir": str(OUT / "release")})
    check("C6 补丁落地且语法通过", impl.get("applied") is True and impl.get("ok") is True,
          str(impl.get("reason") or impl.get("patch")))
    auth_now = (work_c / "server" / "auth.py").read_text(encoding="utf-8")
    check("C7 修复内容真的写进去了", "bump_fail(username)" in auth_now)

    after = audit_counts(work_c)
    check("C8 审计 error 没增加",
          int(after.get("error") or 0) <= int(before.get("error") or 0),
          f"{before} → {after}")

    # ───────────────────────────── 汇总
    failed = [r for r in results if not r[1]]
    print(f"\n{'=' * 60}\n{len(results) - len(failed)}/{len(results)} 项通过")
    if failed:
        for name, _, detail in failed:
            print(f"  FAIL {name} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())