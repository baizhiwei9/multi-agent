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
tests/          53 项离线测试
```

## 12. 测试

```bash
PYTHONPATH=. python -m pytest tests -q
# 53 passed
```