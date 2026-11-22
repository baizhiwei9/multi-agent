import pytest

from multi_agent.nat import HolePuncher, RendezvousServer

TOKEN = "nat-token"


@pytest.fixture
def rv():
    s = RendezvousServer(host="127.0.0.1", port=0, token=TOKEN)
    s.start_background()
    yield s
    s.stop()


def test_hello_observes_endpoint(rv):
    p = HolePuncher("a", ("127.0.0.1", rv.port), token=TOKEN, timeout=5)
    try:
        info = p.start()
        assert p.observed and p.observed[0] == "127.0.0.1"
        assert "a" in rv.peers()
    finally:
        p.close()


def test_direct_punch_and_bidirectional(rv):
    a = HolePuncher("a", ("127.0.0.1", rv.port), token=TOKEN, timeout=5)
    b = HolePuncher("b", ("127.0.0.1", rv.port), token=TOKEN, timeout=5)
    a.start()
    b.start()
    try:
        res = a.connect("b", timeout=5)
        assert res["mode"] == "direct"
        a.send(b"hello")
        assert b.recv(5) == b"hello"
        b.send(b"world")
        assert a.recv(5) == b"world"
    finally:
        a.close()
        b.close()


def test_relay_fallback(rv):
    a = HolePuncher("a", ("127.0.0.1", rv.port), token=TOKEN,
                    force_relay=True, timeout=5)
    b = HolePuncher("b", ("127.0.0.1", rv.port), token=TOKEN, timeout=5)
    a.start()
    b.start()
    try:
        res = a.connect("b")
        assert res["mode"] == "relay"
        a.send(b"via relay")
        assert b.recv(5) == b"via relay"
        b.send(b"relay reply")
        assert a.recv(5) == b"relay reply"
    finally:
        a.close()
        b.close()


def test_unauthorized(rv):
    resp = rv.handle({"type": "hello", "agent_id": "x", "token": "bad"},
                     ("127.0.0.1", 1000))
    assert resp["code"] == "unauthorized"
    with pytest.raises(RuntimeError):
        HolePuncher("x", ("127.0.0.1", rv.port), token="bad", timeout=1).start()


def test_punch_unknown_peer(rv):
    resp = rv.handle({"type": "punch", "from": "a", "to": "nobody", "token": TOKEN},
                     ("127.0.0.1", 1000))
    assert resp["code"] == "peer_offline"