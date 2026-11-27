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
不给就退到 `NEWS_FEED_FILE`，再没有就用 `--news` 粘正文。

它在内存里起 Hub + worker，检索 → 3 轮四席 → 报告 → 清理退出；末尾打印
「检索命中几条 / 调用几次 / 输入多少 token / 这一局花了多少钱（Flash 峰谷两档）」。

给 DSH 的一句话指令：

> 执行 `cd ~/multi-agent && LLM_TOKEN=$LLM_TOKEN python examples/detective_panel/run_once.py --topic "<议题>" --keywords "国常会,逆周期,构筑底部" --search-mcp "<搜索命令>"`，把终端最后的 Markdown 报告原样返回给我。

## 五、成本量级

DeepSeek Flash（2026-09-10 12:00 起空闲时段：输入 1 元/百万、输出 4 元/百万；
工作日 9-12、14-18 高峰翻倍；周末全天低谷）：

| 项 | 一局用量（实测） | 空闲时段 | 高峰时段 |
|---|---|---|---|
| 输入 | ≈22.6k tokens（含检索摘要，13 次累计） | ≈0.023 元 | ≈0.045 元 |
| 输出 | ≈1k tokens（真实模型） | ≈0.004 元 | ≈0.008 元 |
| **合计** | | **≈0.027 元** | **≈0.053 元** |

一局 **两三分到五分钱**，一天跑十局约三毛到五毛。检索若走外部 MCP，不占 DeepSeek token。
价格会变，跑前按官方价格页核对；上表是估算（按字符×0.75 折算），不是账单。

## 六、还有别的用法

```bash
python examples/detective_panel/run_demo.py      # 零依赖跑通链路（不检索）
LLM_TOKEN=sk-xxx bash examples/detective_panel/start_panel.sh   # 常驻 Hub，给 DSH 走 MCP 调
```

`start_panel.sh` 会打印贴进 DSH 的 MCP 配置；同机走 `127.0.0.1`，不需要公网 IP。

## 七、证据包

行情字段（MA40、成交额、两融、PCR 等）**不是新闻能替代的**，只能靠取数补。
`cases/guochanghui.demo.json` 全为 `null` 占位；这些字段空着时，报告结论只代表
“基于新闻的判断，不含技术面”，源码里也这么写。

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