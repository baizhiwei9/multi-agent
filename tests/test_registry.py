import pytest

from multi_agent import registry as reg_mod


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(reg_mod.time, "time", lambda: now[0])
    return now


def _manifest(agent_id, roles, ttl=60):
    return {"agent_id": agent_id, "roles": roles, "ttl_s": ttl}


def test_find_by_capability_and_action(clock):
    r = reg_mod.Registry()
    r.register(_manifest("a1", [{"name": "macro", "actions": ["regime"]}]))
    r.register(_manifest("a2", [{"name": "macro", "actions": ["rates"]}]))
    assert [x.agent_id for x in r.find("macro", "regime")] == ["a1"]
    assert [x.agent_id for x in r.find("macro", "rates")] == ["a2"]
    assert r.find("micro", "dialectic") == []


def test_round_robin(clock):
    r = reg_mod.Registry()
    r.register(_manifest("a1", [{"name": "x", "actions": ["y"]}]))
    r.register(_manifest("a2", [{"name": "x", "actions": ["y"]}]))
    first = r.next("x", "y").agent_id
    second = r.next("x", "y").agent_id
    assert {first, second} == {"a1", "a2"}
    assert first != second


def test_ttl_expiry_and_sweep(clock):
    r = reg_mod.Registry()
    r.register(_manifest("a1", [{"name": "x", "actions": ["y"]}], ttl=5))
    assert r.find("x", "y")
    clock[0] += 6
    assert r.find("x", "y") == []
    assert r.sweep() == ["a1"]
    assert r.get("a1") is None


def test_heartbeat_extends(clock):
    r = reg_mod.Registry()
    r.register(_manifest("a1", [{"name": "x", "actions": ["y"]}], ttl=5))
    clock[0] += 4
    assert r.heartbeat("a1") is True
    clock[0] += 4
    assert r.find("x", "y")


def test_labels_filter(clock):
    r = reg_mod.Registry()
    r.register({**_manifest("a1", [{"name": "x", "actions": ["y"]}]), "labels": {"site": "sh"}})
    r.register({**_manifest("a2", [{"name": "x", "actions": ["y"]}]), "labels": {"site": "bj"}})
    assert [x.agent_id for x in r.find("x", "y", labels={"site": "sh"})] == ["a1"]


def test_exclude(clock):
    r = reg_mod.Registry()
    r.register(_manifest("a1", [{"name": "x", "actions": ["y"]}]))
    r.register(_manifest("a2", [{"name": "x", "actions": ["y"]}]))
    assert r.next("x", "y", exclude=["a1"]).agent_id == "a2"


def test_wildcard_action(clock):
    r = reg_mod.Registry()
    r.register(_manifest("a1", [{"name": "ops", "actions": ["*"]}]))
    assert r.next("ops", "anything").agent_id == "a1"