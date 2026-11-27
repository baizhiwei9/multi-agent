# 这套到底是"哪种模式"：能力路由 + 持久化任务队列，不是纯 pub/sub

结论先说：

- **动作（action）= 服务注册 + 能力路由的异步 RPC**：每个 Agent 启动时把自己的
  `capability + action` 注册到 Hub（服务发现），别人 `submit(capability, action, params)`
  请求它。Hub 按 round-robin 从**同一能力的多个实例里挑一个**派活，其余实例收不到
  → 这是 **点对点单播 + 竞争消费**（work queue / 负载分担），不是广播。
- **请求体真在 DB 里**：`tasks` 表（SQLite）存 `params`(JSON)、`idempotency_key`(唯一)、
  `state`、`attempts`、`result`、`error`；协调官的 run 落在 `pipeline_runs`。
  所以能 `recover()`（Hub 重启后重派）、`resume()`（续跑）、幂等重放（同键返回同一条任务）。
- **真像 pub/sub 的是另一层**：`emit` + 共享通道。发布者把结果并进 `channels.x`，
  订阅者（下游任意多步）用 `${channels.x}` 读，靠 `replace/append/sum/merge` 合并。
  这一层"发布者不知道谁订阅、订阅者之间也不认识"，但**范围是单次 run**，不是全局 topic。
- **扇出（`foreach`）** 是第三种：一个步骤按列表展开成 N 个并行任务，本质是批量单播。

所以整体更像 **Actor 模型的 mailbox + 服务注册表 + 持久化队列**（也可以说"带幂等的
capability-based RPC"），而不是 Kafka 那种"主题 → 多订阅者、离线也能攒 backlog"的消息中间件。

---

## 跑一下看四种行为

```bash
python examples/messaging/demo.py
python -m pytest tests/test_messaging_demo.py -q
```

| 场景 | 现象 | 对应的"模式" |
| --- | --- | --- |
| ① 两个 `ocr-1/ocr-2` 注册同一 `ocr/run`，提交 4 个任务 | 各接 2 个（round-robin），没有任务被执行两次 | 服务发现 + 竞争消费（单播） |
| ② 两个生产者 `emit append` 到 `events`，两个订阅者读 `${channels.events}` | 两边都拿到合并后的全量 `["铜矿停产","库存连降三周","美元走强"]` | run 内广播（发布/订阅味道） |
| ③ 带 `idempotency_key` 提交，先查库再重复提交 | 库里能看到 `params` 原文；同键返回同一个 `task_id`，handler 只跑 1 次 | 请求体落库 + 幂等重放 |
| ④ 没有 Agent 注册该能力就提交 | 立即 `failed / no_agent` | **没有 backlog**：不攒消息等订阅者上线 |

## 关键代码位置

| 行为 | 实现 |
| --- | --- |
| 注册能力 / 心跳 / TTL | `hub.py` `Registry.register` / `Registry.next`（round-robin 选人） |
| 任务落库 + 幂等 | `store.py` `TaskStore.create`（`idempotency_key UNIQUE`）/ `update` |
| 派活与重试换人 | `hub.py` `_execute`（`exclude=tried`，失败/超时换下一个实例） |
| 长轮询取任务 | `hub.py` `next_message`（Agent 只出站请求，无入站端口） |
| 通道合并 | `coordinator.py` `_emit` / `_merge_value` |
| 扇出 | `coordinator.py` `_execute_foreach` |

## 想更接近"消息中间件"还缺什么

诚实标注三条边界（都不是 bug，是定位不同）：

1. **没有离线 backlog**：目标能力没有存活实例时任务直接失败，要"先攒着等人上线"得自己加
   pending 队列或让 Hub 保留 queued 状态重试；
2. **没有多订阅者广播投递**：同一个 `capability+action` 只会被**一个**实例拿到；
   要广播就显式扇出（`foreach`）或走 `channels`（run 内）；
3. **没有主题订阅关系**：`channels` 是 run 级共享状态，不是跨 run 的 topic 订阅表。

如果需要真正的 pub/sub（离线积压 + 多订阅者 + 主题管理），那是另一层：在 Hub 前面挂
RabbitMQ/NATS/Redis Streams，或者把 `channels` 提升成持久化 topic 表 + 订阅表——后者
会改 `coordinator` 的通道语义，得单独设计。