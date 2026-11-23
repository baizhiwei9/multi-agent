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
| `audit.on_exceed` | `redo` | 超限策略：`redo`（判不合格）/ `warn`（只提示）/ `truncate`（静默截断） | — |
| `debate.max_rounds` | 10 | 采集官 ⇄ 审计官最多来回几轮 | 到轮数即停，`stopped_reason=max_rounds` |
| `debate.max_message_chars` | 5000 | 单条稿件/点评字数上限 | 单条截断到该值 |
| `debate.max_total_chars` | 100000 | 一轮评议的总字数预算 | 撞预算即停，`stopped_reason=char_budget` |
| `news.max_topics` | 5 | 协调官初筛最多带几条待议观点 | 截断到该条数 |
| `roles.default_instances` | 1 | 角色没声明 `instances` 时默认起几个 | 见第 4 节 |
| `roles.overrides` | `{}` | 按 capability 覆盖实例数（`{"B": 3}`） | 覆盖优先于角色表 |
| `roles.max_instances_per_role` | 50 | 单角色实例数上限（防误配打出 500 个进程） | 超过报 `LimitsError` |

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
# futures-balance 共需 13 个 Agent：
#   A          ×1    A-1                      A:build_sheet,fetch_official,inventory
#   B          ×2    B-1, B-2                 B:scan_news,scan_events
#   ...
# 每个实例（按 id 起进程）：
#   multi-agent project agent futures-balance --hub $HUB --id B-1 --roles B
```

`--roles` 只注册该实例负责的 capability（一个进程可以只当 B-1，不必把全部角色塞进一个 Agent）。
`auditor` / `moderator` / `debate` 这类**框架内置角色**不在项目角色表里，按
`multi-agent agent --role ... --handlers multi_agent.integrations.<x>:register` 单独起：

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

---

## 5. 测试

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_limits.py` | 默认值 = 配置文件、环境变量覆盖与缺项回填、非法值/未知策略/预算矛盾报错、按字截断 |
| `tests/test_debate.py` | 初筛（重大事件/无新闻/条数上限/关键词可配）、评议（无新闻短路不派活、逐轮带 critique、轮数上限、单条截断、总预算停、缺 client/target 报错、register 暴露两动作） |
| `tests/test_audit.py` | L3 提示词带限额、引用 > N 报 `too_many_citations` 并 fail、输出超字数报 `audit_too_long` |
| `tests/test_projects.py` | 多轮版流水线形状（双路新闻 → 初筛 → 评议 → 报告/风控 → 审计 → 终审）、`extra_pipelines` 一并安装、简版仍在 |