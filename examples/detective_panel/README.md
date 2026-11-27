# 正反辩论（detective_panel）

四席两正两反，默认 3 轮，最后一轮才裁决一次。全部 DeepSeek，只有反方用思考模式。

```
正方·政策与基本面 ┐
正方·资金与技术   ├─ 同读一份证据 → 逐席发言（每轮能看到前面的人说了什么）
反方·估值与盈利   │        ↓
反方·历史与情绪   ┘   裁决官（最后一轮才裁，只跑 1 次）
                        ↓
                   BULL / BEAR / NEUTRAL + 置信度 + 证伪触发
```

## 一、默认参数（就是要省 token）

| 项 | 默认 | 为什么 |
|---|---|---|
| 席位 | 4（2 正 + 2 反） | 再多就是互相说服，且每轮成本线性叠加 |
| 轮次 | **3** | `debate.max_rounds` 上限 10；3 轮足够暴露分歧 |
| 裁决 | 只在最后一轮裁 1 次 | `judge_every_round=False`，省掉中间 2 次调用 |
| 调用总数 | **4×3 + 1 = 13 次** | `max_speeches=24`，留了 11 条余量 |

> 注意成本结构：调用次数是 13，但**输入随轮数累积**（每轮每个席位都要重读此前全部发言），
> 所以总 token 接近平方增长，不是 13 × 单条。想再省就降 `max_rounds`。

## 二、模型档位（全 DeepSeek）

| profile | 默认模型 | 谁在用 |
|---|---|---|
| `detective_fast` | `deepseek-chat` | 正方·政策与基本面、正方·资金与技术 |
| `detective_deep` | `deepseek-reasoner` | 反方·估值与盈利、反方·历史与情绪 |
| `judge_pro` | `deepseek-reasoner` | 裁决官（只跑 1 次，成本可控） |

配置放 `config/llm.json` 的 `profiles`，**Key 只放 `config/llm.local.json`**（已 gitignore）：

```json
// config/llm.json  —— 模型映射，可提交
{"profiles": {
  "detective_fast": {"backend":"http","url":"https://api.deepseek.com/v1","model":"deepseek-chat"},
  "detective_deep": {"backend":"http","url":"https://api.deepseek.com/v1","model":"deepseek-reasoner"},
  "judge_pro":      {"backend":"http","url":"https://api.deepseek.com/v1","model":"deepseek-reasoner"}
}}
```

```json
// config/llm.local.json  —— 只有 Key，不进仓库
{"profiles": {
  "detective_fast": {"token":"sk-..."},
  "detective_deep": {"token":"sk-..."},
  "judge_pro":      {"token":"sk-..."}
}}
```

> `llm/chat` 只发 `model` 与 `messages`，不发 `temperature`。所以「思考模式」= 换模型（`deepseek-reasoner`）+ 换 system 提示，不是调采样参数。

## 三、配置由 Harness 传：可以，分两层

**由 DSH 传的（每次调用）**——议题、席位、轮次、证据、模型档位覆盖：

```json
// tool: debate__run
{
  "topic": "11月27日国常会后，A股是否开始构筑底部",
  "participants": [
    {"capability": "bull_policy",   "action": "argue", "name": "正方·政策与基本面"},
    {"capability": "bull_flow",     "action": "argue", "name": "正方·资金与技术"},
    {"capability": "bear_valuation","action": "argue", "name": "反方·估值与盈利"},
    {"capability": "bear_memory",   "action": "argue", "name": "反方·历史与情绪"}
  ],
  "judge": {"capability": "judge", "action": "verdict", "name": "裁决官"},
  "max_rounds": 3,
  "judge_every_round": false,
  "context": {
    "evidence": { "...": "统一证据包" },
    "models": {"bear_memory": "judge_pro"}     // 想让谁用哪档，在这里点名
  }
}
```

`models` 按 capability 精确匹配，写错名字就退回默认档位，不静默猜。

**不能由 DSH 传的（服务端一次性配置）**：profile → 模型名 → Key 的映射。Key 不该经过工具调用的参数，那是把密钥写进会话历史。

## 四、起服务（同机，无需公网 IP）

```bash
LLM_TOKEN=sk-xxx bash examples/detective_panel/start_panel.sh
```

它会自检三个 profile、起 Hub（`127.0.0.1:8787`）+ worker，并打印贴进 DSH 的 MCP 配置：

```json
{"mcpServers": {"multi-agent-hub": {
  "command": "multi-agent",
  "args": ["mcp-server", "--hub", "http://127.0.0.1:8787", "--token", "<token>"]
}}}
```

没配 Key 也能起：发言带 `【模拟·未配模型】`，报告标「结论不作数」，链路照常验证。

零依赖跑通：

```bash
python examples/detective_panel/run_demo.py
```

## 五、证据包是命门

四席只读同一份 `evidence`，不许自己找数——不统一证据，四个 Agent 会各编一套互相说服。

`cases/guochanghui.demo.json` 全是 `null` 占位。跑真实一局前必须补齐：

- `event.*`：原文要点、新提法、量化目标、落地主体、与上次同类会议的差异
- `market.*`：指数与 MA40、中枢、缺口、成交额、涨跌家数、涨停/炸板、北向、两融、ETF 申赎、PCR、偏斜

补数这一步建议单独做 `collector`（政策原文抓取 + `data_hub` 行情），产出就是这份 JSON。

## 六、文件

| 文件 | 作用 |
|---|---|
| `project.py` | 四席 + 裁决官 + 报告官；`panel_params()` 给出可直接调的参数 |
| `run_demo.py` | 零依赖端到端演示（内存 Hub） |
| `serve_panel.py` | 常驻服务：Hub + N worker，打印 DSH 的 MCP 配置 |
| `start_panel.sh` | 一键启动 + 端点自检 |
| `cases/guochanghui.demo.json` | 证据包模板（全占位） |

## 七、边界

- 只做讨论与裁决，不构成投资建议；证据空着时的结论一律不作数。
- 位置在 `multi-agent` 公开仓，**不得引用私有库**。
- 席位/轮次/字数/token 上限在 `config/limits.json` 的 `debate` 段，改配置不改代码。

## 八、一条命令（手机端 / 没有终端）

不用起服务、不用配 MCP，DSH 执行一条就完事：

```bash
cd ~/multi-agent && LLM_TOKEN=sk-你的key \
  python examples/detective_panel/run_once.py \
  --topic "国常会后A股是否开始构筑底部" \
  --news "（把搜到的政策要点粘在这里）"
```

它在内存里起 Hub + worker，跑 3 轮四席，出 Markdown 报告，然后清理退出；
末尾打印「调用几次 / 输入多少 token / 这一局花了多少钱（Flash 峰谷两档）」。

给 DSH 的一句话指令（搜新闻这一步让 DSH 干，它才有联网）：

> 先用联网搜索查「<议题>」的政策原文与市场反应，把要点压成 300 字以内的文本，
> 然后执行 `cd ~/multi-agent && LLM_TOKEN=$LLM_TOKEN python examples/detective_panel/run_once.py --topic "<议题>" --news "<要点>"`，
> 把终端最后的 Markdown 报告原样返回给我。

成本量级（DeepSeek Flash，2026-09-10 起的空闲时段价：输入 1 元/百万、输出 4 元/百万；
工作日 9-12、14-18 高峰翻倍）：

| 项 | 一局用量 | 空闲时段 | 高峰时段 |
|---|---|---|---|
| 输入 | ≈19k tokens（13 次调用累计） | ≈0.019 元 | ≈0.039 元 |
| 输出 | ≈1k tokens（真实模型） | ≈0.004 元 | ≈0.008 元 |
| **合计** | | **≈0.024 元** | **≈0.047 元** |

即一局 **两三分钱到五分钱**，一天跑十局也就几毛。新闻搜索走 DSH 自己的联网额度，
不占 DeepSeek 的 token。价格会变，跑前按官方价格页核对。