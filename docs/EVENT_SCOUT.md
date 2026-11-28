# 事件侦察官（EventScout）与 Trade Agent

> 解决的问题：上一轮 5 轮辩论的 `calendar` 是**人工手填的 2 条**，panel 链路没有调用事件官，
> 也没有“按关键词搜某一天”的爬虫。辩论里 24 次出现“日历待补”，但没有人去补。

## 1. 谁该调用谁

```
Trade Agent（trade_agent.py）                    ← 交易 Agent：只负责编排
   │
   ├─① events__scan ──► EventScout（事件侦察官）  ← 新增
   │                      ├─ 爬虫：东财搜索，关键词 + 日期过滤
   │                      ├─ 分类/排序/去重：policy / macro / index / ...
   │                      └─ EventOfficer：规则日历（MSCI/月末/PMI/期权到期）
   │                    → events + scheduled + digest + calendar
   │
   ├─② 行情快照（东财日线：上证/深成/创业板/科创50）
   │
   ├─③ debate__run（4 席 × N 轮）  ← calendar 自动注入 evidence.calendar
   │
   └─④ report__build → Markdown
```

**硬约束**：`trade_agent.py` 不允许跳过第①步。`debate__run` 本身仍是通用评议器，不绑定事件官；
但 Trade Agent 的入口只有这一个，所以“交易 Agent 必用事件分析”由编排层保证。

## 2. EventScout 做什么

### 2.1 爬虫

- 源：东方财富全网搜索（`search-api-web.eastmoney.com`，公开接口，无需 Key）；
- 入参：`date` + `keywords[]` + `pages`；
- 规则：**文章发布时间必须等于 `date`**，关键词命中标题或正文才保留；
- 去重：按标题前 60 字；
- 失败：单关键词失败只记 `meta.errors`，不编造。

实测（2026-11-27，8 个关键词 × 2 页）：抓到 98 条当日事件，覆盖国常会、证监会回应、
工业企业利润、韩国央行加息、四大行回购增持、氢氧化锂期权、A股 2 万亿成交。

### 2.2 事件官

`EventOfficer` 负责“将发生”的事件：

- 规则路：MSCI 半年度/季度、月末再平衡、季末结算、美股期权到期；
- 新增规则：中国官方 PMI（月末，待核）、A股 ETF 期权到期（第四个周三）；
- 事实路：从爬到的正文里抽“日期 + 事件词”；
- 每条都带 `kind=fact|inferred` + `basis`（凭什么） + `verify`（怎么核）。

### 2.3 输出

```json
{
  "date": "2026-11-27",
  "events": [{"date","event","category","kind","source","url","published",
              "summary","impact_hint","score","quality","keywords"}],
  "scheduled": [{"date","event","kind","basis","verify","source"}],
  "digest": "按类别分组的 Markdown 摘要",
  "calendar": [{"date","event","note"}],
  "meta": {"fetched","kept","errors","keywords"}
}
```

- `calendar` 直接进 `evidence.calendar`，辩论席必须点名；
- `digest` 进 `evidence.events_digest`，给各席“当天发生了什么”的上下文；
- `impact_hint` 只是提示（bull/bear/neutral），最终判断仍归辩论席与裁决官。

## 3. 三种用法

### 3.1 命令行（不依赖 panel）

```bash
python -m multi_agent.frameworks.events.scout \
  --date 2026-11-27 \
  --keywords "国常会,资本市场,A股,MSCI,PMI,韩国央行,工业企业利润" \
  --horizon 5 --pages 2 --out /tmp/scan.json
```

### 3.2 通过 Hub / MCP 单调

panel 起好后，MCP 工具多一个 `events__scan`：

```json
{"date": "2026-11-27",
 "keywords": "国常会,资本市场,A股,MSCI,PMI",
 "horizon_days": 5, "pages": 2}
```

### 3.3 完整 Trade Agent（推荐）

```bash
# 1) 起 panel（Hub + 4 worker）
PANEL_TOKEN=dsktok DETECTIVES=4 bash examples/detective_panel/start_panel.sh

# 2) 一条命令：事件官 → 行情 → 5 轮辩论 → 报告
python trade_agent.py --date 2026-11-27 \
  --keywords "国常会,资本市场,A股,MSCI,PMI,韩国央行,工业企业利润" \
  --topic "国常会后 A 股是否开始构筑底部" --rounds 5

# 产物：trade_out/{event_scan.json,trade_result.json,trade_report.md}
```

## 4. 设计边界

1. **只搜当天**：不拿“最近”冒充“当天”；搜不到就 `degraded`，不补故事；
2. **事实/推断分开**：规则推出来的 MSCI/月末标 `inferred`，必须带 `verify`；
3. **方向只给 hint**：事件官不替代裁决官；
4. **日历压缩**：同一类别最多 2 条、优先“真事件”而非券商解读，避免把 20 条研报塞进 prompt；
5. **可替换**：爬虫源可换（东财/新浪/财联社/私有源），只要输出 `{title, content, published, url, source}`。

## 5. 下一步

- 事件聚类：同一事件的多篇报道合并成一条（现在按标题去重，仍会有“十大券商解读”这类重复）；
- 官方源优先：新华社/证券时报/中国政府网 > 东财转载 > 券商研报；
- 交易日历：用交易所日历替换“月末按星期算”的近似；
- 调度：每个交易日 09:00 / 15:30 各跑一次 `events__scan`，把日历写进 DB，辩论直接读。

---

## 6. 多源 + LLM 兜底（2026-11-28 补）

### 6.1 源清单

| 源 | 适配器 | 定位 | 回看历史 | 备注 |
|---|---|---|---|---|
| 国家统计局 | `fetch_stats` | **宏观官网，优先级最高** | ✅ 列表页可回看 | 标题即事实，如“1—10月规上工业企业利润增长16.0%” |
| 东方财富 | `fetch_eastmoney` | 关键词搜索，覆盖最广 | ✅ 按时间翻页 | 公开 JSONP，无需 Key |
| 新浪财经 7x24 | `fetch_sina` | 实时快讯，政策/市场 | ✅ 约 5 页可回看昨天 | 一页约 100 条 |
| 腾讯财经 | `fetch_tencent` | 财经频道列表 | ⚠️ 只覆盖最近 | JSON-LD 解析 |
| CNBC | `fetch_cnbc` | **美国**市场/宏观 | ⚠️ RSS 只覆盖最近 | 美联储/BLS 官网被拦，用 RSS 替代 |
| MarketWatch | `fetch_marketwatch` | **美国**市场/宏观 | ⚠️ RSS 只覆盖最近 | 同上 |

- 源优先级：`stats(0) > eastmoney(1) > sina(2) > tencent(3) > cnbc/marketwatch(5)`；
- 同一条事件（实体 + 首个百分比数字）跨源合并，保留优先级最高、质量最高的一条；
- 单源失败只进 `meta.errors`，不影响其他源；`priority=0` 是合法值，代码里不能用 `or 9`（已踩过）。

### 6.2 LLM 兜底（最后一步）

```python
EventScout(..., use_llm="auto")   # auto / always / never
```

- **auto（默认）**：只有“当日事件 < 5 条”或“未来日历为空”时才调用 LLM；
- **always**：强制调用，用于补官网日程/海外会议；
- **never**：完全不用，纯爬虫 + 规则；
- LLM 只做一件事：从给定标题里抽“明确写了日期”的预定事件，输出 `date/event/basis/verify`；
- LLM 产出统一标 `kind="inferred"` + `source="infer"`，必须带 `verify`，否则 `EventOfficer` 直接丢弃；
- LLM 失败返回 `[]`，不影响爬虫结果；`meta.llm_used` 记录是否真的调用过。

实测（2026-11-27，`--llm always`）：爬虫 + 规则已给 3 条 11-30 日历，LLM 额外补了
`2026-12-01 欧洲央行12月货币政策会议`（inferred，带 verify）。`auto` 模式下同一批数据
`llm_used=false`——因为爬虫已经够了，LLM 没有抢跑。

### 6.3 已知边界

- 腾讯/CNBC/MarketWatch 的“回看昨天”能力弱：它们的页面/RSS 只保留最近条目；
  日常 15:30 跑没问题，回测历史日期会返回 0 条，不是编造；
- 东财搜索对“工业企业利润”这类官方发布有时搜不到，必须加“统计局”关键词，
  或直接用 `fetch_stats`（现在默认并联，不依赖关键词）；
- 事件聚类是“实体 + 首个百分比”的粗聚类，同一事件的不同口径（16.0% vs 16%）
  仍可能拆成两条；下一步按“实体 + 时间窗”做语义聚类。
