"""审计官（Auditor）测试：算法正确性 + 分级执行 + 0 token 保证。

重点验证三件事：
1. **算法判得对** —— 随机游走判 unit_root、白噪声判 stationary、
   人为断点能被抓住、过拟合能被识破；
2. **分级真的省钱** —— rule 级别下不跑 L2、auto 在 L1 报 error 时短路；
3. **0 token 是硬约束** —— L1/L2 路径上没有任何大模型 SDK 被加载，
   L3 只能由调用方传 llm 回调（本模块自己不 import、不持 key）。
"""
import json
import random

import pytest

from multi_agent.integrations import audit
from multi_agent.integrations.audit import (
    Auditor, adf_test, chow_break_test, jarque_bera, kpss_test,
    ljung_box_test, overfit_check, run_action, assert_no_llm_import,
)


# ─────────────────────────────────────────────── 工具
def _random_walk(n=120, seed=1, start=100.0):
    rnd = random.Random(seed)
    out, v = [], start
    for _ in range(n):
        v += rnd.gauss(0, 1)
        out.append(v)
    return out


def _white_noise(n=120, seed=2):
    rnd = random.Random(seed)
    return [rnd.gauss(0, 1) for _ in range(n)]


def _break_series(n=120, seed=3):
    rnd = random.Random(seed)
    half = n // 2
    return ([rnd.gauss(0, 0.5) for _ in range(half)]
            + [rnd.gauss(10, 0.5) for _ in range(n - half)])


def _balance_ok():
    return {
        "commodity": "copper", "period": "2025A", "unit": "万吨",
        "supply": {"mine": 2318.0, "refined_primary": 2322.9,
                   "refined_secondary": 534.8, "refined_total": 2857.8},
        "demand": {"refined_usage": 2815.9},
        "balance": {"surplus_deficit": 41.9, "stock_change": 41.0,
                    "stocks_end": 180.9},
        "data_date": "2026-11-21", "as_of": "2026-11-22",
    }


# ─────────────────────────────────────────────── 算法
def test_adf_random_walk_is_unit_root():
    r = adf_test(_random_walk())
    assert r["ok"] and r["verdict"] == "unit_root"


def test_adf_white_noise_is_stationary():
    r = adf_test(_white_noise())
    assert r["ok"] and r["verdict"] == "stationary"


def test_kpss_white_noise_stationary():
    """KPSS 名义 5% 水平 —— 白色噪声下大部分样本应判平稳。

    单次抽样会因 5% 的检验水平偶发误判（这是检验本身的性质，不是 bug），
    所以这里用多个种子验证「大多数判平稳」。
    """
    verdicts = [kpss_test(_white_noise(120, seed=s))["verdict"]
                for s in range(1, 13)]
    assert verdicts.count("stationary") >= 8, verdicts


def test_kpss_random_walk_nonstationary():
    r = kpss_test(_random_walk())
    assert r["ok"] and r["verdict"] == "nonstationary"


def test_ljung_box_detects_autocorrelation():
    # AR(1) with phi=0.9 明显自相关
    rnd = random.Random(7)
    y, v = [], 0.0
    for _ in range(150):
        v = 0.9 * v + rnd.gauss(0, 1)
        y.append(v)
    r = ljung_box_test(y)
    assert r["ok"] and r["verdict"] == "autocorrelated"


def test_ljung_box_white_noise_clean():
    r = ljung_box_test(_white_noise(seed=11))
    assert r["ok"] and r["verdict"] == "clean"


def test_chow_detects_break():
    r = chow_break_test(_break_series())
    assert r["ok"] and r["verdict"] == "break"
    assert r["split_index"] == 60


def test_chow_no_break_on_stationary():
    r = chow_break_test(_white_noise(seed=13))
    assert r["ok"] and r["verdict"] == "no_break"


def test_jarque_bera_flags_heavy_tail():
    rnd = random.Random(5)
    y = [rnd.gauss(0, 1) for _ in range(200)] + [12.0, -12.0] * 5
    r = jarque_bera(y)
    assert r["ok"] and r["verdict"] == "non_normal"


def test_overfit_detected():
    # 样本内近完美拟合，样本外纯噪声
    rnd = random.Random(9)
    y = [float(i) + rnd.gauss(0, 0.1) for i in range(70)]
    y += [70.0 + rnd.gauss(0, 10) for _ in range(30)]
    r = overfit_check(y)
    assert r["ok"] and r["verdict"] == "overfit"
    assert r["r2_out"] < 0


def test_short_series_degrades_gracefully():
    r = adf_test([1.0, 2.0, 3.0])
    assert r["ok"] is False and "样本" in r["reason"]


def test_constant_series_not_faked_as_stationary():
    r = adf_test([5.0] * 40)
    assert r["ok"] is False


# ─────────────────────────────────────────────── L1 平衡表
def test_balance_pass_on_clean_sheet():
    rep = Auditor(level="rule").audit_balance(_balance_ok())
    assert rep.verdict == "pass", rep.as_dict()


def test_balance_detects_missing_field():
    bal = _balance_ok()
    del bal["demand"]
    rep = Auditor(level="rule").audit_balance(bal)
    assert rep.verdict == "fail"
    assert any(f.code == "missing_field" and f.where == "demand" for f in rep.findings)


def test_balance_detects_internal_mismatch():
    bal = _balance_ok()
    bal["supply"]["refined_total"] = 3000.0   # 和 原生+再生 对不上
    rep = Auditor(level="rule").audit_balance(bal)
    assert rep.verdict == "fail"
    assert any(f.code == "internal_mismatch" for f in rep.findings)


def test_balance_detects_magnitude_error():
    bal = _balance_ok()
    # 1 吨级的"精炼总产量"对所有商品都荒谬 —— 典型单位写错（吨/万吨 混用）
    bal["supply"]["refined_primary"] = 0.0001
    bal["supply"]["refined_secondary"] = 0.0
    bal["supply"]["refined_total"] = 0.0001
    rep = Auditor(level="rule").audit_balance(bal)
    codes = {f.code for f in rep.findings}
    assert "magnitude_suspect" in codes, rep.as_dict()


def test_balance_detects_sign_conflict():
    bal = _balance_ok()
    bal["balance"]["stock_change"] = -41.0    # 过剩却在去库
    rep = Auditor(level="rule").audit_balance(bal)
    assert any(f.code == "sign_conflict" for f in rep.findings)


def test_balance_detects_constant_series():
    bal = _balance_ok()
    bal["history"] = {"stocks_end": [180.9] * 6}
    rep = Auditor(level="rule").audit_balance(bal)
    assert any(f.code == "constant_series" for f in rep.findings)


def test_balance_detects_stale_data():
    bal = _balance_ok()
    bal["data_date"] = "2024-01-05"
    bal["as_of"] = "2024-01-06"
    rep = Auditor(level="rule").audit_balance(bal)
    assert any(f.code == "data_stale" for f in rep.findings)


def test_balance_non_numeric_value_is_warn_not_zero():
    bal = _balance_ok()
    bal["supply"]["mine"] = "n/a"
    rep = Auditor(level="rule").audit_balance(bal)
    fs = [f for f in rep.findings if f.code == "value_not_numeric"]
    assert fs and fs[0].severity == "warn"


# ─────────────────────────────────────────────── 分级执行（省钱）
def test_rule_level_never_runs_stat_layer():
    """rule 档位下 L2 必须显式记 ran=False（比"字段不存在"更可审计）。"""
    rep = Auditor(level="rule").audit_balance(_balance_ok())
    assert rep.layers["L2_stat"]["ran"] is False
    assert rep.layers["L3_llm"]["ran"] is False


def test_auto_short_circuits_l2_on_l1_error():
    bal = _balance_ok()
    del bal["supply"]
    rep = Auditor(level="auto").audit_balance(bal)
    assert rep.verdict == "fail"
    assert rep.layers["L2_stat"]["ran"] is False


def test_stat_level_runs_stat_layer():
    rep = Auditor(level="stat").audit_balance(_balance_ok())
    assert rep.layers["L2_stat"]["ran"] is True


def test_llm_not_called_without_callback():
    """level=llm 但没传回调：绝不能自己偷偷发请求，且要显式标降级。"""
    calls = []

    def _llm(prompt):
        calls.append(prompt)
        return '{"verdict":"pass","issues":[]}'

    rep = Auditor(level="llm").audit_report("# 铜平衡表\n\n## 供需\n过剩 41.9 万吨",
                                            _balance_ok())
    assert calls == []                      # 没传回调 → 一次都没调
    assert rep.layers["L3_llm"]["ran"] is False
    assert "回调" in rep.layers["L3_llm"]["reason"]
    assert any(f.code == "llm_unavailable" for f in rep.findings)


def test_llm_injected_by_caller_is_used():
    seen = {}

    def _llm(prompt):
        seen["prompt"] = prompt
        return json.dumps({"verdict": "suspect",
                           "issues": [{"code": "wording", "severity": "warn",
                                       "message": "措辞可能误导"}]})

    rep = Auditor(level="llm", llm=_llm).audit_report(
        "# 铜\n\n## 供需\n过剩 41.9 万吨", _balance_ok())
    assert seen.get("prompt")
    assert any(f.code == "wording" for f in rep.findings)
    assert rep.metrics["llm_verdict"] == "suspect"


def test_llm_bad_output_degrades_to_warn():
    rep = Auditor(level="llm", llm=lambda p: "这不是 JSON").audit_report(
        "# 铜\n\n## 供需\n过剩", _balance_ok())
    assert any(f.code == "llm_unparsable" for f in rep.findings)


# ─────────────────────────────────────────────── 报告审计
def test_report_detects_narrative_conflict():
    bal = _balance_ok()          # surplus_deficit = +41.9 → 应累库
    md = "# 铜平衡表\n\n## 结论\n本期明显去库，库存下降。"
    rep = Auditor(level="rule").audit_report(md, bal)
    assert any(f.code == "narrative_conflict" for f in rep.findings)
    assert rep.verdict == "fail"


def test_report_detects_placeholder():
    md = "# 铜\n\n## 供需\n平衡值 TODO\n## 库存\nnan"
    rep = Auditor(level="rule").audit_report(md, _balance_ok())
    assert any(f.code == "placeholder_in_report" for f in rep.findings)


def test_report_detects_unmatched_numbers():
    md = "# 铜\n\n## 供需\n产量 99999 万吨\n## 库存\n库存 88888"
    rep = Auditor(level="rule").audit_report(md, _balance_ok())
    assert any(f.code == "report_numbers_unmatched" for f in rep.findings)


def test_report_empty_is_error():
    rep = Auditor(level="rule").audit_report("", _balance_ok())
    assert rep.verdict == "fail"
    assert any(f.code == "report_empty" for f in rep.findings)


def test_report_numbers_matching_balance_pass():
    md = "# 铜平衡表\n\n## 供需平衡\n精炼总 2857.8 万吨，用量 2815.9 万吨，过剩 41.9 万吨。"
    rep = Auditor(level="rule").audit_report(md, _balance_ok())
    assert not any(f.code == "report_numbers_unmatched" for f in rep.findings)
    # 「万吨」不应被误判成「吨」的单位冲突
    assert not any(f.code == "unit_mismatch" for f in rep.findings)


def test_report_unit_mismatch_when_really_different():
    bal = dict(_balance_ok(), unit="吨")
    md = "# 铜\n\n## 供需\n产量 2857 万吨"
    rep = Auditor(level="rule").audit_report(md, bal)
    assert any(f.code == "unit_mismatch" for f in rep.findings)


# ─────────────────────────────────────────────── 流水线审计
def test_run_audit_flags_failed_steps():
    run = {"run_id": "r1", "pipeline": "balance_sheet", "state": "failed",
           "steps": {"build": {"state": "succeeded"},
                     "report": {"state": "failed"},
                     "final": {"state": "skipped"}}}
    rep = Auditor().audit_run(run)
    codes = {f.code for f in rep.findings}
    assert "steps_failed" in codes and "run_failed" in codes


def test_run_audit_notes_missing_audit_step():
    run = {"run_id": "r2", "pipeline": "balance_sheet", "state": "succeeded",
           "steps": {"build": {"state": "succeeded"},
                     "report": {"state": "succeeded"}}}
    rep = Auditor().audit_run(run)
    assert any(f.code == "no_audit_step" for f in rep.findings)


def test_run_audit_recognises_audit_step():
    run = {"run_id": "r3", "pipeline": "balance_sheet", "state": "succeeded",
           "steps": {"build": {"state": "succeeded", "capability": "A"},
                     "audit": {"state": "succeeded", "capability": "auditor"}}}
    rep = Auditor().audit_run(run)
    assert not any(f.code == "no_audit_step" for f in rep.findings)


# ─────────────────────────────────────────────── 序列审计
def test_series_audit_flags_unit_root():
    rep = Auditor().audit_series(_random_walk(), name="stock")
    assert any(f.code == "unit_root" for f in rep.findings)


def test_series_audit_flags_break():
    rep = Auditor().audit_series(_break_series(), name="price")
    assert any(f.code == "structural_break" for f in rep.findings)


def test_series_audit_flags_constant():
    rep = Auditor().audit_series([7] * 30, name="flat")
    assert rep.verdict == "fail"
    assert any(f.code == "series_constant" for f in rep.findings)


def test_series_audit_short_series_warns():
    rep = Auditor().audit_series([1, 2, 3], name="tiny")
    assert any(f.code == "series_too_short" for f in rep.findings)


# ─────────────────────────────────────────────── 全量 + 动作
def test_full_audit_combines_three_sources():
    bal = _balance_ok()
    md = "# 铜平衡表\n\n## 供需\n过剩 41.9 万吨"
    run = {"run_id": "r4", "pipeline": "balance_sheet", "state": "succeeded",
           "steps": {"build": {"state": "succeeded", "capability": "A"},
                     "report": {"state": "succeeded", "capability": "E"},
                     "audit": {"state": "succeeded", "capability": "auditor"}}}
    rep = Auditor().audit_full(balance=bal, report_md=md, run=run)
    assert rep.kind == "full"
    assert rep.layers["L1_rule"]["ran"] is True
    assert rep.metrics["run_id"] == "r4"


def test_run_action_dispatch():
    out = run_action("balance", {"balance": _balance_ok(), "level": "rule"})
    assert out["verdict"] == "pass"
    assert json.dumps(out)  # 可序列化（要能走 Hub 回传）


def test_run_action_rejects_unknown():
    with pytest.raises(ValueError):
        run_action("nope", {})


# ─────────────────────────────────────────────── 0 token 硬保证
def test_no_llm_sdk_imported():
    info = assert_no_llm_import()
    assert info["ok"] is True, info


def test_audit_module_does_not_import_llm():
    """静态检查：模块源码里不该出现 import openai/anthropic 等。"""
    import inspect
    src = inspect.getsource(audit)
    for bad in ("import openai", "from openai", "import anthropic",
                "from anthropic", "import dashscope"):
        assert bad not in src, f"审计模块不应直接依赖大模型 SDK：{bad}"


def test_markdown_render_has_verdict():
    rep = Auditor(level="rule").audit_balance(_balance_ok())
    md = rep.to_markdown()
    assert "审计：balance" in md and "pass" in md
