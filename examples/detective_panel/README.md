# 正反辩论（detective_panel）

框架内检索 → 四席两正两反 → 默认 3 轮 → 最后一次裁决。全部 DeepSeek，只有反方用思考模式。

```
关键词 ─→ news/collect（框架内检索：调 Hub 的 search/*，过滤去重）
             ↓
正方·政策与基本面 ┐
正方·资金与技术   ├─ 同读一份证据 → 逐席发言（每轮能看到前面的人说了什么）
反方·估值与盈利   │        ↓
反方·历史与情绪   ┘   裁决官（最后一轮才裁，只跑 1 次）
                        ↓
                   BULL / BEAR / NEUTRAL + 置信度 + 证伪触发
```

## 零、一句话跑一局（推荐入口）

不用记参数：给一句话，关键词自己切、新闻自己取、报告自己落盘。

```bash
python examples/detective_panel/run_topic.py "国常会后A股是否开始构筑底部"
```

- 关键词：按标点切 clause，**整段不动**（不会把「国常会」拆成「国常」+「后A股」）；
  5-6 字再加前 3 / 后 3；长句走 3-gram 滑窗，纯功能词组合（「是否开」「会后」）被剔除
- 取数：默认调 `fetch_news.py`（新浪 7x24 + 央视），窗口 `--hours` / `--pages` 可调
- 行情：**不抓**。要技术面就自己算好，`--evidence` 传（见第七节）
- 产物：`out/<YYYYMMDD-HHMM>-<句子前 20 字>.md`，一议题一份，互不覆盖
- 手工覆盖：`--keywords "国常会,逆周期"`；不取数：`--no-fetch --news "..."`

贴进 DSH 的完整话术见工作区 `DSH-一句话跑一局.md`。

## 一、检索在框架内，调用方只递关键词

`multi_agent/frameworks/news/` 已有采集与关键词过滤（`NewsWatchFramework`、
`DataHubNewsCollector`、`RuleNewsCollector`、`StaticNewsCollector`），但它只做
**过滤/分流**，取数必须注入 `fetch`。本项目的 `news/collect` 按四级取数：

| 优先级 | 来源 | 怎么配 |
|---|---|---|
| 1 | Hub 上的 `search/<action>` 能力 | `--search-mcp "<搜索 MCP 启动命令>"`（内部用 `MCPAgent` 挂载），或独立跑 `multi-agent mcp-agent --hub ... --command "<cmd>" --prefix search` |
| 2 | **取数命令** | `--news-cmd "<命令>"` 或环境变量 `NEWS_FETCH_CMD`；命令 stdout 输出 JSON（数组或 `{"items":[...]}`），字段 `title` + `summary`/`text` + `source` + `published`/`published_at` + `url` |
| 3 | 新闻池文件 | `NEWS_FEED_FILE=/path/news.json`（离线/回放） |
| 4 | 调用方粘贴正文 | `--news "要点……"`（不检索的退路） |

第 2 级是为私有数据源准备的：**取数逻辑留在你的私有仓，公开仓只认“命令 + JSON”**，
不用把私有库名或依赖写进来。

四级都拿不到时，`news/collect` 返回 `degraded` + 具体原因（例如
“没有可执行 search/search 的存活 Agent”“未配置取数命令”），**不会编造新闻**。

关键词格式：逗号、顿号、分号、空格都当分隔符。命中规则是标题或摘要包含任一关键词；
同标题只留一条。

## 二、默认参数（就是要省 token）

| 项 | 默认 | 为什么 |
|---|---|---|
| 席位 | 4（2 正 + 2 反） | 再多就是互相说服，且每轮成本线性叠加 |
| 轮次 | **3** | `debate.max_rounds` 上限 10；3 轮足够暴露分歧 |
| 裁决 | 只在最后一轮裁 1 次 | `judge_every_round=False`，省掉中间 2 次调用 |
| 调用总数 | **4×3 + 1 = 13 次** | `max_speeches=24`，留了 11 条余量 |

> 成本结构：调用次数 13，但**输入随轮数累积**（每轮每个席位重读此前全部发言），
> 总 token 接近平方增长，不是 13 × 单条。

## 三、模型档位（全 DeepSeek）

| profile | 默认模型 | 谁在用 |
|---|---|---|
| `detective_fast` | `deepseek-chat` | 正方两席 |
| `detective_deep` | `deepseek-reasoner` | 反方两席 |
| `judge_pro` | `deepseek-reasoner` | 裁决官（只跑 1 次） |

模型映射放 `config/llm.json`（可提交），**Key 只放 `config/llm.local.json`**（已 gitignore）：

```json
// config/llm.json
{"profiles": {
  "detective_fast": {"backend":"http","url":"https://api.deepseek.com/v1","model":"deepseek-chat"},
  "detective_deep": {"backend":"http","url":"https://api.deepseek.com/v1","model":"deepseek-reasoner"},
  "judge_pro":      {"backend":"http","url":"https://api.deepseek.com/v1","model":"deepseek-reasoner"}
}}
```

```json
// config/llm.local.json —— 只有 Key，不进仓库
{"profiles": {
  "detective_fast": {"token":"sk-..."},
  "detective_deep": {"token":"sk-..."},
  "judge_pro":      {"token":"sk-..."}
}}
```

> `llm/chat` 只发 `model` 与 `messages`，不发 `temperature`。所以「思考模式」= 换模型
> （`deepseek-reasoner`）+ 换 system 提示，不是调采样参数。

档位可由调用方覆盖：`context.models = {"bear_memory": "judge_pro"}`，按 capability 精确匹配，
写错名字就退回默认。

## 四、一条命令（手机端 / 没有终端）

不用起服务、不用配 MCP，DSH 执行一条就完事：

```bash
cd ~/multi-agent && LLM_TOKEN=sk-你的key \
python examples/detective_panel/run_once.py \
  --topic "国常会后A股是否开始构筑底部" \
  --keywords "国常会,逆周期,构筑底部" \
  --search-mcp "<你的搜索 MCP 启动命令>"
```

`--search-mcp` 是可选的：给了就把这个 MCP 挂成 `search` 能力，检索官按关键词去调；
不给就用 `--news-cmd`（见下），再没有就退到 `NEWS_FEED_FILE`，最后才是 `--news` 粘正文。

### 素材从哪来（宏观 + 股票 + 新浪）

`fetch_news.py` 把三类公开源打包好，不需要任何 Key：

| 源 | 接口 | 给什么 |
|---|---|---|
| 新浪财经 7x24 | `zhibo.sina.com.cn` 公开接口 | 宏观 + 市场快讯（主力源，可翻页） |
| 央视新闻联播 | akshare `news_cctv` | 政策口径最硬的宏观通稿 |
| 商品/私有源 | `--extra-cmd "<你的命令>"` | 你自己仓里的源；公开仓只认命令 + JSON |

```bash
python examples/detective_panel/fetch_news.py --hours 30 --pages 3
```

输出 `{"items": [...], "meta": {...}}`：**只有新闻，不含任何行情**。要行情就自己算好后
用 `--evidence` 传（见第七节）——新闻里没有、也不该有行情，两者不掺和。

实测（2026-11-28 01:21）：新浪 199 条 + 央视 25 条 → 去重后 120 条；
关键词「国常会/逆周期/政策/央行/降准」扫 120 条命中 4 条。

> 取数端只花网络时间，不占 DeepSeek token；进模型的只有命中后拼出的 digest。

## 五、成本量级

DeepSeek Flash（2026-09-10 12:00 起空闲时段：输入 1 元/百万、输出 4 元/百万；
工作日 9-12、14-18 高峰翻倍；周末全天低谷）：

| 项 | 一局用量（实测） | 空闲时段 | 高峰时段 |
|---|---|---|---|
| 输入 | ≈27.6k tokens（含新闻摘要，13 次累计；不含行情） | ≈0.028 元 | ≈0.055 元 |
| 输出 | ≈1k tokens（真实模型） | ≈0.004 元 | ≈0.008 元 |
| **合计** | | **≈0.032 元** | **≈0.063 元** |

一局 **三分到六分钱**，一天跑十局约三毛到六毛。取数（新浪/央视）不占 DeepSeek token。
价格会变，跑前按官方价格页核对；上表是估算（按字符×0.75 折算），不是账单。

## 六、还有别的用法

```bash
python examples/detective_panel/run_demo.py      # 零依赖跑通链路（不检索）
LLM_TOKEN=sk-xxx bash examples/detective_panel/start_panel.sh   # 常驻 Hub，给 DSH 走 MCP 调
```

`start_panel.sh` 会打印贴进 DSH 的 MCP 配置；同机走 `127.0.0.1`，不需要公网 IP。

## 七、证据包（行情你自己算）

行情不是新闻能替代的，也**不该由取数脚本顺手抓**：点位、MA40、成交额、两融、PCR 等
由你自己算好后，按 `cases/guochanghui.demo.json` 的结构写成 JSON，用 `--evidence` 传：

```bash
python examples/detective_panel/run_once.py --topic "..." --keywords "..." \
  --evidence /path/evidence.json
```

`market` 字段会压成「一个指数一行」（点位、涨跌幅、成交额、MA40 上下方、近 5/20 日）——
证据包每次发言都要重读，13 次调用，字段越少越省 token。模板里这些字段全为 `null`
占位；空着时报告结论只代表「基于新闻的判断，不含技术面」。

## 八、文件

| 文件 | 作用 |
|---|---|
| `project.py` | 四席 + 检索官 + 裁决官 + 报告官；`panel_params()` 给出可直接调的参数 |
| `run_once.py` | 一条命令跑完（检索 → 辩论 → 报告 → 成本） |
| `run_demo.py` | 零依赖端到端演示（内存 Hub，不检索） |
| `serve_panel.py` | 常驻服务：Hub + N worker，打印 DSH 的 MCP 配置 |
| `start_panel.sh` | 一键启动 + 端点自检 |
| `cases/guochanghui.demo.json` | 证据包模板（全占位） |

## 九、边界

- 只做讨论与裁决，不构成投资建议；证据空着时的结论一律不作数。
- 位置在 `multi-agent` 公开仓，**不得引用私有库**。
- 席位/轮次/字数/token 上限在 `config/limits.json` 的 `debate` 段，改配置不改代码。