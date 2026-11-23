# 接口规范（interface-spec/1.0）：先验规范，再拆活

这份文档定义"开发报告里的接口表要满足什么"，以及框架怎么**用程序**检查它。
目的很直接：**报告阶段接口规范错了，往下拆的任务一定是错的**，所以不合格就不往下分。

```
producer/intake ──► producer/spec_check ──ok=true──► plan ──► dev …
                              │
                              └─ok=false─► plan/dev/audit 全部 skipped
                                            report 出「接口规范不合格」报告
```

## 1. 一行接口 = 七个字段

| 字段 | 必填 | 约束 | 示例 |
| --- | --- | --- | --- |
| 接口ID | 是 | `M-01` / `S-103`，全局唯一 | `M-01` |
| 调用方 → 被调方 | 是 | `→` 分隔；内部接口写鉴权机制 | `客户端 → 网关 → 匹配服` |
| 方法 | 是 | GET/POST/PUT/PATCH/DELETE | `POST` |
| 路径 | 是 | `/vN/` 开头、小写 kebab、段数 ≤5 | `/v1/match/enter` |
| 鉴权 | 是 | 是/否 + 机制（token / mTLS / 服务名 / 验签） | `是（mTLS）` |
| 幂等键 | 写接口必填 | 标准键：request_id / match_id / room_id / order_id / ticket_id / trace_id | `request_id` |
| 错误码 | 写接口必填 | 大写 SNAKE，至少 1 个 | `ROOM_EXHAUSTED` |

报告里用 Markdown 表格写；协议文件里用 JSON（`endpoints` 数组）。两种写法都能被解析。

## 2. 校验规则（0 token / 0 网络）

| 规则 code | 级别 | 触发条件 |
| --- | --- | --- |
| `spec_empty` | error | 报告没有任何接口 |
| `method_invalid` | error | 方法不在允许集合（如 `FETCH`） |
| `path_invalid` | error | 无 `/vN/`、含大写/下划线/尾斜杠、段数 >5 |
| `auth_unspecified` | error | 鉴权列留空或无法识别 |
| `idem_missing` | error | 写接口没有幂等键 |
| `idem_key_nonstandard` | warn | 幂等键不在标准集合 |
| `idem_on_read` | warn | GET 写了幂等键 |
| `errors_missing` | warn | 写接口没声明错误码 |
| `error_code_format` | warn | 错误码不是大写 SNAKE |
| `id_missing` / `id_format` | warn | 缺接口ID / 不是 `M-01` 形式 |
| `id_duplicate` | error | 接口ID 重复 |
| `endpoint_duplicate` | error | 同方法+路径重复声明 |
| `caller_missing` / `callee_missing` | warn | 缺调用方 / 被调方 |
| `internal_auth_weak` | warn | 内部接口没写 mTLS/服务名/token |
| `callback_unsigned` | error | 回调接口没写验签/签名 |

`ok = 没有 error`。warn 不阻塞，但会写进报告，评审时逐条确认。

## 3. 示例文件在哪

**① 规范文件（可复制填写、可单独校验）：**
[`examples/game_server/fixture/interface_spec.json`](../examples/game_server/fixture/interface_spec.json)
—— `interface-spec/1.0` 的完整形态：`conventions`（base_path / auth / idempotency / errors /
versioning）+ 8 个 `endpoints`（跨服匹配 + 商城折扣券，含内部 RPC 与支付回调）。
抄过去改成你们自己的接口即可，不用从空文件起步。

```bash
python -m multi_agent.integrations.game_server_spec \
  examples/game_server/fixture/interface_spec.json
# → 规范版本 interface-spec/1.0，接口 8 个，通过（error 0 / warn 0）；不通过时 exit=1
#   加 --json 拿结构化回执；--no-report-fields 用于纯协议文件（不要求接口ID/调用方）
```

**② 开发报告（Markdown 表格写接口表）对/错两份：**

- ✅ 合格：[`examples/game_server/fixture/ticket_classic.md`](../examples/game_server/fixture/ticket_classic.md)
  （跨服匹配 + 商城折扣券，8 个接口，含内部 RPC 与支付回调）
- ❌ 不合格：[`examples/game_server/fixture/ticket_bad_spec.md`](../examples/game_server/fixture/ticket_bad_spec.md)
  （接口ID 重复、鉴权留空、写接口缺幂等、`FETCH`、路径大写、回调无验签）

跑一次看拦截效果：

```bash
python examples/game_server/run_demo.py \
  --ticket examples/game_server/fixture/ticket_bad_spec.md --ticket-id gs-1199
# 期望：spec_gate.ok=false；plan/dev/pack/audit/gate/ledger/test_plan 全 skipped；
#       report 标题【接口规范不合格】，写明"未拆包、未下发开发"
```

## 4. 通过之后：用例由规范推导

`auditor/test_plan` 按接口逐条生成（纯模板，0 token）：

| 场景 | 生成条件 | 优先级 |
| --- | --- | --- |
| happy | 每个接口 | P0 |
| no_auth / forbidden | 声明需要鉴权 | P0 / P1 |
| bad_param | 每个接口 | P1 |
| replay | 写接口且有幂等键 | P0 |
| concurrent | 写接口 | P1 |
| timeout | 内部接口 | P2 |
| error_&lt;code&gt; | 声明的错误码（各取前 2 个） | P1 |

想让它**写测试报告**（而不是只出用例表），显式打开：

```bash
export GAME_TEST_LLM_URL=http://your-llm-gateway/v1/complete   # 也兼容 AUDIT_LLM_URL
# 调用时传 llm_test_report=true（示例 run_demo.py 默认 false，不烧 token）
```

模型不可用时自动降级为模板，并在报告里写原因；**不会偷偷调用、不会假装生成**。

## 5. 落地位置

| 文件 | 作用 |
| --- | --- |
| `multi_agent/integrations/game_server_spec.py` | 规则、校验、用例生成、可选模型报告 |
| `multi_agent/projects/game_server.py` | DAG 里的 `spec_gate` / `test_plan` 两步 |
| `examples/game_server/fixture/interface_spec.json` | 接口规范文件示例（conventions + 8 个接口），复制即用 |
| `examples/game_server/fixture/ticket*.json|md` | 合格/不合格报告样例 |
| `tests/test_game_server.py` | 规则命中、门禁行为、用例生成 |

## 6. 换成你们的规范

- **加规则**：在 `check_interface_spec()` 里加一段判定 + 在 `RULES` 表登记 code/级别/说明；
- **换字段**：如果团队用 `service/interface` 两列而不是 `调用方→被调方`，改
  `parse_endpoint_table` 的列识别即可，规则不用动；
- **错误码枚举**：把允许的错误码集合传进来（后续可加 `errors_allowlist` 参数），
  未登记的错误码报 warn，避免各写各的；
- **版本兼容**：路径必须带 `/vN/`，删除字段要在协议文件里标 `deprecated`，
  审计的 `deprecated_still_served` 会盯着实现有没有同步下线。