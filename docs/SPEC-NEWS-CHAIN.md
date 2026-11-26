# 新闻链路接入规范（keywords → collect → tune → assess → transmit）

本文件是给**接入方**（期货/BalanceAI、A 股、其他业务线）看的接口契约。
按这里写配置和调用即可复用，不需要读实现；实现细节以代码为准，本文件与代码冲突时以代码为准并回改本文档。

一条链路只有四个环节，每个环节只干一件事：

```
config/keywords/<name>.json
        │  关键词表（加一行就多一条触发）
        ▼
news/collect        NewsWatchFramework.collect()      命中了什么（不判方向）
        ▼
supervisor/tune     tune()                            权重 / 大事阈值 / 最低级别
        ▼
assess/classify     assess()                          是否大事、往哪边压
        ▼
transmit/map        transmit()                        传到哪些环节、哪些标的、多大压力
```

四条硬规矩（改任何一环都不能破）：

1. **不编造**：没数据就 `degraded=True` + `reason`，不给假事件、假数字；
2. **采集不判方向**：事件的 `impact` 恒为 `None`，方向只由评估给；
3. **去重可复现**：同一输入两次跑，条数与顺序一致（去重键固定）；
4. **零 token 优先**：能用规则就不调模型；用模型也必须能降级。

---

## 1. 关键词表

位置：`config/keywords/<name>.json`；`<name>` 默认 `news`。

```json
{
  "name": "credit-watch",
  "max_hits": 50,
  "groups": [
    {
      "group": "credit_stress",
      "description": "CDS 走阔 / 违约概率 / 评级下调",
      "keywords": ["CDS", "信用违约", "违约概率", "评级下调", "降级", "信用利差"],
      "level": "error",
      "mode": "program",
      "target": {"capability": "assess", "action": "classify"}
    }
  ]
}
```

### 1.1 组字段

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `group` | string | 是 | `default` | 组名。`tune` 的权重键、`assess` 看到的 `group` 都是它，改名等于改契约 |
| `keywords` | string[] | 是 | — | 命中词；**空数组的组会被丢弃**（不参与匹配） |
| `level` | `info`/`warn`/`error`/`critical` | 否 | `warn` | 非法值回落 `warn`；`assess` 的 `min_level` 按它过滤 |
| `mode` | `program`/`llm` | 否 | `program` | 非法值回落 `program` |
| `target` | object | 否 | `{}` | `{capability, action}`，只做标注，路由由编排决定 |
| `regex` | bool | 否 | `false` | 为真时把 `keywords` 用 `|` 拼成正则（不区分大小写） |
| `description` | string | 否 | `""` | 人看的备注 |

顶层：

| 字段 | 默认 | 说明 |
|---|---|---|
| `name` | 文件名 | 关键词表名，返回在 `keyword_set` 字段 |
| `max_hits` | 50 | 单次扫描最多留几条命中，防止刷屏日志淹没下游 |

### 1.2 两条判定路径

| `mode` | 怎么判 | 成本 | 端点未配时 |
|---|---|---|---|
| `program` | 关键词/正则命中即算 | 0 token | 与配了完全一样 |
| `llm` | 先按关键词挑候选，再让 `llm/chat` 判"算不算" | 烧 token | **降级为程序命中**，`judged=False`，不丢数据 |

结论：`llm` 只在"关键词太宽、误报多"的组上用（例如"算力""数据中心"这类泛词）。
纯事实组（`CDS`、`评级下调`）用 `program`，别浪费 token。

### 1.3 查找顺序

`KeywordSet.load(name)` 按顺序找 `{name}.json`：环境变量 `MULTI_AGENT_KEYWORDS` 指向的目录 →
调用方 `search=[...]` 给的目录 → 仓库 `config/keywords/`。都没找到返回 `None`，采集环节给
`degraded=True, reason="没找到关键词表"`，不编造事件。

---

## 2. 采集：`NewsWatchFramework.collect()`

```python
from multi_agent.frameworks.keywords import KeywordSet
from multi_agent.frameworks.news.framework import NewsWatchFramework

fw = NewsWatchFramework(
    keyword_set=KeywordSet.load("news"),   # 或 KeywordSet.from_file(".../keywords.json")
    fetch=lambda hours: my_source(hours),  # 取数：fetch(hours) -> list[dict]
    judge=None,                            # mode="llm" 的复核；None 时用 llm/chat
)
res = fw.collect({"hours": 24})            # 也可以直接传 {"items": [...]} 跳过 fetch
```

### 2.1 输入项（`items[]`）

| 字段 | 必填 | 说明 |
|---|---|---|
| `title` | 是（或 `summary` 非空） | 标题 |
| `summary` / `text` | 否 | 摘要/正文；与标题拼成一行去匹配 |
| `published` | 否 | 时间戳字符串，参与 `event_id` 计算 |
| `source` / `site` | 否 | 来源名 |
| `url` | 否 | 原文链接 |

匹配对象是 `"title summary"` 拼成的整行；命中即产出一条事件，**不命中的新闻不会出现在结果里**。

### 2.2 返回

```json
{"events": [...], "scanned": 5, "matched": 3, "by_group": {"credit_stress": 3},
 "truncated": 0, "collector": "news-watch", "degraded": false, "reason": "",
 "source": "keywords", "keyword_set": "news"}
```

### 2.3 事件字段

| 字段 | 说明 |
|---|---|
| `event_id` | 由 `title + published` 求哈希，同一新闻重复出现得到同一个 id |
| `group` / `keyword` / `level` / `target` | 命中的组、命中词、级别、转交目标 |
| `line` | 命中的那一行（截断 300 字符，原样保留） |
| `impact` | **恒为 `None`**（采集官不判方向） |
| 其余 | `title` / `published` / `source` / `url` / `summary` 原样透传 |

去重键是 `(event_id, group)`：同一新闻命中两个组会各出一条（因为是两件事），
同一组重复出现只留一条。

### 2.4 降级

| 情况 | 返回 |
|---|---|
| 找不到关键词表 | `degraded=True`，`reason` 指明路径 |
| `fetch` 抛异常 | `degraded=True`，`reason` 含异常类型 |
| 既没 `fetch` 也没 `items` | `degraded=True`，`reason="未注入取数函数"` |
| `llm` 判定抛异常 | 该条按程序命中处理（宁可多留，不漏） |

---

## 3. 经理调参：`supervisor/tune`

职责边界：**coordinator 只按编排派发，不调权重**；权重、阈值、级别门槛由
`supervisor`（经理）统一定，这样换品种/换季节只改参数，不动评估代码。

```python
from multi_agent.integrations.supervisor import tune

tuned = tune({
    "keyword_set": "credit-watch",        # 读哪张关键词表定默认权重
    "weights": {"credit_stress": 0.95},   # 覆盖默认
    "major_weight": 0.7,                  # 多高算"大事"
    "min_level": "warn",                  # info 级直接滤掉
})
```

### 3.1 入参

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `weights` | `{group: float}` | 按关键词表**每组 1.0** | 非数值项被忽略，不报错 |
| `major_weight` | float | `0.6` | 夹到 `[0,1]`；非法值回落 0.6 |
| `min_level` | string | `info` | 非 `info/warn/error/critical` 一律按 `info` |
| `keyword_set` | string | `news` | 读不到就**不给默认权重**（`weights={}`），仍可用显式 weights |

### 3.2 出参

```json
{"ok": true, "degraded": false, "weights": {"credit_stress": 0.95},
 "major_weight": 0.7, "min_level": "warn", "source": "config+params", "reason": ""}
```

### 3.3 局部覆盖的坑（必读）

默认权重是**按关键词表每组各给 1.0**。所以只覆盖一组时，其余组仍是 1.0，
可能反超你调高的那一组：

```python
# 想让 supply_shock 领先，但 supply_cut 默认 1.0 会压过 0.95
tune({"weights": {"supply_shock": 0.95}})                       # ❌ lead 变成 supply_cut
tune({"weights": {"supply_shock": 0.95, "supply_cut": 0.7}})    # ✅ 显式给出其余组
```

规则：**要么全给，要么全不给**；只调一组时，务必知道其余组的默认值（都是 1.0）。

---

## 4. 评估：`assess/classify`

```python
from multi_agent.projects.trade.news_assessor import assess

verdict = assess({"events": collected, "tuning": tuned})
```

### 4.1 入参

| 字段 | 说明 |
|---|---|
| `events` | 采集返回的 dict（取其 `events`，并透传上游 `degraded/reason`）或事件数组 |
| `tuning` | `supervisor/tune` 的产出（`weights` / `major_weight` / `min_level`） |

### 4.2 出参

```json
{"ok": true, "degraded": false, "reason": "",
 "major": true,
 "lead": {"event_id": "…", "tag": "credit_stress", "impact": "bear", "weight": 0.95, "why": "命中关键词：CDS、违约概率"},
 "items": [{"event_id": "…", "tag": "…", "impact": "bull|bear", "weight": 0.9, "why": "…"}],
 "counts": {"events": 3, "major": 1},
 "tuning": {"weights": {...}, "major_weight": 0.7, "min_level": "warn"}}
```

- `items` 按 `weight` 降序；`lead` 是第一条；
- `major = max(weight) >= major_weight`；
- 一条规则都没命中时 `ok=false, degraded=true, reason="规则表没命中任何事件（不硬判）"`
  —— 这是设计行为，不是故障；
- 输入的 `tuning` 会原样回写到输出的 `tuning` 里，便于下游审计。

### 4.3 规则表

`news_assessor._RULES`，格式 `(命中词, tag, impact, 权重)`：

| tag | 命中示例 | impact | 默认权重 |
|---|---|---|---|
| `supply_shock` | 矿难 / 停产 / 罢工 / 事故 / 不可抗力 | bull | 0.9 |
| `supply_cut` | 配额 / 减产 / 限产 / 收储 | bull | 0.7 |
| `trade_shock` | 制裁 / 关税 / 禁运 | bull | 0.6 |
| `supply_up` | 增产 / 复产 / 抛储 / 累库 | bear | 0.6 |
| `demand_down` | 需求走弱 / 砍单 / 去库放缓 | bear | 0.6 |
| `demand_up` | 旺季 / 补库 / 订单回暖 | bull | 0.4 |
| `credit_stress` | CDS / 信用违约 / 违约概率 / 评级下调 / 降级 / 信用利差 | bear | 0.8 |

加一行即多一条规则：`(("新词1", "新词2"), "tag", "bull|bear", 0.5)`。
`weights` 里的权重按 **tag** 覆盖默认值（不是按关键词）。

### 4.4 两条修正规则

1. **反向终止词翻转**：`supply_shock` / `supply_cut` 命中 bull 时，若同一文本出现
   `结束 / 达成协议 / 解除 / 复工 / 恢复生产 / 恢复正常`，方向翻成 bear，权重压到 `≤0.5`
   （`FLIP_WEIGHT`）。"复产时间待评估"不含上述终态词，**不翻转**——别把这种词加进去。
2. **`credit_stress` 不参与翻转**：信用事件本来就按 bear 处理，终态词不影响它。

---

## 5. 传导：`transmit/map`

```python
from multi_agent.projects.trade.transmit import transmit

mapped = transmit({"severity": 76.7, "capex_cut": 0.10})
```

### 5.1 入参

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `severity` | 0-100 | 必填 | 事件本身的信用压力；夹到 `[0,100]` |
| `capex_cut` | 0-1 | `0.10` | 情景：资本开支下修幅度 |
| `chain` | dict | — | 直接给映射（跳过读文件） |
| `chain_path` | string | `config/transmit/ai_ashare.json` | 指定另一个映射文件 |

### 5.2 两条公式，两套口径

```
压力分(标的)     = severity × financing_dependence(环节) × exposure(标的)      → 0-100
营收影响(标的)%  = -capex_cut × capex_sensitivity(环节) × ai_revenue_share(标的) × 100
```

- 压力分档位：`≥50 高 / ≥35 中高 / ≥20 中 / <20 低`；
- `unit="pressure"`，`scale` 写明"不是涨跌幅、不是目标价"；
- 营收影响只到**收入**，不含毛利、费用、估值变化。

### 5.3 映射文件字段

```json
{
  "name": "ai_ashare", "assumption": true,
  "disclaimer": "示例权重，不代表实测敞口",
  "scenario": {"capex_cut": 0.10, "unit": "revenue_pct"},
  "chains": [
    {"id": "idc", "name": "IDC / 算力租赁",
     "financing_dependence": 0.9, "capex_sensitivity": 1.0,
     "rationale": "重资产、靠项目融资",
     "ashare": [{"ticker": "300442", "name": "润泽科技",
                 "exposure": 0.9, "ai_revenue_share": 0.85}]}
  ]
}
```

`assumption=true` 与 `disclaimer` 会原样带进输出；**换成实测值前不要把 `assumption` 改成 false**，
否则等于把假设当事实。

### 5.4 出参与降级

```json
{"ok": true, "severity": 76.7, "items": [...], "top": [...5], "top_revenue": [...5],
 "by_segment": [{"segment": "idc", "stress": 69.0, "band": "高",
                 "revenue_impact_range": [-8.5, -6.5], "tickers": 2}],
 "counts": {"segments": 7, "tickers": 15},
 "scenario": {"capex_cut": 0.1, "unit": "revenue_pct"},
 "assumption": true, "disclaimer": "...", "unit": "pressure"}
```

映射为空或读不到文件 → `degraded=true`，`reason` 指明原因，不返回任何标的。

---

## 6. 三种接入方怎么用

| 接入方 | 怎么接 | 改哪里 |
|---|---|---|
| 期货 / BalanceAI | 复用 `news/collect → tune → assess`，下游 `balance/build` 不动 | 关键词表换 `config/keywords/news.json`；规则按品种加 `_RULES` 行 |
| A 股 | 同上，末端接 `transmit/map`；环节与标的写映射表 | `config/transmit/ai_ashare.json` |
| 其他业务 | 只继承 `NewsWatchFramework`，实现自己的 `fetch(hours)` | 新增 `config/keywords/<自己>.json` |

部署形态：三个角色各自是独立 Agent（`NewsCollectProject` / `NewsAssessProject` /
`TransmitProject`），也可以和采集官绑在同一个 Agent 上少开进程。编排里只写
`capability + action`（`collector/scan`、`assess/classify`、`transmit/map`），不写项目类名。

---

## 7. 验收

```bash
# 关键词引擎与采集
python -m pytest tests/test_ashare_transmit.py -q

# 端到端示例（逐项 PASS/FAIL，退出码 0 为通过）
python examples/oracle_cds_ashare/run_demo.py

# 全量回归
python -m pytest -q
```

接入一个新的业务线，最少要证明四件事：

1. 命中能采到、噪声不入库（造一条无关新闻，确认它不出现在 `events`）；
2. `tune` 的权重真的进了 `assess`（改 `major_weight` 后 `major` 会翻）；
3. 不命中时不硬判（`degraded=true` 且有 `reason`）；
4. 换成真实数据后，输出里的 `assumption` / `source` 与真实情况一致（不把示例权重当实测）。

---

## 8. 已知限制

- `llm` 模式的复核只在端到端示例里用注入函数验证过，真实模型端点未打通（沙箱无 URL）；
- 关键词匹配是**词面子串**，不理解否定与语境（"罢工结束"靠 `_REVERSAL` 补，不是通用解法）；
- 中文分词未接入，长词包含短词时会同时命中（如"复产"与"恢复生产"）；
- `transmit` 的环节权重与标的敞口全是示例值，生产使用前必须换成实测收入占比/订单占比；
- 采集去重按 `(event_id, group)`，同一新闻若标题微改会当成两条（标题规范化不在本规范范围）。