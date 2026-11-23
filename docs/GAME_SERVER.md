# 游戏服务端（game-server 分支）

这条分支把同一个 multi-agent 底座接到**游戏后台服务器开发**上：后台管理者接一份开发报告，
Coordinator 拆活，Worker 并行改代码，经理汇总送审，审计官拿**仓库里的文档/协议**逐项核对，
风控官按服务器逻辑做容量/回放/回滚闸门，最后 Reporter 交出**代码补丁 + 发布报告 + 文件清单**。

主干的东西一样没改：Hub 注册表、Coordinator DAG、DBManager 唯一写入口、Auditor 引擎、
Supervisor 送审台账全部复用；新增的只有三块 `integrations/game_server_*.py` 和一个
`projects/game_server.py`（外加可跑示例与测试）。

---

## 1. 角色链：你说的每一环落在哪

| 你的说法 | capability | 谁实现 | 输入 → 输出 |
| --- | --- | --- | --- |
| 后台管理者，接开发报告 | `producer`（`intake`/`plan`） | 项目 | 工单文件/Markdown → 工单 + 验收标准 + 分片（`workstreams`） |
| 协调者，往下分任务 | `coordinator` | 框架 | 流水线名 + 参数 → 按 DAG 派活；`foreach` 把分片扇出给 dev |
| Worker，开发 | `dev`（`implement`） | 项目（真实项目接 runner/编码 Agent） | 分片 → 补丁 + 变更文件 + 语法/单测结果 |
| 经理 Supervisor | `supervisor`（`pack`/`handoff`/`ledger`） | 框架 | 分片结果 → 送审件（截断/限额）+ 台账 |
| 审计官 Auditor | `auditor`（`code`） | 框架 + `game_server` profile | 仓库+文档 → `verdict` + `findings` |
| 风控官 | `server_risk`（`gate`/`capacity`/`replay`） | 框架 | 审计结论 + 容量/回放/回滚/经济 → `pass/warn/block` |
| Reporter | `report`（`build`） | 项目 + 交付模块 | 上游全部结果 → 报告 + manifest + 证据 |

与主干的取舍：

- **不新建"Manager"**：编排仍然是 Coordinator；后台管理者只是流水线第一个业务角色。
- **不把审计写进项目**：`auditor/code` 由框架提供，项目只在角色表里声明；写进 `handlers()`
  会把真实现覆盖成空壳（这点和 `examples/my_project` 的约束一致）。
- **风控不混用行情风控**：`integrations/risk.py` 是仓位/波动率风控；服务端上线闸门是
  `server_risk`，两者互不覆盖。

---

## 2. 一次发布的 DAG

```
intake ──► plan ──┬─► dev#auth  ─┐
（工单）  （拆包） ├─► dev#match ─┼─► pack ──► audit ──► gate ──► ledger ──► report ──► store
                  └─► dev#shop  ─┘  （经理）  （审计官） （风控）  （台账）   （发布包）  （入库）
```

| # | 步骤 | capability/action | 关键参数 | 产出 |
| --- | --- | --- | --- | --- |
| 1 | `intake` | `producer/intake` | `ticket`、`doc_root` | 工单、验收项、分片、文档清单 |
| 2 | `plan` | `producer/plan` | `brief`、`max_workstreams` | 规范化 `workstreams`（含 files/edits） |
| 3 | `dev` | `dev/implement`（`foreach`，上限 8） | `workstream`、`repo_root`、`out_dir` | 补丁 + 变更文件 + 语法检查 |
| 4 | `pack` | `supervisor/pack` | `payload=dev` | 送审件（字数/引用限额） |
| 5 | `audit` | `auditor/code` | `repo_root`、`doc_root`、`ticket`、`level` | `verdict` + `findings` + 快照摘要 |
| 6 | `gate` | `server_risk/gate` | `audit`、`capacity`、`replay`、`rollback`、`canary_pct`、`economy` | `gate` + `checks` + `blocked_by` |
| 7 | `ledger` | `supervisor/ledger` | `pack`、`entries` | 台账（每轮送审/结论） |
| 8 | `report` | `report/build` | 全部上游 + `out_dir` | `release_report.md` + manifest + 证据 |
| 9 | `store` | `db/write` | `collection=game_releases` | 发布记录入库（唯一 DB 入口） |

条件与续跑沿用主干：`when`、`foreach`、`emit`、`run_on_skipped`、`resume` 都可直接用；
审计等级默认 `rule`（0 网络 0 token），要统计/语义级由 `audit_level` 显式打开。

---

## 3. 交付契约

一次发布产出固定四类文件（`build_delivery`）：

```
<out_dir>/
├── release_report.md       结论前置：闸门 / 审计 / 开发 / 清单 / 未决项
├── manifest.json           {path, kind, bytes, sha256} × N
├── checksums.sha256        可直接 `sha256sum -c checksums.sha256`
├── artifacts/              开发补丁（unified diff）
└── evidence/               audit.json / gate.json / dev.json / pack.json / ledger.json / ticket.json
```

硬规矩：**报告里的每个数字都能在 `evidence/` 里找到**；闸门是 `block` 时标题强制
`【未通过】`，报告不允许写"全部通过"（`check_report` 会把这个当 `narrative_conflict` 报错）。
同名补丁自动加序号，不递归拷贝目录，不跟随 `..`。

---

## 4. 审计：审的是"开发的和他理解的对不对"

`collect_snapshot()` 真的去读四类东西，再逐项对照：

1. **文档**（`docs/**/*.md`）：标题/小标题 + Markdown 接口表（方法/路径/鉴权/废弃）；
2. **协议**（`proto|api|spec` 下的 JSON）：`endpoints` 列表；
3. **代码**：`@route("POST", "/x")`、`@app.get("/x")`、`register_route(...)`、Express/Go/Java 注解；
4. **迁移与配置**：`migrations/*.sql`、`config/*.json`（道具/掉落/经济）。

规则（`integrations/game_server_audit.py`，全部可复算）：

| 类别 | 规则 code | 级别 |
| --- | --- | --- |
| 协议↔实现 | `endpoint_missing`（声明未实现）/ `endpoint_undeclared` / `endpoint_duplicate` | error / warn / error |
| 鉴权 | `auth_missing`（声明要鉴权、实现没有） | error |
| 版本 | `deprecated_still_served` | warn |
| 迁移 | `migration_gap` / `migration_duplicate` / `migration_destructive` | error |
| | `migration_no_tx`（阈值 `require_migration_tx`）/ `migration_unsafe_alter` | error / warn |
| 配置经济 | `config_item_missing` / `drop_prob_invalid` / `drop_prob_sum` | error |
| | `economy_cap_missing` / `economy_inflation_high` / `economy_missing` | warn |
| 并发 | `tx_missing`（写接口无事务）/ `idempotency_missing` | error / warn |
| 卫生 | `secret_hardcoded`（error）/ `todo_left` / `broad_except` / `busy_wait` / `debug_print` | error / warn |
| 验收 | `acceptance_uncovered`（4 字滑窗在文档里找不到依据） | warn |
| 依据 | `spec_missing` / `docs_missing` / `code_empty` | warn / error |

阈值都在 `GameServerProfile.thresholds`，可用 `params.thresholds` 覆盖（如 `max_todo`、
`require_tx`、`min_acceptance_coverage`、`allow_destructive_migration`）。

---

## 5. 风控闸门：按服务器逻辑"再试、再适配"

| 检查 | 判据 | block 条件 |
| --- | --- | --- |
| `audit` | 审计 verdict | `fail`（`strict=false` 降为 warn） |
| `audit_critical` | 鉴权/事务/迁移/经济/密钥类 error | 命中任一 |
| `capacity_ccu` | 预期 CCU / 容量 | > 90%（> 75% warn） |
| `capacity_tick` | p99 tick / 帧预算 | > 100%（> 80% warn） |
| `replay` | 同输入两遍状态哈希 | 不一致 / 回放报错 |
| `rollback` | 回滚脚本 + 是否演练 | 无脚本（`require_rollback`） |
| `canary` | 灰度比例 | > 20%（<=0 也 warn） |
| `economy` / `inflation` | 每日产出上限 / 日通胀 | 通胀 > 1%/日 |

回放会**执行被审仓库的 `server/sim.py`**：必须显式 `allow_exec=True`，在**子进程 + 超时**
里跑，两次哈希比较；真实环境应交给 `runner`（隔离容器/CI）执行，这里只给同口径的本地复核。

---

## 6. 参数（一次运行）

```json
{
  "ticket": "examples/game_server/fixture/ticket.json",
  "repo_root": "/path/to/ci-checkout",
  "doc_root": "/path/to/ci-checkout/docs",
  "out_dir": "/path/to/release/gs-1042",
  "ticket_id": "gs-1042",
  "audit_level": "rule",
  "max_workstreams": 8,
  "capacity": {"expected_ccu": 5200, "ccu_limit": 5000,
               "tick_budget_ms": 50, "p99_tick_ms": 62},
  "replay": {"engine": "server/sim.py", "scenario": "scenario.json",
             "allow_exec": true, "runs": 2},
  "rollback": {"plan": "回滚镜像并反向执行迁移", "script": "ops/rollback.sh", "tested": false},
  "canary_pct": 5,
  "economy": {"faucet_daily_cap": 10000, "inflation_daily_pct": 0.1},
  "strict": true
}
```

`ticket` 支持 JSON 或 Markdown（`## 验收` / `## 模块` 会被解析）；`workstreams[].edits`
是 `{file, find, replace}`，缺 `find` 命中就记 warning，不假装改过。

---

## 7. 接真实仓库

1. **准备一份可写 checkout**（CI workspace / 分支副本）。`dev/implement` 会把补丁写回
   `repo_root`，不要指向生产目录或示例 fixture。
2. **把真实开发接到 `dev`**：两种方式
   - 保持默认 patch 模式：工单给 `files` + `edits`，框架产出补丁并做语法检查；
   - 换成外部执行：在 `handlers()` 里覆写 `dev.implement`，调 `integrations/runner.py`
     跑编码 Agent / 代码生成脚本，再回传补丁路径与测试结果。
3. **文档与协议要能被解析**：设计文档保留接口表（方法/路径/鉴权/废弃），协议放
   `proto|api|spec` 下的 JSON（`endpoints`）。开发报告（Markdown）里的接口表也会被
   `producer/intake` 解析并作为 `report` 来源参与对照——模板与"接口谁定"见
   [docs/DEV_REPORT.md](DEV_REPORT.md)，填好的例子见
   [`examples/game_server/fixture/ticket_classic.md`](../examples/game_server/fixture/ticket_classic.md)。
   解析不到会明确报 `spec_missing/docs_missing`，不会静默放过。
4. **阈值按团队口径调**：`params.thresholds` 覆盖 `max_todo`、`require_tx`、
   `min_acceptance_coverage` 等；`audit_level=stat` 会额外跑序列统计（需要 numpy）。
5. **闸门证据由 CI 填**：容量/回放/回滚/经济最好来自压测与回放平台，本框架只做统一判定
   与报告；拿不到证据判 `warn` 并写明缺什么。

命令行：

```bash
multi-agent project list                        # 多了 game-server
multi-agent project show game-server            # 角色表 + 9 步 DAG
multi-agent project agents game-server          # 要起几个 Agent（dev 默认 3 实例）
python examples/game_server/run_demo.py         # 内存端到端演示（含故意留的缺陷）
```

---

## 8. 与主干的关系 / 合回 main

分支只新增文件，不改主干行为：

```
multi_agent/integrations/game_server_audit.py     审计 profile + 扫描器
multi_agent/integrations/game_server_risk.py      服务端闸门 + 确定性回放
multi_agent/integrations/game_server_delivery.py  发布包 + 报告器
multi_agent/projects/game_server.py               角色表 + DAG + 业务 handler
multi_agent/projects/__init__.py                  登记 builtin（仅加一行导入）
examples/game_server/                             fixture + run_demo
tests/test_game_server.py                         13 项离线测试
docs/GAME_SERVER.md                               本文件
```

合回 main 的检查：`PYTHONPATH=. python -m pytest tests -q` 全绿、`python examples/game_server/run_demo.py`
退出码 0（演示 fixture 的闸门结论应为 `block`，正是"拦住有缺陷的交付"）。

---

## 9. 边界与已知限制

- **协议解析是启发式**：正则覆盖常见写法；自定义 DSL 需要加一条模式（在 `scan_code` 里）。
- **回放不是压测**：只验确定性，不做真实并发/网络/DB 压测；容量数字要来自压测平台。
- **不做代码语义理解**：`rule` 级不调模型；要"读懂实现意图"用 `audit_level=llm`（显式烧 token）。
- **`dev` 默认只改 `files` 列出的文件**：不做仓库级重构，避免越权改动。
- **路径安全**：`_join_under` 用 `realpath + 前缀` 拦 `..` 与绝对路径逃逸；越界记 `blocked`。
- **示例会被改动**：`run_demo.py` 先复制 fixture 到 `out/work/repo`，示例仓库本身不动。