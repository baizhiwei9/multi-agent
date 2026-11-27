# 侦探团（detective_panel）

五个不同维度的「侦探」看同一份证据、按顺序发言、互相能看见前文，最后交一个用**高思考模型**的裁决官收口。跑在 multi-agent 的 Hub 上，DSH 通过 MCP 一条命令就能用。

```
政策官 ┐
资金官 ├─ 同读一份证据 → 逐席发言（每轮能看到前面的人说了什么）
技术官 │        ↓
情绪官 │   judge（deepseek-reasoner）逐轮裁决
反方   ┘        ↓
             BULL / BEAR / NEUTRAL + 置信度 + 证伪条件
```

## 一、三步用起来

### 1. 配 DeepSeek 端点（只配一次）

`config/llm.json` 里已经有 `profiles` 结构，加三个 profile；**Key 只写在本地的 `config/llm.local.json`**（已 gitignore，不会被提交）：

```json
{
  "profiles": {
    "detective_fast": {"backend": "http", "url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "detective_deep": {"backend": "http", "url": "https://api.deepseek.com/v1", "model": "deepseek-reasoner"},
    "judge_pro":      {"backend": "http", "url": "https://api.deepseek.com/v1", "model": "deepseek-reasoner"}
  }
}
```

```json
// config/llm.local.json
{"profiles": {
  "detective_fast": {"token": "sk-..."},
  "detective_deep": {"token": "sk-..."},
  "judge_pro": {"token": "sk-..."}
}}
```

| profile | 默认模型 | 谁在用 | 为什么 |
|---|---|---|---|
| `detective_fast` | `deepseek-chat` | 政策官 / 资金官 / 技术官 / 情绪官 | 出观点要快要便宜，四席并发 |
| `detective_deep` | `deepseek-reasoner` | 反方 | 挑毛病需要多想一步 |
| `judge_pro` | `deepseek-reasoner` | 裁决官 | 最后一公里收口，值得用高思考 |

> `llm/chat` 只传 `model` 与 `messages`，不传 `temperature`。所以「不同思考模式」= **不同模型 + 不同 system 提示**，不是调采样参数。

### 2. 一键起服务

```bash
LLM_TOKEN=sk-xxx bash examples/detective_panel/start_panel.sh
```

它会打印一段可直接贴进 DSH 的 MCP 配置：

```json
{"mcpServers": {"multi-agent-hub": {
  "command": "multi-agent",
  "args": ["mcp-server", "--hub", "http://127.0.0.1:8787", "--token", "<token>"]
}}}
```

同机部署走 `127.0.0.1`，**不需要公网 IP**。要跨机再上 Cloudflare Tunnel / Tailscale，并且必须带 token。

### 3. 在 DSH 里发起一局

DSH 能看到每个能力一个 tool。发起讨论调一个：

```json
// tool: debate__run
{
  "topic": "11月27日国常会后，A股是否开始构筑底部",
  "participants": [
    {"capability": "policy",     "action": "read",      "name": "政策官"},
    {"capability": "capital",    "action": "probe",     "name": "资金官"},
    {"capability": "technical",  "action": "structure", "name": "技术官"},
    {"capability": "sentiment",  "action": "track",     "name": "情绪官"},
    {"capability": "contrarian", "action": "attack",    "name": "反方"}
  ],
  "judge": {"capability": "judge", "action": "verdict", "name": "裁决官"},
  "max_rounds": 2,
  "context": {"evidence": { "...": "统一证据包" }}
}
```

拿回 `decision` / `transcript` / `judge`，再调 `report__build` 出 Markdown。

## 二、为什么是 5 个而不是 300 个

- **讨论席位有硬上限**：`config/limits.json` 的 `debate.max_participants`（默认 6）、`max_speeches`（24）、`max_rounds`（10）。同场人多不等于结论好，5 个维度 + 1 个裁决已经是「能质证得动」的上限。
- **300 是并发 worker 数**：`DETECTIVES=300` 会起 300 个 worker 共用同一批席位，适合并行跑很多局、或给别的业务当算力池。单进程 >32 会吃满线程，生产要拆多进程/多机。
- token 也是硬账：一局 5 席 × 2 轮 + 2 次裁决 ≈ 12 次调用。300 席同场讨论光一轮就是 300 次调用，且第二轮要重读全部历史，成本是平方级的。

## 三、没配 Key 会怎样

不会假装跑过。每席发言带 `【模拟·未配模型】` 前缀，报告里写 `运行口径：simulated（含模拟发言，结论不作数）`，裁决写「不构成裁决」。链路、路由、轮次、预算都能验证，结论一个字都不能用。

零依赖跑通：

```bash
python examples/detective_panel/run_demo.py
```

## 四、证据包

所有侦探**只读同一份证据**，不许自己找数——不统一证据，五个 Agent 会各编一套互相说服。

`cases/guochanghui.demo.json` 是模板，字段全为 `null`（待补）。跑真实一局前必须先补齐：

- `event.*`：原文要点、新提法、量化目标、落地主体、与上次同类会议的差异
- `market.*`：指数位置与 MA40、中枢位置、缺口、成交额、涨跌家数、涨停/炸板、北向、两融、ETF 申赎、PCR、偏斜

补数这步建议做成一个 `collector` 步骤（政策原文抓取 + `data_hub` 行情），产出就是这份 JSON。

## 五、文件

| 文件 | 作用 |
|---|---|
| `project.py` | 五个侦探 + 裁决官 + 报告官的角色表与实现，`panel_params()` 给出可直接调的参数 |
| `run_demo.py` | 零依赖端到端演示（内存 Hub） |
| `serve_panel.py` | 常驻服务：Hub + N 个 worker，打印 DSH 的 MCP 配置 |
| `start_panel.sh` | 一键启动 + 端点自检 |
| `cases/guochanghui.demo.json` | 证据包模板（全占位） |

## 六、边界

- 本示例**只做讨论与裁决**，不构成投资建议；证据空着时的结论一律不作数。
- 明确写死的依赖方向：本示例在 `multi-agent` 公开仓里，**不得引用私有库**。
- 席位、轮次、字数、token 上限都在 `config/limits.json` 的 `debate` 段，改配置不改代码。