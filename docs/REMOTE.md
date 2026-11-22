# 远程角色分配与执行（runner + RemoteCoordinator）

> 这一层解决两个很现实的问题：
> **① 让指定那台机器上的那个 agent 跑指定脚本；② 在运行时给 agent 挂角色。**

---

## 1. 为什么需要（跟"按能力派活"的区别）

Hub 默认按 `capability + action` 选人，同一能力多实例时 round-robin。这在
"谁有空谁干"时是对的，但真实调度里经常要**点名**：

- "这条流水线跑在 hermes 那台机上，别在 coze 上跑"
- "A 角色绑 openclaw，B 角色绑 hermes" —— 角色和机器的对应关系是固定的
- "让**刚才那个** agent 接着算下一步"（数据在它本地，换人就要重新拉）

还缺一个更基础的能力：**执行脚本本身**。如果没有它，每个业务动作都得提前写进代码，
换个脚本就要改代码、重启进程。

---

## 2. 三块拼起来

```
        RemoteCoordinator（协调者侧）
        ├─ plan_roles()      分配计划体检（查重复角色）
        ├─ assign_role()     让远端 agent 运行时挂角色
        ├─ run_python()      让远端 agent 跑指定 .py
        └─ health()          注册表 + 绑定表 + 重复角色体检
                 │  TCP / HTTP
                 ▼
        Hub（注册表 + 任务队列 + 长轮询）
                 │  TCP
                 ▼
        Runner（agent 侧，在它自己的机器上执行）
        ├─ py / cmd          白名单内跑程序
        ├─ assign / release  动态角色
        └─ roles / allowlist 自查
```

---

## 3. Runner：安全边界（**默认拒绝**）

```python
from multi_agent.integrations import runner
runner.register(rt)      # 一行挂到 AgentRuntime
```

### 5 条硬约束

| # | 约束 | 实现 |
|---|---|---|
| 1 | **白名单默认空 = 全拒** | `AGENT_RUN_ALLOW` 冒号分隔目录；没配一个都跑不了 |
| 2 | **路径逃逸拦截** | 先 `Path.resolve()`，再 `relative_to()` 判定；`../` 和软链接都拦 |
| 3 | **不发 shell** | `subprocess` 用 list 传参 + `shell=False` |
| 4 | **强制超时** | 默认 300s，超时 `killpg(SIGKILL)` 整个进程组（不是只标记） |
| 5 | **输出截断** | stdout/stderr 各保留尾部 8KB，标注"截断" |

```bash
export AGENT_RUN_ALLOW=/srv/jobs:/srv/balanceai/bin
```

### 一个容易搞错的语义

**"被白名单拒绝"是 agent 正常工作，不是任务失败。**

```json
{"ok": true, "denied": true,
 "reason": "路径不在白名单内：/tmp/evil.py（AGENT_RUN_ALLOW=/srv/jobs）"}
```

必须走 `ok=true` + `result.denied=true` 回传。如果按 `ok=false` 走，Hub 会把
`result` 丢成 `error`，调用方看不到拒绝理由 —— 那审计就失去意义了。
（这个坑是实跑踩到的，现在有专门测试 `test_tcp_denied_is_normal_response` 锁住。）

### 超时是真的杀掉

```
timeout_s=0.5 跑一个 sleep(30) 的脚本
→ {"timed_out": true, "returncode": -9, "ok": false}   实际 0.5s 返回
```
用 `start_new_session=True` 起独立进程组，超时 `killpg` 整组，避免子进程残留。

---

## 4. 角色分配

### 4.1 远端动态挂角色（不用重启）

```python
task = api.submit("runner", "assign",
                  {"roles": [{"name": "A", "actions": ["build_sheet"]},
                             {"name": "F"}]})     # actions 省略默认 ["*"]
api.submit("runner", "roles", {})                  # 查当前角色
api.submit("runner", "release", {"roles": ["A"]})  # 卸
```

### 4.2 同一角色不能绑两台机器（硬约束）

BalanceAI 的 `balance/`、`verdicts/`、`agents/state/` 都是按角色命名的目录，
**同一角色两个 agent 会互相覆盖**（docs/AGENTS.md §4.3）。

```python
from multi_agent.remote import RoleBinding, BindingConflict

b = RoleBinding({"A": "openclaw-1"})
b.bind("A", "openclaw-2")                  # → BindingConflict
b.bind("A", "openclaw-2", force=True)      # → 显式抢占，返回 previous
```

### 4.3 分配计划先体检

```python
from multi_agent.remote import plan_roles

plan_roles({"openclaw-1": ["A", "F", "E"],
            "hermes-1":   ["B", "C"],
            "coze-1":     ["D", "Q", "J"]})
# → {"ok": True, "duplicates": [], "hint": "分配无冲突"}
```

重复时直接 `ok: False` 并指出是哪个角色重复，**不自动帮你选**。

---

## 5. RemoteCoordinator

### 5.1 两种线缆，业务代码不变

```python
from multi_agent.remote import RemoteCoordinator

# HTTP
rc = RemoteCoordinator("http://hub:8787", token="***")

# TCP（内网/同机房，少一层 HTTP 解析）
rc = RemoteCoordinator.from_tcp("hub", 8790, token="***")
```

### 5.2 用法

```python
# 体检
rc.health()
# → {"agents": [...], "binding": {...}, "duplicate_roles": [], "ok": True}

# 分配角色（先写绑定表，再调远端 runner/assign）
rc.assign_role("openclaw-1", [{"name": "A"}, {"name": "F"}, {"name": "E"}])

# 点名跑脚本
rc.run_python("openclaw-1", "/srv/jobs/build_sheet.py",
              argv=["--commodity", "copper"], timeout_s=120)
# → {"ok": True, "returncode": 0, "stdout": "...", "duration_ms": 76, ...}
```

`submit_to(verify_target=True)` 会先查注册表：
- agent 没注册 → 立刻报错（不用等 timeout）
- agent 失联 → 立刻报错
- agent 没声明这个能力 → 立刻报错并列出它实际声明的

---

## 6. 端到端演示（可当跨主机自测）

```bash
# 一体化（自己起 Hub + 3 个 agent + 协调者，全走 TCP）
python3 examples/tcp_roles_demo.py

# 拆开跑（真跨主机形态）
# 机器 A：
python3 examples/tcp_roles_demo.py --mode hub --port 8790
# 机器 B：
python3 examples/tcp_roles_demo.py --mode agent --hub <A的IP>:8790 \
    --id openclaw-1 --allow /srv/jobs
# 机器 C：
python3 examples/tcp_roles_demo.py --mode agent --hub <A的IP>:8790 \
    --id hermes-1 --allow /srv/jobs
# 任意机器：
python3 examples/tcp_roles_demo.py --mode coord --hub <A的IP>:8790 \
    --plan '{"openclaw-1":["A","F","E"],"hermes-1":["B","C"],"coze-1":["D","Q","J"]}'
```

一体化输出的实测结果：

```
### 2) 用 TCP 连 Hub
   注册表: ['coze-1', 'hermes-1', 'openclaw-1']

### 3) 给每个 agent 分配角色（经 runner/assign）
   openclaw-1  ['A','F','E'] → succeeded ["A","F","E"]
   hermes-1    ['B','C']     → succeeded ["B","C"]
   coze-1      ['D','Q','J'] → succeeded ["D","Q","J"]

### 4) 点名让指定 agent 跑指定 Python 程序（TCP）
   openclaw-1  build_sheet.py → succeeded rc=0  76ms
   hermes-1    scan_news.py   → succeeded rc=0  72ms
   openclaw-1  report.py      → succeeded rc=0  92ms
   coze-1      final.py       → succeeded rc=0  77ms

### 5) 安全边界验证
   白名单外脚本 → denied=True
      理由: 路径不在白名单内：/tmp/evil_script.py（AGENT_RUN_ALLOW=/tmp/agent_jobs）

### 6) 超时保护验证
   timeout_s=0.001 → timed_out=True rc=-9

=== 总结: ✅ 全部通过
```

---

## 7. 和 BalanceAI 角色怎么对齐

| BalanceAI 角色 | 建议绑在 | 理由 |
|---|---|---|
| A 建表 / F 取数 | 有外网、能抓官方源的那台 | 需要 DataHub |
| B 新闻 | 有外网的那台 | 需要 futures_news |
| C 测算 / D 审校 / E 报告 | 只读本地文件，任意 | 无网络依赖 |
| Q 品种终审 / J 整轮终审 | 离线那台 | J 文档明确要求离线 |
| auditor 审计官 | 任意（默认 rule/stat 都不联网） | 见 docs/AUDIT.md |

`plan_roles` 的示例分配就是按这个来的（openclaw=A/F/E、hermes=B/C、coze=D/Q/J）。

---

## 8. 自检

```bash
python3 -m pytest tests/test_runner_remote.py -q      # 24 个
python3 -m pytest tests/ -q                           # 全集 134 个
python3 examples/tcp_roles_demo.py                    # 端到端
```

检查点：
- 没配 `AGENT_RUN_ALLOW` 时 `runner/py` 一定返回 `denied: true`
- `../` 和白名单外的软链接都 `denied`
- 超时脚本在 1s 内返回 `timed_out: true`
- 点名 `agent-p` 的任务，`snap["agent_id"] == "agent-p"`（不是 round-robin 给别人）
