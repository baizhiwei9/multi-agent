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
        if labels is not None and not isinstance(labels, dict):
            # labels 会原样交给 registry.find 做「局部匹配」，非对象会让调度线程崩；
            # 用 ProtocolError 是为了 HTTP 映射 400、RPC 映射 -32602。
            raise protocol.ProtocolError("labels 必须是对象")
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

        执行线程收尾时会把自己的 waiter 从 `_waiters` 移除（见 `_finish`），所以
        "waiter 不在了"就等于"不会再重试了"：`failed` 且 `attempts <= max_retries`
        的收尾也会立即返回，不再空等整段重试预算。
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
            with self._lock:
                finished = task_id not in self._waiters
            if finished:
                return snap
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
            # 条件更新：只有"仍停在我这一轮派发"时才写。否则与超时分支的
            # 「assigned → queued」交错时，会把刚写好的结果覆盖回重试态。
            expect = {"state": "assigned", "agent_id": task["agent_id"],
                      "attempts": task["attempts"]}
            if ok:
                task, written = self.store.update_if(
                    task_id, expect, state="succeeded", result=result,
                    error=None, agent_id=agent_id)
            else:
                task, written = self.store.update_if(
                    task_id, expect, state="failed", agent_id=agent_id,
                    error=error or {"code": "handler_error",
                                    "message": "agent 返回失败"})
            ev = self._waiters.get(task_id)
            if written and ev:
                ev.set()
            return task or self.store.get(task_id) or {}

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
        # 已取消的任务，收件箱里还没取走的旧消息要一起作废
        self._purge_inbox(task_id)
        with self._lock:
            ev = self._waiters.get(task_id)
        if ev:
            ev.set()
        return task or {}

    def _execute(self, task_id: str, ev: threading.Event) -> None:
        """调度线程入口：任何未预期异常都必须把任务落成 failed。

        否则一次 `AttributeError`（例如 labels 不是对象）就让线程直接退出，
        任务永远停在 queued/assigned，没有 watchdog 会再收它。
        """
        try:
            self._dispatch(task_id, ev)
        except Exception as e:  # noqa: BLE001 - 兜底：先收尾，再往上抛没意义
            snap = self.store.get(task_id)
            if snap is not None and snap["state"] not in TERMINAL_STATES:
                self.store.update(task_id, state="failed", error={
                    "code": "internal",
                    "message": f"调度异常：{type(e).__name__}: {e}",
                })
        finally:
            self._finish(task_id)

    def _dispatch(self, task_id: str, ev: threading.Event) -> None:
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
            task = self.store.update(task_id, state="assigned", agent_id=agent.agent_id,
                                     attempts=attempts, error=None) or task
            # 重派前先清掉这个任务残留的旧消息：上一台 Agent 之后才来取也取不到
            self._purge_inbox(task_id)
            self._push(agent.agent_id, protocol.task_message(task, agent.agent_id))
            ev.wait(task["timeout_s"])
            # 超时判定改成条件更新：只有"仍是我派出去的那一轮且还在 assigned"
            # 才回到 queued。rowcount=0 说明结果/取消已经先写进去了，按新状态收尾，
            # 绝不能把刚写好的 succeeded 覆盖回 queued 再让别人跑一遍。
            snap, moved = self.store.update_if(
                task_id,
                {"state": "assigned", "agent_id": agent.agent_id,
                 "attempts": attempts},
                state="queued", agent_id=None,
                error={"code": "timeout",
                       "message": f"{agent.agent_id} 超时未回结果"})
            if moved:
                self._purge_inbox(task_id)
                ev.clear()
                continue
            if snap is None:
                return
            if snap["state"] in ("succeeded", "canceled"):
                return
            if snap["state"] == "failed":
                # 执行失败：还有重试额度就换下一个人，否则收尾
                if attempts <= snap["max_retries"]:
                    self.store.update_if(
                        task_id,
                        {"state": "failed", "agent_id": snap["agent_id"],
                         "attempts": snap["attempts"]},
                        state="queued", agent_id=None)
                    ev.clear()
                    continue
                return
            ev.clear()

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

    def _finish(self, task_id: str) -> None:
        """显式收尾：移掉 waiter 并唤醒它。

        `wait()` 以"waiter 已不在"判断任务不会再重试，从而避免对已终态 failed
        空等整段重试预算。
        """
        with self._lock:
            ev = self._waiters.pop(task_id, None)
        if ev:
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

    def _purge_inbox(self, task_id: str) -> None:
        """把某个任务的残留消息从所有收件箱里删掉（重派 / 取消后旧消息不许再执行）。"""
        with self._cv:
            for q in self._inbox.values():
                kept = deque(m for m in q if m.get("task_id") != task_id)
                if len(kept) != len(q):
                    q.clear()
                    q.extend(kept)

    def _pop_message(self, agent_id: str,
                     deadline: float) -> Optional[Dict[str, Any]]:
        """取队列头的一条消息；到 deadline 返回 None。"""
        with self._cv:
            while True:
                q = self._inbox.get(agent_id)
                if q:
                    return q.popleft()
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cv.wait(remaining)

    def _deliverable(self, msg: Dict[str, Any], agent_id: str) -> bool:
        """消息是否仍然作数：任务还在 assigned、归属本 Agent、attempt 对得上。

        任务已 succeeded / canceled / 已重派给别人、或这条消息属于上一轮
        attempt，都直接丢弃——否则旧 Agent 恢复轮询后会把同一个任务再执行一遍。
        """
        task_id = msg.get("task_id")
        if not task_id:
            return False
        task = self.store.get(task_id)
        if task is None or task["state"] != "assigned" or task["agent_id"] != agent_id:
            return False
        attempt = msg.get("attempt")
        if attempt is not None and int(attempt) != int(task["attempts"]):
            return False
        return True

    def next_message(self, agent_id: str, wait_s: float = 20) -> Optional[Dict[str, Any]]:
        deadline = time.time() + max(0.0, wait_s)
        while True:
            msg = self._pop_message(agent_id, deadline)
            if msg is None:
                return None
            if self._deliverable(msg, agent_id):
                return msg

    # ---------------- 状态 ----------------
    def stats(self) -> Dict[str, Any]:
        agents = self.registry.list()
        return {
            "agents_alive": len(agents),
            "tasks": self.store.count_by_state(),
            "ts": protocol.now_iso(),
        }