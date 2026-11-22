# 风控官 ↔ 审计师：RISK_BRIEF v2.0 / AUDIT_VERDICT v2.0

> 框架里原来只有 **审计官（auditor）**，负责"这份产出能不能信"。
> 现在加一个 **风控官（risk_manager）**，负责"这个仓位该不该开、能开多大"。
> 两者只用**结构化文本**通信，不共享内存对象；审计官有权把风控官的结论
> 改成 `MODIFY` / `REJECT`，但**不能改写风控官的数字**。

实现：`multi_agent/integrations/risk.py`（`RiskManager` / `Auditor` + 协议解析）。

---

## 1. 为什么是文本协议而不是 JSON

- 风控官和审计官可能跑在不同的 Agent / 不同机器上，中间只过一条消息；
- 文本协议人能直接看、审计官能逐段 parse，出问题时贴进 issue 就能复现；
- JSON 的嵌套结构在跨 Agent 传输里要多一层序列化约定，收益不大。

代价是必须严格按 `[SECTION]` 分节、`key: value` 写字段——解析器（`parse_brief`）
对缺字段是宽容的（缺失按"未提供"处理），但**不猜数**。

## 2. RISK_BRIEF v2.0（风控官 → 审计师）

```
=== RISK_BRIEF v2.0 ===
sender: risk_manager
recipient: auditor
asset_class: equity | futures | both
timestamp: ISO-8601

[REGIME]            state / choppy_score / evidence
[MARKET_RISK_INDEX] 五大指数逐条 signal + metric（含 vol 分位、dd）
[LIQUIDITY_RISK]    成交量/价差/滑点/退出时间 + 期货 OI/基差/交割/保证金
[STRUCTURE_RISK]    相关性状态/主线清晰度/配对可交易性/均值回归/chaos 分
[POSITION_CONSTRAINTS] 最大仓位/止损/持仓天数/杠杆/禁开仓窗口
[HARD_VETO]         veto_triggered / veto_reason / 七条硬否决条件
[SUMMARY]           risk_score(0-100) / recommended_action / reason
=== END RISK_BRIEF ===
```

`recommended_action` 取值：`full_open` / `reduce_30` / `reduce_50` / `flat_only` /
`forced_liquidation`。

## 3. AUDIT_VERDICT v2.0（审计师 → 风控官，抄送 trader/human）

```
=== AUDIT_VERDICT v2.0 ===
sender: auditor
recipient: risk_manager (抄送: trader / human)
timestamp: ISO-8601

[RECHECK]     自算 vol / 回撤 / 流动性退出 vs 风控官值 → match / mismatch / n/a
[CONSISTENCY] regime vs 模型类型；指数信号 vs 提案仓位
[CROSS_CHECK] choppy_score ↔ 轮动速度；流动性 ↔ OI；chaos ↔ 相关性
[VERDICT]     decision: APPROVE | MODIFY | REJECT
              modified_action / binding_conditions / override_available
[REASON]      3-5 句
=== END AUDIT_VERDICT ===
```

## 4. 风控官的判定规则（全部硬计算，零 token）

| 区块 | 规则 |
| --- | --- |
| Regime | panic：vol>90%ile + 5d 下跌 + 相关性 broken/converging；trending：vol<60%ile + 轮动>10d + 主线清晰；choppy：vol 50–85%ile + 轮动<5d；euphoria：vol<30%ile + 5d 上涨 + 估值极端；其余 unknown |
| 指数打分 | 跌破 60/120/250 日线各 +20；20d 收益 <0 +15、<-5% 再 +15；vol>80%ile +15、>90%ile 再 +15；广度<40% +15；总分 >60 warning / >80 danger / >90 crisis |
| 指数汇总 | 取最差两个信号；≥3 个 danger → 全面降仓 |
| 流动性 | 价差>20bps 或退出>8h → tight；退出>24h 或无成交 → frozen；OI 连续下降 + 价格涨 → oi_price_divergence |
| 结构混沌 | `chaos = 轮动倒数×0.3 + 主线崩坏×0.3 + 相关性断裂×0.4` |
| 风险分 | `0.35×指数最差两名 + 0.25×流动性 + 0.2×chaos + 0.2×regime` |
| 硬否决 | panic+做多满仓 / choppy+chaos>0.7+趋势模型 / ≥3 danger+满仓 / frozen 或追保<3% / 交割<5d 加仓 / broken+多品种分散 / 保证金>80% |

## 5. 审计官的四类检查

1. **复算**：同一份数据重算 vol 分位、最大回撤、流动性退出时间；偏差超阈值即 `mismatch`
   （vol 容差 0.15 个分位、回撤 3 个百分点、退出时间 50%）；
2. **一致性**：趋势模型遇 choppy/unknown、均值回归遇 trending、≥3 danger 还满仓、
   `veto=true` 但建议开仓——都算矛盾；
3. **查漏报**：danger≥3 但 `aggregate` 没升级、硬否决条件满足但 `veto_triggered=false`
   → 直接 `REJECT`；OI 下降却没标 `oi_price_divergence` → 补标；
4. **结论**：`REJECT` 时 `modified_action=flat_only`；`MODIFY` 给出降仓档位与
   `binding_conditions`；`override_available=true` 表示人可以覆盖，但要签字。

## 6. 在流水线里怎么用

期货/股票项目的流水线已经内置两步（`projects/futures_balance.py`、`projects/stock_matt.py`）：

```
... → report → risk(brief) → risk_review(auditor/review_brief) → audit → final
```

- `risk` 步返回 `RISK_BRIEF` 文本；
- `risk_review` 步把它连同 `proposal`、`market_data` 交给审计官；
- 审计官注册了 `review_brief` 动作（`integrations/audit.py`），返回 `AUDIT_VERDICT`。

Agent 侧：

```bash
# 风控官（框架通用角色，项目里按 capability=risk 注册）
multi-agent agent --hub ... --id risk-1 --role "risk:brief" --handlers multi_agent.integrations.risk:register
# 审计官
multi-agent agent --hub ... --id audit-1 --role "auditor:review_brief,report" --handlers multi_agent.integrations.audit:register
```

## 7. 数据契约

见 `risk.py` 顶部 docstring。关键字段：

- `indices`: `{name: {code, close[], breadth}}`（close 至少 62 个点才有 vol 分位；
  股票侧建议填全 `FIVE_INDICES`：沪深300 / 中证500 / 中证1000 / 国证2000 / 上证50，
  缺哪个会在 `[REGIME] evidence` 里点名，不补假数据）
- `market`: `breadth / rotation_days / volume / spread_bps / slippage_pct / exit_hours / ret_5d`
  - 不给 `exit_hours` 时，若同时给 `daily_turnover` 与 `position_notional`，
    按"一天最多吃掉 10% 成交额"估算（期货按 4 小时/日折算）
- `futures`: `oi_change_1d / oi_change_5d / oi_down_streak? / basis / basis_change_pct? /
  delivery_days / margin_ratio / margin_call_distance`
  - `oi_down_streak ≥ 3` 或 `oi_change_5d < 0` 且 `market.ret_5d > 0` → 标 `oi_price_divergence`
  - `|basis_change_pct| > 0.5` → 记为基差突变（结构风险）
- `structure`: `corr_state(stable|diverging|converging|broken) / leadership / pairs /
  pairs_spread_sigma? / mean_reversion`
  - `|pairs_spread_sigma| ≥ 2` 视为配对结构 broken（chaos 的相关性项直接按 1.0 计）
- `proposal`: `side(long|short) / action / model / positions`

字段缺失不报错：缺失项在 evidence 里标"未提供"，判定按保守档走。

## 8. 测试覆盖

`tests/test_risk.py`（22 项）：

| 场景 | 断言 |
| --- | --- |
| mock normal | 不触发否决、action=full_open，审计 APPROVE/MODIFY |
| mock volatile | veto=true、action=flat_only（chaos>0.7 + 趋势模型） |
| mock crisis（追保 2%） | liquidity=frozen、action=forced_liquidation |
| mock chaos + 多品种 | 触发"相关性 broken + 分散"否决，配对 2.6σ 标 broken |
| mock 期货多/空 | 两方向同一套流动性检查；只有价格上行才标 OI 背离 |
| 单指数 | aggregate 直接用该指数（不再被压成 normal），并提示五大指数缺失 |
| 篡改 vol | 审计复算 mismatch → MODIFY |
| 压制 veto | 审计强制 REJECT |
| 漏报 danger | aggregate 被降级 → 审计 REJECT |
| 协议 | RISK_BRIEF / AUDIT_VERDICT 分段齐全、可解析、同输入同输出 |
| 注册 | `register()` 暴露 `risk:brief`；`${steps.risk.result}` 的 dict 形态能被审计官吃下 |

## 9. mock_data（`integrations/risk_mock.py`）

需求第 7 条要求的 mock_data 就是它：**确定性合成数据**，无随机、无网络、无第三方包。

```python
from multi_agent.integrations.risk import RiskManager, Auditor
from multi_agent.integrations import risk_mock

data = risk_mock.normal()            # volatile / crisis / chaos 同理，每次返回新副本
brief = RiskManager().generate_report(data)
verdict = Auditor().review(brief, data["proposal"], data)

risk_mock.futures("short")           # 期货双边：只有 proposal.side 与 ret_5d 方向不同
risk_mock.all_scenarios()            # 四场景一次性取全
risk_mock.EXPECTATIONS               # 每场景的预期 veto / action（文档用，测试自己断言）
```

四个场景的合成曲线（260 点历史，够算 250 日分位）：

| 场景 | 曲线 | 关键字段 | 预期 |
| --- | --- | --- | --- |
| normal | 匀速上行 ramps | 轮动 15d、价差 6bps、退出 2.5h | 否决 false、full_open |
| volatile | 波动先放大再收敛 + 锯齿 | 轮动 2d、价差 25bps、退出 10h、主线 unclear | 否决 true（choppy+chaos>0.7+趋势）、flat_only |
| crisis | 前 220 点上行 + 末 40 点 −1.5%/日 | 价差 30bps、退出 30h、保证金 85%、距追保 2% | 否决 true、forced_liquidation |
| chaos | 锯齿 + 相关性 broken + 配对 2.6σ | 4 品种分散、均值回归 broken | 否决 true（结构 broken+分散）、flat_only |

**这些数值是构造出来的，不代表任何真实行情**，只用来验证判定链路。

## 10. 与需求文档的两处有意偏离

1. **pandas/numpy → 纯标准库**：需求第 4 条写"硬计算用 pandas/numpy"。框架本身零依赖
   （见 `pyproject.toml`，只有标准库），所以波动率分位、最大回撤、相关性、保证金
   都用 `statistics` + 纯 Python 循环实现：`statistics.pstdev` 等价于 `numpy.std(ddof=0)`，
   其余为逐点计算，结果与 pandas 口径一致（`tests/test_risk.py` 里比对了复算一致性）。
   好处是任何 Agent 机器不用装科学栈；代价是超长序列（>10 万点）会慢一些。
2. **`risk_manager.py` + `auditor.py` → 单一 `integrations/risk.py`**：需求第 1/3 条按文件分工，
   但框架里 `integrations/audit.py` 已经是平衡表审计官（`Auditor` 类同名），再建 `auditor.py`
   会让"审计官"分叉成两个模块。因此协议两端（`RiskManager` / `Auditor` + render/parse）
   放在同一个 `risk.py` 里，审计官那侧仍通过 `audit.register()` 暴露 `auditor:review_brief`。
   端到端的通信形态没变：两边只交换 RISK_BRIEF / AUDIT_VERDICT 文本。