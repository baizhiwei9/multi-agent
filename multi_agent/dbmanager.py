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


class DBManager:
    ACTIONS = ("write", "read", "list", "delete", "append", "tail",
               "register_schema", "collections", "stats")

    def __init__(self, path: str = "data.db"):
        self.path = path
        self._lock = threading.RLock()
        self._lock_path = Path(str(path) + ".lock") if path != ":memory:" else None
        self._file_lock = FileLock(self._lock_path) if self._lock_path else None
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_DDL)
            self._conn.commit()

    @contextmanager
    def _write_tx(self):
        """写事务：RLock（进程内）+ 文件锁（跨进程）+ BEGIN IMMEDIATE（SQLite）。"""
        with self._lock:
            if self._file_lock:
                self._file_lock.acquire()
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
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

    # ---------- 文档 ----------
    def write(self, collection: str, key: str, value: Any,
              merge: bool = False) -> Dict[str, Any]:
        collection, key = _req_str(collection, "collection"), _req_str(key, "key")
        now = time.time()
        with self._write_tx():
            if merge:
                row = self._conn.execute(
                    "SELECT value FROM docs WHERE collection=? AND key=?", (collection, key)
                ).fetchone()
                if row:
                    base = json.loads(row["value"])
                    if not isinstance(base, dict) or not isinstance(value, dict):
                        raise ValueError("merge=True 只支持对象与对象合并")
                    value = {**base, **value}
            self._conn.execute(
                "INSERT INTO docs(collection,key,value,updated) VALUES(?,?,?,?) "
                "ON CONFLICT(collection,key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                (collection, key, json.dumps(value, ensure_ascii=False), now),
            )
            self._register_schema_locked(collection, value)
        return {"collection": collection, "key": key, "updated": now}

    def read(self, collection: str, key: str) -> Dict[str, Any]:
        collection, key = _req_str(collection, "collection"), _req_str(key, "key")
        with self._lock:
            row = self._conn.execute(
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
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return {"collection": collection,
                "items": [{"key": r["key"], "value": json.loads(r["value"]),
                           "updated": r["updated"]} for r in rows]}

    def delete(self, collection: str, key: str) -> Dict[str, Any]:
        collection, key = _req_str(collection, "collection"), _req_str(key, "key")
        with self._write_tx():
            cur = self._conn.execute(
                "DELETE FROM docs WHERE collection=? AND key=?", (collection, key))
        return {"deleted": cur.rowcount, "collection": collection, "key": key}

    # ---------- 事件流 ----------
    def append(self, stream: str, payload: Any) -> Dict[str, Any]:
        stream = _req_str(stream, "stream")
        now = time.time()
        with self._write_tx():
            cur = self._conn.execute(
                "INSERT INTO streams(stream,payload,created) VALUES(?,?,?)",
                (stream, json.dumps(payload, ensure_ascii=False), now))
            seq = cur.lastrowid
        return {"stream": stream, "seq": seq, "created": now}

    def tail(self, stream: str, limit: int = 50, after: int = 0) -> Dict[str, Any]:
        stream = _req_str(stream, "stream")
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            rows = self._conn.execute(
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
        with self._write_tx():
            self._conn.execute(
                "INSERT INTO schemas(collection,columns,registered) VALUES(?,?,?) "
                "ON CONFLICT(collection) DO UPDATE SET columns=excluded.columns",
                (collection, json.dumps([str(c) for c in columns], ensure_ascii=False), now))
        return {"collection": collection, "columns": columns}

    def collections(self) -> Dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT collection, COUNT(*) n FROM docs GROUP BY collection ORDER BY collection"
            ).fetchall()
            schemas = self._conn.execute(
                "SELECT collection, columns FROM schemas ORDER BY collection").fetchall()
        return {"collections": [{"name": r["collection"], "docs": r["n"]} for r in rows],
                "schemas": {r["collection"]: json.loads(r["columns"]) for r in schemas}}

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            docs = self._conn.execute("SELECT COUNT(*) n FROM docs").fetchone()["n"]
            streams = self._conn.execute("SELECT COUNT(*) n FROM streams").fetchone()["n"]
        return {"docs": docs, "streams": streams, "path": self.path}

    def _register_schema_locked(self, collection: str, value: Any) -> None:
        if not isinstance(value, dict):
            return
        row = self._conn.execute(
            "SELECT columns FROM schemas WHERE collection=?", (collection,)).fetchone()
        if row:
            return
        self._conn.execute(
            "INSERT OR IGNORE INTO schemas(collection,columns,registered) VALUES(?,?,?)",
            (collection, json.dumps(sorted(value.keys()), ensure_ascii=False), time.time()))

    def close(self) -> None:
        with self._lock:
            self._conn.close()


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


def _req_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")
    return value.strip()