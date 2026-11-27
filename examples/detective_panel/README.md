# 正反辩论（detective_panel）

四席两正两反，默认 5 轮，最后一次裁决。全部 DeepSeek，只有反方用思考模式。

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
| 轮次 | 5 | `debate.max_rounds` 上限 10，5 轮够跑出收敛/分歧 |
| 裁决 | 只在最后一轮裁 1 次 | `judge_every_round=False`，省掉中间 4 次调用 |
| 调用总数 | **4×5 + 1 = 21 次** | `max_speeches=24`，留了 3 条余量 |

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

**由 DSH 传的（每次调用）**——席位、轮次、证据、模型档位覆盖：

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
  "max_rounds": 5,
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