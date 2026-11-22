# 协议说明（v1）

传输：HTTP/1.1 + JSON；字符集 UTF-8；认证头 `X-Hub-Token`（或 `Authorization: Bearer`）。

## 1. 统一信封

```json
{
  "v": 1,
  "type": "register | heartbeat | task | result | cancel | error",
  "id": "唯一消息 id",
  "ts": "2026-11-22T08:00:00Z",
  "...": "按类型附加字段"
}
```

校验失败返回 `400 {"ok":false,"error":{"code":"bad_request","message":"..."}}`。

## 2. 端点

| 方法 | 路径 | 认证 | 说明 |
| --- | --- | --- | --- |
| GET | `/healthz` | 否 | `{"ok":true,"version":1}` |
| GET | `/` | 否 | 只读控制台 |
| POST | `/v1/register` | 是 | 注册能力清单 |
| POST | `/v1/heartbeat` | 是 | 续 TTL |
| GET | `/v1/agents` | 是 | 列表 + stats |
| GET | `/v1/agents/{id}` | 是 | 单个 Agent |
| GET | `/v1/agents/{id}/inbox?wait=20` | 是 | 长轮询；无任务返回 `204` |
| DELETE | `/v1/agents/{id}` | 是 | 注销 |
| POST | `/v1/tasks` | 是 | 提交任务 |
| GET | `/v1/tasks?limit=50&state=` | 是 | 任务列表 |
| GET | `/v1/tasks/{id}` | 是 | 任务详情 |
| POST | `/v1/tasks/{id}/result` | 是 | 回传结果 |
| POST | `/v1/tasks/{id}/cancel` | 是 | 取消 |

## 3. register

```json
{
  "v": 1, "type": "register", "id": "m1",
  "agent_id": "balanceai-1",
  "roles": [
    {"name": "A", "actions": ["build_sheet"], "description": "建表"},
    {"name": "Q", "actions": ["verdict"]}
  ],
  "labels": {"site": "sh"},
  "ttl_s": 60,
  "endpoint": "http://10.0.0.8:9000"
}
```

响应：`{"ok":true,"agent_id":"...","ttl_s":60,"heartbeat_interval_s":20}`。
`ttl_s` 会被夹到 `[5, 600]`；`actions:["*"]` 表示通配。

## 4. task

Hub → Agent（长轮询返回）：

```json
{
  "v": 1, "type": "task", "id": "m2",
  "from": "hub", "to": "balanceai-1",
  "task_id": "t_ab12...",
  "capability": "A",
  "action": "build_sheet",
  "params": {"category": "有色"},
  "timeout_s": 900,
  "idempotency_key": "run-1:build"
}
```

提交任务（HTTP body）：

```json
{
  "capability": "A",
  "action": "build_sheet",
  "params": {"category": "有色"},
  "timeout_s": 900,
  "wait": true,
  "max_retries": 1,
  "idempotency_key": "optional",
  "labels": {"site": "sh"}
}
```

`wait=true` 会等到任务收尾（含重试预算）再返回。

## 5. result

```json
{
  "v": 1, "type": "result", "id": "m3",
  "task_id": "t_ab12...",
  "agent_id": "balanceai-1",
  "ok": true,
  "result": {"report": "/path/xxx.md"},
  "duration_ms": 1234
}
```

失败时：

```json
{
  "v": 1, "type": "result", "id": "m4",
  "task_id": "t_ab12...", "agent_id": "a1", "ok": false,
  "error": {"code": "handler_error", "message": "RuntimeError: boom", "detail": "traceback..."}
}
```

错误码：`bad_request | unauthorized | forbidden | not_found | conflict | no_agent |
timeout | handler_error | canceled | internal`。

## 6. 任务状态机

```
queued ──► assigned ──► succeeded
   ▲           │
   └── timeout/失败重试 ──┘
        │
        ├─► failed（额度用尽/无候选）
        └─► canceled
```

- 同一个任务同一时刻只会分给一个 Agent；
- `failed` 若还有 `max_retries`，Hub 会重置为 `queued` 换人；
- 超时后旧 Agent 的迟到结果会被丢弃（只接受 `assigned` 状态的结果）。

## 7. 幂等 / 重试 / 恢复

- `idempotency_key` 在 SQLite 有唯一约束，重复提交返回同一任务（`created=false`）；
- `timeout_s` 是**单个 Agent** 的等待上限，不是整条任务的总预算；
- Hub 启动时扫描 `queued/assigned` 任务重新派发（进程重启不丢单）；
- Agent 结果 POST 失败会在客户端重试 3 次（指数退避）。

## 8. 内置能力

### db（DBManager）

```
write            {collection,key,value,merge?}
read             {collection,key}
list             {collection,prefix?,limit?}
delete           {collection,key}
append           {stream,payload}
tail             {stream,limit?,after?}
register_schema  {collection,columns[]}
collections      {}
stats            {}
```

### coordinator

```
start  {pipeline, params}      → {run_id,state}
status {run_id}                → 运行快照
runs   {limit?}                → 最近运行
notify {run_id?, step?, status?, payload?} → {ok:true}
```

流水线步骤字段：`name/capability/action/params/depends_on/when/timeout_s/max_retries`；
参数模板 `${params.x}`、`${steps.<name>.result.<path>}`；`when` 支持
`gt/gte/lt/lte/eq/ne/exists/truthy`。

## 9. curl 示例

```bash
TOKEN=secret
curl -s -X POST http://127.0.0.1:8787/v1/register \
  -H "X-Hub-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"v":1,"type":"register","id":"m1","agent_id":"a1",
       "roles":[{"name":"demo","actions":["echo"]}],"ttl_s":60}'

curl -s -X POST http://127.0.0.1:8787/v1/tasks \
  -H "X-Hub-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"capability":"demo","action":"echo","params":{"x":1},"wait":true,"timeout_s":10}'

curl -s "http://127.0.0.1:8787/v1/agents/a1/inbox?wait=20" -H "X-Hub-Token: $TOKEN"
```

## 10. JSON-RPC 2.0（POST /rpc）

请求：

```json
{"jsonrpc": "2.0", "id": 1, "method": "hub.submit",
 "params": {"capability": "A", "action": "build_sheet",
            "params": {"category": "有色"}, "wait": true, "timeout_s": 900}}
```

响应：`{"jsonrpc":"2.0","id":1,"result":{...}}`；错误：
`{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"未知方法"}}`。

方法表：

| 方法 | 说明 |
| --- | --- |
| `hub.register` / `hub.heartbeat` / `hub.agents` / `hub.inbox` | 注册与收件箱 |
| `hub.submit` / `hub.task` / `hub.tasks` / `hub.cancel` / `hub.result` | 任务 |
| `db.<action>` | 转成 capability=db 的同步任务 |
| `coordinator.<action>` | 转成 capability=coordinator 的同步任务 |

错误码：`-32700` 解析失败、`-32601` 方法不存在、`-32602` 参数错误、`-32000` 服务端错误。

## 11. MCP（stdio / POST /mcp）

支持方法：`initialize`、`notifications/*`、`ping`、`tools/list`、`tools/call`、
`resources/list`、`prompts/list`。协议版本 `2024-11-05`。

`tools/list` 由 Hub 注册表动态生成：每个 `capability/action` 一个工具，
名字为 `capability__action`（非字母数字替换为 `_`），`inputSchema` 为开放对象。

`tools/call`：

```json
{"jsonrpc":"2.0","id":2,"method":"tools/call",
 "params":{"name":"A__build_sheet","arguments":{"category":"有色"}}}
```

返回：

```json
{"jsonrpc":"2.0","id":2,"result":{
  "content":[{"type":"text","text":"{...任务快照...}"}],
  "isError": false}}
```

`arguments` 直接作为任务 `params`；可选 `_timeout_s` 控制等待秒数（默认 60）。
stdio 模式下每行一条 JSON-RPC；通知类消息不返回响应（HTTP 入口返回 202）。

## 12. TCP（tcp://host:8790）

每条消息一行 JSON（JSON-RPC 2.0，UTF-8，`\n` 结尾），长连接复用。
连接建立后首包必须是 `auth`（服务端启用 token 时）：

```json
{"jsonrpc":"2.0","id":1,"method":"auth","params":{"token":"secret"}}
```

成功：`{"jsonrpc":"2.0","id":1,"result":{"ok":true,"protocol":"tcp"}}`；
失败：`error.code=-32001`，未认证前的其它方法一律拒绝。

之后的方法表与 `/rpc` 完全一致（`hub.*` / `db.*` / `coordinator.*`）：

```json
{"jsonrpc":"2.0","id":2,"method":"hub.submit",
 "params":{"capability":"A","action":"build_sheet","wait":true,"timeout_s":900}}
```

服务端：`multi-agent serve --tcp-port 8790`（与 HTTP 同时监听，共用 token 与 Hub 状态）。
客户端：`TCPClient(host, port, token)` 或 `TcpTransport.from_url("tcp://10.0.0.8:8790", token)`。

注册时把 `endpoint` 填成 `tcp://10.0.0.8:8790`（内网/Pod/公网均可），协调端
`hub.agents` 拿到地址后即可直连，不再经过大模型。

## 13. UDP rendezvous / NAT 打洞

公网协调者监听 UDP（默认 `8791`），消息为单包 JSON：

| type | 字段 | 说明 |
| --- | --- | --- |
| `hello` | agent_id, token | 登记；响应 `hello_ok` 带 `observed`（NAT 后的公网 ip:port）与在线 peers |
| `punch` | from, to, token, session? | 请求打洞；协调者把双方公网地址互相转发，响应 `punch_ok` |
| `relay` | from, to, token, payload(base64) | 打洞失败时由协调者转发业务数据 |
| `ping` | agent_id, token | 保活；响应 `pong` + `observed` |

错误码：`unauthorized`、`peer_offline`、`bad_request`、`unknown_type`。

打洞数据面（两个 Agent 之间，不经协调者）：

- `PUNCH <session>` / `PONG <session>`：开洞与确认；
- `D:<bytes>`：直连业务数据；
- 直连失败时，客户端自动改用上面的 `relay` 消息。

## 14. 任务 trace

`GET /v1/tasks/{task_id}/trace`（或 JSON-RPC `hub.trace`）返回时间线：

```json
{"task_id": "t_ab12", "events": [
  {"id": 1, "ts": 1795..., "event": "created", "detail": {"capability": "A", "action": "build_sheet"}},
  {"id": 2, "ts": 1795..., "event": "assigned", "detail": {"agent_id": "balanceai-1", "attempts": 1}},
  {"id": 3, "ts": 1795..., "event": "succeeded", "detail": {"state": "succeeded"}}
]}
```

事件包括：`created / assigned / queued(重试) / succeeded / failed / canceled`；
`failed` 的 `detail.error` 带错误码与原因，用来判断卡在"取数、执行、回传"哪一步。