"""任务存储：SQLite，负责状态机、幂等键与审计。

状态：queued → assigned → succeeded / failed / timeout / canceled
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

TERMINAL_STATES = ("succeeded", "failed", "timeout", "canceled")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    capability TEXT NOT NULL,
    action TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}',
    labels TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'queued',
    agent_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    error TEXT,
    idempotency_key TEXT UNIQUE,
    timeout_s REAL NOT NULL DEFAULT 60,
    max_retries INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
"""


class TaskStore:
    def __init__(self, path: str = ":memory:"):
        self._path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- 写 ----------
    def create(self, *, capability: str, action: str, params: Dict[str, Any],
               timeout_s: float = 60, max_retries: int = 0,
               idempotency_key: Optional[str] = None,
               labels: Optional[Dict[str, Any]] = None) -> tuple[Dict[str, Any], bool]:
        """返回 (task, created)。命中幂等键时 created=False 且返回原任务。"""
        now = time.time()
        task_id = "t_" + uuid.uuid4().hex[:16]
        with self._lock:
            if idempotency_key:
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                if row:
                    return _row_to_task(row), False
            try:
                self._conn.execute(
                    """INSERT INTO tasks (id, capability, action, params, labels, state,
                       attempts, idempotency_key, timeout_s, max_retries, created, updated)
                       VALUES (?,?,?,?,?,'queued',0,?,?,?,?,?)""",
                    (task_id, capability, action, json.dumps(params, ensure_ascii=False),
                     json.dumps(labels or {}, ensure_ascii=False), idempotency_key,
                     float(timeout_s), int(max_retries), now, now),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                if row:
                    return _row_to_task(row), False
                raise
        return self.get(task_id), True  # type: ignore[return-value]

    def update(self, task_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        if not fields:
            return self.get(task_id)
        allowed = {"state", "agent_id", "attempts", "result", "error", "capability",
                   "action", "params", "timeout_s", "max_retries", "labels"}
        sets, values = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"不允许更新字段：{key}")
            if key in ("result", "error", "params", "labels"):
                value = json.dumps(value, ensure_ascii=False)
            sets.append(f"{key}=?")
            values.append(value)
        sets.append("updated=?")
        values.append(time.time())
        values.append(task_id)
        with self._lock:
            self._conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", values)
            self._conn.commit()
        return self.get(task_id)

    # ---------- 读 ----------
    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    def list(self, limit: int = 50, state: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM tasks"
        args: list[Any] = []
        if state:
            sql += " WHERE state=?"
            args.append(state)
        sql += " ORDER BY created DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_row_to_task(r) for r in rows]

    def non_terminal(self, limit: int = 200) -> List[Dict[str, Any]]:
        """未完成任务（进程重启后需要重新捡起来）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE state IN ('queued','assigned') "
                "ORDER BY created LIMIT ?", (max(1, min(int(limit), 1000)),)
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    def count_by_state(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT state, COUNT(*) n FROM tasks GROUP BY state").fetchall()
        return {r["state"]: r["n"] for r in rows}


def _row_to_task(row: sqlite3.Row) -> Dict[str, Any]:
    def _loads(value: Any, default: Any) -> Any:
        if value in (None, ""):
            return default
        try:
            return json.loads(value)
        except ValueError:
            return default

    return {
        "id": row["id"],
        "capability": row["capability"],
        "action": row["action"],
        "params": _loads(row["params"], {}),
        "labels": _loads(row["labels"], {}),
        "state": row["state"],
        "agent_id": row["agent_id"],
        "attempts": row["attempts"],
        "result": _loads(row["result"], None),
        "error": _loads(row["error"], None),
        "idempotency_key": row["idempotency_key"],
        "timeout_s": row["timeout_s"],
        "max_retries": row["max_retries"],
        "created": row["created"],
        "updated": row["updated"],
    }