# examples/game_server：开发报告 → 发布包（可跑示例）

```
examples/game_server/
├── README.md           本文件
├── run_demo.py         端到端跑一遍（Hub + DB + Worker + 协调官全在本地内存）
├── out/                运行产物（已 gitignore）：work/ 工作副本、release/ 发布包
└── fixture/
    ├── interface_spec.json  接口规范文件示例（8 个接口 + conventions，可复制）
    ├── ticket.json          开发报告/需求单（8 个接口 + 3 个分片）
    ├── ticket_classic.md    合格的 Markdown 开发报告示例
    ├── ticket_bad_spec.md   接口规范不合格的报告（用来看拦截）
    ├── scenario.json        确定性回放的命令序列
    └── repo/                最小服务端仓库（故意留了缺陷）
```

单独校验规范文件：

```bash
python -m multi_agent.integrations.game_server_spec \
  examples/game_server/fixture/interface_spec.json
```

## 跑起来

```bash
python examples/game_server/run_demo.py
python -m pytest tests/test_game_server.py -q
```

跑完看：

```bash
examples/game_server/out/release/gs-1042/release_report.md
examples/game_server/out/release/gs-1042/manifest.json
cd examples/game_server/out/release/gs-1042 && sha256sum -c checksums.sha256
```

## 演示里发生了什么

1. **后台管理者**解析 `ticket.json`：8 个接口 + 3 个分片 + 4 条验收标准；
2. **接口规范门** `spec_check`：纯程序校验（方法/路径/鉴权/幂等/错误码/重复），不合格直接停；
3. **Coordinator** 用 `foreach` 把 3 个分片并行派给 `dev`；
4. **dev** 在 `out/work/repo` 上应用补丁（封禁接口补鉴权、新增登出、匹配/商城加幂等键），
   产出 `.patch` 并做语法检查；
5. **测试准备** `test_plan` 按接口生成 47 条用例（正向/鉴权/越权/幂等/并发/错误码），
   默认模板；`llm_test_report=true` 且配了模型地址时才让大模型写报告；
6. **经理** `pack` 成送审件；
7. **审计官**读 `docs/design.md`（接口表）+ `proto/spec.json` + 代码 + 迁移 + 配置：
   - 开发修掉的：`auth_missing`、`endpoint_missing`；
   - 仍然存在、必须拦的：迁移缺口 `003`、破坏性 `DROP COLUMN`、掉落概率合计 1.7、
     经济产出上限为 0、硬编码密钥、`mail/claim` 无事务、`debug/state` 未登记、遗留接口未下线；
8. **风控官**再叠加服务器证据：5200 CCU / 5000 容量（104%）、p99 62ms / 50ms 帧预算、
   每日通胀 1.2%、灰度 5%、回滚脚本存在但未演练；
9. **Reporter** 出 `【未通过】` 报告 + `manifest.json` + `checksums.sha256` + `evidence/`
   + `artifacts/test_plan.md`。

闸门结论是 **block**，这是正确行为：演示数据就是为了证明"审计和风控真的会拦"。
`run_demo.py` 的退出码因此定义为"流水线跑通且闸门 block"才算 0。

**规范不合格的演示**（不拆包、不下发开发）：

```bash
python examples/game_server/run_demo.py \
  --ticket examples/game_server/fixture/ticket_bad_spec.md --ticket-id gs-1199
# spec_gate.ok=false；plan/dev/pack/audit/gate/ledger/test_plan 全 skipped；
# report 标题【接口规范不合格】，写明未拆包、未下发开发
```

## 换成你自己的仓库

只改 `run_demo.py` 里的 `params_for()`（或命令行传参）：

| 参数 | 说明 |
| --- | --- |
| `ticket` | 你的开发报告（JSON/Markdown） |
| `repo_root` | **可写**的 checkout（dev 会改这里；别指生产目录） |
| `doc_root` | 设计文档目录（默认 `repo_root/docs`） |
| `out_dir` | 发布包落点 |
| `capacity` / `replay` / `rollback` / `canary_pct` / `economy` | 服务器侧证据 |
| `audit_level` | `rule`（默认）/ `stat` / `llm` |
| `llm_test_report` | 是否让大模型写测试报告（默认 false；需配 `GAME_TEST_LLM_URL`） |

规则、阈值、边界见 [docs/GAME_SERVER.md](../../docs/GAME_SERVER.md)。