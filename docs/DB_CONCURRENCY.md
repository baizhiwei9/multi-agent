# DBManager 并发模型：读写锁 + 每线程连接

> 大佬 18:17：
> **「你这几个写公用数据库…那你这 manager 就至少要有锁，如果时间方便的话，你要读写锁。」**

---

## 1. 之前的问题

原来只有一把 `threading.RLock()`：

```
读 → 阻塞 → 读       ← 纯读之间也排队，白白串行
写 → 阻塞 → 读/写
```

多个 agent 同时打到 DBManager 时（E 出报告要读、A 建表要写、B 塞新闻要写），
**所有操作串成一列**。更严重的是：所有代码共用 `self._conn` 一条 sqlite 连接，
而 **sqlite 单连接上并发读是未定义行为** —— 光加锁救不了，必须每线程一条连接。

---

## 2. 三层锁，各管一段

```
┌──────────────────────────────────────────────────────────────┐
│ RWLock（进程内）                                             │
│   read()  多读者并行（写者优先，防饿死）                      │
│   write() 独占                                               │
├──────────────────────────────────────────────────────────────┤
│ FileLock（跨进程，fcntl.flock）  ← 多个 DBManager 写同一文件   │
├──────────────────────────────────────────────────────────────┤
│ SQLite BEGIN IMMEDIATE + WAL + busy_timeout=5000             │
└──────────────────────────────────────────────────────────────┘
```

每一层解决不同问题，缺一不可：

| 层 | 解决 | 不加会怎样 |
|---|---|---|
| RWLock | 同进程多线程（读并行、写独占） | 读读互斥，吞吐差 N 倍 |
| FileLock | 多进程/多实例写同一文件 | `database is locked` |
| BEGIN IMMEDIATE + WAL | 事务边界、读不阻塞写 | 读到中间态 |

---

## 3. RWLock 实现要点

### 3.1 写优先（防止写被饿死）

```python
@contextmanager
def read(self, timeout=None):
    with self._cond:
        while self._writer or self._waiting_writers > 0:   # ← 关键
            ...
```

有写者在等时，**新来的读者要让路**。否则高频读会把写永久卡住
（经典读写锁饥饿问题）。测试 `test_rwlock_writer_priority` 锁住了这条。

### 3.2 支持超时

```python
with rw.read(timeout=2.0):    # 拿不到就抛 TimeoutError
```

排障时能确认"是不是锁导致的慢"，而不是无限等。

### 3.3 自带统计

```python
db.lock_stats()
# → {"rw": {"read_acquired": 1600, "write_acquired": 1,
#           "read_waits": 0, "write_waits": 0,
#           "max_concurrent_readers": 8,
#           "readers_now": 0, "writer_now": false, "waiting_writers": 0},
#    "connections": 9, "file_lock": true}
```

也可以走协议查：`db/lock_stats` 动作。

---

## 4. 每线程连接（关键，光有锁不够）

```python
def _connect(self):
    """取本线程的连接；没有就建一条。"""
    conn = getattr(self._local, "conn", None)
    if conn is not None and not closed:
        return conn
    conn = sqlite3.connect(...)      # 每线程一条
    ...
```

`_read()` / `_write_tx()` 都通过 `self._current` 拿当前线程连接：

```python
@contextmanager
def _read(self):
    with self._rw.read():
        yield self._current          # 本线程连接

@contextmanager
def _write_tx(self):
    with self._rw.write():
        conn = self._current
        if self._file_lock: self._file_lock.acquire()
        conn.execute("BEGIN IMMEDIATE")
        ...
```

> ⚠️ 这个坑很隐蔽：一开始 `write()` 的 merge 分支里还写着 `self._conn.execute(...)`
> （主线程连接），多线程下立刻 `database is locked`。修完之后测试耗时从 **161s → 2.66s**。

### 4.1 `:memory:` 的特殊处理

`:memory:` 的每条连接是**各自独立的内存库**，线程本地连接化之后互相看不见表。
所以用共享缓存 URI：

```python
self._connect_target = (
    f"file:memdb-{id(self)}?mode=memory&cache=shared"
    if path == ":memory:" else path)
```

这样所有线程连接连到同一个内存库，同时还能多读并行。

---

## 5. 实测数据

### 5.1 多读真的并行

```
8 线程 × 200 次并发读总耗时: 0.166s
lock_stats: read_acquired=1600, max_concurrent_readers=8, read_waits=0
connections: 9
```

`max_concurrent_readers: 8` 是硬证据 —— 8 个线程确实同时在临界区内。
如果是互斥锁，这个值永远 ≤ 1。

### 5.2 写不被饿死

```
6 个读线程持续压着时，30 次写耗时: 0.012s
```

---

## 6. 测试覆盖（`tests/test_db_locking.py`，16 个）

| 测试 | 验证什么 |
|---|---|
| `test_rwlock_allows_concurrent_readers` | 5 个读者能同时进入（用 Barrier 证明） |
| `test_rwlock_writer_excludes_everyone` | 写不插在读中间 |
| `test_rwlock_writer_priority` | 写者等待时，后来的读者排队 |
| `test_rwlock_timeout` | 超时抛 TimeoutError |
| `test_concurrent_reads_actually_parallel` | 峰值并发读者 > 1 |
| `test_per_thread_connection` | 4 线程拿到 4 个不同连接 |
| `test_write_not_starved_by_readers` | 6 读压下，30 次写 < 5s |
| `test_concurrent_writers_serialized` | 6×50 次写，一条不丢 |
| `test_mixed_read_write_no_deadlock` | 4 读 + 2 写混跑 1s，无异常 |
| `test_file_lock_across_managers` | 两个 DBManager 写同一文件，120 条不丢 |
| `test_memory_db_still_works` | `:memory:` 也能多线程 |
| `test_write_visible_to_all_readers` | 写后所有线程都读到新值 |
| `test_lock_stats_exposed` / `via_attach` | 统计能查 |

---

## 7. 使用建议

```python
db = DBManager("/data/hub.db")

# 高并发场景：调大 timeout 让写多等一会儿，而不是直接失败
db = sqlite3.connect(..., timeout=10.0)   # 已在 _connect 里设好

# 排障：看是不是锁导致慢
print(db.lock_stats()["rw"])

# 关服务时
db.close()      # 关闭**所有**线程的连接
```

**不要**在业务代码里直接用 `db._conn` —— 那是主线程连接，跨线程用会出错。
需要连接请用 `db._current`（自动按线程取）。

---

## 8. 自检

```bash
python3 -m pytest tests/test_db_locking.py -q   # 16 个
python3 -m pytest tests/ -q                     # 全集 269
```

检查点：
- `max_concurrent_readers` 在并发读测试里 > 1
- 6 个读线程压着时，30 次写 < 5s
- 两个 DBManager 实例写同一文件，数据一条不丢
- `:memory:` 库多线程仍能读写（共享缓存 URI）
