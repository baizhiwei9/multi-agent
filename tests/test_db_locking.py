"""DBManager 读写锁测试。

大佬 18:17：
> 你这几个写公用数据库，公用的数据是不是要多进程写锁呀，就保护锁呀。
> 虽然有一个 DBManager，应该不用，但是你 DBManager 应该是多线程的。
> 因为我不同的 agent 去，有的 agent 去读这个数据库，有的 agent 去写这个数据库，
> 都传到这个 manager 上面了，那你这 manager 就至少要有锁，如果时间方便的话，你要读写锁。

要验证的事：
1. **多读真的并行**（不是排队）—— 这是读写锁存在的唯一理由
2. **写独占**：写的时候不能有读者读到半成品
3. **写优先**：高频读不能把写饿死
4. **多线程必须各自持连接**（sqlite 单连接并发读是未定义行为）
5. **跨进程文件锁**仍然有效（多 DBManager 实例写同一文件）
"""
import os
import sqlite3
import tempfile
import threading
import time

import pytest

from multi_agent.dbmanager import DBManager, RWLock


# ───────────────────────────────────────────── 夹具
@pytest.fixture
def db_path():
    p = tempfile.mktemp(suffix=".db")
    yield p
    for suffix in ("", ".lock", "-wal", "-shm"):
        try:
            os.unlink(p + suffix)
        except OSError:
            pass


@pytest.fixture
def db(db_path):
    d = DBManager(db_path)
    d.write("t", "k0", {"v": 0})
    yield d
    d.close()


# ───────────────────────────────────────────── RWLock 单元
def test_rwlock_allows_concurrent_readers():
    """核心：多个读者能同时进入临界区。"""
    rw = RWLock()
    inside = []
    peak = [0]
    barrier = threading.Barrier(5, timeout=5)

    def reader(i):
        with rw.read():
            inside.append(i)
            peak[0] = max(peak[0], len(inside))
            barrier.wait(timeout=5)      # 5 个都进来才继续
            inside.remove(i)

    ts = [threading.Thread(target=reader, args=(i,)) for i in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)
    assert peak[0] >= 2, f"读者没有并行（峰值 {peak[0]}）"


def test_rwlock_writer_excludes_everyone():
    rw = RWLock()
    events = []

    def reader():
        with rw.read():
            events.append("read_start")
            time.sleep(0.15)
            events.append("read_end")

    t = threading.Thread(target=reader)
    t.start()
    time.sleep(0.03)
    with rw.write():
        events.append("write")
    t.join(timeout=5)
    # 写不能插在读中间
    assert events.index("write") in (0, len(events) - 1) or \
        "read_start" not in events[:events.index("write")]
    assert events.count("write") == 1


def test_rwlock_writer_priority():
    """写者等待时，新读者要让路（否则高频读把写饿死）。"""
    rw = RWLock()
    order = []
    got_write = threading.Event()

    def long_reader():
        with rw.read():
            time.sleep(0.2)                # 占着读锁

    def writer():
        with rw.write():
            order.append("write")
            got_write.set()

    r1 = threading.Thread(target=long_reader)
    r1.start()
    time.sleep(0.03)
    w = threading.Thread(target=writer)
    w.start()
    time.sleep(0.03)                       # 让 writer 进入等待队列

    def late_reader():
        with rw.read():
            order.append("late_read")

    r2 = threading.Thread(target=late_reader)
    r2.start()

    r1.join(timeout=5)
    w.join(timeout=5)
    r2.join(timeout=5)
    # 后来的读者必须排在写之后
    assert order and order[0] == "write", f"写被饿死：{order}"


def test_rwlock_stats():
    rw = RWLock()
    with rw.read():
        pass
    with rw.write():
        pass
    s = rw.snapshot()
    assert s["read_acquired"] == 1 and s["write_acquired"] == 1
    assert s["readers_now"] == 0 and s["writer_now"] is False


def test_rwlock_timeout():
    rw = RWLock()
    rw._writer = True                      # 手工占住写锁
    with pytest.raises(TimeoutError):
        with rw.read(timeout=0.1):
            pass


# ───────────────────────────────────────────── DBManager 并发
def test_concurrent_reads_actually_parallel(db):
    """8 线程 × 200 次读，峰值并发读者要 > 1。"""
    N = 8
    done = {}

    def reader(i):
        for _ in range(200):
            db.read("t", "k0")
        done[i] = True

    ts = [threading.Thread(target=reader, args=(i,)) for i in range(N)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)

    assert len(done) == N
    st = db.lock_stats()
    assert st["rw"]["max_concurrent_readers"] > 1, \
        f"读完全串行了：{st['rw']}"


def test_per_thread_connection(db):
    """每线程必须有独立连接 —— sqlite 单连接并发读是未定义行为。"""
    conns = {}

    def worker(i):
        conns[i] = id(db._current)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)
    assert len(set(conns.values())) == 4, f"线程共享了连接：{conns}"


def test_write_not_starved_by_readers(db):
    """6 个读线程持续压着，写仍要能快速完成。"""
    stop = threading.Event()

    def read_loop():
        while not stop.is_set():
            db.read("t", "k0")

    readers = [threading.Thread(target=read_loop, daemon=True) for _ in range(6)]
    for r in readers:
        r.start()
    time.sleep(0.1)
    try:
        t0 = time.time()
        for i in range(30):
            db.write("t", f"w{i}", {"v": i})
        elapsed = time.time() - t0
        assert elapsed < 5.0, f"写被读饿死了：{elapsed:.2f}s / 30 次"
    finally:
        stop.set()
        time.sleep(0.2)


def test_write_visible_to_all_readers(db):
    """写完之后，所有线程都能读到新值（没有缓存的陈旧连接）。"""
    db.write("t", "shared", {"v": 1})
    seen = {}

    def reader(i):
        seen[i] = (db.read("t", "shared") or {}).get("value", {}).get("v")

    ts = [threading.Thread(target=reader, args=(i,)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)
    assert all(v == 1 for v in seen.values()), seen


def test_concurrent_writers_serialized(db):
    """多个线程同时写：总数要对，不能丢写（写锁必须真的串行）。"""
    N_THREADS, N_WRITES = 6, 50

    def writer(i):
        for j in range(N_WRITES):
            db.write("t", f"t{i}-{j}", {"i": i, "j": j})

    ts = [threading.Thread(target=writer, args=(i,)) for i in range(N_THREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=60)
    got = db.list("t", limit=1000)["items"]
    # 初始 1 条 + 6×50
    assert len(got) == 1 + N_THREADS * N_WRITES, f"丢了写：只有 {len(got)} 条"


def test_mixed_read_write_no_deadlock(db):
    """读写混合不卡死（最容易出问题的地方）。"""
    stop = threading.Event()
    errors = []

    def reader():
        while not stop.is_set():
            try:
                db.read("t", "k0")
                db.list("t", limit=10)
                db.stats()
            except Exception as e:  # noqa: BLE001
                errors.append(f"reader: {type(e).__name__}: {e}")
                return

    def writer():
        i = 0
        while not stop.is_set() and i < 100:
            try:
                db.write("t", f"m{i}", {"i": i})
                db.append("s", {"i": i})
                i += 1
            except Exception as e:  # noqa: BLE001
                errors.append(f"writer: {type(e).__name__}: {e}")
                return

    ts = [threading.Thread(target=reader) for _ in range(4)]
    ts += [threading.Thread(target=writer) for _ in range(2)]
    for t in ts:
        t.start()
    time.sleep(1.0)
    stop.set()
    for t in ts:
        t.join(timeout=10)
    assert not errors, errors


# ───────────────────────────────────────────── 跨进程文件锁
def test_file_lock_across_managers(db_path):
    """两个 DBManager 实例写同一文件：靠文件锁串行，不能报 locked。"""
    a = DBManager(db_path)
    b = DBManager(db_path)
    errors = []

    def w1():
        for i in range(60):
            try:
                a.write("x", f"a{i}", {"i": i})
            except Exception as e:  # noqa: BLE001
                errors.append(f"a: {type(e).__name__}: {e}")

    def w2():
        for i in range(60):
            try:
                b.write("x", f"b{i}", {"i": i})
            except Exception as e:  # noqa: BLE001
                errors.append(f"b: {type(e).__name__}: {e}")

    t1 = threading.Thread(target=w1)
    t2 = threading.Thread(target=w2)
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)
    a.close()
    b.close()

    assert not errors, errors
    # 用新连接验证总数
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM docs WHERE collection='x'").fetchone()[0]
    conn.close()
    assert n == 120, f"丢写：{n}/120"


def test_lock_stats_exposed(db):
    st = db.lock_stats()
    assert "rw" in st and "connections" in st
    assert st["connections"] >= 1


def test_lock_stats_via_attach():
    """能通过 db/lock_stats 动作查锁状态（排障用）。"""
    d = DBManager(":memory:")

    class FakeRT:
        def __init__(self):
            self.handlers = {}

        def add_handler(self, cap, act, fn):
            self.handlers[(cap, act)] = fn

    rt = FakeRT()
    d.attach(rt)
    assert ("db", "lock_stats") in rt.handlers
    out = rt.handlers[("db", "lock_stats")]({}, {})
    assert "rw" in out
    d.close()


# ───────────────────────────────────────────── 边界
def test_memory_db_still_works():
    """`:memory:` 库没有文件锁，但读写锁/线程本地连接照样要工作。"""
    d = DBManager(":memory:")
    d.write("t", "k", {"v": 1})
    assert d.read("t", "k")["value"]["v"] == 1
    assert d.lock_stats()["file_lock"] is False
    d.close()


def test_reader_sees_own_thread_writes():
    """同一线程写完立刻读，必须读到（连接是线程本地的，不能读到别的线程缓存）。"""
    d = DBManager(":memory:")
    d.write("t", "k", {"v": 42})
    assert d.read("t", "k")["value"]["v"] == 42
    d.close()
