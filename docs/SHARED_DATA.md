# 共享数据 + 项目级清理

> 大佬 18:04：
> **「我是希望你们走共同的这个数据协议，就是你们都共同的去上那个 DB manager 那点去拿数据。
> 比如说你那个供需平衡表是上他那拿，然后比如说股票就是拿新闻往他那塞。
> 而且那个 DB manager 要有一个定时就是有一个定时清理的逻辑，
> 就是不同的项目有不同的清理逻辑。」**

---

## 1. 为什么必须"唯一写入口"

三个 agent 分别存一份，跨机器立刻出问题：

```
平衡表在 openclaw 本地 → hermes 改了口径 → coze 读到旧的
新闻在 coze 本地      → openclaw 不知道有新事件 → 判定用了陈数据
```

这不是靠约定能解决的，只能靠**物理上只有一个写入口**（DBManager）。

```
        A 建表 ─┐
        B 新闻 ─┤
        Q 判定 ─┼──►  DBManager（唯一写入口，SQLite + WAL + 文件锁）  ◄── C/E/J/auditor 读
    auditor 审 ─┘
```

任何 agent 都**不持有 DB 路径**，只发 `db/*` 任务。

---

## 2. 共享数据协议（`shared_data.py`）

### 2.1 集合命名规范（写死在代码里）

| 集合 | owner（谁写） | 谁读 |
|---|---|---|
| `balance/{commodity}` | A | Q, E, J, auditor |
| `balance_history/{commodity}` | A | auditor（看口径变化） |
| `verdicts/{commodity}` | Q | E, J, auditor |
| `estimates/{commodity}` | C | E, auditor |
| `reports/{commodity}` | E | J, auditor |
| `audit/{project}:{key}` | auditor | J |
| `stock_financials/{code}` | A | micro, auditor |
| `bond_elements/{code}` | A | auditor |
| `bond_quotes/{code}` | B | auditor |

事件流：`news` / `news:commodity` / `news:macro` / `news:equity`（owner=B）、
`pipeline_events`、`audit_log`。

**为什么要写死**：否则会出现 `balance:copper` 和 `copper_balance` 两种写法各写一半，
下游读不到。`validate_collection()` 会直接抛 `SchemaError`。

### 2.2 读写门面

```python
from multi_agent.shared_data import SharedData

data = SharedData(db_client, role="Q")     # db_client = DBClient(hub_url, token)

bal  = data.get_balance("copper")          # → dict 或 None
news = data.tail_news(limit=50)            # 新闻事件流
data.put_verdict("copper", {"verdict": "accept"})
data.put_audit("copper", report)           # 同时写 docs + audit_log 流
```

所有方法都转成 `db/*` 任务，**不直连库** —— 跨机器时 `db_client` 指向同一个 Hub，
读写的就是同一份数据。

### 2.3 越权检查

```python
SharedData(db, role="B").put_balance("nickel", {})
# → 数据照写，但返回 {"warn": "角色 B 写了 balance（owner=A）—— 违反共享协议"}

SharedData(db, role="B", strict=True).put_balance("nickel", {})
# → SchemaError: 角色 B 写了 balance_history（owner=A）
```

默认只 warn（不阻断，否则排障更难），`strict=True` 才抛错。生产建议开 strict。

### 2.4 协议自检

```python
SharedData(db).audit_protocol()
# → {"known": ["balance","verdicts",...], "unknown": ["rogue_collection"],
#    "ok": false, "hint": "有未登记集合，检查是否有人绕过 SharedData 直接写库"}
```

跨 agent 部署后**必跑一次**：能发现"有人偷偷建了个表从没清理"。

---

## 3. 项目级清理（`db_policies.py`）

### 3.1 为什么不能一条规则清所有

| 数据 | 保留 | 理由 |
|---|---|---|
| `balance` | 最近 3 个期次 | 要回看历史口径变化 |
| `news` 流 | 30 天 / 5 万条 | 时效短，堆久查询变慢 |
| `verdicts` | 180 天 | 追溯"当时为什么这么判" |
| `pipeline_events` | 30 天 / 2 万条 | 高频，最易膨胀 |
| `audit_log` | 365 天 | 合规留痕不能短 |
| `stock_financials` | **永久** | 财报是资产 |
| `bond_elements` | **永久** | 债券要素不变 |
| `bond_quotes` | 2 年 | 行情可重建 |

### 3.2 内置四套策略

```python
from multi_agent.db_policies import PROJECT_POLICIES, plan_prune

plan_prune(db, project="balance_sheet", dry_run=True)   # 先看会删什么
plan_prune(db, project="balance_sheet")                 # 真删

# 自定义项目
from multi_agent.db_policies import ProjectPolicy, Rule, register_policy
register_policy(ProjectPolicy(
    name="fund", description="基金",
    rules=[Rule("nav", keep_days=3650, note="净值留 10 年"),
           Rule("fund_holdings", keep_days=180)]))
```

### 3.3 实测（dry-run 与实删必须一致）

```
=== dry-run
total: 307 | unregistered: ['docs:weird_table']
  doc    balance   would_delete=7    /7     doc:balance 留3条
  stream news      would_delete=300  /300   stream:news 30天/留50000条

=== 真删
deleted: 307
stats after: {'docs': 4, 'streams': 0}
```

计划说删 307，实删就是 307 —— 这条有专门测试
（`test_dry_run_matches_actual_delete`），因为"计划骗人"是最坏的情况。

### 3.4 五条安全约束

| # | 约束 | 实现 |
|---|---|---|
| 1 | **只删能算出来的** | keep_days 基于 `updated`/`created`，不猜 |
| 2 | **不动 schemas** | 字段登记永不清理（清了下次写入会重登，白丢历史） |
| 3 | **dry-run 优先** | 返回明细，不写库 |
| 4 | **单次有上限** | `max_delete_per_run`（默认 2 万）防长事务锁表 |
| 5 | **VACUUM 需显式** | 默认不清（要重写整个文件，几百 MB 要几十秒） |

### 3.5 定时清理

```python
from multi_agent.db_policies import MaintenanceScheduler

sched = MaintenanceScheduler(db, project=["balance_sheet", "stock", "bond"],
                             interval_s=3600)
sched.start()          # 后台 daemon，启动 5s 后先跑一次
sched.status()         # {"projects": [...], "interval_s": 3600, "running": True, ...}
sched.stop()
```

### 3.6 挂到 Hub（任意 agent 都能请求清理，不用直连库）

```python
from multi_agent.db_policies import attach_policies

sched = attach_policies(db_manager, rt, default_project="balance_sheet")
# → capability=db 新增 4 个动作：
#   db/prune_plan         dry-run，看会删什么
#   db/prune              真删（可选 dry_run=true）
#   db/policies           列出所有项目策略
#   db/maintenance_status 定时器状态
```

这样"清理"也走同一套协议：**谁的 DBManager，谁执行；别人只能请求**。

---

## 4. 和打洞/中继的关系

清理是**本地数据库的事**，不需要跨网：

```
协调者 ──(打洞/中继: db/prune 请求)──► 持有 DBManager 的那台
                                          └─► 本地执行 plan_prune
        ◄──(返回 would_delete 明细)──────┘
```

- 打洞通了走 UDP 直连（几毫秒）
- 打不通走中继（1.5s/次）
- 两种模式上层代码完全一样

---

## 5. 自检

```bash
python3 -m pytest tests/test_shared_data.py tests/test_db_policies.py -q   # 42 个
python3 -m pytest tests/ -q                                               # 全集 250
```

检查点：
- A 写的平衡表，Q/E/J/auditor **都能读到同一份**
- B 写 `balance` → 返回里有 `warn` 且含 `owner=A`
- `strict=True` 时越权抛 `SchemaError`
- 审计同时进 `audit` docs 和 `audit_log` 流
- dry-run 数量 == 实删数量
- `financials` / `bond_elements` 永不出现在清理计划里
- 库里出现 `rogue_collection` → `audit_protocol()["ok"] is False`
