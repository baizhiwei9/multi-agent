"""Hub 核心：注册表 + 任务存储 + 调度 + 收件箱（长轮询）。

调度语义
--------
1. 协调端 `submit(capability, action, params)` 建任务；
2. Hub 按能力清单挑一个存活 Agent（round-robin），把 `task` 推进它的收件箱；
3. Agent 长轮询取任务，本地执行（不经过大模型），POST 结果；
4. 超时/执行失败 → 换下一个候选重试（`max_retries`）；
5. 幂等键相同的重复提交返回同一任务，不会重复触发。

线程模型：HTTP 每请求一个线程；每个任务一个后台线程等待结果。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Dict, Optional

from . import protocol
from .registry import Registry
from .store import TERMINAL_STATES, TaskStore


class Hub:
    def __init__(self, db_path: str = ":memory:", ttl_s: float = 90,
                 registry_json: Optional[str] = None):
        self.registry = Registry(ttl_s=ttl_s, persist_path=registry_json)
        self.store = TaskStore(db_path)
        self._inbox: Dict[str, deque] = {}
        self._cv = threading.Condition()
        self._waiters: Dict[str, threading.Event] = {}
        self._lock = threading.RLock()

    # ---------------- 任务 ----------------
    def submit(self, *, capability: str, action: str, params: Optional[Dict[str, Any]] = None,
               timeout_s: float = 60, wait: bool = False, max_retries: int = 0,
               idempotency_key: Optional[str] = None,
               labels: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not capability or not action:
            raise ValueError("capability / action 不能为空")
        timeout_s = max(0.5, float(timeout_s))
        task, created = self.store.create(
            capability=capability, action=action, params=params or {},
            timeout_s=timeout_s, max_retries=max(0, int(max_retries)),
            idempotency_key=idempotency_key, labels=labels or {},
        )
        if created:
            ev = threading.Event()
            with self._lock:
                self._waiters[task["id"]] = ev
            threading.Thread(target=self._execute, args=(task["id"], ev),
                             name=f"task-{task['id']}", daemon=True).start()
        if wait:
            budget = timeout_s * (max(0, int(max_retries)) + 1) + 5
            return self.wait(task["id"], timeout=budget)
        return self.store.get(task["id"]) or task

    def wait(self, task_id: str, timeout: float = 65) -> Dict[str, Any]:
        """等任务真正收尾。

        注意：`failed` 可能只是"这一轮失败、还要换人重试"，不能当终态返回；
        只有 succeeded / canceled / timeout，或 failed 且重试额度用尽才算结束。
        """
        deadline = time.time() + max(0.1, timeout)
        while True:
            with self._lock:
                ev = self._waiters.get(task_id)
            if ev is None:
                snap = self.store.get(task_id)
                if snap is None:
                    raise KeyError(task_id)
                return snap
            remaining = deadline - time.time()
            if remaining <= 0:
                snap = self.store.get(task_id)
                if snap is None:
                    raise KeyError(task_id)
                return snap
            ev.wait(remaining)
            snap = self.store.get(task_id)
            if snap is None:
                raise KeyError(task_id)
            state = snap["state"]
            final = state in ("succeeded", "timeout", "canceled")
            if state == "failed" and snap["attempts"] > snap["max_retries"]:
                final = True
            if final:
                return snap
            ev.clear()

    def complete(self, task_id: str, agent_id: str, ok: bool,
                 result: Any = None, error: Optional[Dict[str, Any]] = None,
                 duration_ms: Optional[int] = None) -> Dict[str, Any]:
        with self._lock:
            task = self.store.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if task["state"] in TERMINAL_STATES:
                return task  # 迟到的结果直接忽略，避免覆盖终态
            if task["state"] != "assigned":
                # 已超时回到 queued（换人重试）或还没派出去：旧结果一律丢弃，
                # 防止上一轮的迟到结果把重试中的任务误判为成功。
                return task
            if task["agent_id"] and task["agent_id"] != agent_id:
                raise PermissionError(f"任务 {task_id} 不属于 {agent_id}")
            if ok:
                task = self.store.update(task_id, state="succeeded", result=result,
                                         error=None, agent_id=agent_id)
            else:
                task = self.store.update(task_id, state="failed", error=error or {
                    "code": "handler_error", "message": "agent 返回失败",
                }, agent_id=agent_id)
            ev = self._waiters.get(task_id)
            if ev:
                ev.set()
            return task or {}

    def cancel(self, task_id: str) -> Dict[str, Any]:
        with self._lock:
            task = self.store.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if task["state"] in TERMINAL_STATES:
                return task
            task = self.store.update(task_id, state="canceled", error={
                "code": "canceled", "message": "协调端取消",
            })
            ev = self._waiters.get(task_id)
            if ev:
                ev.set()
            return task or {}

    def _execute(self, task_id: str, ev: threading.Event) -> None:
        tried: set[str] = set()
        attempts = 0
        while True:
            task = self.store.get(task_id)
            if task is None or task["state"] in TERMINAL_STATES:
                return
            if attempts > task["max_retries"]:
                break
            agent = self.registry.next(task["capability"], task["action"],
                                       exclude=tried, labels=task["labels"])
            if agent is None:
                break
            tried.add(agent.agent_id)
            attempts += 1
            self.store.update(task_id, state="assigned", agent_id=agent.agent_id,
                              attempts=attempts, error=None)
            self._push(agent.agent_id, protocol.task_message(task, agent.agent_id))
            got = ev.wait(task["timeout_s"])
            snap = self.store.get(task_id)
            if snap is None:
                return
            if snap["state"] == "canceled":
                return
            if snap["state"] == "succeeded":
                return
            if snap["state"] == "failed":
                # 执行失败：还有重试额度就换下一个人，否则收尾
                if attempts <= snap["max_retries"]:
                    self.store.update(task_id, state="queued", agent_id=None)
                    ev.clear()
                    continue
                return
            if not got:
                # 超时：换人重试；没有候选时由循环出口收尾
                self.store.update(task_id, state="queued", agent_id=None,
                                  error={"code": "timeout",
                                         "message": f"{agent.agent_id} 超时未回结果"})
                ev.clear()
                continue
            return

        snap = self.store.get(task_id)
        if snap is None or snap["state"] in TERMINAL_STATES:
            return
        err = {"code": "no_agent",
               "message": f"没有可执行 {snap['capability']}/{snap['action']} 的存活 Agent"}
        if attempts:
            err = {"code": "timeout",
                   "message": f"{attempts} 个候选均失败或超时，任务放弃"}
        self.store.update(task_id, state="failed", error=err)
        ev.set()

    def recover(self) -> int:
        """Hub 重启后把未完成任务重新派发（否则会永远卡在 queued/assigned）。"""
        n = 0
        for task in self.store.non_terminal():
            with self._lock:
                if task["id"] in self._waiters:
                    continue
                ev = threading.Event()
                self._waiters[task["id"]] = ev
            self.store.update(task["id"], state="queued", agent_id=None)
            threading.Thread(target=self._execute, args=(task["id"], ev),
                             name=f"recover-{task['id']}", daemon=True).start()
            n += 1
        return n

    # ---------------- 收件箱（长轮询） ----------------
    def _push(self, agent_id: str, message: Dict[str, Any]) -> None:
        with self._cv:
            self._inbox.setdefault(agent_id, deque()).append(message)
            self._cv.notify_all()

    def next_message(self, agent_id: str, wait_s: float = 20) -> Optional[Dict[str, Any]]:
        deadline = time.time() + max(0.0, wait_s)
        with self._cv:
            while True:
                q = self._inbox.get(agent_id)
                if q:
                    return q.popleft()
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cv.wait(remaining)

    # ---------------- 状态 ----------------
    def stats(self) -> Dict[str, Any]:
        agents = self.registry.list()
        return {
            "agents_alive": len(agents),
            "tasks": self.store.count_by_state(),
            "ts": protocol.now_iso(),
        }