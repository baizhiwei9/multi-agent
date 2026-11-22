# 项目扩展：一个框架，三个项目，五种扩展点

> 这份文档回答一件事：**别的项目要怎么接进 multi-agent，接的时候改哪里、不改哪里。**

框架本身（Hub / Coordinator / DBManager / 审计引擎 / 传输 / 打洞）不认识任何业务。
业务项目只继承 `multi_agent.projects.base.Project`，声明五样东西：

| 扩展点 | 作用 | 不写会怎样 |
| --- | --- | --- |
| `roles` | capability/action 表 → Agent manifest + 路由依据 | 项目无法注册、无法接活 |
| `pipeline` | DAG（步骤语法同 `pipelines.py`） | 只能手工 submit，不能编排 |
| `audit_project` | 用哪套审计 profile（`balance_sheet` / `stock` / 自定义） | 跳过审计，直接出报告 |
| `news()` | 新闻采集官（`NewsCollector`） | 事件为空，明确 `degraded` |
| `reporter()` | 报告器（`Reporter`） | 只有数据，没有成品报告 |

框架侧只做三件事：`register()` 登记、`manifests()` 生成注册清单、`install_pipelines()`
把项目流水线塞进 `PIPELINES` 交给 Coordinator。

---

## 1. 三个实现（照抄任意一个）

| 项目 | 类 | 角色 | 审计口径 | 新闻 | 报告 |
| --- | --- | --- | --- | --- | --- |
| 期货收支平衡表 | `FuturesBalanceProject` | `A/B/T/F/Q/C/D/E/J` | `balance_sheet` | `DataHubNewsCollector`（DataHub `futures_news`） | `BalanceSheetReporter`（7 段式） |
| 股票 A 股日报 | `StockMattProject` | `M1/M2/M3/M4/M5/news/J` | `stock` | `RuleNewsCollector`（正则，零 token） | `MattReporter`（日报） |
| 最小示例 | `MinimalReportProject` | `collect/render` | 不审计 | 无 | `MinimalReporter` |

三者的差别全在声明与两三个覆写上，框架一行没改。这正是"继承而不是改框架"的意思。

---

## 2. 新增一个项目（最短路径）

```python
from multi_agent.projects import Project, RoleSpec, register
from multi_agent.projects.reporter import MarkdownReporter

class MyReporter(MarkdownReporter):
    name = "my-md"; title = "我的报告"
    def sections(self, ctx):
        return [("结论", [f"- {ctx.get('verdict')}"])]

class MyProject(Project):
    name = "my-project"
    title = "我的项目"
    roles = [RoleSpec("K", ["run"], "采集/计算"), RoleSpec("E", ["report"], "出报告")]
    pipeline = {"name": "my_pipeline", "steps": [
        {"name": "run", "capability": "K", "action": "run",
         "params": {"x": "${params.x}"}},
        {"name": "report", "capability": "E", "action": "report",
         "params": {}, "depends_on": ["run"]},
        {"name": "audit", "capability": "auditor", "action": "report",
         "params": {"project": "stock", "level": "rule"}, "depends_on": ["report"]},
    ]}
    audit_project = "stock"
    def handlers(self):
        return {"K": {"run": lambda p, c: {"ok": True, "x": p.get("x")}},
                "E": {"report": lambda p, c: MyReporter().build({"verdict": "pass"})}}
    def reporter(self): return MyReporter()

register(MyProject())          # 校验角色/流水线一致性后登记
```

装进流水线并起 Agent：

```bash
multi-agent project pipelines                       # 把项目流水线注册进 PIPELINES
multi-agent project show my-project                # 看角色表/步骤/扩展点
multi-agent project agent --hub http://127.0.0.1:8787 --id my-1 --name my-project
```

---

## 3. 真正的实现怎么接（executor）

`FuturesBalanceProject` / `StockMattProject` 默认返回 `degraded=True`，并写明
"需要在有 BalanceAI / stocks_matt_report 的机器上执行 X/Y"。要跑真业务，注入 executor：

```python
# 本机直接调项目函数
proj = FuturesBalanceProject(executor=lambda cap, act, p, ctx: dispatch(cap, act, p))

# 跨机：让装了项目的 Agent 用 runner 跑脚本（白名单 + 超时 + 输出截断）
def remote_executor(cap, act, p, ctx):
    return coordinator.client.submit(
        "runner", "py",
        {"script": f"/srv/BalanceAI/run_role.py", "argv": ["--role", cap]},
        wait=True)["result"]
```

流水线、审计、报告都不变；换的只是"谁来算"。

---

## 4. 新闻采集官接口

```python
class NewsCollector:
    def collect(self, params) -> dict: ...
```

返回形状固定：

```json
{"events": [{"event_id": "...", "title": "...", "published": "...", "impact": null}],
 "scanned": 120, "deduped": 3, "collector": "datahub-futures-news", "degraded": false}
```

规则：

- 取不到就 `degraded: true` + `reason`，**events 留空，不许造**；
- 采集官不判方向（`impact` 恒为 null），方向留给测算与审计；
- 去重按 `event_id`（标题+时间的 sha1 前 16 位），重复只出一次。

现有实现：`DataHubNewsCollector`（期货，注入 `fetch(hours)`）、`RuleNewsCollector`
（股票，正则 `{"hard": [...], "noise": [...]}`）、`StaticNewsCollector`（测试）、
`NullNewsCollector`（明确占位）。

新增新闻源 = 继承 `NewsCollector` 实现 `collect()`，再在项目里 `def news(self)` 返回它。

---

## 5. 报告器接口

```python
class Reporter:
    def build(self, context) -> str: ...
    def save(self, md, path) -> str: ...
```

`MarkdownReporter` 已经处理了标题、时间、数据缺口提示、审计结论段，子类只实现：

```python
def sections(self, context) -> [(标题, [行...]), ...]
```

硬规矩：**不重算、可复算、零 token**——报告里的每个数字都要能在 context 里找到，
缺数据就写"（无数据）"，不许自己补。

---

## 6. 为什么审计口径必须按项目分

同一个审计引擎、不同 profile：

| 项目 | profile | 查什么 |
| --- | --- | --- |
| 期货 | `balance_sheet` | 供需勾稽、单位量级、库存滞后、叙事与数据矛盾 |
| 股票 | `stock` | 三表勾稽、应计质量、毛利率背离、EPS 一致 |
| 债券 | `bond` | 久期凸性、曲线单调、信用利差、票息到期 |

所以 `audit_project` 是项目声明的一部分，**不能全局写死**。新增项目时先想清楚
"我的数应该满足什么恒等式"，再决定复用哪个 profile 还是新写一个
（`audit_profiles.register_profile`）。

---

## 7. 自检清单（新项目接入前）

1. `project show <name>` 里 `roles` 每个 capability 都有 action；
2. 流水线每个步骤的 capability 都在角色表里（`db`/`coordinator`/`auditor`/`runner` 除外）；
3. `audit_project` 指向真实存在的 profile（`python -c "from multi_agent.integrations.audit_profiles import profile_names; print(profile_names())"`）；
4. `news()` 没配也能跑（返回 `degraded` 而不是抛异常）；
5. `reporter().build({})` 不炸、不出现 TODO/占位符；
6. `tests/test_projects.py` 里补一条你项目的最小用例。