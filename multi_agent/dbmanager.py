"""DBManager：全平台唯一的数据库读写入口。

为什么不让每个 Agent 直连库
--------------------------
- 只有一个写入口，schema/事务/审计集中，不会出现"各写各的"；
- Agent 不持有 DB 路径/账号，权限只到 `db/*` 这组动作；
- 换库（SQLite → Postgres）只改 DBManager，Agent 无感。

存储模型
--------
- `docs(collection, key, value)`：按 key 的文档表（平衡表、估算结果、报告索引）；
- `streams(stream, seq, payload)`：只追加的事件流（新闻、修正事件、审计）；
- `schemas(collection, columns)`：集合的字段登记（第一次写入自动登记）。

动作（capability = "db"）
------------------------
write / read / list / delete / append / tail / register_schema / collections / stats
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # POSIX 文件锁；Windows 下退化为进程内锁
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore

from .client import HubClient, HubError

_DDL = """
CREATE TABLE IF NOT EXISTS docs (
    collection TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated REAL NOT NULL,
    PRIMARY KEY (collection, key)
);
CREATE TABLE IF NOT EXISTS streams (
    stream TEXT NOT NULL,
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    payload TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_streams_stream ON streams(stream, seq);
CREATE TABLE IF NOT EXISTS schemas (
    collection TEXT PRIMARY KEY,
    columns TEXT NOT NULL,
    registered REAL NOT NULL
);
"""


class FileLock:
    """跨进程写锁（fcntl.flock）；同一进程内再由 RLock 串行化。

    SQLite 自己的锁只保证单连接/单事务；多个 DBManager 进程同时写同一文件时，
    仍需要一把文件锁把"写事务"串起来，避免 busy/locked。
    """

    def __init__(self, path: Path, timeout: float = 10.0):
        self.path = Path(path)
        self.timeout = timeout
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        if fcntl is None:
            return
        deadline = time.time() + self.timeout
        while True:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.time() >= deadline:
                    self._fh.close()
                    self._fh = None
                    raise TimeoutError(f"获取数据库文件锁超时：{self.path}")
                time.sleep(0.05)

    def release(self) -> None:
        if not self._fh:
            return
        if fcntl is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        self._fh.close()
        self._fh = None



class RWLock:
    """读写锁（写优先，避免写饥饿）。

    为什么 DBManager 需要它
    ----------------------
    多个 Agent 会同时打到一个 DBManager 上：有的读（E 出报告要读平衡表）、
    有的写（A 建表、B 塞新闻、Q 写判定）。如果只用一把互斥锁：

        读 → 阻塞 → 读          ← 纯读之间也排队，白白串行
        写 → 阻塞 → 读/写

    读写锁允许**多读并行**，只在有写的时候独占：

        多读  │▮▮▮▮▮▮▮│  并行
        写    │       ▮ │  独占

    写优先：有写者等待时，新来的读者要让路，否则高频读会把写饿死。

    注意（很重要）：这里的 `read()` 只保证"读者之间不互斥"，
    真正让读能并行的前提是**每个线程用自己的 sqlite 连接**（见 DBManager._conn）。
    单连接上并发读是未定义行为，光有锁救不了。
    """

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.RLock())
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0
        self.stats = {"read_acquired": 0, "write_acquired": 0,
                      "read_waits": 0, "write_waits": 0,
                      "max_concurrent_readers": 0}

    @contextmanager
    def read(self, timeout: Optional[float] = None):
        deadline = None if timeout is None else time.time() + timeout
        with self._cond:
            waited = False
            while self._writer or self._waiting_writers > 0:
                if deadline is not None and time.time() >= deadline:
                    raise TimeoutError("获取读锁超时")
                waited = True
                remain = None if deadline is None else max(0.01, deadline - time.time())
                if not self._cond.wait(remain):
                    if deadline is not None and time.time() >= deadline:
                        raise TimeoutError("获取读锁超时")
            self._readers += 1
            self.stats["read_acquired"] += 1
            if waited:
                self.stats["read_waits"] += 1
            self.stats["max_concurrent_readers"] = max(
                self.stats["max_concurrent_readers"], self._readers)
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @contextmanager
    def write(self, timeout: Optional[float] = None):
        deadline = None if timeout is None else time.time() + timeout
        with self._cond:
            self._waiting_writers += 1
            waited = False
            try:
                while self._writer or self._readers > 0:
                    if deadline is not None and time.time() >= deadline:
                        raise TimeoutError("获取写锁超时")
                    waited = True
                    remain = None if deadline is None else max(0.01, deadline - time.time())
                    if not self._cond.wait(remain):
                        if deadline is not None and time.time() >= deadline:
                            raise TimeoutError("获取写锁超时")
            finally:
                self._waiting_writers -= 1
            self._writer = True
            self.stats["write_acquired"] += 1
            if waited:
                self.stats["write_waits"] += 1
        try:
            yield
        finally:
            with self._cond:
                self._writer = False
                self._cond.notify_all()

    def snapshot(self) -> Dict[str, Any]:
        with self._cond:
            return {**self.stats, "readers_now": self._readers,
                    "writer_now": self._writer,
                    "waiting_writers": self._waiting_writers}


class DBManager:
    ACTIONS = ("write", "read", "list", "delete", "append", "tail",
               "register_schema", "collections", "stats", "lock_stats")

    def __init__(self, path: str = "data.db"):
        self.path = path
        # ★ `:memory:` 的每条连接是**各自独立的内存库**，
        #   线程本地连接化之后会互相看不见表 → 用共享缓存 URI，
        #   让所有连接连到同一个内存库（真文件库不受影响）。
        #   同时这也是 :memory: 能"多读并行"的唯一办法。
        # 名字必须唯一且不复用：早先用 id(self)，对象回收后 id 会被复用，
        # 新库可能撞上还没释放的旧内存库（残留连接让它活着），读到别人的行。
        self._is_memory = path in (":memory:", "", None)
        self._connect_target = (
            f"file:memdb-{uuid.uuid4().hex}?mode=memory&cache=shared"
            if self._is_memory else path)
        # ★ 读写锁：多读并行，写独占（写优先防饿死）
        self._rw = RWLock()
        # 兼容旧代码：_lock 现在只是"跨线程串行化建连"用，不再当读写锁用
        self._lock = threading.RLock()
        self._lock_path = Path(str(path) + ".lock") if path != ":memory:" else None
        self._file_lock = FileLock(self._lock_path) if self._lock_path else None
        # ★ 每线程一条连接：sqlite 单连接上并发读是未定义行为，
        #   只有"每线程独立连接 + WAL"才能真正多读并行。
        self._local = threading.local()
        self._all_conns: List[sqlite3.Connection] = []
        self._conns_lock = threading.RLock()

        main = self._connect()
        with self._lock:
            if not self._is_memory:
                main.execute("PRAGMA journal_mode=WAL")
                main.execute("PRAGMA synchronous=NORMAL")
            main.execute("PRAGMA busy_timeout=5000")
            main.executescript(_DDL)
            main.commit()
        # 对外仍暴露 _conn（清理逻辑等会用到），指向主线程连接
        self._conn = main

    def _connect(self) -> sqlite3.Connection:
        """取本线程的连接；没有就建一条（WAL 下多连接并发读没问题）。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None and not getattr(self._local, "closed", False):
            return conn
        conn = sqlite3.connect(self._connect_target, check_same_thread=False,
                               timeout=10.0, uri=self._is_memory)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        if not self._is_memory:
            conn.execute("PRAGMA journal_mode=WAL")
        self._local.conn = conn
        self._local.closed = False
        with self._conns_lock:
            self._all_conns.append(conn)
        return conn

    @property
    def _current(self) -> sqlite3.Connection:
        """当前线程应使用的连接。"""
        return getattr(self._local, "conn", None) or self._connect()

    @contextmanager
    def _read(self):
        """读：读写锁的读侧（多读并行）+ 本线程连接。"""
        with self._rw.read():
            yield self._current

    @contextmanager
    def _write_tx(self):
        """写事务：写锁（进程内独占）+ 文件锁（跨进程）+ BEGIN IMMEDIATE。

        三层锁各管一段，缺一不可：
          · RWLock.write()  —— 同进程内多线程（写独占、读者让路）
          · FileLock        —— 多进程/多 DBManager 实例写同一文件
          · BEGIN IMMEDIATE —— SQLite 自己的事务边界
        """
        with self._rw.write():
            conn = self._current
            if self._file_lock:
                self._file_lock.acquire()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                if self._file_lock:
                    self._file_lock.release()

    # ---------- 挂到 Agent ----------
    def attach(self, runtime) -> None:
        runtime.add_handler("db", "write", lambda p, c: self.write(**p))
        runtime.add_handler("db", "read", lambda p, c: self.read(**p))
        runtime.add_handler("db", "list", lambda p, c: self.list(**p))
        runtime.add_handler("db", "delete", lambda p, c: self.delete(**p))
        runtime.add_handler("db", "append", lambda p, c: self.append(**p))
        runtime.add_handler("db", "tail", lambda p, c: self.tail(**p))
        runtime.add_handler("db", "register_schema", lambda p, c: self.register_schema(**p))
        runtime.add_handler("db", "collections", lambda p, c: self.collections())
        runtime.add_handler("db", "stats", lambda p, c: self.stats())
        runtime.add_handler("db", "lock_stats", lambda p, c: self.lock_stats())

    # ---------- 文档 ----------
    def write(self, collection: str, key: str, value: Any,
              merge: bool = False) -> Dict[str, Any]:
        collection, key = _req_str(collection, "collection"), _req_str(key, "key")
        now = time.time()
        with self._write_tx() as conn:
            if merge:
                row = conn.execute(
                    "SELECT value FROM docs WHERE collection=? AND key=?", (collection, key)
                ).fetchone()
                if row:
                    base = json.loads(row["value"])
                    if not isinstance(base, dict) or not isinstance(value, dict):
                        raise ValueError("merge=True 只支持对象与对象合并")
                    value = {**base, **value}
            conn.execute(
                "INSERT INTO docs(collection,key,value,updated) VALUES(?,?,?,?) "
                "ON CONFLICT(collection,key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                (collection, key, json.dumps(value, ensure_ascii=False), now),
            )
            _register_schema_locked(conn, collection, value)
        return {"collection": collection, "key": key, "updated": now}

    def read(self, collection: str, key: str) -> Dict[str, Any]:
        collection, key = _req_str(collection, "collection"), _req_str(key, "key")
        with self._read() as conn:
            row = conn.execute(
                "SELECT value, updated FROM docs WHERE collection=? AND key=?", (collection, key)
            ).fetchone()
        if not row:
            return {"collection": collection, "key": key, "found": False, "value": None}
        return {"collection": collection, "key": key, "found": True,
                "value": json.loads(row["value"]), "updated": row["updated"]}

    def list(self, collection: str, prefix: str = "", limit: int = 100) -> Dict[str, Any]:
        collection = _req_str(collection, "collection")
        limit = max(1, min(int(limit), 1000))
        sql = "SELECT key, value, updated FROM docs WHERE collection=?"
        args: List[Any] = [collection]
        if prefix:
            sql += " AND key LIKE ?"
            args.append(prefix + "%")
        sql += " ORDER BY key LIMIT ?"
        args.append(limit)
        with self._read() as conn:
            rows = conn.execute(sql, args).fetchall()
        return {"collection": collection,
                "items": [{"key": r["key"], "value": json.loads(r["value"]),
                           "updated": r["updated"]} for r in rows]}

    def delete(self, collection: str, key: str) -> Dict[str, Any]:
        collection, key = _req_str(collection, "collection"), _req_str(key, "key")
        with self._write_tx() as conn:
            cur = conn.execute(
                "DELETE FROM docs WHERE collection=? AND key=?", (collection, key))
        return {"deleted": cur.rowcount, "collection": collection, "key": key}

    # ---------- 事件流 ----------
    def append(self, stream: str, payload: Any) -> Dict[str, Any]:
        stream = _req_str(stream, "stream")
        now = time.time()
        with self._write_tx() as conn:
            cur = conn.execute(
                "INSERT INTO streams(stream,payload,created) VALUES(?,?,?)",
                (stream, json.dumps(payload, ensure_ascii=False), now))
            seq = cur.lastrowid
        return {"stream": stream, "seq": seq, "created": now}

    def tail(self, stream: str, limit: int = 50, after: int = 0) -> Dict[str, Any]:
        stream = _req_str(stream, "stream")
        limit = max(1, min(int(limit), 1000))
        with self._read() as conn:
            rows = conn.execute(
                "SELECT seq, payload, created FROM streams WHERE stream=? AND seq>? "
                "ORDER BY seq DESC LIMIT ?", (stream, int(after), limit)
            ).fetchall()
        items = [{"seq": r["seq"], "payload": json.loads(r["payload"]),
                  "created": r["created"]} for r in reversed(rows)]
        return {"stream": stream, "items": items}

    # ---------- schema / 统计 ----------
    def register_schema(self, collection: str, columns: List[str]) -> Dict[str, Any]:
        collection = _req_str(collection, "collection")
        if not isinstance(columns, list) or not columns:
            raise ValueError("columns 必须是非空数组")
        now = time.time()
        with self._write_tx() as conn:
            conn.execute(
                "INSERT INTO schemas(collection,columns,registered) VALUES(?,?,?) "
                "ON CONFLICT(collection) DO UPDATE SET columns=excluded.columns",
                (collection, json.dumps([str(c) for c in columns], ensure_ascii=False), now))
        return {"collection": collection, "columns": columns}

    def collections(self) -> Dict[str, Any]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT collection, COUNT(*) n FROM docs GROUP BY collection ORDER BY collection"
            ).fetchall()
            schemas = conn.execute(
                "SELECT collection, columns FROM schemas ORDER BY collection").fetchall()
        return {"collections": [{"name": r["collection"], "docs": r["n"]} for r in rows],
                "schemas": {r["collection"]: json.loads(r["columns"]) for r in schemas}}

    def stats(self) -> Dict[str, Any]:
        with self._read() as conn:
            docs = conn.execute("SELECT COUNT(*) n FROM docs").fetchone()["n"]
            streams = conn.execute("SELECT COUNT(*) n FROM streams").fetchone()["n"]
        return {"docs": docs, "streams": streams, "path": self.path}

    # 兼容旧调用名（内部实现已移到模块级函数）
    def _register_schema_locked(self, collection: str, value: Any) -> None:
        _register_schema_locked(self._current, collection, value)

    def lock_stats(self) -> Dict[str, Any]:
        """锁的实时统计（排查"是不是锁导致的慢"时用）。"""
        return {"rw": self._rw.snapshot(),
                "connections": len(self._all_conns),
                "file_lock": bool(self._file_lock),
                "path": self.path}

    def close(self) -> None:
        with self._conns_lock:
            for c in self._all_conns:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
            self._all_conns.clear()
        try:
            self._local.conn = None
            self._local.closed = True
        except Exception:  # noqa: BLE001
            pass


class DBClient:
    """Agent 侧的 DB 客户端：所有读写都变成 `db/*` 任务，不直连库。"""

    def __init__(self, hub_url: str, token: Optional[str] = None, timeout: float = 30):
        self.client = HubClient(hub_url, token=token)
        self.timeout = timeout

    def _call(self, action: str, params: Dict[str, Any]) -> Any:
        task = self.client.submit("db", action, params, wait=True, timeout_s=self.timeout)
        if task["state"] != "succeeded":
            raise HubError(f"db/{action} 失败：{task.get('error')}")
        return task.get("result")

    def write(self, collection: str, key: str, value: Any, merge: bool = False) -> Any:
        return self._call("write", {"collection": collection, "key": key,
                                    "value": value, "merge": merge})

    def read(self, collection: str, key: str) -> Any:
        return self._call("read", {"collection": collection, "key": key})

    def list(self, collection: str, prefix: str = "", limit: int = 100) -> Any:
        return self._call("list", {"collection": collection, "prefix": prefix, "limit": limit})

    def delete(self, collection: str, key: str) -> Any:
        return self._call("delete", {"collection": collection, "key": key})

    def append(self, stream: str, payload: Any) -> Any:
        return self._call("append", {"stream": stream, "payload": payload})

    def tail(self, stream: str, limit: int = 50, after: int = 0) -> Any:
        return self._call("tail", {"stream": stream, "limit": limit, "after": after})

    def register_schema(self, collection: str, columns: List[str]) -> Any:
        return self._call("register_schema", {"collection": collection, "columns": columns})

    def collections(self) -> Any:
        return self._call("collections", {})

    def stats(self) -> Any:
        return self._call("stats", {})

    def lock_stats(self) -> Any:
        """实时锁状态：在读读者数、等待写者数、文件锁是否持有。

        与 `DBManager.ACTIONS` 一一对应，避免客户端要绕过 DBClient
        去手写 `HubClient.submit("db", "lock_stats", ...)`。
        """
        return self._call("lock_stats", {})


def _register_schema_locked(conn: sqlite3.Connection, collection: str,
                            value: Any) -> None:
    """第一次写入时登记字段（同集合只登记一次）。必须传入当前线程连接。"""
    if not isinstance(value, dict):
        return
    row = conn.execute(
        "SELECT columns FROM schemas WHERE collection=?", (collection,)).fetchone()
    if row:
        return
    conn.execute(
        "INSERT OR IGNORE INTO schemas(collection,columns,registered) VALUES(?,?,?)",
        (collection, json.dumps(sorted(value.keys()), ensure_ascii=False), time.time()))


def _req_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")
    return value.strip()