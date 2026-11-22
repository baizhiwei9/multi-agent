# multi-agent：跨 Agent 通讯平台

一个**不经过大模型**的多 Agent 协作底座：Agent 启动时把自己的「角色 + 动作表」注册到 Hub，
协调者按 `capability/action` 发任务，Agent 在本地执行代码、回传结果；所有数据读写统一走
**DBManager**，所有编排放 **Coordinator** 里。

配套场景：大宗商品收支平衡表（A 建表 → 入库 → B 新闻 → Q 修正判定 → E 报告 → J 终审）、
股票（宏观角色 + 微观辩证角色）。

---

## 1. 三层模型

```
        ┌──────────────────────────── Hub（注册表 + 任务队列 + 长轮询）────────────────────────────┐
        │                                                                                          │
        │   register(roles/actions)        task(capability,action)          result                 │
        │        ▲                                  │                          ▲                     │
        │        │                                  ▼                          │                     │
        │   ┌────┴─────┐                     ┌────────────┐              ┌─────┴─────┐               │
        │   │  Agent   │  ← 长轮询取任务 ──  │ Coordinator│  ── 派发 ──  │  Agent    │               │
        │   │ (绑角色) │                     │  (编排 DAG)│              │ (绑角色)  │               │
        │   └────┬─────┘                     └─────┬──────┘              └─────┬─────┘               │
        │        │                                 │                           │                     │
        │        └───────────── db/read|write ─────┴─────────── db/* ──────────┘                     │
        │                                   ┌──────────────┐                                        │
        │                                   │  DBManager   │  ← 唯一数据库入口（docs/streams/schema）│
        │                                   └──────────────┘                                        │
        └──────────────────────────────────────────────────────────────────────────────────────────┘
```

- **角色（Role）**：能力单元，如 `A/build_sheet`、`macro/regime`；
- **Agent**：进程，绑定一个或多个角色，向 Hub 注册"我能做什么"；
- **Coordinator**：编排者，按 DAG 发任务、等结果、推进下一步（可选落库）；
- **DBManager**：唯一 DB 读写入口，其他 Agent 不直连数据库；
- **Reporter**：按需/定时从 DBManager 拉数据出报告（普通 Agent 的一种用法）。

## 1.1 角色拓扑：Manager / DBManager / Worker

| 组件 | 角色（capability） | 职责 |
| --- | --- | --- |
| **Manager** | `coordinator` + `rendezvous` | 编排 DAG、派任务、收结果；兼 NAT 打洞协调（观测公网地址、交换 endpoint、中继回退） |
| **DBManager** | `db` | 唯一 SQLite 入口（WAL + `BEGIN IMMEDIATE` + 文件锁），所有读写都经它 |
| **Worker** | `A/B/T/F/Q/E/J`、`macro/micro`、`mcp`… | 只做本地业务逻辑，执行完回 `result`；可多个、可一进程绑多角色 |

约束：Manager 不执行业务、DBManager 不编排、Worker 不直连数据库；三者都通过 Hub 的
`capability/action` 协议说话。Worker 数量按负载加，Manager/DBManager 各一个即可。

**最少要起几个进程**：Hub 1 个（Server/注册表，不是 Agent）+ Manager 1 个 + DBManager 1 个
+ Worker 1 个及以上。Hub 与 Manager 可以同机部署（公网机器同时开 `serve` 和
`coordinator`，rendezvous 也挂在 Manager 侧），内网 Worker 只做出站连接。
任务里 `capability/action` 没有任何存活 Worker 注册时，Hub 直接判 `no_agent` 失败，
`trace` 里能看到"没有可执行 X/Y 的存活 Agent"，不用猜。

## 2. 为什么不用文件邮箱 / 大模型对话

| 方案 | 跨机器 | 执行成本 | 谁在驱动 | 结论 |
| --- | --- | --- | --- | --- |
| 同机文件邮箱（BalanceAI 现状） | 否 | 低 | 进程轮询文件 | 只能单机，权限/并发难控 |
| 大模型对话触发 | 是 | 每步一次推理 | 模型 | 不稳定、贵、不可审计 |
| **HTTP/JSON Hub + 长轮询** | **是** | **零推理** | **协议 + 本地代码** | **本项目** |

大模型只适合放在**规划**阶段（生成 pipeline/任务清单），执行阶段全部走协议。

## 3. 快速开始

```bash
pip install -e .                # 无第三方依赖，纯 stdlib

# 1) Hub（跨机器：--host 0.0.0.0，并设 token）
multi-agent serve --host 0.0.0.0 --port 8787 --db hub.db --token secret

# 2) DBManager（唯一写入口）
multi-agent db --hub http://127.0.0.1:8787 --id db-manager --db-path data.db --token secret

# 3) Coordinator（DAG 编排；--db-path 表示把运行状态也落到 DBManager）
multi-agent coordinator --hub http://127.0.0.1:8787 --id coordinator \
    --db-path data.db --token secret

# 4) 业务 Agent：一个进程可绑多个角色
multi-agent agent --hub http://127.0.0.1:8787 --id balanceai-1 --token secret \
    --role "A:build_sheet" --role "Q:verdict" --role "E:report" \
    --handlers examples.balanceai_agent:register

# 5) 跑流水线
multi-agent pipeline --hub http://127.0.0.1:8787 --token secret \
    --name balance_sheet --params '{"commodity":"copper","category":"有色","hours":6}' --wait
```

本机冒烟（Hub + DBManager + Coordinator + 演示 Agent 一条命令）：

```bash
multi-agent demo-flow
```

浏览器打开 `http://127.0.0.1:8787/` 可看 Agent 与任务流水（token 存 localStorage）。

## 4. 能力清单（动作表）

Agent 注册时提交：

```json
{
  "agent_id": "stock-a",
  "roles": [
    {"name": "macro", "actions": ["regime", "rates"], "description": "宏观状态"},
    {"name": "micro", "actions": ["dialectic"], "description": "微观辩证"}
  ],
  "labels": {"site": "sh"},
  "ttl_s": 60
}
```

Hub 只按 `capability(=role.name) + action` 精确投递；同一能力多实例时 round-robin。
`actions: ["*"]` 表示该角色接收任意动作（慎用）。

## 5. 内置两个特殊组件

### DBManager（capability = `db`）

| 动作 | 参数 | 用途 |
| --- | --- | --- |
| `write` | collection, key, value, merge? | 按 key 覆盖/合并写文档 |
| `read` | collection, key | 读单文档 |
| `list` | collection, prefix?, limit? | 列文档 |
| `delete` | collection, key | 删除 |
| `append` | stream, payload | 追加事件（新闻/修正/审计） |
| `tail` | stream, limit?, after? | 拉事件流 |
| `register_schema` | collection, columns | 显式登记字段 |
| `collections` / `stats` | — | 概览 |

SQLite 锁：`WAL` + `BEGIN IMMEDIATE` + `fcntl.flock` 文件锁（`<db>.lock`）三层，
多进程/多实例写同一文件时串行化，读走 WAL 快照不阻塞；`busy_timeout=5000`。

Agent 侧用 `DBClient`：

```python
from multi_agent.dbmanager import DBClient
db = DBClient("http://hub:8787", token="secret")
db.write("balance", "copper:latest", {...})
db.append("news", {"title": "...", "impact": "减产"})
```

### Coordinator（capability = `coordinator`）

| 动作 | 参数 | 用途 |
| --- | --- | --- |
| `start` | pipeline, params | 启动一条流水线，返回 run_id |
| `status` | run_id | 查运行状态（逐步状态/结果） |
| `runs` | limit? | 最近运行 |
| `notify` | run_id, step, status, payload | Agent 主动上报事件（写 `pipeline_events`） |

## 6. 收支平衡表流水线

```
A 建表 ──► db.write(balance) ──► B 新闻 ──► db.append(news)
                                   │
                                   ├─(事件>0)─► Q 修正判定 ──► db.write(verdicts)
                                   │
                                   └──────────► E 报告 ──► ★审计官 audit ──► J 终审
```

定义在 `multi_agent/pipelines.py` 的 `BALANCE_SHEET`：步骤带 `depends_on`、`when`、
参数模板 `${steps.build.result}` / `${params.commodity}`。Coordinator 每步执行完把
run/step 状态写 `pipeline_runs`，报告者可以定时 `db.read("pipeline_runs", ...)` 或
直接读 `balance/*` 出报告。

### 6.1 审计官（auditor）：默认不联网、不烧 token

报告出完不是终点 —— 先过一道审计再进终审。级别由**协调者**决定，
默认 `rule`（纯规则，0 网络 0 token）：

```python
coord.start("balance_sheet", {"commodity": "copper", "audit_level": "rule"})  # 默认
coord.start("balance_sheet", {"commodity": "copper", "audit_level": "stat"})  # +统计检验
coord.start("balance_sheet", {"commodity": "copper", "audit_level": "llm"})   # +语义审计
```

| 级别 | 走网络 | 烧 token | 查什么 |
|---|---|---|---|
| `rule`（默认） | ❌ | ❌ | 字段/单位/量级/内部一致性/常量列/新鲜度/报告占位符/叙事与数据矛盾 |
| `stat` | ❌ | ❌ | ADF 单位根、KPSS、Chow 断点、Ljung-Box 自相关、JB 正态、AR(1) 过拟合 |
| `llm` | ✅ | ✅ | 语义矛盾（需要调用方传 `llm` 回调） |

**终态多了一个 `degraded`**：跑通了但审计判 fail/suspect —— 结果可看不可直接用。

细节（算法实现、临界值、finding 清单）见 [docs/AUDIT.md](docs/AUDIT.md)。

### 6.2 远程派活与角色分配（runner）

除了"谁能干就谁干"，还需要"**让那台机器上的那个 agent 跑那个脚本**"。
`multi_agent/integrations/runner.py` 把它做成一个受控能力（`capability=runner`）：

```python
from multi_agent.integrations import runner
runner.register(rt)          # agent 侧一行挂上，读 AGENT_RUN_ALLOW 白名单
```

协调者侧（HTTP 或 TCP 都行）：

```python
from multi_agent.remote import RemoteCoordinator
rc = RemoteCoordinator.from_tcp("hub-host", 8790, token="***")

rc.health()                                    # 注册表 + 绑定表 + 重复角色体检
rc.assign_role("hermes-1", [{"name": "B"}, {"name": "C"}])   # 运行时挂角色
rc.run_python("hermes-1", "/srv/jobs/scan_news.py", argv=["--hours", "24"])
# → {"ok": true, "returncode": 0, "stdout": "...", "duration_ms": 72}
```

**安全边界默认拒绝**：`AGENT_RUN_ALLOW` 没配就一个脚本都跑不了；路径先 `realpath`
再用 `relative_to` 判白名单（`../` 和软链接都拦）；`shell=False`；超时 `killpg`
整组；输出截断 8KB。

⚠️ **同一角色不能绑两台机器**（`balance/`、`verdicts/` 会互相覆盖，见
docs/AGENTS.md §4.3）。`RoleBinding` 会拒绝，`plan_roles()` 能提前体检出重复。

细节见 [docs/REMOTE.md](docs/REMOTE.md)、[docs/ROUTING.md](docs/ROUTING.md)，端到端演示：
```bash
python3 examples/tcp_roles_demo.py          # 起 Hub + 3 agent + 协调者，全走 TCP
python3 examples/capability_routing_demo.py # 按能力派活（谁声明谁接）
```

### 6.3 按能力派活（协调者不写死机器）

协调者不该知道"谁在哪台 IP"，只该知道"谁声明了这个能力"。
`multi_agent/router.py` 做这件事：

```python
from multi_agent.router import CapabilityRouter

router = CapabilityRouter(client)          # HubClient(HTTP) 或 TCPClient(TCP)

router.catalog()                           # 全网能力清单：每个能力 ← 哪些 agent
router.route("auditor:bond", "balance")    # 选一台，带 reason
router.gaps(["auditor:bond/balance", "runner/py"])   # 缺什么，提前报
router.route_plan([...])                   # 一条流水线的可行性预检
router.submit("auditor:bond", "balance", {...}, wait=True)
router.submit_with_fallback(...)           # 失败自动换下一台
```

**能力 = 角色名 + 动作**，所以角色名要带口径：

```python
audit.register(rt, projects=["bond"])     # → capability = "auditor:bond"
audit.register(rt, projects=["bond", "stock"])  # → 两个角色，一台机器多种能力
```

不拆口径的代价（实测）：所有审计官都叫 `auditor` 时，4 次派活会
`['openclaw-2','coze-1','hermes-1','openclaw-1']` 全混在一起；拆完变成
`平衡表→openclaw、股票→hermes、债券→coze` 精确命中。

要点：只按能力（没声明=不支持，不猜）、每次给 reason、点名不静默换人、
传了 prefer_labels 就一定先按标签筛、缺能力提前报。

细节见 [docs/ROUTING.md](docs/ROUTING.md)。

### 6.4 零 IP 打洞（STUN → 换票 → 直连，省 token）

三方都在私有网络里、谁也不知道对方公网 IP 时，用三层引导：

```bash
# ① 自己问出公网地址（不需人知道 IP）
python3 -m multi_agent.bootstrap stun
# → {"mapped": ["120.245.114.140", 50237], "symmetric_hint": false}

# ② 出一张 ~40 字符的门票，只发一次（这就是全部 token 成本）
python3 -m multi_agent.bootstrap ticket --id openclaw
# → A1|120.245.114.140|50237|8792|openclaw

# ③ 双方拿对方门票打洞，打透后直连
python3 -m multi_agent.bootstrap join --id hermes \
    --peer-ticket 'A1|120.245.114.140|50237|8792|openclaw' \
    --exec-on-msg --listen 300
```

实测（同 NAT 后两个进程）：`✅ direct ... rtt≈2ms`，然后 A 发一条
`{"action":"run","script":"..."}`，B 在本机执行并把 stdout/返回码回传 ——
**全程只有一次门票交换，之后零 token。**

打不通（对称 NAT / 严格防火墙）时明确返回 `state: failed` + "需要中继"，
不会静默假装成功。降级路径：`nat.py` 的 RendezvousServer 或 ntfy 中继。

细节见 [docs/BOOTSTRAP.md](docs/BOOTSTRAP.md)；成本实测（帧/字节/零 token 的复现方式）见 [docs/COST.md](docs/COST.md)。

### 6.5 共享数据协议 + 项目级清理

**所有 agent 读写同一份数据**，靠"唯一写入口"保证：

```python
from multi_agent.shared_data import SharedData

data = SharedData(db_client, role="Q")     # 全部转成 db/* 任务，不直连库
bal  = data.get_balance("copper")          # A 写的，Q 读到同一份
data.put_verdict("copper", {"verdict": "accept"})
data.put_audit("copper", report)           # 同时写 docs + audit_log 流
```

集合命名规范写死在 `COLLECTIONS`（owner + readers）：写错名字立刻 `SchemaError`，
越权写会 `warn`（`strict=True` 抛错），`audit_protocol()` 能查出"有人绕过门面直接写库"。

**项目级清理**（不同项目不同规则）：

```python
from multi_agent.db_policies import plan_prune, MaintenanceScheduler

plan_prune(db, "balance_sheet", dry_run=True)   # 先看会删什么（307 条）
plan_prune(db, "balance_sheet")                 # 真删，数量必须与计划一致
MaintenanceScheduler(db, ["balance_sheet","stock","bond"], interval_s=3600).start()
```

| 项目 | 平衡表 | 新闻流 | 判定 | 财报 | 债券要素 |
|---|---|---|---|---|---|
| balance_sheet | 留 3 期 | 30 天/5万 | 180 天 | — | — |
| stock | — | 30 天/5万 | — | **永久** | — |
| bond | — | 30 天/5万 | — | — | **永久** |

五条安全约束：只删能算出来的、不动 schemas、dry-run 优先、单次有上限、
VACUUM 需显式。

细节见 [docs/SHARED_DATA.md](docs/SHARED_DATA.md)。

### 6.6 DBManager 并发：读写锁 + 每线程连接

多个 agent 同时打到 DBManager（E 读、A 写、B 写）时，原来的单把 RLock
会让**读读也互斥**，而且共享一条 sqlite 连接在并发读下是未定义行为。

现在三层锁：

```
RWLock（进程内，多读并行 + 写独占 + 写优先防饿死）
FileLock（跨进程 flock）      ← 多 DBManager 写同一文件
SQLite BEGIN IMMEDIATE + WAL  ← 事务边界
```

配套改：**每线程一条 sqlite 连接**（`_current`），否则光有锁也不安全。
`:memory:` 用共享缓存 URI，保证多线程连到同一个内存库。

实测：8 线程 × 200 次并发读，`max_concurrent_readers=8`（真并行）；
6 个读线程压着时 30 次写只用 12ms（没被饿死）。

⚠️ 修了一个隐蔽 bug：`write()` 的 merge 分支还写着 `self._conn`（主线程连接），
多线程下立刻 `database is locked`。修完测试耗时 161s → 2.66s。

排障：`db.lock_stats()` 或 `db/lock_stats` 动作能看实时锁状态。

细节见 [docs/DB_CONCURRENCY.md](docs/DB_CONCURRENCY.md)。

### 6.7 项目扩展：一个框架，三个项目

框架（Hub / Coordinator / DBManager / 审计引擎）不认识业务。项目继承
`multi_agent.projects.base.Project`，只声明五样东西：`roles`（capability/action 表）、
`pipeline`（DAG）、`audit_project`（审计口径）、`news()`（新闻采集官）、
`reporter()`（报告器）。框架把它们变成 Agent manifest 与 `PIPELINES` 条目。

| 项目 | 类 | 角色 | 审计 | 新闻 | 报告 |
| --- | --- | --- | --- | --- | --- |
| 期货收支平衡表 | `FuturesBalanceProject` | A/B/T/F/Q/C/D/E/J | `balance_sheet` | DataHub `futures_news` | 7 段式 |
| 股票 A 股日报 | `StockMattProject` | M1..M5 + news/J | `stock` | 正则规则（零 token） | 日报 |
| 最小示例 | `MinimalReportProject` | collect/render | 无 | 无 | 最小模板 |

```bash
multi-agent project list                      # 三个项目 + 角色/流水线/审计
multi-agent project show futures-balance      # 角色表、步骤、扩展点
multi-agent project pipelines                 # 装进 PIPELINES（coordinator 进程自动装）
multi-agent project agent --hub ... --id w1 --name stock-matt   # 按项目角色起 Agent
```

真实执行通过 `executor=` 注入（本机调函数，或 `runner/py` 跨机跑脚本）；没注入时
handler 返回 `degraded` + 原因，流水线能先跑通。新增项目照 [docs/PROJECTS.md](docs/PROJECTS.md) 抄。

### 6.8 风控官（risk_manager）与审计官的分工

审计官判"这份产出能不能信"，风控官判"这个仓位该不该开、能开多大"。两者**只用结构化文本**通信：

- 风控官 → `RISK_BRIEF v2.0`：regime / 五大指数信号 / 流动性 / 结构混沌 / 硬否决 / 建议动作；
- 审计官 → `AUDIT_VERDICT v2.0`：复算 vol·回撤·流动性、查一致性、查漏报，给出 `APPROVE / MODIFY / REJECT`。

硬计算（vol 分位、回撤、OI、保证金、相关性）全用 Python/numpy，**零 token**；模型只用于
文字归纳且需显式注入。风控的硬否决被漏标时，审计官强制 `REJECT`。

```python
from multi_agent.integrations.risk import RiskManager, Auditor
brief = RiskManager().generate_report(market_data)          # 文本
verdict = Auditor().review(brief, proposal, market_data)    # 文本
```

流水线已内置 `risk(brief) → risk_review(auditor/review_brief) → audit → final` 两步；
协议与规则见 [docs/RISK_PROTOCOL.md](docs/RISK_PROTOCOL.md)。

## 7. 协议端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查（免 token） |
| POST | `/v1/register` | 注册能力清单 |
| POST | `/v1/heartbeat` | 心跳（TTL 默认 90s） |
| GET | `/v1/agents` | 列表 + 统计 |
| GET | `/v1/agents/{id}/inbox?wait=20` | 长轮询取任务（204 = 暂时没有） |
| POST | `/v1/tasks` | 提交任务（`wait` 可同步） |
| GET | `/v1/tasks/{id}` | 任务状态 |
| POST | `/v1/tasks/{id}/result` | Agent 回传结果 |
| POST | `/v1/tasks/{id}/cancel` | 取消 |
| DELETE | `/v1/agents/{id}` | 注销 |

详细字段/状态机/错误码见 [PROTOCOL.md](PROTOCOL.md)。

## 7.1 多协议：同一套 Hub，三种线缆

| 协议 | 入口 | 客户端 | 适用 |
| --- | --- | --- | --- |
| REST/JSON | `/v1/*` | `HubClient` | 脚本、curl、简单集成 |
| JSON-RPC 2.0 | `POST /rpc` | `JsonRpcClient` | 程序间直调，方法名扁平（`hub.submit`、`db.write`） |
| **TCP** | `tcp://host:8790` | `TCPClient` / `TcpTransport` | 内网/同机房直连，长连接、少一层 HTTP 解析 |
| MCP | stdio / `POST /mcp` | `MCPServer` / `MCPClient` | Claude Desktop、IDE、其它 Agent |

统一调用面：`HubAPI(transport)`，底层换线缆不改业务代码：

```python
from multi_agent.transports import HubAPI, JsonRpcTransport
api = HubAPI(JsonRpcTransport("http://hub:8787", token="secret"))
api.submit("A", "build_sheet", {"category": "有色"}, wait=True)
api.db("write", {"collection": "balance", "key": "copper:latest", "value": {...}})
```

MCP 两种用法：

```bash
# 把 Hub 能力暴露成 MCP tools（stdio，给 Claude Desktop/IDE 用）
multi-agent mcp-server --hub http://127.0.0.1:8787 --token secret

# 把外部 MCP server 的工具挂到 Hub（协调者可直接派活）
multi-agent mcp-agent --hub http://127.0.0.1:8787 --id mcp-1 --token secret \
    --command "npx -y @modelcontextprotocol/server-filesystem /data"
```

Claude Desktop 配置：

```json
{"mcpServers": {"multi-agent": {"command": "python",
  "args": ["-m", "multi_agent.cli", "mcp-server",
           "--hub", "http://127.0.0.1:8787", "--token", "secret"]}}}
```

低 token 的依据：每次调用只传方法名 + 参数（MCP 只传 tool name + arguments），
没有系统提示、没有历史上下文、没有模型推理；传输层是确定性请求/响应。

## 7.2 注册 → 地址交换 → 直连（LLM 只负责开局）

大模型 `@` 互相点名只适合**开局**：确认谁是谁、地址是什么。确认完之后应立刻落到协议上：

```
① 每个 Agent 启动：register {agent_id, endpoint:"tcp://10.0.0.8:8790" 或 "http://10.0.0.8:8787", roles, ttl_s}
② 协调端：GET /v1/agents（或 hub.agents）拿到所有 Agent 的 endpoint + 能力清单
③ 之后：协调端按 capability/action 发 task；Agent 长轮询/直连取任务、回结果
   —— 不再需要任何 LLM 调用，也不再需要互相 @
```

`endpoint` 可以是内网 IP、K8s Pod DNS、公网域名或反代地址；TCP 与 HTTP 都支持。
同一份注册消息就是"地址交换"：注册表里存下 endpoint，协调端/其它 Agent 直接按地址连。

```bash
# Hub 同时开 HTTP 和 TCP
multi-agent serve --host 0.0.0.0 --port 8787 --tcp-port 8790 --token secret

# Agent 注册时带地址（示例见 examples/）
# 协调端查地址
multi-agent call --target tcp://127.0.0.1:8790 --token secret --method hub.agents
```

TCP 协议：每条消息一行 JSON（JSON-RPC 2.0），连接后首包 `auth` 握手，之后直接发
`hub.*` / `db.*` / `coordinator.*`；详见 [PROTOCOL.md](PROTOCOL.md) 第 12 节。

## 7.3 NAT 打洞（内网 Agent 互连）

公网协调者开 UDP rendezvous（`serve --udp-port 8791`）。每个 Agent 用**同一个 UDP
socket** 发 `hello`，协调者从源地址看到 NAT 映射后的公网 `ip:port`（STUN 同款原理）。

A 要连 B：

1. A 发 `punch`，协调者把 A 的公网地址转给 B、B 的转给 A；
2. 双方从同一个 socket 向对方公网地址反复发 `PUNCH`，NAT 放行回包后直连；
3. 对称 NAT / 严格防火墙打不通 → 自动 `relay` 中继（协调者转发）。

```bash
multi-agent serve --host 0.0.0.0 --port 8787 --tcp-port 8790 --udp-port 8791 --token secret

# 两台内网机器各跑一个：先 hello，再打洞
multi-agent punch --rendezvous <公网IP>:8791 --id agent-a --peer agent-b \
    --token secret --listen 10
```

关键：hello / punch / 业务数据必须复用同一个 socket；换 socket 端口映射就变了。

## 7.4 卡在哪一步：diagnose 与 trace

- `diagnose` 逐项体检，失败项直接给原因与耗时：

```bash
multi-agent diagnose --hub http://127.0.0.1:8787 --tcp tcp://127.0.0.1:8790 \
    --rendezvous 127.0.0.1:8791 --peer agent-b --token secret
```

```
[OK  ] http.health          12ms  {"ok":true,...}
[OK  ] hub.agents           18ms  {"count":3,...}
[FAIL] db.roundtrip       5012ms  HubError: db/write 失败...
[OK  ] rendezvous.hello      8ms  {"observed":["1.2.3.4",54321],...}
[WARN] punch.agent-b      4003ms  {"mode":"relay","reason":"打洞超时，回退中继"}
```

三档含义：`OK` 通；`WARN` 能跑但降级（当前只有打洞退回中继）；`FAIL` 该步挂了，
后面几行仍会继续跑完，所以一次就能看到"卡在哪一步"。

- `trace` 看单个任务的时间线（created → assigned → queued(重试) → succeeded/failed）：

```bash
multi-agent trace t_ab12... --hub http://127.0.0.1:8787 --token secret
```

## 8. 可靠性

- **TTL + 心跳**：Agent 掉线后不再被派活（默认 90s）；
- **长轮询**：Agent 只出站连接，NAT/内网可用，无 inbound 端口；
- **超时换人**：`timeout_s` 到点未回结果 → 换下一个候选（`max_retries`）；
- **幂等**：`idempotency_key` 相同只建一个任务；
- **迟到结果丢弃**：超时后任务已重新排队，旧 Agent 的迟到结果不会覆盖新尝试；
- **重启恢复**：Hub 启动时把 `queued/assigned` 任务重新派发；
- **失败重试**：handler 抛错 → 任务 `failed`，有额度则换 Agent 重试。

## 9. 部署与安全

- 跨机器：`--host 0.0.0.0`，防火墙放行端口；建议前置 Nginx/Caddy 做 TLS；
- 认证：所有 `/v1/*` 校验 `X-Hub-Token`（`Authorization: Bearer` 也支持）；
- 内网隔离：DBManager 只注册到 Hub，不暴露数据库文件/端口；
- UDP 发现：`--beacon` 仅限同一广播域，跨网段请直接配域名/IP。

```ini
# systemd 示例（/etc/systemd/system/multi-agent-hub.service）
[Service]
WorkingDirectory=/opt/multi-agent
Environment=MULTI_AGENT_TOKEN=change-me
ExecStart=/usr/bin/python3 -m multi_agent.cli serve --host 0.0.0.0 --port 8787 --db /var/lib/mah/hub.db
Restart=always
```

## 10. 已知限制

- 单 Hub 无内置 HA（可用反向代理 + 共享 SQLite/PG 做多活，需自行扩展）；
- Handler 执行中不能被强制中断，`cancel` 只阻止后续派发/结果采纳；
- token 是单租户共享密钥，未做 per-agent 权限；
- Coordinator 串行推进 DAG（独立分支未并行），大规模编排需要扩展。

## 11. 目录

```
multi_agent/
  protocol.py   消息信封/校验
  registry.py   能力清单/心跳/TTL/路由
  store.py      任务状态机（SQLite）
  hub.py        调度/重试/收件箱
  server.py     HTTP/JSON 接口
  client.py     Agent SDK（注册/长轮询/执行/回传）
  dbmanager.py  DBManager + DBClient
  coordinator.py DAG 编排 + CoordinatorAgent
  pipelines.py  内置流水线（balance_sheet / stock_daily / demo）
  cli.py        serve/agent/db/coordinator/pipeline/mcp-server/mcp-agent/demo
  rpc.py        JSON-RPC 2.0 方法分发
  mcp.py        MCP server / client / agent（stdio + HTTP）
  tcp.py        TCP 传输（newline JSON-RPC + auth 握手）
  nat.py        UDP rendezvous + 打洞 + 中继回退
  diagnose.py   分步体检（HTTP/TCP/rendezvous/DB/punch）
  transports/   Transport 抽象 + REST / JSON-RPC 传输 + HubAPI
  discovery.py  UDP 局域网发现（可选）
  console.py    只读控制台
  integrations/ BalanceAI、股票示例
examples/       balanceai_agent.py / stock_agent.py / coordinator.py
tests/          269 项离线测试
```

## 12. 测试

```bash
PYTHONPATH=. python -m pytest tests -q
# 269 passed
```