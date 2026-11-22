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

- `indices`: `{name: {code, close[], breadth}}`（close 至少 62 个点才有 vol 分位）
- `market`: `breadth / rotation_days / volume / spread_bps / slippage_pct / exit_hours / ret_5d`
- `futures`: `oi_change_1d / oi_change_5d / basis / delivery_days / margin_ratio / margin_call_distance`
- `structure`: `corr_state(stable|diverging|converging|broken) / leadership / pairs / mean_reversion`
- `proposal`: `side(long|short) / action / model / positions`

字段缺失不报错：缺失项在 evidence 里标"未提供"，判定按保守档走。

## 8. 测试覆盖

`tests/test_risk.py`：

| 场景 | 断言 |
| --- | --- |
| 正常趋势 | 不触发否决，审计 APPROVE/MODIFY |
| 波动混沌 | veto=true，action 降为 flat_only；审计接受降级 |
| 危机（追保 2%） | liquidity=frozen、action=forced_liquidation |
| 结构混沌 + 多品种 | 触发"相关性 broken + 分散"否决 |
| 期货多/空 | 两个方向同一套流动性检查，OI 下降标不可持续 |
| 篡改 vol | 审计复算 mismatch → MODIFY |
| 压制 veto | 审计强制 REJECT |
| 漏报 danger | aggregate 被降级 → 审计 REJECT |
| 协议 | RISK_BRIEF / AUDIT_VERDICT 分段齐全、可解析、同输入同输出 |