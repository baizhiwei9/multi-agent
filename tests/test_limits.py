"""限额配置测试：默认值、覆盖、校验、截断。"""
from __future__ import annotations

import json

import pytest

from multi_agent import limits as L


def test_defaults_come_from_config_file():
    lim = L.load_limits()
    assert lim.audit.max_citations == 5
    assert lim.audit.max_output_chars == 5000
    assert lim.audit.max_input_chars == 5000
    assert lim.debate.max_rounds == 10
    assert lim.debate.max_message_chars == 5000
    assert lim.debate.max_total_chars == 100000
    assert lim.news.max_topics == 5
    assert lim.roles.default_instances == 1
    assert lim.roles.max_instances_per_role == 50
    assert lim.roles.overrides == {}
    assert lim.instances_for("B", declared=2) == 2      # 角色表声明优先
    assert lim.instances_for("E") == 1                  # 没声明走 default
    assert lim.source.endswith("config/limits.json")


def test_env_override_merges_missing_keys(tmp_path, monkeypatch):
    p = tmp_path / "limits.json"
    p.write_text(json.dumps({"audit": {"max_citations": 3, "max_output_chars": 800}}),
                 encoding="utf-8")
    monkeypatch.setenv("MULTI_AGENT_LIMITS", str(p))
    lim = L.load_limits()
    assert lim.audit.max_citations == 3
    assert lim.audit.max_output_chars == 800
    assert lim.debate.max_rounds == 10          # 没写的字段按默认补
    assert lim.roles.default_instances == 1


@pytest.mark.parametrize("bad", [0, -1, "5", 1.5, True])
def test_invalid_values_raise(tmp_path, bad):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"audit": {"max_citations": bad}}), encoding="utf-8")
    with pytest.raises(L.LimitsError):
        L.load_limits(str(p), reload=True)


def test_unknown_on_exceed_raises(tmp_path):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"audit": {"on_exceed": "ignore"}}), encoding="utf-8")
    with pytest.raises(L.LimitsError):
        L.load_limits(str(p), reload=True)


def test_message_chars_must_fit_total_budget(tmp_path):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"debate": {"max_message_chars": 6000,
                                       "max_total_chars": 5000}}), encoding="utf-8")
    with pytest.raises(L.LimitsError):
        L.load_limits(str(p), reload=True)


def test_unknown_section_raises(tmp_path):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"audit": []}), encoding="utf-8")
    with pytest.raises(L.LimitsError):
        L.load_limits(str(p), reload=True)


def test_truncate_by_chars():
    assert L.truncate("abcdef", 3) == "abc"
    assert L.truncate("abc", 10) == "abc"
    assert L.truncate(None, 5) == ""
    assert L.truncate(12345, 2) == "12"


# ───────────────────────────────────────────── 角色规模（要几个 Agent）
def test_role_overrides_take_precedence(tmp_path):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"roles": {"overrides": {"B": 3}}}), encoding="utf-8")
    lim = L.load_limits(str(p), reload=True)
    assert lim.instances_for("B", declared=2) == 3     # 覆盖优先于角色表
    assert lim.instances_for("E") == 1                 # 未覆盖走兜底


@pytest.mark.parametrize("bad", [{"B": "2"}, {"B": True}, {"B": -1}, {"B": 51},
                                 {"": 1}, {"B": 1.5}])
def test_bad_role_overrides_raise(tmp_path, bad):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"roles": {"overrides": bad}}), encoding="utf-8")
    with pytest.raises(L.LimitsError):
        L.load_limits(str(p), reload=True)


def test_default_instances_must_not_exceed_max(tmp_path):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"roles": {"default_instances": 9,
                                       "max_instances_per_role": 2}}), encoding="utf-8")
    with pytest.raises(L.LimitsError):
        L.load_limits(str(p), reload=True)


def test_co_locate_accepts_role_groups(tmp_path):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"roles": {"co_locate": [["B", "supervisor"]]}}),
                 encoding="utf-8")
    lim = L.load_limits(str(p), reload=True)
    assert lim.roles.co_locate == [["B", "supervisor"]]


@pytest.mark.parametrize("bad", ["B", [["B"]], [["B", "B"]], [["", "supervisor"]], [1]])
def test_bad_co_locate_raise(tmp_path, bad):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"roles": {"co_locate": bad}}), encoding="utf-8")
    with pytest.raises(L.LimitsError):
        L.load_limits(str(p), reload=True)