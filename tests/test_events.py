"""事件官：日期规则、文本抽日期、推理路必须带 verify、去重与截断。

重点不是"能不能猜"，而是**不许无依据地编日期**：
kind=inferred 却没有 verify 的条目一律丢弃；规则求值坏掉只丢那一条。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from multi_agent.frameworks.events import (EventOfficer, from_facts,
                                           from_inference, from_rules,
                                           last_business_day, nth_weekday,
                                           parse_day)

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "config" / "events" / "rules.json"


# ───────────────────────────────────────────── 日期工具
def test_last_business_day_skips_weekend():
    # 2026-11-30 周一；2026-05-31 周日 → 退到 5-29 周五
    assert last_business_day(2026, 11) == date(2026, 11, 30)
    assert last_business_day(2026, 5) == date(2026, 5, 29)
    assert last_business_day(2026, 2) == date(2026, 2, 27)


def test_nth_weekday_in_month():
    # 2026-11 的周五：6/13/20/27
    assert nth_weekday(2026, 11, 4, 3) == date(2026, 11, 20)
    assert nth_weekday(2026, 11, 4, 5) is None       # 当月没第 5 个周五


def test_parse_day_accepts_three_formats_and_rejects_junk():
    assert parse_day("2026-11-30") == date(2026, 11, 30)
    assert parse_day("20261130") == date(2026, 11, 30)
    assert parse_day("2026/11/30") == date(2026, 11, 30)
    with pytest.raises(ValueError):
        parse_day("11月30日")


# ───────────────────────────────────────────── 规则路
def _rules():
    return json.loads(RULES.read_text(encoding="utf-8"))["rules"]


def test_default_rules_find_msci_on_month_end():
    out = EventOfficer(today="2026-11-28", horizon_days=7, rules=_rules()).scan()
    ids = {e["source"] for e in out["events"]}
    assert "rule:msci_semiannual" in ids
    assert all(e["date"] == "2026-11-30" for e in out["events"])
    assert all(e["kind"] == "inferred" and e["verify"] for e in out["events"])


def test_horizon_gates_far_events():
    near = EventOfficer(today="2026-11-28", horizon_days=1, rules=_rules()).scan()
    far = EventOfficer(today="2026-11-28", horizon_days=3, rules=_rules()).scan()
    assert near["events"] == []
    assert len(far["events"]) == 2


def test_quarter_end_rule_only_in_quarter_months():
    errors = []
    assert from_rules(date(2026, 11, 28), 7, _rules(), errors) != []
    assert [e for e in from_rules(date(2026, 11, 28), 7, _rules(), errors)
            if "季末" in e["event"]] == []
    jun = [e for e in from_rules(date(2026, 6, 26), 7, _rules(), errors)
           if "季末" in e["event"]]
    assert jun and jun[0]["date"] == "2026-06-30"


def test_broken_rule_只丢自己并记错误():
    rules = [
        {"id": "bad", "when": {"nth_weekday_in_month": {"weekday": "xxx"}},
         "event": "坏规则"},
        {"id": "ok", "when": {"month_end_last_business_day": True},
         "event": "月末再平衡", "kind": "inferred", "verify": "查日历"},
    ]
    errors = []
    out = from_rules(date(2026, 11, 28), 7, rules, errors)
    assert [e["source"] for e in out] == ["rule:ok"]
    assert errors == []


def test_empty_rules_is_reported_not_silent():
    out = EventOfficer(today="2026-11-28", rules=[]).scan()
    assert out["events"] == []
    assert any("规则表为空" in e for e in out["meta"]["errors"])


# ───────────────────────────────────────────── 事实路
def test_facts_need_date_and_event_word():
    facts = [
        {"title": "明晟公司 11月30日 收盘后实施指数调整", "source": "新浪"},
        {"title": "今天天气不错", "source": "新浪"},
        {"title": "指数 11月30日 有变化", "source": "新浪"},   # 无事件词
    ]
    out = from_facts(facts, date(2026, 11, 28), 7)
    assert len(out) == 1
    assert out[0]["kind"] == "fact" and out[0]["date"] == "2026-11-30"


def test_facts_month_day_rolls_into_next_year():
    out = from_facts([{"title": "1月5日 生效"}], date(2026, 12, 30), 7)
    assert out and out[0]["date"] == "2027-01-05"


# ───────────────────────────────────────────── 推理路
def test_inference_without_verify_is_dropped():
    def infer(_payload):
        return json.dumps([
            {"date": "2026-12-01", "event": "MSCI 生效", "basis": "先验"},      # 无 verify
            {"date": "2026-12-01", "event": "MSCI 生效", "basis": "先验",
             "verify": "查 MSCI 官方公告"},                                   # 有 verify
            {"date": "明年", "event": "认不出的日期", "verify": "x"},
        ])
    errors = []
    out = from_inference(date(2026, 11, 28), 7, [], infer, errors)
    assert len(out) == 1
    assert out[0]["kind"] == "inferred" and out[0]["source"] == "infer"


def test_inference_bad_json_is_error_not_crash():
    errors = []
    out = from_inference(date(2026, 11, 28), 7, [], lambda p: "不是 JSON", errors)
    assert out == [] and errors and "不是 JSON" in errors[0]


def test_inference_callback_exception_is_contained():
    def boom(_payload):
        raise RuntimeError("端点 500")
    errors = []
    assert from_inference(date(2026, 11, 28), 7, [], boom, errors) == []
    assert "端点 500" in errors[0]


def test_inference_not_enabled_when_no_callable():
    assert from_inference(date(2026, 11, 28), 7, [], None, []) == []


# ───────────────────────────────────────────── 合并
def test_same_event_from_two_paths_is_deduped():
    facts = [{"title": "MSCI 半年度指数审议实施（收盘后调仓）11月30日 收盘实施"}]
    out = EventOfficer(today="2026-11-28", horizon_days=7, rules=_rules()) \
        .scan(facts=facts)
    keys = [(e["date"], e["event"][:30]) for e in out["events"]]
    assert len(keys) == len(set(keys))
    assert out["meta"]["counts"]["fact"] == 1


def test_scan_sorted_by_date_and_counts_consistent():
    out = EventOfficer(today="2026-06-26", horizon_days=7, rules=_rules()).scan()
    dates = [e["date"] for e in out["events"]]
    assert dates == sorted(dates)
    c = out["meta"]["counts"]
    assert c["fact"] + c["inferred"] == len(out["events"])
    assert out["meta"]["rules_loaded"] == len(_rules())