# 规则化结算账本：决策 → 持有期 → 收益 / alpha / 回撤 / 命中

> 结算只认数字，不认措辞。大模型可以写反思，但**不许参与算账**。
> 实现：`multi_agent/integrations/settlement.py`（零 token、零网络、纯标准库）。

## 1. 为什么单独做一层

LLM 的评级（BUY / 5 档）不可验证；可验证的是"这笔决策在持有期结束后赚没赚、跑没跑赢基准"。
把这两件事混在 prompt 里，最后一定变成"模型自己说自己对"。所以：

- **结算归规则**（本模块）：收益、alpha、MFE/MAE、回撤、命中、聚合统计；
- **反思归大模型**（`reflect_prompt`）：只准引用本模块算出的 `facts`，不准自己推价格或收益；
- **缺价不猜**：到期价拿不到就保持 `pending` 并写 `missing_reason`，绝不用最后一根 K 线顶替。

## 2. 口径（改口径先改这里，再改代码）

| # | 规则 | 说明 |
| --- | --- | --- |
| 1 | 按**交易日**对齐 | 到期日 = `entry_date` 在序列中的位置 + `horizon_days`，不按自然日 |
| 2 | 方向 | `LONG(+1)` / `SHORT(-1)` / `FLAT(0)`；也接受 `BUY`/`SELL`/`HOLD` |
| 3 | 收益 | `sign × (exit / entry − 1)` |
| 4 | 基准 alpha | `收益 − sign × 基准收益`；做空时基准也取反，避免"做空跌市白送 alpha" |
| 5 | MFE / MAE | 持有期内相对入场价的最大有利 / 最大不利偏移（按方向，带符号） |
| 6 | 命中 | `sign × 收益 > 0`；`|收益| < 1e-12` 记 `flat`，不计命中 |
| 7 | 回撤 | 持有期内按方向的峰值回撤 `(peak − close) / peak` |

## 3. 记录字段（录入）

```json
{
  "ticker": "600519", "entry_date": "2026-11-20", "direction": "LONG",
  "entry_price": 1680.0, "horizon_days": 5,
  "benchmark": "000300", "rating": "BUY", "key": "可选，默认 ticker@entry_date#direction"
}
```

必填：`ticker`、`entry_date`、`direction`、`entry_price`；`horizon_days` 缺省 5（≥1 的整数）。
不合法直接抛 `SettlementError`，不静默收下（`entry_price` 必须正数、方向必须可识别）。

## 4. 结算结果（输出）

已结算：`status=settled`，带 `exit_date`、`exit_price`、`trading_days_held`、`return`、
`benchmark_return`、`alpha`、`mfe`、`mae`、`max_drawdown`、`hit`、`flat`。

待结算：`status=pending` + `missing_reason`，例如：

- `entry_date 2026-12-31 不在行情序列里`
- `持有 5 个交易日需要 2026-12-04 的收盘价，当前序列只到 2026-11-27`
- `基准 000300 缺 2026-11-20 或 2026-11-27 收盘价，alpha 未计算`（收益照算，alpha 明确为空）

## 5. 聚合（`stats`）

`count / settled / pending / bets / win / loss / flat / hit_rate / avg_return / avg_alpha /
avg_max_drawdown / by_direction / by_rating`。

- `hit_rate = win / bets`，`bets` 只算"非 FLAT 且方向可识别"的已结算记录（FLAT 不计分母，
  否则空仓多就把命中率稀释成噪声）；
- `avg_alpha` 只对真正算出 alpha 的记录求均值，缺基准的不参与；
- 分组同时给 `count / hit_rate / avg_return`，方便看"哪个评级真有 alpha"。

## 6. 存储

```python
from multi_agent.integrations.settlement import Ledger, JsonStore

led = Ledger(JsonStore("ledger.json"))          # 文件账本；MemoryStore() 用于测试
led.add({"ticker": "600519", "entry_date": "2026-11-20",
         "direction": "LONG", "entry_price": 1680.0, "horizon_days": 5})
out = led.settle(prices, benchmark_prices)      # 只结算能算的，其余保持 pending
out["summary"]["hit_rate"]
```

- `JsonStore`：`{"records": [...]}`，写入走临时文件 + `os.replace` 原子替换，同 `key` 覆盖；
- 接 DBManager 时写一个薄适配器，只要实现 `append / items / update` 三个方法即可，
  **口径不要复制第二份**；
- 已结算的记录不会被重算（口径变了要显式重录），避免"每天重跑把历史改了"。

## 7. 命令行（可直接给 cron/CI 用）

```bash
python -m multi_agent.integrations.settlement ledger.json \
  --prices prices.json [--benchmark bench.json] [--json]
# 退出码：0 正常；2 行情/账本不合法（不是"没结算"）
```

行情支持三种形态：`{"日期": 收盘}`、`{"dates": [...], "close": [...]}`、
`[{"date": "...", "close": ...}]`。

## 8. 与反思记忆的边界

`reflect_prompt(record)` 只把 `facts()` 里的可核验字段拼进提示词，并写明"不许推断未给出的价格、
不许编造收益"。教训文本可以进 prompt，但**进记忆时也要带上这些数字**，否则复盘会变成事后叙事
（回溯偏差）。这也是和 TradingAgents 的 memory log 最大的差别：它把"教训"当文本存，
我们把"教训 + 结算数字"一起存，事后能对账。

## 9. 测试

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_settlement.py` | 三种行情形态、多/空/平收益与 alpha、MFE/MAE/回撤、缺入场价与缺到期价保持 pending、基准缺口只缺 alpha、非法记录报错、聚合与分组、账本不重算、同 key 覆盖、CLI 退出码 |