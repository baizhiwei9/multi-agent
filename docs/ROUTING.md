# 能力路由 —— 协调者按「谁声明了谁接」派活

> 你说的这个意思：**某个 agent 声明"我支持债券审计"，这个能力注册到 Hub，
> 协调者按能力往下分活**，而不是靠写死机器地址。
> 这一层就是把它做出来，并且给可解释的理由。

---

## 1. 为什么要"按能力"而不是"按机器"

写死机器有三个问题：

| 问题 | 后果 |
|---|---|
| 换一台机器要改配置 | 扩容/缩容都得改代码或改配置 |
| 某台挂了任务就废了 | 没有降级路径 |
| 不知道谁擅长什么 | 只能"谁都发一遍"，让不支持的自己拒绝 |

按能力的做法：

```
agent 启动 → 声明 roles=[{name, actions}] → Hub 注册表
                                                    │
协调者 ← catalog() 看全网有什么能力 ──────────────────┘
   │
   └─ route("auditor:bond", "balance") → 选一台 + 给出理由 → 定向投递
```

**能力是 agent 自己声明的，不是协调者猜的。**

---

## 2. 核心概念：能力 = 角色名 + 动作

Hub 的选人键是 `capability(=role.name) + action`。所以要按"业务口径"路由，
**角色名就得带口径**：

```python
# ❌ 所有审计官都叫 auditor → 分不清谁支持债券
roles = [{"name": "auditor", "actions": ["balance"]}]

# ✅ 带上口径 → 调度能精确命中
roles = [{"name": "auditor:bond", "actions": ["balance"]}]
roles = [{"name": "auditor:stock", "actions": ["balance"]}]
```

`audit.register(rt, projects=["bond"])` 会自动做这件事（见 docs/AUDIT.md §7.1）。

实测对比（这就是不拆口径的代价）：

```
# 都叫 auditor 时
4 次派活 → ['openclaw-2', 'coze-1', 'hermes-1', 'openclaw-1']   ← 全混在一起
                              ↑ coze 只支持债券，收到平衡表任务后报错

# 拆成 auditor:bond / auditor:stock / auditor:balance_sheet 后
平衡表 → openclaw-1   股票 → hermes-1   债券 → coze-1          ← 精确
```

---

## 3. CapabilityRouter

```python
from multi_agent.router import CapabilityRouter

router = CapabilityRouter(client)     # client = HubClient(HTTP) 或 TCPClient(TCP)
```

### 3.1 看全网能力

```python
router.catalog()
# → {"capabilities": {"auditor:bond/balance": [{"agent_id": "coze-1", "alive": true,
#                                              "age_s": 3.2, "ttl_s": 120, "labels": {...}}],
#                     "A/build_sheet": [...]},
#    "agent_count": 5, "alive_count": 4}
```

`age_s` / `ttl_s` 能看出某台是不是快掉线了。

### 3.2 选一台

```python
router.route("auditor:bond", "balance")
# → {"agent_id": "coze-1",
#    "reason": "轮询第 1/1 个",
#    "candidates": ["coze-1"],
#    "strategy": "round_robin"}
```

**每次都给 `reason`** —— 排障时不用猜"为什么选了它"。

### 3.3 四种策略

| 策略 | 行为 | 适用 |
|---|---|---|
| `round_robin`（默认） | 轮流，避免总压第一个实例 | 同能力多实例、负载均匀 |
| `least_loaded` | 选当前 assigned 任务最少的 | 任务耗时差异大 |
| `prefer_label` | 优先匹配标签（如 `site=home`），没匹配退回轮询并说明 | 就近/机房亲和 |
| `first` | 永远第一个 | 调试 |

```python
router.route("auditor:bond", "balance", prefer_labels={"site": "cloud"})
# 标签匹配 → {"agent_id": "coze-1", "reason": "标签匹配 {'site': 'cloud'}"}
# 没匹配   → {"agent_id": "...", "reason": "标签 {'site': 'x'} 无匹配，退回轮询；轮询第 1/2 个"}
```

> 传了 `prefer_labels` 就一定会先按标签筛 —— **不管默认策略是什么**。
> 这条是踩过的坑：早期版本只在 `strategy="prefer_label"` 时才看标签，
> 别的策略下静默忽略，非常隐蔽。

### 3.4 点名要某台

```python
router.route("auditor:balance_sheet", "balance", require_agent="coze-1")
# → NoCapability: agent coze-1 没有声明 auditor:balance_sheet/balance；
#                 它声明的是 [('auditor:bond', 'balance'), ...]
```

**点名失败直接报错，不静默换人** —— 静默换人会违背调用方意图
（比如"必须在这台跑，因为数据在它本地"）。

### 3.5 派活（一步到位）

```python
snap = router.submit("auditor:bond", "balance",
                     {"balance": bond, "project": "bond"},
                     wait=True, timeout_s=60)
snap["routed_to"]    # "coze-1"
snap["reason"]       # 为什么选它
snap["state"]        # succeeded
snap["result"]       # 审计报告
```

### 3.6 失败自动换人

```python
router.submit_with_fallback("calc", "add", {"a": 1}, max_attempts=3, wait=True)
# → 第一台 handler 抛异常 → 换第二台 → succeeded
#   snap["attempts"] == ["bad-1", "good-1"]
```

只有"任务失败/超时"才换人；"这个能力根本没人提供"直接抛错（换人也没用）。

---

## 4. 缺能力提前发现（省时间）

```python
router.gaps(["auditor:bond/balance", "runner/py"])
# → {"checks": [{"need": "auditor:bond/balance", "normalized": "auditor:bond/balance",
#                "available": True, "providers": ["coze-1"]},
#               {"need": "runner/py", "available": False, "providers": []}],
#    "missing": ["runner/py"], "ok": False}
```

⚠️ 写法注意：用 **`role/action`**（最后一个 `/` 切分），因为角色名本身含冒号
（`auditor:bond`）。旧写法 `auditor:bond:balance` 也兼容（按最后一个 `:` 切），
但推荐用 `/`。

```python
router.route_plan([("A", "build_sheet"), ("auditor:bond", "balance")])
# → {"ok": True,
#    "steps": [{"capability": "A", "action": "build_sheet", "agent_id": "openclaw-1",
#               "reason": "轮询第 1/1 个"}, ...],
#    "missing": []}
```

一条流水线要 8 个能力 → 先 `route_plan()` 算可行性，**缺的提前报，不用跑到一半挂**。

---

## 5. 错误信息长什么样（排障友好）

能力没人提供时：

```
NoCapability: 没有存活 agent 提供 auditor:balance/balance；
在线 agent：{'a1': [('auditor', 'bond')], 'a2': [('A', 'build_sheet')]}
```

直接把"谁在线、各自声明了什么"列出来 —— **不用再猜为什么派不出去**。

Agent 未注册时：

```
NoCapability: 点名的 agent ghost 未注册；当前可用：['coze-1', 'hermes-1', 'openclaw-1']
```

---

## 6. 端到端演示

```bash
python3 examples/capability_routing_demo.py
```

起 1 个 Hub + 4 个 agent（openclaw-1/openclaw-2 支持平衡表、hermes-1 支持股票、
coze-1 支持债券），全程**只给能力、不给地址**：

```
### 1) 全网能力清单（协调者看到的视图）
    auditor:balance_sheet/balance ← ['openclaw-1', 'openclaw-2']
    auditor:bond/balance          ← ['coze-1']
    auditor:stock/balance         ← ['hermes-1']

### 2) 按能力派活（协调者只认能力，不知道谁在哪台机器）
    平衡表 需要 auditor:balance_sheet → openclaw-1  verdict=pass
    股票   需要 auditor:stock         → hermes-1    verdict=pass
    债券   需要 auditor:bond          → coze-1      verdict=pass

### 3) 平衡表有两台 → 轮询会分到两台
    4 次派活 → ['openclaw-2', 'openclaw-1', 'openclaw-2', 'openclaw-1']

### 4) 点名要某台（它不支持就报错，不静默换人）
    ✓ 正确拒绝：agent coze-1 没有声明 auditor:balance_sheet/balance

### 5) 缺能力能提前发现
    查 [...balance_sheet/balance, ...bond/balance, runner/py] → 缺：['runner/py']

### 6) 一条流水线的可行性预检
    ok=True
      auditor:balance_sheet/balance → openclaw-2
      auditor:bond/balance          → coze-1

### 7) 一台挂了 → fallback 自动换另一台
    仍成功派到 openclaw-2，state=succeeded

=== ✅ 能力路由全部通过（who declares, who serves）
```

---

## 7. 和"协调者"合起来是什么样

```python
# 1) 三个 agent 各自声明能力启动
audit.register(rt_openclaw, projects=["balance_sheet"])   # → auditor:balance_sheet
audit.register(rt_hermes,   projects=["stock"])           # → auditor:stock
audit.register(rt_coze,     projects=["bond"])            # → auditor:bond
runner.register(rt_openclaw)                              # → runner/py
# ... rt.start()

# 2) 协调者接活
router = CapabilityRouter(TCPClient(hub_host, 8790, token=***))

# 3) 先看可行性（缺什么提前报）
plan = router.route_plan([("A", "build_sheet"),
                          ("auditor:balance_sheet", "full"),
                          ("auditor:bond", "full")])
if not plan["ok"]:
    raise SystemExit(f"缺能力：{plan['missing']}")

# 4) 按能力派活，不用知道谁在哪台机器
for cap, act, params in tasks:
    snap = router.submit(cap, act, params, wait=True, timeout_s=300)
    if snap["state"] != "succeeded":
        log.warning("派给 %s (%s) 失败：%s", snap["routed_to"], snap["reason"],
                    snap.get("error"))
```

协调者不需要维护"IP 表"，只需要维护**能力清单**；agent 上下线由
`register` / `heartbeat` / TTL 自动管理。

---

## 8. 自检

```bash
python3 -m pytest tests/test_router.py -q     # 24 个
python3 examples/capability_routing_demo.py   # 端到端
```

检查点：
- 只声明 `auditor:bond` 的 agent，连续 6 次 `route("auditor:balance_sheet",...)`
  都不会选中它
- 同能力两台 → 6 次派活各 3 次（round_robin）
- `require_agent` 指定不支持的机器 → `NoCapability`，不是悄悄换人
- `route_plan` 里缺的能力出现在 `missing`，不是等到超时
- 一台 handler 抛异常 → `submit_with_fallback` 里 `attempts` 含两台
