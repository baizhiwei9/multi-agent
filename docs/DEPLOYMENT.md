# 部署边界：每台机器一个进程，DB 落本地（不是云盘）

> 初稿，待默尔根拍板。
> 写这份文档的原因：pr6 三方协作时，对「项目文件 / 数据放哪、怎么跨机」理解不一致——
> 有人（hermer2 自检回报）把 `/Coze/Drive/...` 当成三方共享的云盘，实际那是**某台机器上的本地路径**。
> 代码本身没问题，是「部署形态」没有一份基准说明，导致各自按自己的机器环境去理解。

---

## 1. 一句话结论

- 每台机器本地起进程、执行自己绑定的角色；
- **DBManager 的数据落在跑 DBManager 的那台机器本地**（SQLite 文件），不落云盘、不落共享盘；
- 机器之间**不共享文件系统**，协作只走 Hub 的 HTTP 出站连接（或 BOOTSTRAP 打洞 / 中继）。

## 2. 拓扑

```
          ┌──────────────────── Hub（一台可达的机器，0.0.0.0:8787）────────────────────┐
          │   hub.db（任务库）落在这台机器本地                                          │
          └───────────────┬──────────────────────────┬───────────────────────────────┘
                          │ 出站 register + 长轮询     │ 出站 register + 长轮询
          ┌───────────────▼────────────┐   ┌──────────▼───────────────┐
          │ Coordinator（编排 DAG）     │   │ DBManager（唯一 DB 入口）  │
          │ 某台机器，本地进程          │   │ 某台机器，本地进程         │
          │                           │   │ data.db + -wal/-shm/.lock │
          └───────────────────────────┘   └──────────────────────────┘
                          │
          ┌───────────────▼──────────────────────────────────────────┐
          │ Worker（A/B/T/F/Q/E/J …），各跑在各自机器本地              │
          │ 业务数据（平衡表/新闻/报告）落在跑 Worker 的那台机器本地    │
          └──────────────────────────────────────────────────────────┘
```

## 3. 路径澄清（这次误解的源头）

`/Coze/Drive/...`、`/root/projects/...`、`/root/cs_repo/...` 都是**各自机器上的本地路径**，不是三方共用的盘：

- 每个 Coze agent 的工作目录是**隔离的沙箱**，没有三方共用的 `/Coze/Drive` 挂载；
- 蓁宝任务书里的 `/Coze/Drive/大宗的收支平衡表_/pr6_review/...` 是他那台机器上已 clone 好的仓库路径，**别的机器读不到**；
- 想跨机传文件（协作交付物）用 `coze agent file list/read --project-id <group_id>`，那是**协作传文件**通道，不是运行时数据通道；
- 运行时的数据读写永远走本机文件或 Hub 的 `db/*` 动作，不走云盘。

## 4. 数据落哪（都本地）

| 数据 | 文件 | 落在哪台机器 |
| --- | --- | --- |
| Hub 任务库 | `hub.db` | 跑 `multi-agent serve` 的机器 |
| DBManager | `data.db` + `-wal`/`-shm`/`.lock` | 跑 `multi-agent db` 的机器 |
| 业务平衡表/新闻/报告 | BalanceAI `data/`、`reports/` | 跑对应 Worker 角色的机器 |

SQLite 文件不能跨机器直接挂载共享——多台机器各自起 DBManager 写同一个文件会依赖
`fcntl.flock` 文件锁，但**前提是同一份文件系统**；跨机器要统一入口，只有一个
DBManager，其余机器都通过 Hub 的 `db/*` 动作访问。

## 5. 跨机怎么连

- Hub 监听 `0.0.0.0:8787`（设 token）；Worker / DBManager / Coordinator 都**只出站**连 Hub
  （register + 长轮询 + 心跳），不需要被反向访问；
- 内网 / 私有网络且够得着 Hub 时，直接出站连即可（出站连接能过 NAT）；
- 打洞 / 中继的取舍见 `BOOTSTRAP.md`（先直连省 token，失败再中继）；
- token 统一由 Hub 那台机器分发，不要写进仓库。

## 6. 本项目 pr6 实际分工（示例，待最终确认）

| 进程 | capability | 跑在哪台机器 | 数据 |
| --- | --- | --- | --- |
| Hub | —（Server） | DH4300PLUS | `/root/data_multiagent/hub.db` |
| Coordinator | `coordinator` | 待定 | 运行状态可落 DBManager |
| DBManager | `db` | 待定（分工给 hermer2） | 本地 `data.db` |
| Worker A/B/T/F/Q/E/J | 业务角色 | 各 agent 机器 | BalanceAI 本地 `data/` |

> 注意：苓宝2号的部署自检报告里 Hub + DBManager + Coordinator + 2 Worker 是**同机**（DH4300PLUS）
> 拉起的，那是「先跑通」的形态；按分工，DBManager / Worker 最终应落到各自负责的机器上，再由出站连接归口到 Hub。

---

## 7. 私有部署：默认不连任何外部服务

| 项 | 默认 | 说明 / 怎么关 |
| --- | --- | --- |
| 框架运行时依赖 | 无 | `pyproject.toml` 里 `dependencies = []`，纯 stdlib |
| DataHub 新闻池 | **关** | 期货项目 `news()` 默认返回 `NullNewsCollector`（`degraded=True`，`events=[]`，不造事件）；要用再 `FuturesBalanceProject(enable_datahub=True)` 或 `MULTI_AGENT_DATAHUB=1` |
| GitHub | 不访问 | 代码里没有 GitHub API 调用；安装用本地 wheel / 内网源即可（`pip install -e .` 或 `pip install dist/*.whl`） |
| 外网 | 不访问 | Agent 只**出站**连自己的 Hub；Hub / DBManager / Worker 都在内网 |
| 统计库 numpy/pandas/scipy/statsmodels | 可选 | 缺了自动降级到规则层，不报错 |
| LLM | 不调用 | `audit_level` 默认 `rule`；只有显式传 `llm` 才联网 |

私有环境自检：

```bash
python -m pytest tests -q                                   # 410 passed（离线，纯 stdlib）
python -m multi_agent.cli diagnose --hub http://127.0.0.1:8787
```

> 唯一需要外部数据的是「新闻采集」这一路；关掉后流水线仍能跑完，
> 报告会明确写"无新闻源 / 数据缺口"，而不是编造事件。
