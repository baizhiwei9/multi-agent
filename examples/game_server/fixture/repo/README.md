# game-server-fixture（示例服务端仓库）

这是 `examples/game_server` 用来跑通"开发 → 审计 → 风控 → 发布包"的最小仓库，
包含：

- `server/`：路由注册、玩家/匹配/商城接口、确定性回放引擎（`sim.py`）
- `docs/design.md`：设计文档（含接口表、鉴权/事务/经济口径）——审计官"取文档"的对象
- `migrations/`：SQL 迁移（故意留了版本缺口和破坏性变更）
- `config/`：道具表、掉落表、经济配置（故意留了未知道具和概率合计 > 1）
- `ops/rollback.sh`：回滚脚本

**不要直接在这个目录上跑开发流程**——`dev/implement` 会把补丁写回 `repo_root`。
`examples/game_server/run_demo.py` 会先把它复制到 `out/work/repo` 再跑。