import pytest

from multi_agent import protocol


def test_envelope_and_task_validate():
    msg = protocol.envelope("task", task_id="t1", capability="A", action="build_sheet")
    assert protocol.validate(msg)["task_id"] == "t1"


def test_register_roles_normalized():
    msg = protocol.envelope("register", agent_id="a1",
                            roles=[{"name": "A", "actions": ["build"], "description": "建表"}])
    out = protocol.validate(msg)
    assert out["roles"][0]["description"] == "建表"


def test_register_requires_actions():
    msg = protocol.envelope("register", agent_id="a1", roles=[{"name": "A", "actions": []}])
    with pytest.raises(protocol.ProtocolError):
        protocol.validate(msg)


def test_version_mismatch():
    msg = protocol.envelope("heartbeat", agent_id="a1")
    msg["v"] = 2
    with pytest.raises(protocol.ProtocolError):
        protocol.validate(msg)


def test_unknown_type():
    with pytest.raises(protocol.ProtocolError):
        protocol.validate({"v": 1, "type": "hello", "id": "x"})


def test_task_bad_timeout():
    msg = protocol.envelope("task", task_id="t", capability="A", action="b", timeout_s=-1)
    with pytest.raises(protocol.ProtocolError):
        protocol.validate(msg)


def test_failed_result_needs_error():
    msg = protocol.envelope("result", task_id="t", agent_id="a", ok=False)
    with pytest.raises(protocol.ProtocolError):
        protocol.validate(msg)


def test_failed_result_with_known_code():
    msg = protocol.envelope("result", task_id="t", agent_id="a", ok=False,
                            error={"code": "handler_error", "message": "boom"})
    assert protocol.validate(msg)["ok"] is False