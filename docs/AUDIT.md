# 审计官（Auditor）—— 三级审计，默认不联网不烧 token

> 一个角色解决一件事：**判定"这份产出能不能信"**。
> 不改数、不估算、不重算业务；只查矛盾、查结构性缺陷、查假设是否被数据推翻。

---

## 1. 为什么需要它

BalanceAI 链路里已经有三个"把关"的角色，但它们各管一段：

| 角色 | 管什么 | 不管什么 |
|---|---|---|
| **Q** | 单品种平衡表能不能进测算（accept/correct/question/reject） | 报告内容对不对 |
| **D** | 单个测算的口径与量级 | 跨角色的整体一致性 |
| **J** | 这一轮跑没跑通（ok/degraded/blocked） | 跑通了但数字错了 |

**留下的缺口**正是生产里最常出事的：

- 报告里的数字和平衡表对不上（口径漂移、手抄、单位混用）
- 平衡表整列是常数（抓取失败被填了默认值，看着有数其实是死的）
- 时间序列有单位根 → 做水平值回归就是伪回归
- 残差还有自相关 → 模型没吃干净信息，参数不可信
- 有明显的结构断点 → 用全样本拟合的系数在两段上是两个东西
- 样本内 R² 0.99、样本外 R² −0.8 → 教科书级过拟合
- 报告写"去库"，平衡表却是供给过剩

这些都不该让大模型"读一遍看看有没有问题"——那是又慢又贵又不确定。
**能写成规则的写成规则，能写成统计检验的写成检验，剩下的才交给语义审计。**

---

## 2. 三级执行（核心：默认最省）

| 级别 | 名字 | 走网络 | 烧 token | 依赖 | 能查什么 |
|---|---|---|---|---|---|
| **L1** | `rule` 规则审计 | ❌ | ❌ | 纯 stdlib | 字段完整性、单位、量级、内部一致性、符号冲突、常量列、时间新鲜度、库存滞后、报告占位符、报告数字能否在平衡表找到、叙事与数据方向矛盾 |
| **L2** | `stat` 统计审计 | ❌ | ❌ | 纯 stdlib（有 numpy 更好） | ADF 单位根、KPSS 平稳性、Chow 断点、Ljung-Box 残差自相关、Jarque-Bera 正态、AR(1) 样本内外 R² 落差（过拟合） |
| **L3** | `llm` 语义审计 | ✅ | ✅ | **调用方传回调** | 措辞是否误导、结论是否有证据支撑、报告与数据的语义矛盾 |

### 级别怎么定（关键设计）

**不是 Auditor 自己决定，是协调者决定。**

```python
coord.start("balance_sheet", {"commodity": "copper", "audit_level": "rule"})  # 默认
coord.start("balance_sheet", {"commodity": "copper", "audit_level": "stat"})
coord.start("balance_sheet", {"commodity": "copper", "audit_level": "llm"})
```

优先级：**运行参数 `audit_level` > 环境变量 `AUDIT_LEVEL` > `rule`**。
也就是说 **不传就永远不联网**，这个默认值写死在 `coordinator.DEFAULT_AUDIT_LEVEL`。

### auto 模式的省钱逻辑

`level="auto"`（函数直接调用时的默认）会这样短路：

```
L1 有 error  → 直接返回 fail，跳过 L2 和 L3（问题已经很明确了，没必要再花钱）
L1 通过      → 跑 L2
L2 有 warn 且调用方给了 llm 回调 → 才进 L3
```

`rule` / `stat` / `llm` 是**硬档位**：说 rule 就只跑 L1，说 stat 一定跑 L2，
说 llm 但没有回调 → 显式标记 `llm_unavailable` 降级，**绝不假装审计过了**。

---

## 3. 0 token 是硬保证，不是口号

`multi_agent/integrations/audit.py` 里有两道锁：

1. **运行时自检** `assert_no_llm_import()` —— 检查 `sys.modules` 里有没有
   openai / anthropic / dashscope / transformers 等，结果写进报告的 `layers.L1_rule`。
2. **静态检查测试** `test_audit_module_does_not_import_llm()` —— 扫源码，出现
   `import openai` / `from anthropic` 直接 fail。

L3 的 llm 是**调用方注入的回调**：

```python
Auditor(level="llm", llm=lambda prompt: my_model(prompt))
```

本模块不 import SDK、不持 key、不发请求。想接本地/内网端点，用现成的工厂：

```bash
export AUDIT_LLM_URL=http://127.0.0.1:18799/v1/chat/completions   # 任何 OpenAI 兼容端点
export AUDIT_LLM_MODEL=openclaw/yuanbao-agent
export AUDIT_LLM_TOKEN=***
```

```python
from multi_agent.integrations.audit import llm_from_env
llm = llm_from_env()          # 没配就返回 None，Auditor 自动跳过 L3
```

---

## 4. 用法

### 4.1 直接当函数用（不经过 Hub，最省事）

```python
from multi_agent.integrations.audit import Auditor

aud = Auditor()                                  # mode=auto：先 L1，不够再 L2
rep = aud.audit_balance(balance_dict)            # 审平衡表
rep = aud.audit_report(report_md, balance_dict)  # 审报告 vs 平衡表
rep = aud.audit_series(values, name="stock")     # 审时间序列
rep = aud.audit_run(run_snapshot)                # 审一轮流水线
rep = aud.audit_full(balance=..., report_md=..., run=...)   # 一次全审

print(rep.verdict)          # pass / suspect / fail
print(rep.to_markdown())    # 可直接贴进日报的审计段
print(rep.as_dict())        # 走 Hub / 落库用
```

### 4.2 挂到 Hub 上当角色

```python
from multi_agent.client import AgentRuntime
from multi_agent.integrations import audit

rt = AgentRuntime(hub, "auditor-1",
                  [{"name": "auditor", "actions": audit._ROLE_ACTIONS["auditor"]}],
                  token=TOKEN)
audit.register(rt)
rt.start()
```

动作表：

| action | params | 说明 |
|---|---|---|
| `balance` | `balance` / `target`, `level?`, `thresholds?` | 审平衡表 |
| `report` | `report` / `target`, `balance?`, `level?` | 审报告（报告可以是 str、dict{markdown/text/path}） |
| `series` | `series`, `name?`, `level?` | 审时间序列 |
| `run` | `run` / `run_id`, `level?` | 审流水线快照 |
| `full` | `balance?`, `report?`, `run?`, `level?` | 一次全审 |

### 4.3 流水线里已经接好了

`pipelines.BALANCE_SHEET` 现在的形状：

```
A 建表 → 入库 → B 新闻 → 入库 → Q 判定(有事件时) → 入库
                                    → E 报告 → ★audit→ J 终审
```

审计步骤定义：

```python
{"name": "audit", "capability": "auditor", "action": "full",
 "params": {"balance": "${steps.build.result}",
            "report":  "${steps.report.result}",
            "level":   "${params.audit_level}"},
 "depends_on": ["report"]}
```

### 4.4 终态多了一个 `degraded`

| 终态 | 含义 |
|---|---|
| `succeeded` | 跑通 且 审计 pass |
| **`degraded`** | **跑通了，但审计判 fail/suspect —— 结果可看不可直接用** |
| `failed` | 有步骤失败 |
| `canceled` | 被取消 |

**这个区分是审计官存在的意义**：以前"跑完就是 succeeded"，
现在"跑完但数字对不上"会被明确标成 `degraded`，报告尾部能看到具体哪条不过。

---

## 5. 审计发现一览（code 清单）

### L1 规则（`balance`）
| code | 级别 | 触发条件 |
|---|---|---|
| `balance_empty` | error | 平衡表为空 |
| `missing_field` | error | 缺 commodity/period/unit/supply/demand/balance |
| `unit_unknown` | warn | 单位不在已知列表（吨/千吨/万吨/百万吨） |
| `value_not_numeric` | warn | 字段不是数字（**不填 0，标出来**） |
| `magnitude_suspect` | error | 换算成吨后超出该字段合理量级（单位写错的典型） |
| `internal_mismatch` | error | 精炼总 ≠ 原生 + 再生（容差 2%） |
| `balance_inconsistent` | error | 供需差 ≠ 精炼总 − 用量（容差 2%） |
| `sign_conflict` | warn | 供需差与库存变动符号相反 |
| `constant_series` | warn | 长度 ≥4 且全相同的列（抓取失败/占位） |
| `data_stale` / `data_aging` | error / warn | 数据日期距今 > 120 / > 45 天 |
| `inventory_stale` | warn | 库存子项滞后 > 120 天 |

### L1 规则（`report`）
| code | 级别 | 触发条件 |
|---|---|---|
| `report_empty` | error | 报告为空 |
| `report_no_title` / `report_few_sections` | warn | 缺标题 / 小标题 < 2 |
| `placeholder_in_report` | warn | 出现 None/nan/TODO/TBD/`{{` 等占位符 |
| `report_numbers_unmatched` | warn | ≥2 个数字在平衡表里找不到（容差 2%） |
| `narrative_conflict` | **error** | 报告写"去库"但平衡表是过剩（或反之） |
| `unit_mismatch` | warn | 报告单位与平衡表单位不同（长优先匹配，不会把"万吨"误判成"吨"） |
| `llm_unavailable` / `llm_unparsable` / `llm_error` | warn | L3 请求了但没回调 / 回调输出不可解析 / 回调报错 |

### L2 统计（`series` / `balance.history`）
| code | 级别 | 触发条件 |
|---|---|---|
| `series_too_short` | warn | 有效点 < 12 |
| `series_constant` | error | 全常量，无法审计 |
| `unit_root` | warn | ADF 未拒绝单位根（t > −2.86） |
| `kpss_nonstationary` | warn | KPSS stat > 0.463 |
| `structural_break` | warn | Chow F > 临界值（默认 3.0） |
| `residual_autocorr` | warn | Ljung-Box Q > χ²(0.95, lags) |
| `overfit` / `overfit_suspect` | **error** / warn | 样本内 R²>0.5 且样本外 R²<0 且落差>0.5 / 落差>0.4 |
| `non_normal` | info | Jarque-Bera 拒绝正态 |

### L1 规则（`run`）
| code | 级别 | 触发条件 |
|---|---|---|
| `steps_failed` / `run_failed` | error | 有步骤失败 / 整轮 failed |
| `steps_skipped` | warn | 有步骤被跳过（含 when 条件跳过） |
| `no_audit_step` | info | 这条流水线没有审计步骤 |

---

## 6. 算法实现细节（没有第三方库也能跑）

本模块**不依赖 statsmodels/scipy 才能工作**。所有检验都有纯 stdlib 实现，
装了 numpy/pandas/statsmodels/scipy 只是用得更准（`_stats_capabilities()` 写进报告，
避免"以为查了其实没查"）。

| 检验 | 纯 stdlib 实现方式 | 临界值来源 |
|---|---|---|
| **ADF** | Δy 对 y₋₁ 做 OLS，t 统计量 | MacKinnon 近似：−3.43 / −2.86 / −2.57 |
| **KPSS** | 累积和 η，Newey-West 长期方差 (Bartlett 窗) | 0.347 / 0.463 / 0.574 / 0.739 |
| **Chow** | 两段 OLS 的 SSR，F = ((SSR_all − SSR₁ − SSR₂)/k) / ((SSR₁+SSR₂)/dof) | F(2,dof) 近似 3.0~3.5 |
| **Ljung-Box** | Q = n(n+2)Σρ²ₖ/(n−k) | χ²(0.95, lags) ≈ lags + 1.645√(2·lags) |
| **Jarque-Bera** | JB = n/6·(S² + (K−3)²/4) | χ²(0.05, 2) = 5.99 |
| **过拟合** | AR(1) 训练集拟合，样本外 R² | 落差 > 0.4 warn / > 0.5+外R²<0 error |

实测校验（`tests/test_audit.py`）：
- 随机游走 → `unit_root`（ADF t=−0.93）✅
- 白噪声 → `stationary`（ADF t=−12.05，KPSS stat=0.12）✅
- AR(1), φ=0.9 → `autocorrelated` ✅
- 人为均值跳变（0 → 10）→ `structural_break`（F=1251）✅
- 样本内完美趋势 + 样本外噪声 → `overfit`（内 1.0 / 外 −0.80）✅
- 常量序列 → 明确返回 `ok=False`，**不伪装成"平稳"** ✅

> KPSS 在单次抽样下有 5% 的名义误判率（这是检验本身的性质）。
> 测试用 12 个种子验证"大多数判平稳"，而不是断言单次一定平稳。

---

## 7. 设计约束（和 BalanceAI 一致）

1. **不改数**：只读输入。发现问题只标记，修正留给上游角色。
2. **不估算**：缺数据判 `missing` / `value_not_numeric`，**不插值、不填 0、不影响其它字段**。
3. **可解释**：每条 finding 都带 `evidence`（实际值、阈值、来源字段路径 `where`）。
4. **可降级**：L2 依赖缺失降级为 L1 并标 `degraded`；L3 无回调标 `llm_unavailable`；
   任何一条检验异常不影响其它检验（逐个 try/except 包裹）。
5. **不自己决定花钱**：级别由调用方（协调者）传入，默认最省。

---

## 8. 自检清单

```bash
# 单测（审计算法 + 分级 + 0 token + 协调者接入）
python3 -m pytest tests/test_audit.py tests/test_audit_coordinator.py -q

# 本地三档演示（不连 Hub、不联网）
python3 examples/audit_agent.py --local-only

# 连 Hub 跑一次真审计任务
multi-agent serve --host 127.0.0.1 --port 8787 --db hub.db --token secret &
python3 examples/audit_agent.py --hub http://127.0.0.1:8787 --token secret
```

检查点：
- `assert_no_llm_import()` 返回 `{"ok": true, "llm_modules_loaded": []}`
- `rule` 级别的报告里**没有** `L2_stat` 层
- 坏报告跑完 `balance_sheet`，`state == "degraded"` 且 `audit.verdict == "fail"`
- `AUDIT_LLM_URL` 未设时，`level="llm"` 的报告里出现 `llm_unavailable`（而不是静默通过）
