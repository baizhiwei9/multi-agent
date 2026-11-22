# 审计官（Auditor）—— 多项目框架 + 三级成本

> 一个角色解决一件事：**判定"这份产出能不能信"**。
> 不改数、不估算、不重算业务；只查矛盾、查结构性缺陷、查假设是否被数据推翻。

---

## 1. 两条轴：引擎 × 口径

审计官的实现分成互不干扰的两层，这是这一版最重要的设计：

```
┌──────────────── Auditor（引擎，通用，不管项目）────────────────┐
│ · 三级分级与短路（rule / stat / llm）                          │
│ · Finding / Report / verdict（pass / suspect / fail）           │
│ · L2 统计检验：ADF / KPSS / Chow / Ljung-Box / JB / 过拟合       │
│ · L3 语义审计入口（回调由调用方注入）                            │
│ · 0-token 硬保证（运行时自检 + 静态测试）                        │
└───────────────────────────┬────────────────────────────────────┘
                            │ project="balance_sheet" / "stock" / "bond"
                            ▼
┌────────────── AuditProfile（插件，一个项目一个）──────────────┐
│ balance_sheet：父子项勾稽 / 供需差 / 单位量级 / 库存滞后        │
│ stock        ：三表勾稽 / 应计质量 / 毛利率背离 / EPS 一致       │
│ bond         ：久期凸性 / 曲线单调 / 信用利差 / 票息到期         │
└────────────────────────────────────────────────────────────────┘
```

**新增一个项目 = 写一个 `AuditProfile` 子类 + `register_profile()`。**
引擎、Coordinator、Hub 协议都不用改。

---

## 2. 三级执行（默认最省）

| 级别 | 名字 | 走网络 | 烧 token | 能查什么 |
|---|---|---|---|---|
| **L1** | `rule` 规则 | ❌ | ❌ | 项目特定的字段/勾稽/量级/一致性/时间新鲜度/报告占位符/叙事矛盾 |
| **L2** | `stat` 统计 | ❌ | ❌ | ADF 单位根、KPSS、Chow 断点、Ljung-Box 自相关、JB 正态、AR(1) 过拟合 |
| **L3** | `llm` 语义 | ✅ | ✅ | 措辞误导、结论无证据支撑（**回调由调用方注入**） |

### 级别由协调者决定，不由 Auditor 自己决定

```python
coord.start("balance_sheet", {"commodity": "copper", "audit_level": "rule"})  # 默认
coord.start("balance_sheet", {"commodity": "copper", "audit_level": "stat"})
coord.start("balance_sheet", {"commodity": "copper", "audit_level": "llm"})
```

优先级：**运行参数 `audit_level` > 环境变量 `AUDIT_LEVEL` > `rule`**。
不传就永远不联网 —— 这个默认值写死在 `coordinator.DEFAULT_AUDIT_LEVEL`。

`auto` 模式的省钱短路：L1 有 error → 直接返回 fail，跳过 L2/L3。

---

## 3. 三个内置口径

### 3.1 `balance_sheet` 供需平衡表

```python
Auditor(project="balance_sheet").audit_balance(copper_balance)
```

| 检查 | 级别 | 触发 |
|---|---|---|
| `missing_field` | error | 缺 commodity/period/unit/supply/demand/balance |
| `unit_unknown` | warn | 单位不在 吨/千吨/万吨/百万吨 |
| `value_not_numeric` | warn | 字段不是数字（**不填 0，标出来**） |
| `magnitude_suspect` | error | 换算成吨后超出该字段合理量级 |
| `internal_mismatch` | error | 精炼总 ≠ 原生 + 再生（容差 2%） |
| `balance_inconsistent` | error | 供需差 ≠ 精炼总 − 用量 |
| `sign_conflict` | warn | 供需差与库存变动符号相反 |
| `constant_series` | warn | 长度 ≥4 全相同的列 |
| `data_stale` / `data_aging` | error/warn | 距今 >120 / >45 天 |
| `inventory_stale` | warn | 库存子项滞后 >120 天 |
| `narrative_conflict` | **error** | 报告写"去库"但平衡表是过剩 |
| `report_numbers_unmatched` | warn | ≥2 个数字在平衡表里找不到 |

### 3.2 `stock` 股票 / 财务

```python
Auditor(project="stock").audit_financials(fin)     # 或 audit_balance(fin) 等价
```

| 检查 | 级别 | 触发 |
|---|---|---|
| `gross_profit_mismatch` | error | 毛利 ≠ 营收 − 成本（容差 1%） |
| `balance_sheet_mismatch` | error | 资产 ≠ 负债 + 权益 |
| `accruals_poor` / `accruals_weak` | **error**/warn | 应计比例 \|（净利−经营现金流）/净利\| > 25% / 10% |
| `net_income_divergent` | warn | 净利与营业利润偏离 >35% |
| `growth_margin_divergence` | warn | 营收 +20% 但毛利率掉 >5pp（低价换量） |
| `eps_mismatch` | warn | EPS ≠ 净利/股本 |
| `narrative_conflict` | error | 营收实际下滑但报告说增长；或现金流为负却说"现金流健康" |

### 3.3 `bond` 债券 / 固收

```python
Auditor(project="bond").audit_bond(bond)
```

| 检查 | 级别 | 触发 |
|---|---|---|
| `duration_mismatch` | warn | 修正久期 ≠ 麦考利/(1+y/f)（容差 5%） |
| `negative_convexity` | warn | 凸性为负（只有含权债合理） |
| `coupon_ytm_conflict` | warn | 票息远高于 YTM 但价格低于面值 |
| `spread_outlier` | warn | 信用利差 > 800bp |
| `already_matured` | error | 到期日已过却还在存续清单 |
| `curve_inversion` | warn | 曲线某段倒挂 |
| `narrative_conflict` | warn | 走陡却说平坦化（或反之） |

---

## 4. 加一个新项目（三步）

```python
from multi_agent.integrations.audit_profiles import AuditProfile, register_profile

class FundProfile(AuditProfile):
    name = "fund"
    description = "基金审计（净值勾稽、持仓集中度、费用率）"
    thresholds = {"concentration_warn": 0.30}

    def check_balance(self, data, rep):
        # rep.add(code, severity, message, evidence, where)
        nav = data.get("nav")
        units = data.get("units")
        total = data.get("total_asset")
        if None not in (nav, units, total):
            if not self.close_enough(nav * units, total, 0.01):
                rep.add("nav_mismatch", "error",
                        f"净值×份额 {nav*units:.4g} ≠ 总资产 {total}",
                        {"nav": nav, "units": units, "total": total},
                        where="nav")

        top = data.get("top10_weight")
        if top is not None and top > self.thresholds["concentration_warn"]:
            rep.add("concentration", "warn",
                    f"前十大持仓 {top:.1%}，集中度偏高",
                    {"top10_weight": top}, where="holdings")

    def series_from(self, data):
        # 声明哪些字段值得做 L2 统计检验
        out = {}
        s = data.get("nav_history") or []
        if len(s) >= 12:
            out["nav"] = s
        return out

register_profile(FundProfile())
Auditor(project="fund").audit_balance(fund_dict)   # 立即可用
```

要点：
- `check_balance` / `check_report` / `check_series` / `check_run` / `series_from` 都是可选
- `rep.add()` 的 severity 用 `info` / `warn` / `error`
- `evidence` 放实际值 + 阈值（审计报告要能自证）
- `where` 是字段路径，出问题时能直接定位
- **不实现 `series_from` 就不做 L2 统计**（不会瞎猜序列）

---

## 5. 0 token 是硬保证

`multi_agent/integrations/audit.py` 有两道锁：

1. **运行时** `assert_no_llm_import()` —— 查 `sys.modules` 有无 openai/anthropic/
   dashscope/transformers，结果写进报告 `layers.L1_rule`
2. **静态测试** `test_audit_module_does_not_import_llm()` —— 扫源码，出现
   `import openai` / `from anthropic` 直接 fail

L3 的 llm 是**调用方注入的回调**：

```python
Auditor(project="stock", level="llm", llm=lambda prompt: my_model(prompt))
```

没传回调时显式标 `llm_unavailable` 降级，**绝不假装审计过了**。

```bash
export AUDIT_LLM_URL=http://127.0.0.1:18799/v1/chat/completions
export AUDIT_LLM_MODEL=openclaw/yuanbao-agent
export AUDIT_LLM_TOKEN=***
```

---

## 6. 算法实现（无 statsmodels 也能跑）

所有检验都有纯 stdlib 实现，装了 numpy/pandas 只是更准
（`_stats_capabilities()` 写进报告，避免"以为查了其实没查"）。

| 检验 | 纯 stdlib 实现 | 临界值 |
|---|---|---|
| **ADF** | Δy 对 y₋₁ 做 OLS 取 t | MacKinnon: −3.43/−2.86/−2.57 |
| **KPSS** | 累积和 η + Newey-West 长期方差 | 0.347/0.463/0.574/0.739 |
| **Chow** | 两段 OLS 的 SSR 做 F 检验 | F(2,dof) ≈ 3.0~3.5 |
| **Ljung-Box** | Q = n(n+2)Σρ²ₖ/(n−k) | χ²(0.95,lags) |
| **Jarque-Bera** | JB = n/6(S²+(K−3)²/4) | 5.99 |
| **过拟合** | AR(1) 训练集拟合，样本外 R² | 落差>0.4 warn / >0.5+外R²<0 error |

实测校验：
- 随机游走 → `unit_root`（t=−0.93）✅
- 白噪声 → `stationary`（t=−12.05）✅
- AR(1) φ=0.9 → `autocorrelated` ✅
- 均值 0→10 跳变 → `structural_break`（F=1251）✅
- 样本内完美 + 样本外噪声 → `overfit`（内 1.0 / 外 −0.80）✅
- 常量序列 → 明确 `ok=False`，不伪装平稳 ✅

> KPSS 单次抽样有 5% 名义误判率（检验本身性质），测试用多 seed 验证"大多数判平稳"。

---

## 7. 挂到 Hub + 按能力路由

### 7.1 注册（角色名带口径，关键）

```python
from multi_agent.integrations import audit

# 只支持债券审计
audit.register(rt, projects=["bond"])
# → 注册成 capability = "auditor:bond"

# 支持多个口径（一台机器干多种活）
audit.register(rt, projects=["bond", "stock", "balance_sheet"])
# → 注册 auditor:bond / auditor:stock / auditor:balance_sheet 三个角色

# 不传 projects（兼容老用法）→ 注册通用 "auditor"，靠 params.project 区分
audit.register(rt)
```

**为什么角色名要带口径**：Hub 只按 `capability(=role.name) + action` 选人。
如果所有审计官都叫 `auditor`，协调者就分不清谁支持债券 —— 只能投给所有人，
再让不支持的那台报错重试，白跑一轮。拆成 `auditor:bond` 之后一条
`submit("auditor:bond", ...)` 就直接落到对的那台。

### 7.2 协调者按能力派活

```python
from multi_agent.router import CapabilityRouter

router = CapabilityRouter(hub_client)        # HTTP 或 TCP client 都行

router.catalog()          # 全网能力清单：每个能力 ← 哪些 agent
router.route("auditor:bond", "balance")      # 选一台，带理由
router.gaps(["auditor:bond/balance", "runner/py"])   # 缺什么
router.route_plan([("A","build_sheet"), ("auditor:bond","balance")])  # 流水线预检
router.submit("auditor:bond", "balance", {"balance": bond}, wait=True)
router.submit_with_fallback(...)             # 失败自动换下一台
```

实测输出（`examples/capability_routing_demo.py`）：

```
### 1) 全网能力清单
    auditor:balance_sheet/balance ← ['openclaw-1', 'openclaw-2']
    auditor:bond/balance          ← ['coze-1']
    auditor:stock/balance         ← ['hermes-1']

### 2) 按能力派活
    平衡表 需要 auditor:balance_sheet → openclaw-1  verdict=pass
    股票   需要 auditor:stock         → hermes-1    verdict=pass
    债券   需要 auditor:bond          → coze-1      verdict=pass

### 3) 平衡表有两台 → 轮询分到两台
    4 次派活 → ['openclaw-2', 'openclaw-1', 'openclaw-2', 'openclaw-1']

### 4) 点名要某台（不支持就报错，不静默换人）
    ✓ 正确拒绝：agent coze-1 没有声明 auditor:balance_sheet/balance

### 5) 缺能力提前发现
    查 [...balance_sheet/balance, ...bond/balance, runner/py] → 缺：['runner/py']

### 7) 一台挂了 → fallback 自动换另一台
    仍成功派到 openclaw-2，state=succeeded
```

### 7.3 路由要点

- **只按能力**：没声明就是不支持，不会"试试看"
- **可解释**：每次返回 `reason` + `candidates`
- **点名不静默换人**：`require_agent` 指定的机器不支持 → 直接报错
- **标签优先**：传了 `prefer_labels` 就先按标签筛（不管默认策略是什么），
  没匹配上会明说"退回轮询"而不是静默忽略
- **4 种策略**：`round_robin`（默认）/ `least_loaded` / `prefer_label` / `first`

---

## 8. 流水线接入

`pipelines.BALANCE_SHEET` 现在的形状：

```
A 建表 → 入库 → B 新闻 → 入库 → Q 判定 → E 报告 → ★audit → J 终审
```

```python
{"name": "audit", "capability": "auditor", "action": "full",
 "params": {"balance": "${steps.build.result}",
            "report":  "${steps.report.result}",
            "project": "${params.audit_project}",     # 默认 balance_sheet
            "level":   "${params.audit_level}"},      # 默认 rule
 "depends_on": ["report"]}
```

终态多了一个 `degraded`：

| 终态 | 含义 |
|---|---|
| `succeeded` | 跑通 且 审计 pass |
| **`degraded`** | **跑通了，但审计判 fail/suspect —— 结果可看不可直接用** |
| `failed` | 有步骤失败 |

---

## 9. 自检

```bash
# 全量（引擎 + 三口径 + 路由 + 协调者接入）
python3 -m pytest tests/ -q          # 191 passed

# 单独
python3 -m pytest tests/test_audit.py tests/test_audit_profiles.py \
                  tests/test_audit_coordinator.py tests/test_router.py -q

# 演示
python3 examples/audit_agent.py --local-only          # 三档对比，不联网
python3 examples/capability_routing_demo.py           # 按能力派活
```

检查点：
- `assert_no_llm_import()` → `{"ok": true, "llm_modules_loaded": []}`
- `rule` 档位下报告里 `L2_stat.ran == False`（显式记录，不是字段缺失）
- 坏报告跑完 `balance_sheet` → `state == "degraded"` 且 `audit.verdict == "fail"`
- `AUDIT_LLM_URL` 未设时 `level="llm"` → 出现 `llm_unavailable`（不是静默通过）
- 只声明 `auditor:bond` 的 agent 绝不会收到 `auditor:balance_sheet` 的任务
