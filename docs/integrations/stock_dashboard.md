# 接入 stock-dashboard：结构指标作为证据条

## 为什么接

`data_feed`（通用取数角色）只算**价格派生**指标：动量、回撤、区间位置、波动分位。
这些回答不了"结构有没有坏"：

- 缠论的买卖点出没出、中枢在扩张还是收敛
- DFA 的长程相关性从趋势态滑到随机态没有
- 期权有没有在给尾部定价（IV 分位、25D 偏度、PCR）
- 期货是升水还是贴水

这些指标 [stock-dashboard](https://github.com/baizhiwei9/stock-dashboard) 已经按统一契约
算好并定时落库（`data_hub.tech`），Agent 侧不必重算——读出来打包成证据条即可，零 token。

## 分工

| 模块 | capability | 动作 | 数据 |
| --- | --- | --- | --- |
| `integrations/data_feed.py` | `data` | `catalog` `metrics` `evidence` `valuation` `inventory` `moneyflow` | 价格派生、估值、资金流、库存 |
| `integrations/stock_dashboard.py` | `data` | `tech_catalog` `tech_structure` `tech_option` `tech_evidence` `board_status` | 缠论、DFA、RMT、期权、调度状态 |

动作名带 `tech_` / `board_` 前缀是刻意的：同一个 `data` capability 下 `catalog` 和
`evidence` 已被占用，重名会互相覆盖。两个模块可以同时注册。

## 装

```bash
export DATAHUB_PATH=/path/to/datahub-multi-tier      # 必填
export STOCK_DASHBOARD_URL=http://127.0.0.1:8767     # 可选，读调度状态用
```

```python
from multi_agent.integrations.data_feed import register as register_feed
from multi_agent.integrations.stock_dashboard import register as register_board

register_feed(runtime)
register_board(runtime)
```

## 五个动作

### `tech_catalog`

列出指标库里全部指标 key，按域分组（`chanlun` / `dfa` / `rmt` / `option` / `trend` /
`macro` / `liquidity` / `margin`），每个域附人话说明。编排层拿这个决定要哪几项，
不用猜字段名。

```python
{"ok": true, "n_metrics": 52, "domains": {"chanlun": {"note": "...", "n": 8, "metrics": [...]}, ...}}
```

### `tech_structure`

```python
{"code": "000300.SH", "domains": ["chanlun", "dfa"]}
```

返回该标的在指定域的定稿指标点：`value` / `unit` / `direction` / `as_of` / `quality`。
**只回有记录的指标**，缺的不补零。库里没有这个标的就明说，并提示先跑哪个 producer。

### `tech_option`

```python
{"code": "000300.SH"}   # 或留空取全部覆盖的指数
```

平值IV、25D偏度、持仓/成交 PCR、VIX，各带近 250/500 日分位。

口径两点：
1. 一个指数可能有多套期权（沪深300 既有 IO 股指期权也有 510300 ETF 期权），
   只取「现货与指数同一只」的那套；
2. 分位样本 < 60 个返回 `None`，不给假精确。

### `tech_evidence`

```python
{"codes": ["000300.SH", "000905.SH"], "names": {"000300.SH": "沪深300"}}
```

格式与 `data_feed.evidence` 同构，直接喂 `adversary` / `equity_pick`：

```python
{"ok": true, "evidence": {"沪深300": {"chanlun.buy_point": {"value": 1, "direction": "higher_bullish", "signal": null, "weight": null, ...}}}}
```

**`signal` / `weight` 刻意留空**：方向由指标契约的 `direction` 给出，多空判断归辩论侧。
取数层不下结论——这是分工，不是偷懒。

### `board_status`

读看板 `GET /api/state`，回各 task 上次跑的时间、成功与否、启停状态。
看板没起就明说"看板没起或地址不对"，不假装拿到了状态。

## 边界

- **不发网络请求取行情**。只读本地指标库 + 读本机看板 HTTP。
- **取不到就说取不到**：不补零、不拿旧值顶替、不拿"差不多"的数糊过去。
- `DATAHUB_PATH` 不设默认值：不同机器 checkout 位置不同，写死会把"路径错"伪装成"没数据"。
