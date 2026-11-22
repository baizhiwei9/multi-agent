"""度量脚本的测试：保证"零 token 证明"本身是可信的。

为什么要给度量脚本写测试
========================
`tools/measure_cost.py` 是"这套东西不烧 token"的**证据来源**。
如果证据本身会漏扫（比如新增了执行路径模块却没加进列表），
那结论就是假的。所以这些测试守的是"证据的完整性"：

1. 静态扫描必须覆盖所有执行路径模块
2. 漏掉一个模块时，扫描结果必须能看出来（不是静默通过）
3. 成本模型必须只有"交换门票"需要模型
4. 度量脚本本身不能 import 模型 SDK
"""
import importlib
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools import measure_cost as MC


# ───────────────────────────────────────────── 扫描覆盖
def test_scan_covers_all_execution_paths():
    """执行路径的每个模块都必须在扫描列表里。"""
    required = [
        "multi_agent/bootstrap.py",
        "multi_agent/signal.py",
        "multi_agent/integrations/audit.py",
        "multi_agent/integrations/audit_profiles.py",
        "multi_agent/integrations/runner.py",
        "multi_agent/router.py",
        "multi_agent/shared_data.py",
        "multi_agent/db_policies.py",
        "multi_agent/dbmanager.py",
    ]
    for rel in required:
        assert rel in MC._EXECUTION_MODULES, f"{rel} 没被扫描"
        assert os.path.exists(os.path.join(ROOT, rel)), f"{rel} 不存在"


def test_scan_reports_files_actually_scanned():
    out = MC.scan_imports()
    assert len(out["files_scanned"]) == len(MC._EXECUTION_MODULES)
    assert out["ok"] is True, out["violations"]


def test_scan_detects_violation(tmp_path, monkeypatch):
    """故意放一个 import openai 的文件，扫描必须报出来。"""
    fake = tmp_path / "evil.py"
    fake.write_text("import openai\n", encoding="utf-8")
    monkeypatch.setattr(MC, "_EXECUTION_MODULES", (str(fake),))
    out = MC.scan_imports()
    assert out["ok"] is False
    assert out["violations"][0]["marker"] == "import openai"


def test_sdk_marker_list_covers_major_sdks():
    joined = " ".join(MC._SDK_MARKERS)
    for sdk in ("openai", "anthropic", "dashscope", "litellm",
                "transformers", "vllm", "ollama"):
        assert sdk in joined, f"{sdk} 不在标记清单里"


# ───────────────────────────────────────────── 运行时检查
def test_runtime_check_clean():
    out = MC.runtime_check()
    assert out["ok"] is True, out["loaded_sdks"]


def test_runtime_check_detects_loaded_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", object())
    out = MC.runtime_check()
    assert out["ok"] is False and "openai" in out["loaded_sdks"]


# ───────────────────────────────────────────── 成本模型
def test_only_ticket_step_needs_llm():
    """成本模型的核心断言：只有"交换门票"需要模型。"""
    cm = MC.cost_model()
    need_llm = [s["step"] for s in cm["steps"] if s["needs_llm"]]
    assert need_llm == ["交换门票"], f"需要模型的步骤：{need_llm}"


def test_every_other_step_is_zero_token():
    for s in MC.cost_model()["steps"]:
        if not s["needs_llm"]:
            assert s.get("tokens") == 0, s


def test_cost_model_documents_audit_llm_is_optional():
    note = MC.cost_model()["audit_llm_note"]
    assert "rule" in note and "可选" in note


# ───────────────────────────────────────────── 度量脚本自身
def test_measure_cost_has_no_llm_imports():
    """用 AST 查真实 import —— 不能拿字符串搜索，否则会把标记清单本身误判。"""
    import ast
    path = os.path.join(ROOT, "tools/measure_cost.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    banned = {"openai", "anthropic", "dashscope", "litellm", "cohere",
              "transformers", "vllm", "replicate", "ollama"}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root_mod = a.name.split(".")[0]
                if root_mod in banned:
                    found.append(root_mod)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in banned:
                found.append(node.module)
    assert not found, f"度量脚本不该 import 模型 SDK：{found}"


def test_udp_direct_measurement_smoke():
    """本机自连的 UDP 度量要能跑通并给出数字（不依赖外网）。"""
    out = MC.measure_direct(rounds=3, payload_size=64)
    if not out.get("ok"):
        pytest.skip(f"本机 UDP 自连不可用：{out.get('reason')}")
    assert out["messages_received"] == 3
    assert out["bytes_sent"] > 0
    assert out["llm_calls"] == 0


def test_measure_json_output_is_serialisable(capsys):
    """--json 输出必须能解析（CI 会用）。"""
    import json as _json
    MC.main(["--skip-relay", "--rounds", "1", "--json"])
    raw = capsys.readouterr().out.strip()
    data = _json.loads(raw)
    assert "cost_model" in data and "static_imports" in data
