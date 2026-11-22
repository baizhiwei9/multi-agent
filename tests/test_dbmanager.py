import pytest

from multi_agent.client import AgentRuntime, HubError
from multi_agent.dbmanager import DBClient, DBManager
from multi_agent.server import HubServer

TOKEN = "db-token"


@pytest.fixture
def stack():
    srv = HubServer(host="127.0.0.1", port=0, db_path=":memory:", token=TOKEN)
    srv.start_background()
    dbm = DBManager(":memory:")
    rt = AgentRuntime(srv.url, "db-manager",
                      [{"name": "db", "actions": list(DBManager.ACTIONS)}],
                      token=TOKEN, concurrency=4, poll_wait=1)
    dbm.attach(rt)
    rt.start()
    yield srv, dbm, DBClient(srv.url, token=TOKEN)
    rt.stop()
    srv.stop()
    dbm.close()


def test_write_read(stack):
    _, _, db = stack
    db.write("balance", "copper:2026-11", {"deficit": -15.0, "unit": "万吨"})
    got = db.read("balance", "copper:2026-11")
    assert got["found"] is True
    assert got["value"]["deficit"] == -15.0


def test_merge(stack):
    _, _, db = stack
    db.write("c", "k", {"a": 1, "b": 2})
    db.write("c", "k", {"b": 3}, merge=True)
    assert db.read("c", "k")["value"] == {"a": 1, "b": 3}


def test_list_prefix_and_delete(stack):
    _, _, db = stack
    db.write("balance", "copper:1", {"x": 1})
    db.write("balance", "copper:2", {"x": 2})
    db.write("balance", "gold:1", {"x": 3})
    items = db.list("balance", prefix="copper:")["items"]
    assert [i["key"] for i in items] == ["copper:1", "copper:2"]
    assert db.delete("balance", "copper:1")["deleted"] == 1
    assert db.read("balance", "copper:1")["found"] is False


def test_stream_append_tail(stack):
    _, _, db = stack
    for i in range(3):
        db.append("news", {"i": i})
    tail = db.tail("news", limit=2)
    assert [i["payload"]["i"] for i in tail["items"]] == [1, 2]
    after = db.tail("news", after=tail["items"][0]["seq"])
    assert [i["payload"]["i"] for i in after["items"]] == [2]


def test_schema_auto_and_explicit(stack):
    srv, dbm, db = stack
    db.write("balance", "x", {"a": 1, "b": 2})
    cols = db.collections()["schemas"]["balance"]
    assert cols == ["a", "b"]
    db.register_schema("custom", ["z"])
    assert db.collections()["schemas"]["custom"] == ["z"]


def test_invalid_params_return_error(stack):
    _, _, db = stack
    with pytest.raises(HubError):
        db.write("", "k", {"a": 1})
    with pytest.raises(HubError):
        db.read("c", "")

def test_cross_instance_file_lock(tmp_path):
    """两个 DBManager 实例写同一 SQLite 文件：文件锁 + WAL 不能丢写/报锁冲突。"""
    import threading

    path = str(tmp_path / "shared.db")
    a = DBManager(path)
    b = DBManager(path)

    def write_many(dbm, prefix):
        for i in range(20):
            dbm.write("concurrent", f"{prefix}:{i}", {"i": i})

    threads = [threading.Thread(target=write_many, args=(a, "a")),
               threading.Thread(target=write_many, args=(b, "b"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        items = a.list("concurrent", limit=100)["items"]
        assert len(items) == 40
    finally:
        a.close()
        b.close()
