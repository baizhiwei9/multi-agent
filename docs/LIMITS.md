# 限额与多轮评议（`config/limits.json`）

> 这份文档回答两个问题：**限额写在哪、怎么改**；**采集官和审计官怎么来回评议、轮数怎么卡住**。

实现：`multi_agent/limits.py`（配置加载 + 校验）、`multi_agent/integrations/debate.py`
（协调官初筛 `moderator/screen` + 多轮评议 `debate/run`）、审计侧在
`multi_agent/integrations/audit.py`（L3 提示词与回执校验）。

---

## 1. 配置文件

```json
{
  "audit":  {"max_citations": 5, "max_input_chars": 5000, "max_output_chars": 5000,
             "max_rounds": 10, "max_total_chars": 100000, "on_exceed": "redo"},
  "debate": {"max_rounds": 10, "max_message_chars": 5000, "max_total_chars": 100000},
  "news":   {"max_items_per_collector": 50, "max_topics": 5},
  "roles":  {"default_instances": 1, "max_instances_per_role": 50, "overrides": {}}
}
```

角色**要几个 Agent** 主要写在项目角色表（`RoleSpec(..., instances=N)`），这里的
`roles` 段只负责兜底与全局覆盖，见第 4 节。

| 字段 | 默认 | 作用 | 超了会怎样 |
| --- | --- | --- | --- |
| `audit.max_citations` | 5 | 审计官最多引用几处错误观点 | 超出 → 回执加 `too_many_citations`（error，fail），只保留最严重的 N 条并要求重做 |
| `audit.max_input_chars` | 5000 | 给大模型的输入预算（平衡表 + 报告） | 按 1:2 分配预算后截断，提示词里写明限制 |
| `audit.max_output_chars` | 5000 | 审计回执字数上限 | 超出 → `audit_too_long`（error，fail），要求压缩重做 |
| `audit.max_rounds` | 10 | 审计链允许的最大评议轮数（与 debate 对齐） | 由 `debate.max_rounds` 实际执行 |
| `audit.max_total_chars` | 100000 | 全流程字数预算（10 轮 × 约 1 万） | 由 `debate.max_total_chars` 实际执行 |
| `audit.on_exceed` | `redo` | 超限策略：`redo`（判不合格）/ `warn`（只披露、不计判定）/ `truncate`（不截断 JSON 原文，只记录超限字数并截断引用条数） | — |
| `debate.max_rounds` | 10 | 评议最多几轮（审计：一来一回算一轮；多空：双方各说一次算一轮） | 到轮数即停，`stopped_reason=max_rounds` |
| `debate.max_message_chars` | 5000 | 单条稿件/点评字数上限 | 单条截断到该值 |
| `debate.max_total_chars` | 100000 | 一轮评议的总字数预算 | 撞预算即停，`stopped_reason=char_budget` |
| `debate.max_tokens` | 40000 | 整场辩论的 token 估算上限（收发字符 × `token_per_char`，非计费口径） | 撞预算即停，`stopped_reason=token_budget` |
| `debate.token_per_char` | 0.75 | 字符→token 估算系数（中文约 0.6~0.8，按自己模型校准） | — |
| `debate.judge_reserve_ratio` | 0.25 | 总预算里给裁决官预留的比例（只有多空辩论生效） | 预留算进每轮闸门，提前收口 |
| `debate.judge_round_chars` | 600 | 裁决官压缩稿里每轮每方保留的字数 | 超出截断并标「已截断」 |
| `news.max_topics` | 5 | 协调官初筛最多带几条待议观点 | 截断到该条数 |
| `roles.default_instances` | 1 | 角色没声明 `instances` 时默认起几个 | 见第 4 节 |
| `roles.overrides` | `{}` | 按 capability 覆盖实例数（`{"B": 3}`） | 覆盖优先于角色表 |
| `roles.max_instances_per_role` | 50 | 单角色实例数上限（防误配打出 500 个进程） | 超过报 `LimitsError` |
| `roles.co_locate` | `[]` | 可合在一个 Agent 上的角色组（`[["B","supervisor"]]`） | 组内实例数不一致时报错 |

查找顺序：`MULTI_AGENT_LIMITS` 环境变量 → 当前目录 `config/limits.json` → 仓库根
`config/limits.json` → 内置默认值（装成 wheel 时的兜底）。**写错就报错**：
0、负数、字符串、轮数 > 50、单条字数大于总预算，都会抛 `LimitsError`，不会静默放行。

```bash
MULTI_AGENT_LIMITS=/etc/multi-agent/limits.json multi-agent agent ...
```

---

## 2. 多轮评议流程（`futures_balance_debate`）

```
news_balance（B/scan_news，平衡表口径）
news_event  （B/scan_events，矿难/罢工/禁令…）
        └── moderator_screen（协调官初筛，零 token）
                 │  no_news=true → 评议 SKIPPED，直接出"今日无重大新闻"
                 ▼
        debate/run（采集官 ⇄ 审计官，≤ max_rounds 轮，每轮 ≤ max_message_chars 字）
                 ▼
        report（E）      risk（风控官，只依赖 build，不受新闻链失败牵连）
                 └── risk_review（审计复核 RISK_BRIEF）
                 ▼
        audit（审计官审报告，带 report/balance） → final（J 终审）
```

为什么这是**两条链**而不是一条：风控官出 `RISK_BRIEF` 只需要建表结果，不能因为
"当日没有新闻、报告链故障"就沉默；报告链则必须先走完评议才能定稿。

评议器每次派活用 `collector`/`auditor` 两个目标（capability + action），
第二轮及以后会把上一轮审计意见放在 `critique` 里回传，采集官据此修改。
审计官结论识别顺序：显式 `decision: APPROVE|MODIFY|REJECT` → `verdict: pass/fail` →
关键字（通过/驳回/重做）；识别不到按 `MODIFY` 继续。

### 2.1 一个执行器，两种形态

`DebateRunner` 是同一个引擎，靠 `mode` 切换，不维护两套轮数逻辑：

| | `mode="audit"`（默认） | `mode="adversarial"` |
| --- | --- | --- |
| 参与方 | `collector` 出稿 ⇄ `auditor` 审 | `bull` ⇄ `bear`，各说一次算一轮 |
| 终止判据 | 审计 `APPROVE` 早停；`char_budget`；`max_rounds`；可选 `converged` | `char_budget`；`max_rounds`；可选 `converged`（无"通过"概念） |
| 裁决 | 审计官最后一轮回执即结论 | 跑满后交 `judge` 一次定调，输出 `BULL`/`BEAR`/`NEUTRAL` |
| 返回 | `decision=APPROVE|MODIFY|REJECT`，`rounds`=来回次数 | `decision=BULL|BEAR|NEUTRAL`，`rounds`=完整轮数，`turns`=发言次数（=2×轮数） |

共同硬上限：`debate.max_rounds` / `max_message_chars` / `max_total_chars`；
`max_rounds` 由调用方只能下调不能超过配置。

收敛（`converge=true`，默认关）：从第 2 轮起比对"本方本轮 vs 本方上一轮"的字二元组
新内容占比，两边都低于 `converge_novelty`（默认 0.15）即判在原地复读，早停并记
`stopped_reason=converged`。只看自己跟自己比，不比较双方观点是否接近——多空本来就
不该趋同，该停的是"没新话"。

多空辩论必须给 `judge`：不给直接返回 `ok=false`，不产出"没有裁决的辩论"。

### 2.2 Token 预算（多空辩论的成本闸门）

字数预算（`max_total_chars`）只管"存了多少字"，管不住"重复喂了多少遍"。多空辩论每轮把
上下文重发一次，真正烧的是调用量，所以另记一层 token 估算：

- **口径**：`tokens ≈ 字符数 × token_per_char`，字符按"每次调用实际发出去的参数文本 +
  收回来的文本"累加（同一段论证第二轮再发一次就再计一次，这才是真实成本）。
  没有接 tokenizer，所以返回里叫 `tokens_used` 而不是 `billed_tokens`；
- **开轮前闸门**：按最坏情况估一轮（上下文 + 双方各写满 `max_message_chars`），
  若 `已用 + 本轮预估 + 裁决预留 > max_tokens` 就不开这一轮，记 `token_budget`；
- **保底轮数**：`min_rounds`（默认 1）内的轮次不受预算闸门限制，避免"预算一紧就一句话
  都不说"；超出后的第一轮照常收尾；
- **裁决预留**：多空模式先扣 `token_budget × judge_reserve_ratio` 给裁决官，否则最后一步
  最容易撞预算、拿到"辩完了但没结论"；
- **裁决看压缩稿**：裁决官拿到的是每轮"正反双方首段"（`judge_round_chars`），不是全量
  transcript——全量输入随轮数平方增长，10 轮就能吃掉整场预算。

三个闸门（字数 / token / 轮数）先到先停，`stopped_reason` 会写清楚是哪一关拦下的。

---

## 3. 无新闻路径（不是异常）

协调官初筛合并两路新闻，判据：
- `impact` 为 `high`/`major`，或
- 标题/摘要命中 `major_keywords`（默认：矿难、罢工、制裁、禁运、地震、洪水、战争、
  停产、爆炸、出口禁令、关税、收储、抛储、减产协议、限产；可由 step 参数覆盖）。

两路都没有新闻 → `no_news=true`：
- `debate/run` 直接返回 `rounds=0, decision=SKIPPED, stopped_reason=no_major_news`，
  **不会给审计官派空点评**；
- 协调官 `verdict.summary` 写"今日无重大新闻，不输出观点"，报告官据此降级出稿。

---

## 4. 起几个 Agent：数量写在角色表里

**一个角色要几个 Agent，是角色自己的属性**，写在项目角色表里：

```python
class FuturesBalanceProject(Project):
    roles = [
        RoleSpec("B", ["scan_news", "scan_events"],
                 "新闻采集：平衡表口径 / 突发事件口径", instances=2),   # ← 两个采集官
        RoleSpec("E", ["report"], "报告：7 段式 Markdown"),             # 默认 1 个
        ...
    ]
```

`RoleSpec(..., instances=N)` 就是这个角色**需要几个实例**；不写按
`config/limits.json` 的 `roles.default_instances`（默认 1）。

需要**不改代码调规模**（比如临时加一个采集官）时，在 `config/limits.json` 里按
capability 覆盖，覆盖优先于角色表：

```json
"roles": {
  "default_instances": 1,
  "max_instances_per_role": 50,
  "overrides": {"B": 3, "auditor": 2}
}
```

优先级：`roles.overrides[capability]` → `RoleSpec.instances` → `roles.default_instances`。

### 看清单、起进程

```bash
# 这个项目要起几个 Agent、每个 id 叫什么、注册哪些角色
multi-agent project agents futures-balance
# futures-balance 共需 14 个 Agent：
#   A          ×1    A-1                      A:build_sheet,fetch_official,inventory
#   B          ×2    B-1, B-2                 B:scan_news,scan_events
#   ...
# 每个实例（按 id 起进程）：
#   multi-agent project agent futures-balance --hub $HUB --id B-1 --roles B
```

`--roles` 只注册该实例负责的 capability（一个进程可以只当 B-1，不必把全部角色塞进一个 Agent）。

`moderator` / `debate` / `supervisor` 已写进期货项目角色表，由
`FuturesBalanceProject.register()` 调用 `integrations.debate` / `integrations.supervisor`
挂上真实实现（不再落到 BalanceAI executor 桩上）；`auditor` 仍按
`multi-agent agent --role ... --handlers multi_agent.integrations.audit:register` 单独起：

```bash
# 协调官 + 评议器（限额从这里读；评议器用自己的 client 给采集/审计派活）
multi-agent agent --hub $HUB --id debate-1 --role "moderator:screen,debate:run" \
  --handlers multi_agent.integrations.debate:register

# 审计官（程序化 / 大模型都行；L3 才用 llm 回调，输入输出受限额约束）
multi-agent agent --hub $HUB --id audit-1 --role "auditor:report,review_brief" \
  --handlers multi_agent.integrations.audit:register
```

多个同 capability 的实例在 Hub 里是**等价 worker**：任务按注册顺序轮询派发，
实例挂了由 TTL 与超时重试接管，不需要额外配置。

### 合并部署：Worker 少时一个 Agent 兼两个角色

`roles.co_locate` 声明"可以合在一个 Agent 上"的角色组，例如让采集官兼管理员：

```json
"roles": {
  "default_instances": 1,
  "max_instances_per_role": 50,
  "overrides": {"B": 1},
  "co_locate": [["B", "supervisor"]]
}
```

```bash
multi-agent project agents futures-balance
#   B+supervisor-1   ...   B:scan_news,scan_events,supervisor:pack,handoff,ledger
multi-agent project agent futures-balance --hub $HUB --id B+supervisor-1 --roles B,supervisor
```

- 组内**实例数必须一致**（都是 1 才能合成一个进程）；不一致直接报错，不猜；
- 合并只影响"进程怎么起"，Hub 里仍然是两个 capability，路由、审计口径不变；
- 不合就用默认 `co_locate: []`，角色各自独立起。

## 4.1 管理员 / 监理（`supervisor`）

`supervisor` 是框架内置角色，负责"把东西递给审计官"这一环——采集官只管采、报告官只管写、
审计官只管审，谁送、送的哪一版、多长、截没截、回执是什么，由它记账：

| 动作 | 作用 | 是否要 token |
| --- | --- | --- |
| `supervisor/pack` | 按 `audit.max_output_chars` 截断、按 `audit.max_citations` 数引用并指出多出的条数，产出送审件 | 零 |
| `supervisor/handoff` | 把送审件交给审计官（默认 `auditor/report`），带回 `task_id` / `state` / `verdict`；没有 Hub 连接时明确 `degraded`，不假装送到 | 取决于审计官 |
| `supervisor/ledger` | 把送审件与各轮回执汇成台账（`entries/count/delivered/blocked/chars_total`） | 零 |

流水线 `futures_balance_debate` 已经接上：
`moderator_screen → supervisor_pack → debate → supervisor_ledger → report → risk → audit → final`
（`report` 改成依赖 `supervisor_ledger`，报告官能看到"送了几轮、送到没有"）。

启动：

```bash
# 单独起
multi-agent agent --hub $HUB --id supervisor-1 \
  --role "supervisor:pack,handoff,ledger" \
  --handlers multi_agent.integrations.supervisor:register

# 或和采集官合并（见上面的 co_locate）
multi-agent project agent futures-balance --hub $HUB --id B+supervisor-1 --roles B,supervisor
```

---

## 5. 测试

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_limits.py` | 默认值 = 配置文件、环境变量覆盖与缺项回填、非法值/未知策略/预算矛盾报错、按字截断 |
| `tests/test_debate.py` | 初筛（重大事件/无新闻/条数上限/关键词可配）、评议（无新闻短路不派活、逐轮带 critique、轮数上限、单条截断、总预算停、缺 client/target 报错、register 暴露两动作） |
| `tests/test_audit.py` | L3 提示词带限额、引用 > N 报 `too_many_citations` 并 fail、输出超字数报 `audit_too_long` |
| `tests/test_projects.py` | 多轮版流水线形状（双路新闻 → 初筛 → 送审 → 评议 → 台账 → 报告/风控 → 审计 → 终审）、`extra_pipelines` 一并安装、简版仍在、`co_locate` 合包与实例数不一致报错 |
| `tests/test_supervisor.py` | 送审件截断/引用超限点名、嵌套产物摊平、无 client 时 `degraded`、handoff 派给审计官并带回 verdict、台账汇总、register 暴露三动作 |
| `tests/test_store.py` | 事件写入与主写同锁：多线程 create/update/add_event 混跑不触发 sqlite "cannot commit - no transaction is active"，事件顺序 created 首、succeeded 尾 |