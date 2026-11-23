"""TaskStore 事务并发回归。

历史隐患：`create()` 在 `with self._lock` **之外**调 `_add_event_locked`，
别的线程会在同一连接上与之交错 `execute/commit`，sqlite 抛
`cannot commit - no transaction is active`（偶发测试失败也是它）。
本文件把「事件写入必须和主写同锁」钉死：多线程 create/update/add_event 混跑，
既不能抛错，也不能丢事件。
"""
from __future__ import annotations

import threading

from multi_agent.store import TaskStore


def test_concurrent_create_update_add_event_has_no_sqlite_error():
    store = TaskStore(":memory:")
    errors = []
    created = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            for n in range(25):
                task, is_new = store.create(capability="B", action="scan_news",
                                            params={"worker": i, "n": n})
                assert is_new is True
                with lock:
                    created.append(task["id"])
                store.update(task["id"], state="running", agent_id=f"a{i}")
                store.add_event(task["id"], "note", {"worker": i, "n": n})
                store.update(task["id"], state="succeeded")
        except Exception as exc:  # noqa: BLE001 - 把线程里的异常带回主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    store.close()
    assert errors == [], errors
    assert len(created) == 6 * 25


def test_event_order_is_created_first_and_succeeded_last():
    store = TaskStore(":memory:")
    task, _ = store.create(capability="A", action="build_sheet", params={})
    store.update(task["id"], state="running", agent_id="a-1")
    store.add_event(task["id"], "note", {"x": 1})
    store.update(task["id"], state="succeeded")

    events = [e["event"] for e in store.events(task["id"])]
    assert events[0] == "created"
    assert events[-1] == "succeeded"
    assert events.count("created") == 1
    store.close()